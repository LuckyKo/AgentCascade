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

"""Image generation tool.

Two paths:
  * SVG code in ``prompt`` → rendered locally to PNG via cairosvg (no VRAM use).
  * Text prompt → submitted to a ComfyUI server using a saved workflow JSON file.

Both return ``[ContentItem(image=path), ContentItem(text=feedback)]`` with the
image item left UNCAPTIONED — the same shape as ``view_image`` — so the router's
return-path guard (``_has_uncaptioned_images``) auto-generates a genuine vision
caption on demand on the next send.

VRAM management (text path only): before talking to ComfyUI we save the owning
instance's KV state and unload all models from llama-autoloader to free VRAM.
The sequence is save → unload → ComfyUI → save media → restore. The restore is
NOT in a ``finally`` block: it is called explicitly at the end and also on every
early error path (generation failure, media-save failure) so the agent's KV is
never left dangling. The saved label must always be cleared whenever the state
was saved, regardless of whether unload or ComfyUI succeeded.

No captioning happens inside this tool: the generated image is returned
uncaptioned and the router's ``caption_images`` flow supplies the caption
later, so there is no inner KV save/restore to reason about here.

This tool holds NO LLM of its own and never constructs a chat model. The old
placeholder's Change-E breaker gate and sticky-slot side-call gate are gone with
the placeholder: there is no LLM/endpoint here to gate.
"""

import json
import logging
import mimetypes
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import httpx

from agent_cascade.llm.schema import ContentItem
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.utils.media_utils import save_image_to_media

logger = logging.getLogger(__name__)

# ── Image gen config cache ─────────────────────────────────────────────────────
# The config lives at <AgentCascade_root>/config/image_gen.json. It is read with a
# short TTL so a concurrent UI write can't tear the read; the REST POST handler
# calls _invalidate_image_gen_config() to bust it immediately after saving.

_CONFIG_TTL = 30  # seconds — balance freshness vs. read frequency
_config_cache: dict = {}
_config_lock = threading.Lock()


def _image_gen_config_path() -> Path:
    """Return the path to image_gen.json under the AgentCascade project root.

    This file lives at agent_cascade/tools/image_gen.py, so the project root is
    three levels up (tools → agent_cascade → <AgentCascade_root>). A naive
    ``parent.parent`` would resolve to a stray ``agent_cascade/config/``; the real
    config dir sits one level higher.
    """
    return Path(__file__).resolve().parent.parent.parent / 'config' / 'image_gen.json'


def _get_image_gen_config() -> dict:
    """Read image gen config with a 30s cache to avoid concurrent read/write races.

    Returns an empty dict if the file is missing or malformed.
    """
    global _config_cache
    now = time.time()
    with _config_lock:
        cached = {k: v for k, v in _config_cache.items() if k != '_ts'}
        if cached and now - _config_cache.get('_ts', 0) < _CONFIG_TTL:
            return cached
        config_path = _image_gen_config_path()
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            _config_cache = {**data, '_ts': now}
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            logger.debug('image_gen config read failed (%s): %s', config_path, e)
            _config_cache = {'_ts': now}
        return {k: v for k, v in _config_cache.items() if k != '_ts'}


def _invalidate_image_gen_config() -> None:
    """Bust the image gen config cache.

    Called by the REST ``POST /api/image_gen`` handler after writing settings so
    the next tool call picks up fresh values immediately. Clears both the cached
    value and its timestamp under the lock.
    """
    global _config_cache
    with _config_lock:
        _config_cache = {}


# ── SVG detection & rendering ──────────────────────────────────────────────────

def _is_svg_code(text: str) -> bool:
    """Return True if ``text`` looks like an SVG document.

    Tolerates a leading XML declaration (``<?xml ... ?>``) and surrounding
    whitespace. Requires both a ``<svg`` opener and a ``</svg>`` closer so stray
    fragments are not mistaken for renderable documents.
    """
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    if stripped.startswith('<?xml'):
        end = stripped.find('?>')
        if end != -1:
            stripped = stripped[end + 2:].lstrip()
    return stripped.startswith('<svg') and '</svg>' in stripped


def _render_svg_to_png_bytes(svg_text: str) -> bytes:
    """Render an SVG string to PNG bytes via cairosvg.

    Raises ImportError/OSError (with install hints) if cairosvg or its native libs
    are unavailable, and ValueError on malformed SVG.
    """
    try:
        import cairosvg
    except ImportError as e:
        raise ImportError(
            'cairosvg is required to render SVG. Install it with: pip install cairosvg'
        ) from e
    except OSError as e:
        raise OSError(
            f"cairosvg native library error: {e}. On Windows you may need the GTK3 "
            'runtime (https://github.com/tschoonj/GTK3-Runtime-for-Windows/releases) '
            'or set GTK_LIBS.'
        ) from e
    return cairosvg.svg2png(bytestring=svg_text.encode('utf-8'))


def _svg_dimensions(svg_text: str, fallback_width: int = 1024,
                    fallback_height: int = 1024) -> Tuple[int, int]:
    """Best-effort read of an SVG's width/height for the caption.

    Falls back to (fallback_width, fallback_height) when not determinable. Never raises.
    """
    try:
        m = re.search(r'<svg[^>]*\bwidth\s*=\s*["\']?([\d.]+)', svg_text)
        w = int(float(m.group(1))) if m else 0
        m = re.search(r'<svg[^>]*\bheight\s*=\s*["\']?([\d.]+)', svg_text)
        h = int(float(m.group(1))) if m else 0
        return (w or fallback_width, h or fallback_height)
    except Exception:
        return (fallback_width, fallback_height)


# ── Workflow loading & parameter injection ─────────────────────────────────────

