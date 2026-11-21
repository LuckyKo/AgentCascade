# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generic multi-media generation tool (image / video / audio).

``media_gen`` is a **backend registry** tool: each backend (a class under
``media_backends/``) declares the media types it can produce and attaches a
presentation ``manifest`` (UI fields, ``__help`` text, behaviour defaults). All
four registered backends are implemented end to end: ``comfyui`` (ComfyUI
server — image / video / music), ``sdcpp`` (stable-diffusion.cpp CLI — image),
``kobold_tts`` (koboldcpp GGUF — speech), and ``chatterbox`` (Chatterbox-TTS
one-shot CLI — speech). Adding a backend = adding its class + manifest entry;
nothing in this file changes.

The canonical media-type vocabulary is exactly three types (image / video /
audio). TTS and music are both *audio* (a backend/config distinction, not an
output type), so both return ``ContentItem(audio=...)``.

Backend selection is EXPLICIT: the caller must name a backend — there is no
AUTO resolver and no media_type parameter. For ComfyUI the media type (image/
video/audio) is inferred from the loaded workflow, so the caller only needs the
backend + config stem. Run with ``prompt='__help'`` to list every backend, its
available configs, what each produces, and the exact params to pass per case;
the backend id list in the tool schema comes from the composed manifest too.

VRAM management (every backend, gated per backend by the manifest's
``release_model_around_call`` — default on): before generation we save the
owning instance's KV state and unload all models from llama-autoloader to free
VRAM. The sequence is preflight → save → unload → generate → save media →
restore. The restore is NOT in a ``finally`` block: it is called explicitly at
the end and on every early error path (generation failure, media-save failure)
so the agent's KV is never left dangling. ``preflight`` runs strictly BEFORE
any VRAM work, so a bad config / missing model / out-of-roots ``reference_file``
never leaves the model unloaded.

