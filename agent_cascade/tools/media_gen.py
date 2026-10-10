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

``media_gen`` generalises the ``image_gen`` tool over a **backend registry** where
each backend declares which media types it can produce. Backends implemented in
increment 1: ``sdcpp`` (stable-diffusion.cpp, image) and ``comfyui`` (ComfyUI
server, image). ``kobold_tts`` is registered as a capability stub so the AUTO
resolver's config-ownership path is testable; its generation lands in a later
increment. Video and music land in later increments too.

The canonical media-type vocabulary is exactly three types (image / video / audio).
TTS and music are both *audio* (a backend/config distinction, not an output type),
so both return ``ContentItem(audio=...)``.

Backend selection is ``AUTO`` by default: the config filename stem is the PRIMARY
discriminator (config ownership), never dict-order. An explicit ``backend`` always
wins.

VRAM management (every backend): before generation we save the owning instance's
KV state and unload all models from llama-autoloader to free VRAM. The sequence is
preflight → save → unload → generate → save media → restore. The restore is NOT in
a ``finally`` block: it is called explicitly at the end and on every early error
path (generation failure, media-save failure) so the agent's KV is never left
dangling. ``preflight`` runs strictly BEFORE any VRAM work, so a bad config /
missing model / out-of-roots ``reference_file`` never leaves the model unloaded.

No captioning happens inside this tool: the generated image is returned uncaptioned
and the router's ``caption_images`` flow supplies the caption later (image only —
video/audio are never sent to the vision guard). This is why the *primary* restore
is the one at the end: there is no inner LLM call to reason about.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

from agent_cascade.llm.schema import ContentItem
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.utils.media_utils import save_media_to_media
from agent_cascade.tools.media_backends import resolve_backend
from agent_cascade.tools.media_backends.base import MEDIA_TYPES, MediaRequest

from agent_cascade.tools.image_gen import _fmt_elapsed