def _load_workflow(workflow_path: str) -> dict:
    """Load a ComfyUI API-format workflow JSON from a full path.

    Raises FileNotFoundError if the file is missing, json.JSONDecodeError if it is
    not valid JSON, and ValueError if it is not a mapping of node_id → node.
    """
    path = Path(workflow_path)
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {workflow_path}")
    with open(path, 'r', encoding='utf-8') as f:
        workflow = json.load(f)
    if not isinstance(workflow, dict):
        raise ValueError(
            f"Workflow JSON must be an object of node_id → node, got {type(workflow).__name__}"
        )
    return workflow


def _list_workflows(workflow_dir: str) -> List[dict]:
    """Return available workflows as ``[{'name': ..., 'path': ...}, ...]``.

    Used by error messages (and the REST workflows endpoint). Returns [] if the
    directory does not exist or contains no JSON files.
    """
    d = Path(workflow_dir)
    if not d.exists() or not d.is_dir():
        return []
    workflows = [{'name': f.stem, 'path': str(f)} for f in d.glob('*.json')]
    workflows.sort(key=lambda w: (w['name'], w['path']))
    return workflows


def _inject_params(workflow: dict, prompt: str, negative_prompt: str = '',
                   width: Optional[int] = None, height: Optional[int] = None,
                   seed: Optional[int] = None) -> Tuple[dict, List[str]]:
    """Inject generation parameters into a ComfyUI workflow (mutates in place).

    Handles the two observed workflow shapes:
      * direct integer dims + CLIPTextEncode nodes (zimg_turbo pattern)
      * PrimitiveStringMultiline prompt + node-reference dims via PrimitiveInt
        (flux2 pattern)

    Returns ``(workflow, report)`` where ``report`` lists what was injected.
    Raises ValueError if the positive prompt could not be placed anywhere.
    """
    if seed is None:
        seed = random.randint(0, 2**32 - 1)  # ComfyUI uses 32-bit unsigned seeds

    report: List[str] = []

    # 1. Collect candidate text nodes in document order.
    clip_nodes = []   # (node_id, node) — CLIPTextEncode with a "text" input
    prim_nodes = []   # (node_id, node) — PrimitiveStringMultiline with a "value" input
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get('inputs', {}) or {}
        ct = node.get('class_type')
        if ct == 'CLIPTextEncode' and 'text' in inputs:
            clip_nodes.append((node_id, node))
        elif ct == 'PrimitiveStringMultiline' and 'value' in inputs:
            prim_nodes.append((node_id, node))

    # 2. Inject the positive prompt.
    # Priority: PrimitiveStringMultiline (flux2 pattern), else first CLIPTextEncode
    # that already carries non-empty text (the positive slot), else the first one.
    positive_node_id = None
    if prim_nodes:
        nid, node = prim_nodes[0]
        node['inputs']['value'] = prompt
        positive_node_id = nid
        report.append(f"prompt → {nid} (PrimitiveStringMultiline)")
    elif clip_nodes:
        target = None
        for nid, node in clip_nodes:
            if node['inputs']['text']:  # non-empty ⇒ likely the positive slot
                target = (nid, node)
                break
        if target is None:
            target = clip_nodes[0]
        positive_node_id = target[0]
        target[1]['inputs']['text'] = prompt
        report.append(f"prompt → {positive_node_id} (CLIPTextEncode)")

    if positive_node_id is None:
        raise ValueError(
            'Could not inject prompt into workflow. No CLIPTextEncode or '
            'PrimitiveStringMultiline nodes found — check the workflow format.'
        )

    # 3. Inject the negative prompt (a CLIPTextEncode that is NOT the positive one).
    if negative_prompt:
        placed = False
        for nid, node in clip_nodes:
            if nid != positive_node_id:
                node['inputs']['text'] = negative_prompt
                report.append(f"negative → {nid} (CLIPTextEncode)")
                placed = True
                break
        if not placed:
            logger.debug(
                'image_gen: negative prompt ignored — no secondary CLIPTextEncode node found'
            )

    # 4. Override width/height (direct ints and/or node references).
    if width or height:
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get('inputs', {}) or {}
            ct = node.get('class_type', '')

            # Direct integer dims (zimg_turbo pattern).
            if 'width' in inputs and isinstance(inputs['width'], int) and width:
                inputs['width'] = width
                report.append(f"width={width} → {node_id}")
            if 'height' in inputs and isinstance(inputs['height'], int) and height:
                inputs['height'] = height
                report.append(f"height={height} → {node_id}")

            # Node references like ["75:68", 0] (flux2 pattern): follow to the
            # PrimitiveInt node and set its scalar value.
            for dim_key in ('width', 'height'):
                val = inputs.get(dim_key)
                if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                    ref_node_id = val[0]
                    ref_node = workflow.get(ref_node_id)
                    new_val = width if dim_key == 'width' else height
                    if (ref_node is not None
                            and ref_node.get('class_type') == 'PrimitiveInt'
                            and isinstance(new_val, int)):
                        ref_node['inputs']['value'] = new_val
                        report.append(f"{dim_key}={new_val} → {ref_node_id} (PrimitiveInt)")

            # CR Aspect Ratio node: force custom mode so our dims take effect.
            if ct == 'CR Aspect Ratio':
                if 'aspect_ratio' in inputs:
                    inputs['aspect_ratio'] = 'custom'
                if 'swap_dimensions' in inputs:
                    inputs['swap_dimensions'] = 'Off'
                report.append(f"CR Aspect Ratio → {node_id} (forced custom)")

    # 5. Set the seed on every node that carries a seed input.
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get('inputs', {}) or {}
        if 'seed' in inputs:
            inputs['seed'] = seed
            report.append(f"seed={seed} → {node_id}")
        if 'noise_seed' in inputs:
            inputs['noise_seed'] = seed
            report.append(f"noise_seed={seed} → {node_id}")

    return workflow, report