No captioning happens inside this tool: the generated image is returned
uncaptioned and the router's ``caption_images`` flow supplies the caption later
(image only — video/audio are never sent to the vision guard). This is why the
*primary* restore is the one at the end: there is no inner LLM call to reason
about.
"""

import json
import jsonschema
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

import httpx

from agent_cascade import settings
from agent_cascade.llm.schema import ContentItem
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.utils.media_utils import save_media_to_media
from agent_cascade.tools.media_backends import resolve_backend
from agent_cascade.tools.media_backends import manifest_ids
from agent_cascade.tools.media_backends import BACKEND_MANIFEST
from agent_cascade.tools.schema_common import tool_params
from agent_cascade.tools.media_backends.base import (
    MEDIA_TYPES, MAX_SEED, MediaRequest, resolve_media_timeout, resolve_release_model_around_call,
    resolve_block_dir, list_voice_samples,
)

from agent_cascade.tools.image_gen import _fmt_elapsed
from agent_cascade.log import _AppLoggerProxy

# Late-bound delegate to the live app logger (see agent_cascade.log._AppLoggerProxy):
# a module logger (logging.getLogger(__name__)) never reaches the console/file
# handlers, so [MediaGen] records (VRAM release, save/restore) would silently vanish.
logger = _AppLoggerProxy()

# ── media_gen config cache ─────────────────────────────────────────────────────
# The config lives at <AgentCascade_root>/config/media_gen/media_gen.json. Read
# with a short TTL so a concurrent UI write can't tear the read; the REST handler
# calls _invalidate_media_gen_config() to bust it immediately after saving.

_CONFIG_TTL = 30  # seconds — balance freshness vs. read frequency
_config_cache: dict = {}
_config_lock = threading.Lock()


def _media_gen_config_path() -> Path:
    """Return the path to media_gen.json under the AgentCascade project root.

    This file lives at agent_cascade/tools/media_gen.py, so the project root is
    three levels up (tools → agent_cascade → <AgentCascade_root>). The config
    itself lives one level deeper, in a ``media_gen/`` subdirectory, so it can
    coexist with the deprecated ``image_gen.json``.
    """
    return Path(__file__).resolve().parent.parent.parent / 'config' / 'media_gen' / 'media_gen.json'


def _get_media_gen_config() -> dict:
    """Read media_gen config with a 30s cache to avoid concurrent read/write races.

    Returns an empty dict if the file is missing or malformed.
    """
    global _config_cache
    now = time.time()
    with _config_lock:
        cached = {k: v for k, v in _config_cache.items() if k != '_ts'}
        if cached and now - _config_cache.get('_ts', 0) < _CONFIG_TTL:
            return cached
        config_path = _media_gen_config_path()
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            _config_cache = {**data, '_ts': now}
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            logger.debug('media_gen config read failed (%s): %s', config_path, e)
            _config_cache = {'_ts': now}
        return {k: v for k, v in _config_cache.items() if k != '_ts'}


def _invalidate_media_gen_config() -> None:
    """Bust the media_gen config cache (REST handler calls this after writing settings)."""
    global _config_cache
    with _config_lock:
        _config_cache = {}


def _probe_server_status(url: str) -> str:
    """Probe a backend server's reachability for __help status reporting.

    Any HTTP response — even 404 — means the server is up, so this returns 'LIVE';
    a failed connection or timeout means it is not, returning 'OFFLINE'. Never raises.
    The 1.5s timeout bounds the wait so an unreachable host fails fast (connection
    refused is near-instant). Mirrors the bare ``httpx.get(url, timeout=...)`` shape
    used in kobold_tts — a single request, no polling loop.
    """
    try:
        httpx.get(url, timeout=1.5)
        return 'LIVE'
    except Exception:
        return 'OFFLINE'


@register_tool('media_gen')
class MediaGen(BaseTool):
    """Generate image / video / audio via a pluggable backend registry.

    Returns ``[ContentItem(<type>=path), ContentItem(text=feedback)]``. The image
    item is left uncaptioned (the router auto-captions it on demand); video/audio
    items are never sent to the vision guard. No LLM is constructed here; config is
    read lazily at call time.
    """

    name = 'media_gen'
    description = TOOL_METADATA['media_gen']['description']
    parameters = tool_params(
        properties={
            'prompt': {
                'type': 'string',
                'description': TOOL_METADATA['media_gen']['parameters']['prompt'],
            },
            'prompts': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': TOOL_METADATA['media_gen']['parameters']['prompts'],
            },
            'variations': {
                'type': 'integer',
                'minimum': 1,
                'description': TOOL_METADATA['media_gen']['parameters']['variations'],
            },
            'backend': {
                'type': 'string',
                'enum': manifest_ids(),
                'description': TOOL_METADATA['media_gen']['parameters']['backend'],
            },
            'config': {
                'type': 'string',
                'description': TOOL_METADATA['media_gen']['parameters']['config'],
            },
            'negative_prompt': {
                'type': 'string',
                'description': TOOL_METADATA['media_gen']['parameters']['negative_prompt'],
            },
            'reference_file': {
                'type': 'string',
                'description': TOOL_METADATA['media_gen']['parameters']['reference_file'],
            },
            'media_size': {
                'type': 'object',
                'properties': {
                    'width': {'type': 'integer', 'description': 'Output width in px (image/video).'},
                    'height': {'type': 'integer', 'description': 'Output height in px (image/video).'},
                    'duration': {'type': 'number', 'description': 'Duration in seconds (video/audio).'},
                    'fps': {'type': 'integer', 'description': 'Frames per second (video only).'},
                },
                'description': TOOL_METADATA['media_gen']['parameters']['media_size'],
            },
            'overrides': {
                'type': 'object',
                'description': TOOL_METADATA['media_gen']['parameters']['overrides'],
            },
        },
        # Flat required is empty: 'prompt' is not always required (text-output backends take
        # reference_file, not a prompt). The anyOf encodes the real rule — backend is required
        # for generation UNLESS prompt is the '__help' sentinel. The help branch matches
        # case-insensitively/after strip to mirror call()'s runtime gate, so every previously
        # valid call passes and backend-less non-help calls (which failed at runtime anyway)
        # are now rejected earlier and more clearly.
        required=[],
        anyOf=[
            {'required': ['backend']},
            {'properties': {'prompt': {'pattern': r'(?i)^\s*__help\s*$'}}, 'required': ['prompt']},
        ],
    )

    def __init__(self, cfg: Optional[dict] = None, **kwargs):
        super().__init__(cfg)
        # No LLM construction. The owning agent pool is used only to resolve the
        # calling instance for VRAM save/unload/restore at call time, and to sandbox
        # the reference_file path.
        self.agent_pool = kwargs.get('agent_pool')

    def _get_instance(self, kwargs: dict) -> Optional[object]:
        """Resolve the calling AgentInstance (defensive; None if unavailable)."""
        pool = getattr(self, 'agent_pool', None)
        if pool is None:
            return None
        inst_name = (
            kwargs.get('agent_instance_name')
            or kwargs.get('agent_name')
            or getattr(self, 'agent_name', None)
        )
        if not inst_name:
            return None
        try:
            return pool.get_instance(inst_name)
        except Exception as e:
            logger.debug("media_gen: failed to resolve instance '%s': %s", inst_name, e)
            return None

    # ── VRAM seam (ported verbatim from image_gen's _vram_release / _restore_vram_state) ──

    def _vram_release(self, instance, state: dict, *, release: bool = True) -> None:
        """Additive VRAM save+unload prologue, used by every backend.

        Three explicit gates, in order:
          1. ``release`` — the per-backend ``release_model_around_call`` opt-out
             (resolved by ``base.resolve_release_model_around_call``; default True).
             When False NOTHING is saved and NOTHING is unloaded, so every restore
             site no-ops (``state['saved']`` stays False).
          2. ``instance is None`` (no calling agent).
          3. capability — the shared predicate ``state_ops.is_state_save_capable``
             (``state_save_enabled`` + ``api_base`` + ``model``; port-independent),
             replacing the inline verbatim copy of image_gen's gate.

        ``state`` is a mutable holder dict with keys ``'saved'`` and ``'held'``.
        ``saved`` is set True as soon as ``save_instance_state`` succeeds — BEFORE
        ``unload_all_models`` runs — so a raise from unload still lets the caller's
        except-block restore the KV (V2/F1), mirroring the inline prologue.
        """
        state['saved'] = False
        state['held'] = None
        if not release or instance is None:
            return
        endpoint_cfg = getattr(instance, '_last_endpoint_config', None)
        from agent_cascade.state_ops import (
            is_state_save_capable, save_instance_state, unload_all_models,
        )
        if not is_state_save_capable(endpoint_cfg):
            return
        api_base = endpoint_cfg.get('api_base')
        model = endpoint_cfg.get('model', '')
        logger.info('[MediaGen] VRAM release: saving state for %s (model=%s)', instance.instance_name, model)

        if not save_instance_state(instance):
            logger.warning('[MediaGen] VRAM release: state save failed for %s', instance.instance_name)
            return
        # Record success BEFORE unload so a raise from unload_all_models below
        # still leaves state['saved'] True -> the caller restores the KV (V2/F1).
        state['saved'] = True
        state['held'] = {
            'api_base': api_base,
            'model': model,
        }
        logger.info('[MediaGen] VRAM release: state saved, unloading model %s', model)
        if not unload_all_models(api_base):
            logger.warning(
                '[MediaGen] VRAM may be constrained; model was not unloaded before generation'
            )
        else:
            logger.info('[MediaGen] VRAM release: model unloaded successfully')

    def _restore_via_held_endpoint(self, instance) -> bool:
        """Restore KV via the live held endpoint + the stable instance-name label.

        Shared mechanism for the pool-backed restore used by both _restore_vram_state's
        pool branch and _restore_vram_from_label: resolves the held endpoint LIVE from
        the pool router (the carried ``held`` dict is the SAVED endpoint and may be stale
        on a shared conc=0 autoloader) and restores on the stable instance-name label,
        which survives a system agent consuming ``_state_label`` inside the window.
        Returns False when there is no pool (tests / direct tool use have no live endpoint
        to target) — callers keep their own distinct no-pool fallback.
        """
        pool = getattr(self, 'agent_pool', None)
        if pool is None:
            return False
        from agent_cascade.state_ops import restore_held_slot_state, kv_save_label
        restore_held_slot_state(pool, instance, instance.instance_name, label=kv_save_label(instance))
        return True

    def _restore_vram_state(self, instance, held: dict) -> None:
        """Restore saved KV state after all LLM-side work is done (one retry on failure).

        With a pool available, routes through ``_restore_via_held_endpoint`` (the seam
        proven by shell_cmd safe mode): the held endpoint is resolved LIVE from the pool
        router and the stable instance-name label survives a system agent consuming
        ``_state_label`` inside the window. Without a pool (tests, direct tool use), falls
        back to the previous call with the saved-endpoint config. A final restore failure
        is logged at ERROR but is non-fatal: the next LLM call triggers a fresh model load
        via autoloader JIT, so the system self-heals (the user only loses KV cache continuity).
        """
        pool = getattr(self, 'agent_pool', None)
        logger.info('[MediaGen] VRAM restore: restoring state for %s (model=%s)', instance.instance_name, held.get('model', '?'))
        for attempt in range(2):
            try:
                if pool is not None:
                    self._restore_via_held_endpoint(instance)
                else:
                    from agent_cascade.state_ops import restore_instance_state
                    restore_instance_state(instance, held_endpoint_cfg=held)
                logger.info('[MediaGen] VRAM restore: state restored successfully for %s', instance.instance_name)
                return
            except Exception as e:
                if attempt == 0:
                    logger.warning('[MediaGen] Restore attempt 1 failed, retrying: %s', e)
                    time.sleep(2)
                else:
                    logger.error(
                        '[MediaGen] State restore FAILED after generation — model may not be loaded: %s', e
                    )

    def _restore_if_needed(self, instance, state: dict, is_last_vram: bool) -> None:
        """Decide whether THIS call owns the end-of-batch KV restore, then perform it.

        Replaces the four inline ``if state['saved'] and instance is not None: _restore_vram_state(...)``
        sites with one batch-aware dispatcher (single source of truth for the restore decision):

          • instance is None OR not is_last_vram → no-op. A non-last call DEFERS: the last call
            (or the dispatch-loop safety net) owns the restore, so the model stays unloaded across the
            batch and only one swap-in happens.
          • state['saved'] and state['held'] → this call did its own save/unload (single call, or first==last).
            Restore via the held endpoint we recorded — byte-identical to today.
          • otherwise, but the instance still has a pending _state_label → batch path: a PRIOR call in this
            batch did the save and this (last) call skipped its own prologue. Restore via that stable label.

        The label path is safe because nothing inside a pure media_gen batch consumes _state_label (no LLM
        generation, no system agent runs between the calls), and restore_held_slot_state clears it on success,
        so a successful restore never leaks a pending label.
        """
        if instance is None or not is_last_vram:
            return
        if state['saved'] and state['held']:
            self._restore_vram_state(instance, state['held'])
        elif getattr(instance, '_state_label', None) is not None:
            self._restore_vram_from_label(instance)

    def _restore_vram_from_label(self, instance) -> None:
        """Restore KV via the instance's pending _state_label (set by a prior save in the batch).

        The batch-last call skipped its own save/unload prologue, so it has no ``held`` endpoint dict — but the
        first call's save wrote a state file under the stable instance-name label and armed ``_state_label``.
        Routes through the same pool-aware seam as _restore_vram_state (held endpoint resolved live, stable
        label), just without a held dict. No pool → nothing to do (direct/test use has no live endpoint).
        """
        if instance is None or getattr(instance, '_state_label', None) is None:
            return
        logger.info('[MediaGen] VRAM restore: batch restore via state label for %s', instance.instance_name)
        self._restore_via_held_endpoint(instance)

    # ── The single choke point ────────────────────────────────────────────────

    def dms_bound(self, tool_args, instance):
        """Holder-DMS bound for media_gen: the resolved generation timeout for the named backend.

        Backend selection is explicit (no AUTO), so the backend id is known from the tool args and
        this resolves to EXACTLY the timeout the backend's preflight stashes in req.prepared['timeout']
        — resolve_media_timeout(config, config.get(backend_id)) — plus any declared teardown
        headroom. The dispatch boundary (ToolDispatcher) is the single writer of the DMS marker.
        """
        params = tool_args
        if isinstance(params, str):
            try:
                parsed = json.loads(params)
                if isinstance(parsed, dict):
                    params = parsed
            except Exception:
                params = {}
        if not isinstance(params, dict):
            params = {}
        config = _get_media_gen_config()
        backend_id = params.get('backend') or ''
        block = (config or {}).get(backend_id)
        # Teardown that runs in the finally AFTER generation (e.g. waiting for a
        # process tree to exit) lives outside the generation budget; backends that
        # have one declare ``teardown_headroom`` seconds in their manifest, and the
        # DMS wall-clock bound carries it. Backends without teardown declare 0.
        headroom = (BACKEND_MANIFEST.get(backend_id) or {}).get('teardown_headroom') or 0
        # BUG_0350: a batch call fans out len(batch) renders, so the bound scales with the
        # batch size — computed through the SAME parser call() uses (single source of truth).
        # Falls back to 1 on any parse error (never larger than a single render's bound for
        # garbage args); capped by MEDIA_GEN_MAX_BATCH inside _parse_batch, so the DMS
        # shield is never unbounded. Non-batch args parse to [] -> max(1, 0) = 1 = today.
        try:
            batch_size = max(1, len(self._parse_batch(params)))
        except Exception:
            batch_size = 1
        return resolve_media_timeout(config, block) * batch_size + headroom

    def call(self, params, **kwargs) -> List[ContentItem]:
        try:
            params = self._verify_json_format_args(params)
        except (ValueError, TypeError, jsonschema.ValidationError) as e:
            return [ContentItem(text=f"ERROR: Invalid media_gen parameters: {e}")]

        prompt = params.get('prompt')

        # ── __help: informational, works for ALL agents (incl. restricted). ──
        # Checked before prompt validation and before backend resolution so it needs
        # no backend/config and is unambiguous. Lists every backend + its configs.
        if isinstance(prompt, str) and prompt.strip().lower() == '__help':
            backend_filter = (params.get('backend') or '').strip().lower()
            return [ContentItem(text=self._format_media_help(_get_media_gen_config(), backend_filter))]

        # Text-output backends (output_kind='text', e.g. voice_to_text) have NO text prompt —
        # their required input is reference_file, validated in preflight. Detect from the requested
        # backend's manifest (the registry is the single source of truth for output_kind).
        requested_backend = (params.get('backend') or '').strip()
        _m = BACKEND_MANIFEST.get(requested_backend) or {}
        is_text_output = _m.get('output_kind') == 'text'
        # Backends that can source their required input from reference_file (e.g. svg2png
        # reading a .svg file) opt out of the prompt requirement here; their preflight()
        # then owns the "neither supplied" rejection. Default False keeps every other
        # backend's prompt requirement intact.
        prompt_optional = _m.get('prompt_optional_with_reference_file', False)

        # Prompt gate: satisfied by 'prompt' OR a 'prompts' array (the schema already
        # types 'prompts' as array-of-string; content validation lives in _parse_batch).
        if not (is_text_output or prompt_optional) and (not isinstance(prompt, str) or not prompt.strip()) \
                and not isinstance(params.get('prompts'), list):
            return [ContentItem(text="ERROR: 'prompt' is required and must be a non-empty string.")]

        try:
            batch_items = self._parse_batch(params)
        except ValueError as e:
            return [ContentItem(text=f"ERROR: {e}")]

        instance = self._get_instance(kwargs)
        # Parallel-media_gen batch flags (threaded per-call by the dispatch loop, single source of truth
        # there — never recomputed here). Defaults True so a single call is byte-identical to today: only the
        # FIRST call of a batch does the save/unload prologue, only the LAST does the restore.
        is_first_vram = kwargs.get('is_first_vram', True)
        is_last_vram = kwargs.get('is_last_vram', True)

        if not batch_items:
            # ── Single-shot path: identical code path and result shape to pre-batch media_gen ──
            try:
                backend, req = self._prepare_item(
                    {'prompt': prompt, 'seed': None, 'take': 1, 'takes': 1}, params,
                    _get_media_gen_config())
            except (RuntimeError, ValueError, OSError) as e:
                # audit defect 6: _load_workflow raises FileNotFoundError/OSError; resolve_tool_path
                # raises ValueError. None of these touch VRAM.
                return [ContentItem(text=f"ERROR: {e}")]
            return self._run_item(backend, req, instance, is_first_vram, is_last_vram)

        # ── Batch path (BUG_0350): fan-out inside ONE call, serialised by design (one
        # backend invocation in flight at a time), ONE VRAM swap for the whole batch. ──
        n = len(batch_items)
        backend_id = requested_backend
        seeds = [it['seed'] for it in batch_items if it['seed'] is not None]
        ready = []            # [(item_idx, item, backend, req), ...]
        preflight_errors = []  # [(item_idx, item, error_text), ...]
        out_paths = []        # temp files created by successful preflights (batch-level cleanup)
        try:
            # All-or-nothing validation: every item resolved + preflighted BEFORE any VRAM
            # work. A bad item fails the whole call with zero VRAM churn and zero GPU time.
            for item_idx, item in enumerate(batch_items):
                try:
                    backend, req = self._prepare_item(item, params, _get_media_gen_config())
                except (RuntimeError, ValueError, OSError) as e:
                    preflight_errors.append((item_idx, item, f"ERROR: {e}"))
                    continue
                out_path = (req.prepared or {}).get('out_path')
                if out_path:
                    out_paths.append(out_path)
                ready.append((item_idx, item, backend, req))

            if preflight_errors:
                results = [ContentItem(text=self._batch_summary(
                    0, n, len(preflight_errors), backend_id, params.get('config') or '', seeds))]
                for item_idx, item, err_text in preflight_errors:
                    results.append(ContentItem(text=f"{self._batch_label(item, item_idx + 1, n)} — {err_text}"))
                return results

            # Run serially. VRAM ownership composes with the engine's turn-batch flags:
            # item 0 owns save/unload, item n-1 owns restore — but only when this call is
            # itself turn-first / turn-last, so a batch call that is 2nd of 3 media_gen
            # calls in one turn neither saves nor restores (the turn's first/last do).
            produced = []
            ok = 0
            for run_idx, (item_idx, item, backend, req) in enumerate(ready):
                item_first = is_first_vram and run_idx == 0
                item_last = is_last_vram and run_idx == n - 1
                out = self._run_item(backend, req, instance, item_first, item_last)
                failed = bool(out) and (out[0].text or '').startswith('ERROR:')
                label = self._batch_label(item, item_idx + 1, n)
                for j, ci in enumerate(out):
                    if ci.text:
                        out[j] = ContentItem(text=f'{label} — {ci.text}')
                        break
                produced.extend(out)
                if not failed:
                    ok += 1

            config_name = (ready[0][3].prepared or {}).get('config_name', '')
            results = [ContentItem(text=self._batch_summary(
                ok, n, 0, backend_id, config_name, seeds))]
            results.extend(produced)
            return results
        finally:
            # V8 belt-and-suspenders: unlink every preflighted temp file (idempotent;
            # _run_item's own finally already did it for every item it entered).
            for p in out_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    # ── Batch internals (BUG_0350) ────────────────────────────────────────────

    def _parse_batch(self, params: dict) -> list:
        """Resolve the batch take list (BUG_0350). PURE: no I/O, no VRAM, no backend
        work — safe to call from dms_bound and from call(). Raises ValueError with
        self-diagnosing messages; the caller turns those into ERROR results BEFORE any
        preflight/VRAM.

        Returns [] when this is not a batch call (neither 'prompts' nor 'variations'
        present) → the caller keeps the exact single-shot path. Otherwise one overlay
        dict per output, in fan-out order ``for prompt in prompts: for take in 1..K``:

            {'prompt': str, 'seed': int|None, 'take': int, 'takes': int}

        Seed resolution (single source of truth for dms_bound, labels and injection):
        'variations' omitted → seed None per item (today's per-backend randomisation);
        'variations' present on a seed-capable backend → ONE base seed ('overrides.seed',
        else one random draw) then a contiguous run across ALL outputs, wrapping at
        MAX_SEED; seedless backend ('seed' not in manifest overrides_keys, e.g. kobold_tts)
        → seed None (takes still produced, labelled take=k/K — not an error).
        """
        prompts = params.get('prompts')
        variations = params.get('variations')
        if prompts is None and variations is None:
            return []

        prompt = params.get('prompt')
        if prompts is not None and isinstance(prompt, str) and prompt.strip():
            raise ValueError("pass either 'prompt' or 'prompts', not both.")

        # bool is an int subclass — reject it explicitly so variations=True is not a sneaky 1.
        if variations is not None and (
                isinstance(variations, bool) or not isinstance(variations, int) or variations < 1):
            raise ValueError("'variations' must be an integer >= 1.")

        if prompts is not None:
            if not isinstance(prompts, list):
                raise ValueError("'prompts' must be an array of strings.")
            if not prompts:
                raise ValueError("'prompts' must not be empty.")
            if not all(isinstance(p, str) and p.strip() for p in prompts):
                raise ValueError("'prompts' must be an array of non-empty strings.")
        else:
            # variations-only: K takes of the shared 'prompt'. An absent/empty 'prompt'
            # is already rejected by call()'s prompt gate (or permitted by the backend's
            # prompt_optional_with_reference_file opt-out), so no check here.
            prompts = [prompt]

        backend_id = (params.get('backend') or '').strip()
        manifest = BACKEND_MANIFEST.get(backend_id) or {}
        if manifest.get('output_kind') == 'text':
            raise ValueError(
                f"backend '{backend_id}' produces text, not media; batching is not supported.")

        takes = variations if variations is not None else 1
        total = len(prompts) * takes
        cap = settings.MEDIA_GEN_MAX_BATCH
        if cap and total > cap:
            raise ValueError(
                f'batch of {total} exceeds MEDIA_GEN_MAX_BATCH ({cap}). '
                'Raise AGENT_CASCADE_MEDIA_GEN_MAX_BATCH or split the call.')

        seed_capable = 'seed' in (manifest.get('overrides_keys') or ())
        base_seed = None
        if variations is not None and seed_capable:
            overrides = params.get('overrides')
            raw = overrides.get('seed') if isinstance(overrides, dict) else None
            if raw is None:
                base_seed = random.randint(0, MAX_SEED)  # drawn ONCE so the run is contiguous
            else:
                try:
                    base_seed = int(raw)
                except (TypeError, ValueError):
                    raise ValueError(
                        "'overrides.seed' must be an integer when 'variations' is used "
                        '(the seed set is built by incrementing it).')

        items = []
        out_idx = 0
        for p in prompts:
            for take in range(1, takes + 1):
                seed = None
                if base_seed is not None:
                    seed_val = base_seed + out_idx  # one global contiguous run across ALL outputs
                    # Increment wraps at the unsigned-32-bit ceiling. A negative base
                    # passes through unwrapped (passthrough semantics of today's seed).
                    seed = seed_val % (MAX_SEED + 1) if seed_val > MAX_SEED else seed_val
                items.append({'prompt': p, 'seed': seed, 'take': take, 'takes': takes})
                out_idx += 1
        return items

    def _batch_label(self, item: dict, idx: int, total: int) -> str:
        """One-line identity prefix for a batch item result (the ONLY place labels are
        formatted — every output is reproducible from it: re-run with that seed as
        'overrides.seed'). Falls back to take=k/K when no seed was resolved (seedless
        backend, or variations omitted)."""
        if item['seed'] is not None:
            seed_part = f"seed={item['seed']}"
        else:
            seed_part = f"take={item['take']}/{item['takes']}"
        prompt_text = item['prompt'] or ''
        return f'[{idx}/{total}] prompt="{prompt_text}" {seed_part}'

    def _batch_summary(self, ok: int, total: int, failed_preflight: int,
                       backend_id: str, config_name: str, seeds: list) -> str:
        """First line of a batch result — the contract with _detect_tool_result_failure:
        a partial batch is NOT a failed tool call (non-ERROR first line); a batch with
        zero produced outputs starts with ERROR: so telemetry/frontend still flag it."""
        parts = []
        if backend_id:
            parts.append(f'backend={backend_id}')
        if config_name:
            parts.append(f'config={config_name}')
        distinct = sorted(set(seeds))
        if len(distinct) > 1:
            parts.append(f'seeds={distinct[0]}-{distinct[-1]}')
        elif len(distinct) == 1:
            parts.append(f'seeds={distinct[0]}')
        suffix = f" ({', '.join(parts)})" if parts else ''
        if failed_preflight:
            return (f'ERROR: media_gen batch aborted before generation — {failed_preflight} of '
                    f'{total} item(s) failed preflight, 0 generated, VRAM untouched{suffix}')
        if ok == 0:
            return f'ERROR: all {total} media_gen batch items failed{suffix}'
        if ok < total:
            return f'Batch: {ok}/{total} generated, {total - ok} failed{suffix}'
        return f'Batch: {ok}/{total} generated{suffix}'

    def _prepare_item(self, item: dict, params: dict, config: dict):
        """Build one batch item's MediaRequest, then resolve_backend + preflight — both
        strictly PRE-VRAM (the same V1 gate the single path had inline). Shared params
        (backend/config/negative_prompt/media_size/reference_file/overrides) come from
        the call; only prompt and the resolved seed are per-item, and the overrides dict
        is copied before the seed is set so items cannot contaminate each other.

        Raises RuntimeError/ValueError/OSError (the existing pre-VRAM error class).
        Returns (backend, req)."""
        overrides = params.get('overrides')
        if item['seed'] is not None:
            overrides = dict(overrides or {})
            overrides['seed'] = item['seed']
        req = MediaRequest(
            backend_id=params.get('backend') or '',
            config_name=params.get('config'),
            prompt=item['prompt'],
            negative_prompt=params.get('negative_prompt') or '',
            reference_file=params.get('reference_file'),
            media_size=params.get('media_size'),
            overrides=overrides,
            config=config,
            agent_pool=self.agent_pool,
        )
        backend = resolve_backend(req)
        backend.preflight(req)
        return backend, req

    def _run_item(self, backend, req, instance, is_first_vram: bool,
                  is_last_vram: bool) -> List[ContentItem]:
        """VRAM gate + generation + save for ONE already-preflighted request. Unchanged
        error paths (restore on every early exit, NOT in finally). The VRAM flags compose
        with the engine's turn-batch flags: in a batch only item 0 gets is_first_vram=True
        and only item n-1 gets is_last_vram=True, so the fan-out shares one save/unload/
        restore and only one model swap-in (deferred-restore semantics: see
        _restore_if_needed).
        """
        state = {'saved': False, 'held': None}
        # ── VRAM + GENERATE ──
        try:
            # Per-backend opt-out (default True = today's behaviour): when a backend's
            # block sets release_model_around_call=false, zero save/unload/restore calls.
            # Batch deferral: a non-first call in a media_gen batch skips this prologue entirely. The
            # deferral is keyed to BATCH POSITION (is_first_vram), NOT to the per-backend release flag —
            # so within one batch only the first call's release decision governs the save/unload. Accepted
            # trade-off: if the FIRST call fails in the PRE-VRAM block above (backend resolve / preflight),
            # it returns before arming any save or _state_label, and every later call (is_first_vram=False)
            # also skips the prologue — so the whole batch generates with the model resident (no VRAM freed).
            # This is accepted: it is rare (only a first-call preflight failure), self-heals, and has no
            # correctness impact — the safety net / label restore paths are simply never armed, so nothing leaks.
            # (BUG_0350 note: inside ONE batch call this gap no longer exists — _parse_batch + the
            # all-or-nothing preflight gate make any item's preflight failure abort before any VRAM work.)
            if is_first_vram:
                self._vram_release(
                    instance, state,
                    release=resolve_release_model_around_call(req.config, req.backend_id),
                )  # save → unload (INSIDE try, deliberately — V2)

            # Time only the actual generation call (not the VRAM dance) — V7.
            _gen_start = time.monotonic()
            result = backend.run(req)
            _gen_elapsed = time.monotonic() - _gen_start
        except (RuntimeError, TimeoutError) as e:
            self._restore_if_needed(instance, state, is_last_vram)
            return [ContentItem(text=f"ERROR: Media generation failed: {e}")]
        except Exception as e:
            logger.exception('Unexpected error during media generation')
            self._restore_if_needed(instance, state, is_last_vram)
            return [ContentItem(text=f"ERROR: Unexpected media generation error: {e}")]
        finally:
            # V8: unlink the temp output file on EVERY path, including the pre-run
            # raise (where run() is never reached). run() also unlinks on its own
            # paths; double-unlink is guarded by try/except OSError.
            out_path = (req.prepared or {}).get('out_path')
            if out_path:
                try:
                    os.remove(out_path)
                except OSError:
                    pass

        # ── POST: TEXT-output backends return the transcript inline (no media save). ──
        # Result shaping only: skip save_media_to_media + the typed media item. The VRAM restore
        # path is identical (restore after all work). The transcript file was already unlinked by
        # the finally above; the string is carried in result.text. Same manifest gate the single
        # path used (output_kind from the request's backend — value-identical to the old
        # requested_backend lookup, and batch calls reject text backends upstream).
        is_text_output = (BACKEND_MANIFEST.get((req.backend_id or '').strip()) or {}).get('output_kind') == 'text'
        if is_text_output:
            self._restore_if_needed(instance, state, is_last_vram)
            feedback = self._build_feedback_text(backend.id, req.prepared, _gen_elapsed, result.text)
            return [ContentItem(text=result.text), ContentItem(text=feedback)]

        # ── POST: save to media (may itself fail → must restore) ──
        try:
            media_path = save_media_to_media(
                result.data, req.media_type, source_name=f"{backend.id}_gen",
                ext=result.ext,
            )
        except Exception as e:
            logger.exception('Failed to save generated media')
            self._restore_if_needed(instance, state, is_last_vram)
            return [ContentItem(text=f"ERROR: Failed to save generated media: {e}")]

        # Primary restore now that all LLM-side work is done (V4/V5: not in finally,
        # no eager captioning ahead of it, so this is the actual state-restore).
        self._restore_if_needed(instance, state, is_last_vram)

        feedback = self._build_feedback(req.media_type, media_path, backend.id, req.prepared, _gen_elapsed)
        return [self._typed_content_item(req.media_type, media_path),
                ContentItem(text=feedback)]

    # ── Result shaping ────────────────────────────────────────────────────────

    def _typed_content_item(self, media_type: str, media_path: str) -> ContentItem:
        """Wrap the saved path in the ContentItem field matching the media type."""
        if media_type == 'image':
            return ContentItem(image=media_path)
        if media_type == 'audio':
            return ContentItem(audio=media_path)
        if media_type == 'video':
            return ContentItem(video=media_path)
        return ContentItem(text=media_path)

    def _build_feedback(self, media_type: str, media_path: str, backend_id: str,
                       prepared: dict, elapsed: float) -> str:
        """Build the descriptive feedback line shown after a generation. Never raises."""
        config_name = (prepared or {}).get('config_name', '')
        elapsed_str = _fmt_elapsed(elapsed)

        if media_type == 'image':
            # Report the ACTUAL generated dimensions, not the requested ones (read from
            # the saved file; fall back to 0). Never raises. Same rationale as image_gen.
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(media_path) as _im:
                    width, height = _im.size
            except Exception:
                width = height = 0
            return (f"Generated image: {media_path} ({width}x{height}, "
                    f"config={config_name}, backend={backend_id}, {elapsed_str})")

        if media_type == 'audio':
            voice = (prepared or {}).get('reference') or (prepared or {}).get('voice', '')
            voice_str = f", voice={voice}" if voice else ''
            return (f"Generated audio: {media_path} "
                    f"(config={config_name}, backend={backend_id}{voice_str}, {elapsed_str})")

        if media_type == 'video':
            return (f"Generated video: {media_path} "
                    f"(config={config_name}, backend={backend_id}, {elapsed_str})")

        return (f"Generated media: {media_path} "
                f"(config={config_name}, backend={backend_id}, {elapsed_str})")

    def _build_feedback_text(self, backend_id: str, prepared: dict,
                             elapsed: float, text: str) -> str:
        """Feedback line for a TEXT-output (ASR) result. Never raises.

        Reports the transcript length, not a saved path (there is no saved media).
        """
        elapsed_str = _fmt_elapsed(elapsed)
        n = len((text or '').strip())
        return f"Transcribed: {n} chars, backend={backend_id}, {elapsed_str}"

    # ── __help self-documentation ─────────────────────────────────────────────

    # Curated one-line descriptions keyed by config/workflow stem. Used only as a
    # fallback when the config/workflow JSON has no 'description' field. New configs
    # need no code change: add a "description" key to their JSON and it is used
    # automatically; otherwise a generic fallback is shown.
    _CONFIG_DESCRIPTIONS = {
        'Anima_INT8': 'text→image (Anima INT8 checkpoint)',
        'flux2_edit_9b_API': 'image edit (flux2 9B) — pass reference_file as the image to edit',
        'flux2_t2i_9b_API': 'text→image (flux2 9B text-to-image)',
        'zimg_turbo_API': 'text→image (z-image turbo, fast)',
        'video_wan2_2_14B_i2v': 'image→video (Wan2.2 14B i2v) — pass reference_file as the FIRST FRAME',
        'yue2_full': 'text→music (YuE2) — pass overrides.style + overrides.lyrics',
        'anima': 'text→image (stable-diffusion.cpp, Anima model)',
        'flux2_klein_9b': 'text→image (stable-diffusion.cpp, flux2 klein 9B)',
        'z_image_turbo': 'text→image (stable-diffusion.cpp, z-image turbo)',
        'kokoro_default': 'text→speech (Kokoro TTS voice)',
        'outetts_default': 'text→speech (OuteTTS voice)',
    }

    def _format_media_help(self, config: dict, backend_filter: str = '') -> str:
        """Render the __help self-documentation: generic section + per-backend sections.

        ``backend_filter`` (optional, lowercased id) restricts the per-backend
        sections to just that backend; empty renders all (unchanged default). An
        unknown filter renders no backend section plus a short known-backends note.

        Never raises. Sections come from ``BACKEND_MANIFEST`` (registry order), so
        a new backend appears here without touching this formatter. Availability
        lists are scanned lazily HERE only (never on the generation path),
        ownership-filtered (a stem claimed by another backend's ``media_types``
        belongs to that engine, not this one — the shared TTS config_dir holds
        voices for three engines), and capped.
        """
        try:
            from agent_cascade.utils.media_utils import _get_media_root
            media_root = str(_get_media_root())
        except Exception:
            media_root = '<workspace>/logs/media'
        lines = [
            'media_gen — generate image / video / audio. You MUST name a backend '
            '(no AUTO). Below: the generic contract, then each backend, the configs '
            'it accepts, what each produces, and the exact params to pass.',
            '',
            '== generic ==',
            '  args:      prompt (required) | backend (required, see below) | config (stem or full .json path)',
            '             negative_prompt (image) | reference_file (edit/i2v/TTS-clone)',
            '             media_size={width,height} image/video · {duration,fps} video · {duration} audio',
            '             overrides={...} merged over the loaded config',
            f'  output:    {media_root}/<images|audio|video>/ (fixed; not configurable)',
            '  vram:      by default the resident model is KV-saved and unloaded before the call and restored',
            "             after. Set a backend's \"release_model_around_call\" to false to keep the model loaded",
            '             (faster swap, more VRAM contention). Applies only when your endpoint supports state save.',
            '  help:      prompt="__help" lists backends and their available configs. No generation runs.',
            '  batch:     prompts=["a","b"] and/or variations=N -> cross product of takes, one backend invocation',
            f'             at a time, one VRAM swap (cap MEDIA_GEN_MAX_BATCH={settings.MEDIA_GEN_MAX_BATCH}); seeds increment from overrides.seed',
            '',
        ]
        # Ownership map: every stem claimed by ANY backend block (this is what makes
        # media_types useful — it is the ownership claim, preserved across UI saves by
        # the POST /api/media_gen merge-preserve).
        claimed = set()
        for block in (config or {}).values():
            if isinstance(block, dict):
                for stems in (block.get('media_types') or {}).values():
                    if isinstance(stems, list):
                        claimed.update(s for s in stems if isinstance(s, str))
        from agent_cascade.tools.media_backends import BACKEND_MANIFEST
        backend_filter = (backend_filter or '').strip().lower()
        matched = False
        for manifest in BACKEND_MANIFEST.values():
            if backend_filter and manifest['id'].lower() != backend_filter:
                continue
            matched = True
            try:
                lines.extend(
                    self._format_backend_help(manifest, (config or {}).get(manifest['id']), config, claimed)
                )
            except Exception:  # never-raises contract
                logger.debug('[MediaGen] __help backend section failed: %s', manifest['id'])
                continue
            lines.append('')
        if backend_filter and not matched:
            known = ', '.join(m['id'] for m in BACKEND_MANIFEST.values())
            lines.append(f'  (unknown backend "{backend_filter}" — known backends: {known})')
        return '\n'.join(lines)

    def _format_backend_help(self, manifest: dict, block, config: dict, claimed: set) -> list:
        """One backend's __help section, entirely from its manifest. May raise (caller guards)."""
        bid = manifest['id']
        block = block if isinstance(block, dict) else {}
        lines = [
            f"== backend: {bid} — {manifest['label']} ==",
            f"  note:      {manifest['note']}",
        ]
        if manifest['agent_args']:
            lines.append(f"  honors:    {', '.join(manifest['agent_args'])}")
        if manifest['overrides_keys']:
            lines.append(f"  overrides: {', '.join(manifest['overrides_keys'])}")

        # Server status: driven by any FieldSpec(kind='url') in the manifest, so a future
        # server-backed backend gets this for free (no hardcoded backend id). The live value
        # comes from the backend's own config block (same source as the rest of this section).
        # _probe_server_status never raises; this block is additionally guarded so a
        # malformed manifest/config value here cannot suppress the rest of the section.
        try:
            for spec in manifest.get('connection_fields') or []:
                if not isinstance(spec, dict) or spec.get('kind') != 'url':
                    continue
                url = (block.get(spec.get('key')) or '').strip()
                if not url:
                    lines.append('  server:    (not configured)')
                else:
                    lines.append(f"  server:    {url} — {_probe_server_status(url)}")
        except Exception:  # malformed field/config shape must not break __help
            logger.debug('[MediaGen] __help server status failed for %s', bid)

        # TEXT-output backends (output_kind='text'): show the exact params inline. The generic
        # _usage_for fallback always prepends prompt="<...>" and is unreachable for availability='none',
        # so a text backend must document its own usage here (no prompt fragment).
        if manifest.get('output_kind') == 'text':
            lines.append(f"  usage:     backend={bid}, reference_file=\"<input.wav>\"")
            lines.append('  output:    transcript returned inline as text (no saved media file)')

        mine = block.get('media_types') or {}
        if manifest['music_notes'] and isinstance(mine.get('audio'), list) and mine['audio']:
            lines.extend(manifest['music_notes'])

        avail_key = manifest['availability']
        avail_dir = block.get(avail_key) if avail_key != 'none' else None
        if isinstance(avail_dir, str) and avail_dir.strip():
            if list(manifest['media_types']) == ['audio']:
                noun = 'voices'
            elif 'video' in manifest['media_types']:
                noun = 'workflows'
            else:
                noun = 'configs'
            lines.append(f"  available {noun} ({avail_key}):")
            listed = 0
            own_stems = []
            # 1) stems claimed by THIS backend, in config order, grouped by media type.
            for mtype, stems in mine.items():
                if not isinstance(stems, list):
                    continue
                for stem in stems:
                    if not isinstance(stem, str):
                        continue
                    own_stems.append(stem)
                    if listed < self._HELP_MAX_STEMS:
                        lines.extend(self._help_entry(bid, mtype, stem, config))
                    listed += 1
            # 2) stems on disk claimed by NOBODY (marked as such).
            for stem in self._scan_available(bid, avail_dir):
                if stem in claimed or stem in own_stems:
                    continue
                if listed < self._HELP_MAX_STEMS:
                    lines.extend(
                        self._help_entry(bid, manifest['media_types'][0], stem, config, unclaimed=True)
                    )
                listed += 1
            extra = listed - self._HELP_MAX_STEMS
            if extra > 0:
                lines.append(f"  … (+{extra} more)")

        # Voice samples: list audio files in voices_dir (chatterbox).
        vd = resolve_block_dir(block, 'voices_dir')
        if vd and vd.is_dir():
            all_samples = list_voice_samples(vd)
            if all_samples:
                shown = all_samples[:self._HELP_MAX_STEMS]
                lines.append(f"  voice samples ({len(all_samples)}, {bid}.voices_dir = {vd}):")
                for name in shown:
                    lines.append(f"    - {name}")
                if len(all_samples) > len(shown):
                    lines.append(f"    … (+{len(all_samples) - len(shown)} more)")

        if manifest['release_model_supported']:
            default = bool(manifest['defaults'].get('release_model_around_call', True))
            lines.append(f"  release_model_around_call: supported (default {'true' if default else 'false'})")
        else:
            lines.append('  release_model_around_call: not supported (this backend never unloads)')
        return lines

    _HELP_MAX_STEMS = 40

    def _help_entry(self, backend_id: str, mtype, stem: str, config: dict, unclaimed: bool = False) -> list:
        desc = self._describe_config(backend_id, stem, config)
        usage = self._usage_for(backend_id, mtype, stem)
        mark = ' (unclaimed)' if unclaimed else ''
        return [f"    - {stem}{mark}  {desc}", f"      usage: {usage}"]

    def _scan_available(self, backend_id: str, directory: str) -> list:
        """Sorted stems in a config/workflow dir. Reuses image_gen's scanners; never raises."""
        try:
            avail = (BACKEND_MANIFEST.get(backend_id) or {}).get('availability')
            if avail == 'workflow_dir':
                from agent_cascade.tools.image_gen import _list_workflows
                entries = _list_workflows(directory)
            else:
                from agent_cascade.tools.image_gen import _list_sdcpp_configs
                entries = _list_sdcpp_configs(directory)
            return [e['name'] for e in entries if isinstance(e, dict) and isinstance(e.get('name'), str)]
        except Exception:
            return []

    def _describe_config(self, backend_id: str, stem: str, config: dict) -> str:
        """One-line description for a config/workflow stem. Never raises.

        Resolution order:
        1. Sidecar ``<stem>.meta.json`` (safe for ComfyUI workflows — the main
           JSON must remain a pure node map with only numeric-string keys).
        2. Inline ``description`` field in the config/workflow JSON itself
           (works for non-ComfyUI configs like sdcpp that have no node-map
           constraint).
        3. Hardcoded ``_CONFIG_DESCRIPTIONS`` fallback.
        """
        path = self._config_json_path(backend_id, stem, config)
        if path:
            # 1) Sidecar metadata file
            meta_path = path.replace('.json', '.meta.json')
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('description'), str):
                    return data['description']
            except (OSError, ValueError):
                pass
            # 2) Inline description in the config/workflow JSON
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('description'), str):
                    return data['description']
            except (OSError, ValueError):
                pass
        return self._CONFIG_DESCRIPTIONS.get(stem) or f"{stem} ({backend_id})"

    def _config_json_path(self, backend_id: str, stem: str, config: dict) -> str | None:
        """Absolute path to a config/workflow JSON for a stem, or None."""
        block = (config or {}).get(backend_id) or {}
        avail = (BACKEND_MANIFEST.get(backend_id) or {}).get('availability')
        d = block.get(avail) if avail in ('config_dir', 'workflow_dir') else None
        if not d or not isinstance(d, str):
            return None
        p = Path(d)
        if not p.is_absolute():
            root = Path(__file__).resolve().parent.parent.parent
            p = root / p
        return str(p / f"{stem}.json")

    def _usage_for(self, backend_id: str, mtype: str, stem: str) -> str:
        """Exact params to pass for one (backend, media-type, config) case.

        Data-driven: the per-media-type usage tails live in the manifest's
        ``usage_hints``; a backend that declares none for a media type gets a
        generic fallback assembled from its agent_args/overrides_keys. No
        per-backend branch here — a new backend documents itself in its manifest.
        """
        entry = BACKEND_MANIFEST.get(backend_id) or {}
        tail = (entry.get('usage_hints') or {}).get(mtype)
        if tail is None:
            parts = [f'prompt="<{mtype} description>"']
            if 'media_size' in entry.get('agent_args', ()):
                parts.append('media_size={"width": 1024, "height": 1024}')
            if entry.get('overrides_keys'):
                parts.append('overrides={'
                             + ', '.join(f'"{k}": "<{k}>"' for k in entry['overrides_keys'])
                             + '}')
            if 'reference_file' in entry.get('agent_args', ()):
                parts.append('reference_file="<input-file.ext>"')
            tail = ', '.join(parts)
        return f'backend={backend_id}, config={stem}, {tail}'