logger = logging.getLogger(__name__)

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
    parameters = {
        'type': 'object',
        'properties': {
            'prompt': {
                'type': 'string',
                'description': (
                    'Text prompt for generation (or the text to speak for TTS).'
                ),
            },
            'backend': {
                'type': 'string',
                'description': (
                    "Backend id: 'AUTO' (default) | 'comfyui' | 'sdcpp' | 'kobold_tts'. "
                    'AUTO resolves the config stem to its owning backend.'
                ),
            },
            'config': {
                'type': 'string',
                'description': (
                    'Config name (filename stem) or full .json path. The primary path: '
                    'everything else is derived from it. If omitted, the resolved backend\'s '
                    'configured default is used.'
                ),
            },
            'media_type': {
                'type': 'string',
                'enum': list(MEDIA_TYPES),
                'description': (
                    'Optional hint — image | video | audio; used only to disambiguate AUTO '
                    '(music & tts both = audio).'
                ),
            },
            'negative_prompt': {
                'type': 'string',
                'description': 'Negative prompt to exclude elements (image backends).',
            },
            'server_url': {
                'type': 'string',
                'description': 'Optional override for server-backed backends (ComfyUI).',
            },
            'backend_dir': {
                'type': 'string',
                'description': 'Optional override for local-binary backend working dir.',
            },
            'reference_file': {
                'type': 'string',
                'description': (
                    'Reference/edit image for image-edit & i2v video first-frame; reference '
                    'audio for TTS voice-cloning. Generalizes image_gen input_image. Resolved '
                    'through the path sandbox before any upload/subprocess.'
                ),
            },
            'media_size': {
                'type': 'object',
                'properties': {
                    'width': {'type': 'integer', 'description': 'Output width in px (image/video).'},
                    'height': {'type': 'integer', 'description': 'Output height in px (image/video).'},
                    'duration': {'type': 'number', 'description': 'Duration in seconds (video/audio).'},
                    'fps': {'type': 'integer', 'description': 'Frames per second (video only).'},
                },
                'description': (
                    'Generalized size. Backend picks the fields it uses: image→width/height; '
                    'video→width/height/duration/fps; audio→duration.'
                ),
            },
            'overrides': {
                'type': 'object',
                'description': (
                    'Scalar overrides merged over the loaded config before injection '
                    '(e.g. seed, steps, guidance). Backend-namespaced objects cover extras.'
                ),
            },
        },
        'required': ['prompt'],
    }

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

    def _vram_release(self, instance, state: dict) -> None:
        """Additive VRAM save+unload prologue, used by every backend.

        Reproduces image_gen's guard verbatim: ``getattr(instance, '_last_endpoint_config',
        None)``, the ``isinstance(dict)`` check, the ``state_save_enabled``/``api_base``
        checks, the lazy ``state_ops`` import, and the ``is_autoloader_endpoint`` gate.
        Any non-qualifying path leaves ``state['saved']`` False with no state change.

        ``state`` is a mutable holder dict with keys ``'saved'`` and ``'held'``.
        ``saved`` is set True as soon as ``save_instance_state`` succeeds — BEFORE
        ``unload_all_models`` runs — so a raise from unload still lets the caller's
        except-block restore the KV (V2/F1), mirroring the inline prologue.
        """
        state['saved'] = False
        state['held'] = None
        if instance is None:
            return
        endpoint_cfg = getattr(instance, '_last_endpoint_config', None)
        if not isinstance(endpoint_cfg, dict):
            return
        if not endpoint_cfg.get('state_save_enabled'):
            return
        api_base = endpoint_cfg.get('api_base')
        if not api_base:
            return

        from agent_cascade.state_ops import (
            is_autoloader_endpoint, save_instance_state, unload_all_models,
        )
        if not is_autoloader_endpoint(api_base):
            return

        if not save_instance_state(instance):
            return
        # Record success BEFORE unload so a raise from unload_all_models below
        # still leaves state['saved'] True -> the caller restores the KV (V2/F1).
        state['saved'] = True
        state['held'] = {
            'api_base': api_base,
            'model': endpoint_cfg.get('model', ''),
        }
        if not unload_all_models(api_base):
            logger.warning(
                '[MediaGen] VRAM may be constrained; model was not unloaded before generation'
            )

    def _restore_vram_state(self, instance, held: dict) -> None:
        """Restore saved KV state after all LLM-side work is done (one retry on failure).

        A final restore failure is logged at ERROR but is non-fatal: the next LLM call
        triggers a fresh model load via autoloader JIT, so the system self-heals (the
        user only loses KV cache continuity).
        """
        from agent_cascade.state_ops import restore_instance_state
        for attempt in range(2):
            try:
                restore_instance_state(instance, held_endpoint_cfg=held)
                return
            except Exception as e:
                if attempt == 0:
                    logger.warning('[MediaGen] Restore attempt 1 failed, retrying: %s', e)
                    time.sleep(2)
                else:
                    logger.error(
                        '[MediaGen] State restore FAILED after generation — model may not be loaded: %s', e
                    )

    # ── The single choke point ────────────────────────────────────────────────

    def call(self, params, **kwargs) -> List[ContentItem]:
        try:
            params = self._verify_json_format_args(params)
        except (ValueError, TypeError) as e:
            return [ContentItem(text=f"ERROR: Invalid media_gen parameters: {e}")]

        prompt = params.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            return [ContentItem(text="ERROR: 'prompt' is required and must be a non-empty string.")]

        # Build the normalised request. media_type may be unset; the resolver derives
        # it from config ownership (or the explicit hint).
        req = MediaRequest(
            media_type=params.get('media_type') or '',
            backend_id=params.get('backend') or 'AUTO',
            config_name=params.get('config'),
            prompt=prompt,
            negative_prompt=params.get('negative_prompt') or '',
            server_url=params.get('server_url'),
            backend_dir=params.get('backend_dir'),
            reference_file=params.get('reference_file'),
            media_size=params.get('media_size'),
            overrides=params.get('overrides'),
            config=_get_media_gen_config(),
            agent_pool=self.agent_pool,
        )

        instance = self._get_instance(kwargs)
        state = {'saved': False, 'held': None}

        # ── PRE-VRAM: everything that can fail cheaply (V1) ──
        try:
            backend = resolve_backend(req)
            backend.preflight(req)
        except (RuntimeError, ValueError, OSError) as e:
            # audit defect 6: _load_workflow raises FileNotFoundError/OSError; resolve_tool_path
            # raises ValueError. None of these touch VRAM.
            return [ContentItem(text=f"ERROR: {e}")]

        # ── VRAM + GENERATE ──
        try:
            self._vram_release(instance, state)  # save → unload (INSIDE try, deliberately — V2)

            # Time only the actual generation call (not the VRAM dance) — V7.
            _gen_start = time.monotonic()
            result = backend.run(req)
            _gen_elapsed = time.monotonic() - _gen_start
        except (RuntimeError, TimeoutError) as e:
            if state['saved'] and instance is not None:
                self._restore_vram_state(instance, state['held'])
            return [ContentItem(text=f"ERROR: Media generation failed: {e}")]
        except Exception as e:
            logger.exception('Unexpected error during media generation')
            if state['saved'] and instance is not None:
                self._restore_vram_state(instance, state['held'])
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

        # ── POST: save to media (may itself fail → must restore) ──
        try:
            media_path = save_media_to_media(
                result.data, req.media_type, source_name=f"{backend.id}_gen"
            )
        except Exception as e:
            logger.exception('Failed to save generated media')
            if state['saved'] and instance is not None:
                self._restore_vram_state(instance, state['held'])
            return [ContentItem(text=f"ERROR: Failed to save generated media: {e}")]

        # Primary restore now that all LLM-side work is done (V4/V5: not in finally,
        # no eager captioning ahead of it, so this is the actual state-restore).
        if state['saved'] and instance is not None:
            self._restore_vram_state(instance, state['held'])

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
        """Build the descriptive feedback line (§5.4). Never raises."""
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
            return (f"Generated audio: {media_path} "
                    f"(config={config_name}, backend={backend_id}, {elapsed_str})")

        if media_type == 'video':
            return (f"Generated video: {media_path} "
                    f"(config={config_name}, backend={backend_id}, {elapsed_str})")

        return (f"Generated media: {media_path} "
                f"(config={config_name}, backend={backend_id}, {elapsed_str})")