# ── ComfyUI client (submit / poll / download) ──────────────────────────────────

def _extract_seed(workflow: dict) -> Optional[int]:
    """Best-effort read of the seed that was injected (for metadata only)."""
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get('inputs', {}) or {}
        if 'seed' in inputs and isinstance(inputs['seed'], int):
            return inputs['seed']
        if 'noise_seed' in inputs and isinstance(inputs['noise_seed'], int):
            return inputs['noise_seed']
    return None


def _comfyui_generate(url: str, workflow: dict, timeout: int = 180,
                      client: Optional[httpx.Client] = None) -> Tuple[bytes, dict]:
    """Submit a workflow to ComfyUI, poll for completion, download the image.

    Args:
        url: Base URL of the ComfyUI server (e.g. ``http://localhost:8188``).
        workflow: The injected API-format workflow dict.
        timeout: Overall wall-clock budget in seconds for the whole generation.
        client: Optional httpx.Client (injected by tests with a MockTransport).

    Returns:
        ``(image_bytes, metadata)`` where metadata is ``{'seed': int | None}``.

    Raises:
        RuntimeError: Server unreachable, submission rejected, no image in outputs,
            or ComfyUI reported an execution error.
        TimeoutError: Generation did not complete within ``timeout`` seconds.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client()

    try:
        # 1. Submit the prompt.
        try:
            resp = client.post(f"{url}/prompt", json={'prompt': workflow}, timeout=30)
        except httpx.ConnectError as e:
            raise RuntimeError(f"ComfyUI server not reachable at {url}. Is it running?") from e
        except httpx.TimeoutException as e:
            raise RuntimeError(f"ComfyUI request timed out at {url}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"ComfyUI prompt submission failed: {resp.status_code} {resp.text[:200]}"
            )

        try:
            prompt_id = resp.json()['prompt_id']
        except (ValueError, KeyError) as e:
            raise RuntimeError(
                f"ComfyUI submit response missing 'prompt_id': {resp.text[:200]}"
            ) from e

        # 2. Poll /history/{id} until the prompt completes or times out.
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                hist_resp = client.get(f"{url}/history/{prompt_id}", timeout=10)
            except httpx.RequestError:
                continue  # transient network hiccup — keep polling until deadline
            if hist_resp.status_code != 200:
                continue
            try:
                history = hist_resp.json()
            except ValueError:
                continue
            entry = history.get(prompt_id)
            if not entry:
                continue

            status = entry.get('status', {}) or {}

            if status.get('status_str') == 'error':
                raise RuntimeError(f"ComfyUI generation error: {json.dumps(status)[:300]}")

            if status.get('completed'):
                # 3. Extract the first image from the node outputs and download it.
                outputs = entry.get('outputs', {}) or {}
                for _node_id, node_out in outputs.items():
                    images = (node_out or {}).get('images', []) or []
                    if images:
                        img_info = images[0]
                        filename = img_info['filename']
                        subfolder = img_info.get('subfolder', '')
                        folder_type = img_info.get('type', 'output')
                        view_url = (
                            f"{url}/view?filename={filename}"
                            f"&subfolder={subfolder}&type={folder_type}"
                        )
                        img_resp = client.get(view_url, timeout=30)
                        if img_resp.status_code == 200 and img_resp.content:
                            return img_resp.content, {'seed': _extract_seed(workflow)}
                raise RuntimeError('ComfyUI completed but no image found in outputs')

        raise TimeoutError(f"ComfyUI generation timed out after {timeout}s")
    finally:
        if own_client:
            client.close()


# ── stable-diffusion.cpp (sd-cli) backend ─────────────────────────────────────
# A one-shot local generation backend selected by ``type: "sdcpp"`` in the config.
# The ComfyUI path above is untouched; these helpers are additive and no-ops
# unless the config carries an ``sdcpp`` block.

_SDCPP_TYPE = 'sdcpp'


def _resolve_sdcpp_preset(cfg: dict, name: Optional[str] = None) -> dict:
    """Pick an sdcpp preset by name and resolve its model files against models_dir.

    Falls back to ``sdcpp.default_model`` when ``name`` is None/empty. Raises
    ``RuntimeError`` naming ALL missing files up front (pre-VRAM fast-fail, §4.3)
    so a missing VAE is not discovered only after the LLM has already been
    unloaded. Returns a copy of the preset dict with the model-file keys
    (``model``/``vae``/``clip_l``/``clip_g``/``t5xxl``/``llm``) rewritten to
    absolute paths.
    """
    sdcpp = cfg.get(_SDCPP_TYPE) or {}
    if not isinstance(sdcpp, dict):
        raise RuntimeError('sdcpp config block is missing or not an object')

    presets = sdcpp.get('presets') or {}
    if not isinstance(presets, dict) or not presets:
        raise RuntimeError(
            'No sdcpp presets configured. Add a "presets" object to the sdcpp config block.'
        )

    preset_name = name if name else sdcpp.get('default_model')
    if not preset_name or preset_name not in presets:
        available = ', '.join(sorted(presets.keys()))
        raise RuntimeError(
            f"sdcpp preset '{preset_name}' not found. Available presets: {available}"
        )

    preset = dict(presets[preset_name])
    models_dir = sdcpp.get('models_dir', '')

    def _join(rel: str) -> str:
        return str(Path(models_dir) / rel) if models_dir else str(rel)

    missing = []
    for key in ('model', 'vae', 'clip_l', 'clip_g', 't5xxl', 'llm'):
        rel = preset.get(key)
        if rel:
            full = _join(rel)
            if not Path(full).is_file():
                missing.append(full)
            preset[key] = full

    if not preset.get('model'):
        raise RuntimeError(f"sdcpp preset '{preset_name}' has no 'model' file configured")
    if missing:
        raise RuntimeError('sdcpp model file(s) not found: ' + ', '.join(missing))
    return preset


def _sdcpp_argv(cfg: dict, preset: dict, *, prompt: str, negative_prompt: str = '',
                width: Optional[int] = None, height: Optional[int] = None,
                seed: Optional[int] = None, steps: Optional[int] = None,
                cfg_scale: Optional[float] = None, guidance: Optional[float] = None,
                sampler: Optional[str] = None, output_path: str) -> List[str]:
    """Build the sd-cli argv list. **Pure** — no I/O, no logging, no env reads.

    Emission order is fixed so unit tests can assert an exact list. Model paths
    are already absolute (resolved by ``_resolve_sdcpp_preset``); this function
    only selects the binary and formats scalars. It NEVER builds a command
    string — the caller runs ``subprocess.run(argv, shell=False)`` so the list
    form does its own quoting (a shell would split a multi-word prompt).

    Resolution order for every numeric/dim: tool param > preset default > fallback.
    """
    sdcpp = cfg.get(_SDCPP_TYPE) or {}

    # F8: a prompt whose first non-space char is '-' is mis-parsed by list2cmdline
    # (emitted unquoted) as a CLI flag. Reject with a clear error.
    if prompt.lstrip().startswith('-'):
        raise ValueError(
            "prompt must not start with '-' (it would be mis-parsed as a CLI flag)"
        )

    argv: List[str] = [sdcpp.get('binary', 'sd-cli')]
    argv += ['-M', 'img_gen']

    # Model selection: -m (full checkpoint) or --diffusion-model (standalone).
    model = preset.get('model', '')
    if preset.get('model_arg') == 'diffusion_model':
        argv += ['--diffusion-model', model]
    else:
        argv += ['-m', model]

    # Text encoders + VAE — each only if non-empty, in fixed order.
    for flag, key in (('--clip_l', 'clip_l'), ('--clip_g', 'clip_g'),
                      ('--t5xxl', 't5xxl'), ('--llm', 'llm'), ('--vae', 'vae')):
        val = preset.get(key)
        if val:
            argv += [flag, val]

    vae_format = preset.get('vae_format')
    if vae_format:
        argv += ['--vae-format', vae_format]

    # Prompt / negative prompt.
    argv += ['-p', prompt]
    if negative_prompt:
        argv += ['-n', negative_prompt]

    # Dimensions: tool param > preset default > 512.
    w = width if width else (preset.get('width') or 512)
    h = height if height else (preset.get('height') or 512)
    argv += ['-W', str(int(w)), '-H', str(int(h))]

    # Steps / cfg-scale: tool param > preset default > fallback.
    s = steps if steps is not None else (preset.get('steps') or 20)
    argv += ['--steps', str(int(s))]
    c = cfg_scale if cfg_scale is not None else (preset.get('cfg_scale') or 7.0)
    argv += ['--cfg-scale', f'{float(c):g}']

    # Guidance: only emitted when resolved non-None (tool param > preset).
    g = guidance if guidance is not None else preset.get('guidance')
    if g is not None:
        argv += ['--guidance', f'{float(g):g}']

    # Sampling method: tool param > preset default > omit.
    sm = sampler if sampler is not None else preset.get('sampler')
    if sm:
        argv += ['--sampling-method', sm]

    # Seed (F3): None -> -s -1 (explicit random); >=0 -> -s <seed>; <0 -> -s -1.
    try:
        seed_val = int(seed)
    except (TypeError, ValueError):
        seed_val = None
    if seed_val is None or seed_val < 0:
        argv += ['-s', '-1']
    else:
        argv += ['-s', str(seed_val)]

    # Output path (always present, absolute) + log level.
    argv += ['-o', str(output_path)]
    argv += ['--log-level', 'warn']

    # Optional backend / offload / verbatim tail.
    backend = sdcpp.get('backend')
    if backend:
        argv += [backend]
    if sdcpp.get('offload_to_cpu'):
        argv += ['--offload-to-cpu']
    extra = sdcpp.get('extra_args') or []
    if isinstance(extra, list):
        argv.extend(extra)

    return argv


def _sdcpp_generate(argv: List[str], timeout: int = 900) -> bytes:
    """Run sd-cli as a one-shot subprocess and return the generated image bytes.

    Mirrors ``_comfyui_generate``'s error contract so the caller's existing
    ``except (RuntimeError, TimeoutError)`` works unchanged. The ``-o`` output
    path (emitted by ``_sdcpp_argv``) is read to bytes and then unlinked in a
    ``finally`` — the temp-file cleanup is the one genuinely new concern the
    sdcpp path has that the ComfyUI path does not (§5.2).

    Raises:
        RuntimeError: binary missing, non-zero exit (with stderr tail + exit code),
            or rc==0 but no/empty output file.
        TimeoutError: the subprocess did not finish within ``timeout`` seconds.
    """
    binary = argv[0]
    if not Path(binary).is_file():
        raise RuntimeError(
            f'sd-cli not found at {binary}. Check the sdcpp.binary config value.'
        )

    # Locate the -o output path emitted by _sdcpp_argv.
    out_path = None
    for i, tok in enumerate(argv):
        if tok == '-o' and i + 1 < len(argv):
            out_path = argv[i + 1]
            break
    if not out_path:
        raise RuntimeError('sd-cli argv is missing an -o output path')

    # The temp file is cleaned up on EVERY path (success and failure) so it never
    # leaks, even when a non-zero exit / timeout / missing output raises below.
    try:
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f'stable-diffusion.cpp generation timed out after {timeout}s'
            ) from e

        rc = proc.returncode
        if rc != 0:
            stderr_tail = (proc.stderr or b'').decode('utf-8', errors='replace')[-800:]
            raise RuntimeError(
                f'sd-cli exited with code {rc} (argv: {argv}). stderr tail:\n{stderr_tail}'
            )

        if not Path(out_path).is_file() or Path(out_path).stat().st_size == 0:
            raise RuntimeError(f'sd-cli exited 0 but produced no image at {out_path}')

        return Path(out_path).read_bytes()
    finally:
        # Best-effort unlink of the temp file. A failure here is non-fatal.
        try:
            os.remove(out_path)
        except OSError:
            pass


def _upload_image(url: str, local_path: Path, client: Optional[httpx.Client] = None) -> str:
    """Upload a local image to ComfyUI's input directory.

    Sends the file as a multipart ``image`` part (the standard ComfyUI upload
    mechanism) and returns the server-side filename reference to wire into a
    ``LoadImage`` node. The server-returned ``name`` is used (not the local
    filename) so duplicate uploads that ComfyUI renames are referenced correctly.

    Args:
        url: Base URL of the ComfyUI server (e.g. ``http://localhost:8188``).
        local_path: Local path to the image file to upload.
        client: Optional httpx.Client (injected by tests with a MockTransport).

    Returns:
        The server-side image reference, e.g. ``"cat.png"`` or ``"sub/cat.png"``.

    Raises:
        RuntimeError: Upload failed (non-200, missing 'name', or network error).
    """
    own_client = client is None
    if own_client:
        client = httpx.Client()
    try:
        mime_type = mimetypes.guess_type(local_path.name)[0] or 'application/octet-stream'
        with local_path.open('rb') as f:
            resp = client.post(
                f"{url}/upload/image",
                files={'image': (local_path.name, f, mime_type)},
                data={'type': 'input'},
                timeout=30,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ComfyUI image upload failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        name = data.get('name')
        if not name:
            raise RuntimeError(f"ComfyUI upload response missing 'name': {resp.text[:200]}")
        subfolder = data.get('subfolder') or ''
        return f"{subfolder}/{name}" if subfolder else name
    except OSError as e:
        raise RuntimeError(f"ComfyUI image upload failed to read {local_path.name}: {e}") from e
    except httpx.RequestError as e:
        raise RuntimeError(f"ComfyUI image upload failed at {url}: {e}") from e
    finally:
        if own_client:
            client.close()


# ── The tool ───────────────────────────────────────────────────────────────────

@register_tool('image_gen', allow_overwrite=True)
class ImageGen(BaseTool):
    """Generate an image via ComfyUI (text prompt) or render SVG code to an image.

    Returns ``[ContentItem(image=path), ContentItem(text=feedback)]`` with the
    image item left uncaptioned — same shape as view_image, so the router's
    return-path guard (_has_uncaptioned_images) auto-captions it on demand.
    No LLM is constructed here; config is read lazily at call time.
    """

    name = 'image_gen'
    description = TOOL_METADATA['image_gen']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'prompt': {
                'type': 'string',
                'description': (
                    'Text prompt for image generation, or SVG code to render to an image.'
                ),
            },
            'negative_prompt': {
                'type': 'string',
                'description': 'Negative prompt to exclude elements (API generation only).',
            },
            'workflow': {
                'type': 'string',
                'description': (
                    'Full path to a ComfyUI workflow JSON file. If omitted, uses the '
                    'default workflow selected in UI settings.'
                ),
            },
            'width': {'type': 'integer', 'description': 'Output width in pixels (overrides workflow default).'},
            'height': {'type': 'integer', 'description': 'Output height in pixels (overrides workflow default).'},
            'seed': {'type': 'integer', 'description': 'Random seed for reproducibility (random if omitted).'},
            'input_image': {
                'type': 'string',
                'description': (
                    'Optional local image file path to upload into ComfyUI for workflows that '
                    'contain exactly one standard LoadImage node. Use for image editing/reference. '
                    'Not used for SVG rendering.'
                ),
            },
            'model': {
                'type': 'string',
                'description': (
                    'Name of a stable-diffusion.cpp preset to use (only when type=sdcpp). '
                    'If omitted, uses the default_model preset from settings.'
                ),
            },
            'sampler': {
                'type': 'string',
                'description': 'Sampling method to pass to sd-cli (overrides the preset default, e.g. "euler_a").',
            },
            'guidance': {
                'type': 'number',
                'description': 'Guidance scale for sd-cli (overrides the preset default; omit to use the preset).',
            },
            'steps': {
                'type': 'integer',
                'description': 'Number of diffusion steps for sd-cli (overrides the preset default; lower = faster).',
            },
        },
        'required': ['prompt'],
    }

    def __init__(self, cfg: Optional[Dict] = None, **kwargs):
        super().__init__(cfg)
        # No LLM construction. The owning agent pool is used only to resolve the
        # calling instance for VRAM save/unload/restore at call time.
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
            logger.debug("image_gen: failed to resolve instance '%s': %s", inst_name, e)
            return None

    def call(self, params: Union[str, dict], **kwargs) -> List[ContentItem]:
        try:
            params = self._verify_json_format_args(params)
        except (ValueError, TypeError) as e:
            return [ContentItem(text=f"ERROR: Invalid image_gen parameters: {e}")]

        prompt = params.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            return [ContentItem(text="ERROR: 'prompt' is required and must be a non-empty string.")]

        # ── SVG path (local render — no VRAM management needed) ──────────────
        # input_image is a ComfyUI-only feature (it uploads a reference image and wires a
        # LoadImage node); SVG rendering is fully local, so reject the combination early.
        if params.get('input_image') and _is_svg_code(prompt):
            return [ContentItem(text='ERROR: input_image is only supported for ComfyUI workflows, not SVG rendering.')]
        if _is_svg_code(prompt):
            return self._handle_svg(prompt, params)

        # ── Backend dispatch: stable-diffusion.cpp vs ComfyUI ─────────────────
        # Selected by the config 'type' key (default 'comfyui'). At this point
        # 'prompt' is validated non-empty and the SVG branch has already returned,
        # so every variable the sdcpp path needs is bound.
        if _get_image_gen_config().get('type') == _SDCPP_TYPE:
            return self._handle_sdcpp(params, kwargs)

        # ── Text prompt path (ComfyUI + VRAM management) ─────────────────────
        return self._handle_text_prompt(params, kwargs)

    # ------------------------------------------------------------------ #
    #  SVG path                                                          #
    # ------------------------------------------------------------------ #

    def _handle_svg(self, svg_text: str, params: dict) -> List[ContentItem]:
        try:
            png_bytes = _render_svg_to_png_bytes(svg_text)
        except ImportError as e:
            return [ContentItem(text=f"ERROR: {e}")]
        except OSError as e:
            return [ContentItem(text=f"ERROR: {e}")]
        except Exception as e:
            logger.exception('SVG render failed')
            return [ContentItem(text=f"ERROR: SVG parse/render error: {e}")]

        try:
            media_path = save_image_to_media(image_source=png_bytes, source_name='svg_render')
        except Exception as e:
            logger.exception('Failed to save rendered SVG image')
            return [ContentItem(text=f"ERROR: Failed to save rendered image: {e}")]

        w, h = _svg_dimensions(svg_text)
        # Match the ComfyUI path's feedback shape (absolute path + dimensions). The SVG
        # render is fully local (no LLM call produced a real description), so the image item
        # is left UNCAPTIONED: the return path (_has_uncaptioned_images → caption_images)
        # generates a genuine vision caption for it, same as any other image. The separate
        # text item carries the descriptive line for text-only agents.
        feedback = f"Generated image: {media_path} ({w}x{h}, source=svg)"
        return [ContentItem(image=media_path), ContentItem(text=feedback)]

    # ------------------------------------------------------------------ #
    #  Text prompt path (ComfyUI)                                        #
    # ------------------------------------------------------------------ #

    def _handle_text_prompt(self, params: dict, kwargs: dict) -> List[ContentItem]:
        config = _get_image_gen_config()

        url = config.get('url')
        if not url:
            return [ContentItem(text=(
                'ERROR: No ComfyUI server configured. Set the image generation '
                'server URL in UI settings (config/image_gen.json).'
            ))]
        try:
            timeout = int(config.get('timeout', 180))
        except (TypeError, ValueError):
            timeout = 180

        # Resolve the workflow path: param > config default > error listing available.
        workflow_path = params.get('workflow') or config.get('default_workflow')
        if not workflow_path:
            available = _list_workflows(config.get('workflow_dir', ''))
            names = ', '.join(w['name'] for w in available) if available else 'none'
            return [ContentItem(text=(
                'ERROR: No workflow specified and no default workflow configured. '
                f"Available workflows: {names}. Pass a full path via the 'workflow' "
                'parameter or set a default in UI settings.'
            ))]

        # Load + inject BEFORE touching VRAM, so config/format errors don't leave
        # the model unloaded with state saved.
        try:
            workflow = _load_workflow(workflow_path)
        except FileNotFoundError as e:
            available = _list_workflows(config.get('workflow_dir', ''))
            names = ', '.join(w['name'] for w in available) if available else 'none'
            return [ContentItem(text=f"ERROR: {e}. Available workflows: {names}")]
        except (json.JSONDecodeError, ValueError) as e:
            return [ContentItem(text=f"ERROR: Invalid workflow JSON '{workflow_path}': {e}")]

        try:
            workflow, report = _inject_params(
                workflow,
                prompt=params['prompt'],
                negative_prompt=params.get('negative_prompt') or '',
                width=params.get('width'),
                height=params.get('height'),
                seed=params.get('seed'),
            )
        except ValueError as e:
            return [ContentItem(text=f"ERROR: {e}")]
        logger.info('image_gen injection for %s: %s', Path(workflow_path).name, '; '.join(report))

        # ── Optional input_image: upload + wire LoadImage node (before VRAM save) ──────
        # After parameter injection, before the VRAM block — upload failures return early
        # without saving/unloading model state. Absent param = byte-identical path.
        input_image = params.get('input_image')
        if input_image:
            if not isinstance(input_image, str) or not input_image.strip():
                return [ContentItem(text="ERROR: 'input_image' must be a non-empty string path.")]
            try:
                from agent_cascade.utils.tool_path_resolver import resolve_tool_path
                local_path = resolve_tool_path(input_image.strip(), mode='ro', agent_pool=self.agent_pool)
            except ValueError as e:
                return [ContentItem(text=f"ERROR: Invalid input_image path: {e}")]
            if not local_path.is_file():
                return [ContentItem(text=f"ERROR: input_image file not found: {input_image}")]
            # Find standard LoadImage nodes (scalar string 'image' input only).
            load_nodes = [
                node_id
                for node_id, node in workflow.items()
                if isinstance(node, dict)
                and node.get('class_type') == 'LoadImage'
                and isinstance((node.get('inputs') or {}).get('image'), str)
            ]
            if not load_nodes:
                return [ContentItem(text=(
                    'ERROR: input_image was provided, but the selected workflow has no '
                    'standard LoadImage node.'
                ))]
            if len(load_nodes) > 1:
                return [ContentItem(text=(
                    f"ERROR: input_image supports exactly one LoadImage node, but the workflow "
                    f"has {len(load_nodes)}."
                ))]
            try:
                image_ref = _upload_image(url, local_path)
            except RuntimeError as e:
                return [ContentItem(text=f"ERROR: {e}")]
            workflow[load_nodes[0]]['inputs']['image'] = image_ref
            report.append(f"input_image → {load_nodes[0]} (LoadImage)")

        # ── VRAM management: save → unload → (ComfyUI) → restore ──
        # Restore is NOT in a finally block. It is called explicitly at the end (after
        # all LLM-side work, so the model reload happens once) and on every early
        # error path so the agent's KV is never left dangling. _state_saved
        # stays False until save_instance_state returns True, so a failure before the
        # state was saved never triggers a spurious restore.
        # NOTE: no captioning happens here — the image is returned uncaptioned and
        # the router's caption_images flow captions it later on the return path.
        instance = self._get_instance(kwargs)
        endpoint_cfg = getattr(instance, '_last_endpoint_config', None) if instance is not None else None
        _state_saved = False
        held = None

        try:
            if (instance is not None and isinstance(endpoint_cfg, dict)
                    and endpoint_cfg.get('state_save_enabled')
                    and endpoint_cfg.get('api_base')):
                from agent_cascade.state_ops import (
                    is_autoloader_endpoint, save_instance_state, unload_all_models,
                )
                if is_autoloader_endpoint(endpoint_cfg.get('api_base', '')):
                    _state_saved = save_instance_state(instance)
                    if _state_saved:
                        held = {
                            'api_base': endpoint_cfg['api_base'],
                            'model': endpoint_cfg.get('model', ''),
                        }
                        if not unload_all_models(endpoint_cfg['api_base']):
                            logger.warning(
                                '[ImageGen] VRAM may be constrained; model was not unloaded before ComfyUI'
                            )

            image_bytes, _meta = _comfyui_generate(url, workflow, timeout=timeout)
        except (RuntimeError, TimeoutError) as e:
            # Generation failed — restore immediately so the agent's KV is not left dangling.
            if _state_saved and instance is not None:
                self._restore_vram_state(instance, held)
            return [ContentItem(text=f"ERROR: Image generation failed: {e}")]
        except Exception as e:
            logger.exception('Unexpected error during image generation')
            if _state_saved and instance is not None:
                self._restore_vram_state(instance, held)
            return [ContentItem(text=f"ERROR: Unexpected image generation error: {e}")]

        # Save the result through the media pipeline.
        try:
            media_path = save_image_to_media(image_source=image_bytes, source_name='comfyui_gen')
        except Exception as e:
            logger.exception('Failed to save generated image')
            if _state_saved and instance is not None:
                self._restore_vram_state(instance, held)
            return [ContentItem(text=f"ERROR: Failed to save generated image: {e}")]

        width = params.get('width') or 0
        height = params.get('height') or 0
        wf_name = Path(workflow_path).name

        # Primary restore now that all LLM-side work is done. There is no eager
        # captioning ahead of it (the image is returned uncaptioned), so this is the
        # actual state-restore. One retry with a 2s delay; non-fatal on final failure.
        if _state_saved and instance is not None:
            self._restore_vram_state(instance, held)

        feedback = f"Generated image: {media_path} ({width}x{height}, workflow={wf_name})"
        # The image item is intentionally left uncaptioned so the router's return-path
        # guard (_has_uncaptioned_images) auto-generates a genuine vision caption on
        # demand (same as the SVG path). The separate text item carries the descriptive
        # line for text-only agents.
        return [ContentItem(image=media_path), ContentItem(text=feedback)]

    @staticmethod
    def _restore_vram_state(instance, held: dict) -> None:
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
                    logger.warning('[ImageGen] Restore attempt 1 failed, retrying: %s', e)
                    time.sleep(2)
                else:
                    logger.error(
                        '[ImageGen] State restore FAILED after ComfyUI — model may not be loaded: %s', e
                    )

    # ------------------------------------------------------------------ #
    #  stable-diffusion.cpp path                                         #
    # ------------------------------------------------------------------ #

    def _vram_release(self, instance, state: dict) -> None:
        """Additive VRAM save+unload prologue, used ONLY by ``_handle_sdcpp`` in v1.

        Reproduces the guard of ``_handle_text_prompt`` verbatim (F6) so the sdcpp
        path frees VRAM identically to the ComfyUI path: ``getattr(instance,
        '_last_endpoint_config', None)``, the ``isinstance(dict)`` check, the
        ``state_save_enabled``/``api_base`` checks, the lazy ``state_ops`` import,
        and the ``is_autoloader_endpoint`` gate. Any non-qualifying path leaves
        ``state['saved']`` False with no state change.

        ``state`` is a mutable holder dict with keys ``'saved'`` and ``'held'``.
        ``saved`` is set to True as soon as ``save_instance_state`` succeeds —
        BEFORE ``unload_all_models`` runs — so that a raise from unload still lets
        the caller's except-block restore the KV (F1), exactly mirroring the
        ComfyUI inline prologue where ``_state_saved`` is set prior to unload.
        ``held`` is the endpoint config needed by ``_restore_vram_state``. The
        ComfyUI path keeps its own inline block byte-identical; this seam exists
        so a future change can adopt it without smuggling a refactor into this
        additive change (§5.2/§11.1).
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
        # still leaves state['saved'] True -> the caller restores the KV (F1).
        state['saved'] = True
        state['held'] = {
            'api_base': api_base,
            'model': endpoint_cfg.get('model', ''),
        }
        if not unload_all_models(api_base):
            logger.warning(
                '[ImageGen] VRAM may be constrained; model was not unloaded before sd-cli'
            )

    def _handle_sdcpp(self, params: dict, kwargs: dict) -> List[ContentItem]:
        """Generate an image via a one-shot local sd-cli (stable-diffusion.cpp).

        Reuses the same VRAM save/unload/restore dance as the ComfyUI path
        (via the additive ``_vram_release`` seam + ``_restore_vram_state``).
        Returns ``[ContentItem(image=...), ContentItem(text=...)]`` with the
        image item left UNCAPTIONED, same as the ComfyUI/SVG paths.
        """
        config = _get_image_gen_config()
        sdcpp_cfg = config.get(_SDCPP_TYPE)
        if not isinstance(sdcpp_cfg, dict) or not sdcpp_cfg:
            return [ContentItem(text=(
                'ERROR: stable-diffusion.cpp backend selected (type=sdcpp) but no '
                '"sdcpp" config block is present. Add it in UI settings '
                '(config/image_gen.json).'
            ))]

        # sdcpp timeout: sdcpp.timeout > top-level timeout > 900 (F2).
        try:
            timeout = int(sdcpp_cfg.get('timeout', config.get('timeout', 900)))
        except (TypeError, ValueError):
            timeout = 900

        # input_image is a ComfyUI-only feature; reject it for the sdcpp backend
        # (mirrors the SVG rejection in call()).
        if params.get('input_image'):
            return [ContentItem(text='ERROR: input_image is not supported by the stable-diffusion.cpp backend.')]

        # Resolve the preset + model files BEFORE touching VRAM, so a missing file
        # fails fast without a wasted save/unload/restore cycle (§4.3).
        try:
            preset = _resolve_sdcpp_preset(config, params.get('model'))
        except RuntimeError as e:
            return [ContentItem(text=f"ERROR: {e}")]

        # Create the temp output file (mkstemp) — the one genuinely new concern the
        # sdcpp path has. _sdcpp_generate reads it and unlinks it in a finally (F4).
        fd, out_path = tempfile.mkstemp(suffix='.png', prefix='sdcpp_')
        os.close(fd)

        # Build the argv (pure). A prompt whose first non-space char is '-' is
        # rejected here (F8) so list2cmdline can't mis-parse it as a flag.
        try:
            argv = _sdcpp_argv(
                config, preset,
                prompt=params['prompt'],
                negative_prompt=params.get('negative_prompt') or '',
                width=params.get('width'),
                height=params.get('height'),
                seed=params.get('seed'),
                steps=params.get('steps'),
                guidance=params.get('guidance'),
                sampler=params.get('sampler'),
                output_path=out_path,
            )
        except ValueError as e:
            try:
                os.remove(out_path)
            except OSError:
                pass
            return [ContentItem(text=f"ERROR: {e}")]

        # ── VRAM management: save → unload → (sd-cli) → restore ──────────────
        # F1 (CRITICAL): the save+unload prologue runs INSIDE the try (via the
        # _vram_release seam, §5.2/§11.1) so a raise from unload_all_models still
        # falls through to the restore below — the agent's KV is never left
        # dangling. Restore is NOT in a finally: it is called explicitly on every
        # error path and once at the end, matching the ComfyUI invariant.
        # F7: on a timeout, process.kill() (TerminateProcess on Windows) frees
        # CUDA VRAM only on a driver delay; _restore_vram_state's 2s retry covers
        # this settle window.
        instance = self._get_instance(kwargs)
        _state = {'saved': False, 'held': None}

        try:
            self._vram_release(instance, _state)

            image_bytes = _sdcpp_generate(argv, timeout=timeout)
        except (RuntimeError, TimeoutError) as e:
            # Generation failed — restore immediately so the KV is not left dangling.
            if _state['saved'] and instance is not None:
                self._restore_vram_state(instance, _state['held'])
            return [ContentItem(text=f"ERROR: Image generation failed: {e}")]
        except Exception as e:
            logger.exception('Unexpected error during sdcpp image generation')
            if _state['saved'] and instance is not None:
                self._restore_vram_state(instance, _state['held'])
            return [ContentItem(text=f"ERROR: Unexpected sdcpp image generation error: {e}")]
        finally:
            # _sdcpp_generate unlinks the temp file on every path it reaches; this
            # covers the one path where it is never called (the VRAM prologue raised
            # first) so the mkstemp file never leaks.
            try:
                os.remove(out_path)
            except OSError:
                pass

        # Save the result through the media pipeline.
        try:
            media_path = save_image_to_media(image_source=image_bytes, source_name='sdcpp_gen')
        except Exception as e:
            logger.exception('Failed to save generated sdcpp image')
            if _state['saved'] and instance is not None:
                self._restore_vram_state(instance, _state['held'])
            return [ContentItem(text=f"ERROR: Failed to save generated image: {e}")]

        width = params.get('width') or 0
        height = params.get('height') or 0
        preset_name = params.get('model') or sdcpp_cfg.get('default_model', '')

        # Primary restore now that all LLM-side work is done. There is no eager
        # captioning ahead of it (the image is returned uncaptioned), so this is
        # the actual state-restore. One retry with a 2s delay; non-fatal on failure.
        if _state['saved'] and instance is not None:
            self._restore_vram_state(instance, _state['held'])

        feedback = f"Generated image: {media_path} ({width}x{height}, model={preset_name}, backend=sdcpp)"
        # The image item is intentionally left uncaptioned so the router's return-path
        # guard (_has_uncaptioned_images) auto-generates a genuine vision caption on
        # demand (same as the ComfyUI/SVG paths). The separate text item carries the
        # descriptive line for text-only agents.
        return [ContentItem(image=media_path), ContentItem(text=feedback)]
