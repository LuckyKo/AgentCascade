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

Both return ``[ContentItem(image=path), ContentItem(text=caption)]`` — the same
shape as ``view_image`` — so the router's existing ``_caption_images`` flow adds
an LLM caption on the next send.

VRAM management (text path only): before talking to ComfyUI we save the owning
instance's KV state and unload all models from llama-autoloader to free VRAM,
then restore the state in a ``finally`` block (one retry). The restore is
attempted whenever the state was saved, regardless of whether unload or ComfyUI
succeeded — the saved label must always be cleared.

This tool holds NO LLM of its own and never constructs a chat model. The old
placeholder's Change-E breaker gate and sticky-slot side-call gate are gone with
the placeholder: there is no LLM/endpoint here to gate.
"""

import json
import logging
import random
import re
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
    return Path(__file__).resolve().parent.parent.parent / "config" / "image_gen.json"


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
            logger.debug("image_gen config read failed (%s): %s", config_path, e)
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
            "cairosvg is required to render SVG. Install it with: pip install cairosvg"
        ) from e
    except OSError as e:
        raise OSError(
            f"cairosvg native library error: {e}. On Windows you may need the GTK3 "
            "runtime (https://github.com/tschoonj/GTK3-Runtime-for-Windows/releases) "
            "or set GTK_LIBS."
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
    workflows = [{"name": f.stem, "path": str(f)} for f in d.glob("*.json")]
    workflows.sort(key=lambda w: (w["name"], w["path"]))
    return workflows


def _inject_params(workflow: dict, prompt: str, negative_prompt: str = "",
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
        inputs = node.get("inputs", {}) or {}
        ct = node.get("class_type")
        if ct == "CLIPTextEncode" and "text" in inputs:
            clip_nodes.append((node_id, node))
        elif ct == "PrimitiveStringMultiline" and "value" in inputs:
            prim_nodes.append((node_id, node))

    # 2. Inject the positive prompt.
    # Priority: PrimitiveStringMultiline (flux2 pattern), else first CLIPTextEncode
    # that already carries non-empty text (the positive slot), else the first one.
    positive_node_id = None
    if prim_nodes:
        nid, node = prim_nodes[0]
        node["inputs"]["value"] = prompt
        positive_node_id = nid
        report.append(f"prompt → {nid} (PrimitiveStringMultiline)")
    elif clip_nodes:
        target = None
        for nid, node in clip_nodes:
            if node["inputs"]["text"]:  # non-empty ⇒ likely the positive slot
                target = (nid, node)
                break
        if target is None:
            target = clip_nodes[0]
        positive_node_id = target[0]
        target[1]["inputs"]["text"] = prompt
        report.append(f"prompt → {positive_node_id} (CLIPTextEncode)")

    if positive_node_id is None:
        raise ValueError(
            "Could not inject prompt into workflow. No CLIPTextEncode or "
            "PrimitiveStringMultiline nodes found — check the workflow format."
        )

    # 3. Inject the negative prompt (a CLIPTextEncode that is NOT the positive one).
    if negative_prompt:
        placed = False
        for nid, node in clip_nodes:
            if nid != positive_node_id:
                node["inputs"]["text"] = negative_prompt
                report.append(f"negative → {nid} (CLIPTextEncode)")
                placed = True
                break
        if not placed:
            logger.debug(
                "image_gen: negative prompt ignored — no secondary CLIPTextEncode node found"
            )

    # 4. Override width/height (direct ints and/or node references).
    if width or height:
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {}) or {}
            ct = node.get("class_type", "")

            # Direct integer dims (zimg_turbo pattern).
            if "width" in inputs and isinstance(inputs["width"], int) and width:
                inputs["width"] = width
                report.append(f"width={width} → {node_id}")
            if "height" in inputs and isinstance(inputs["height"], int) and height:
                inputs["height"] = height
                report.append(f"height={height} → {node_id}")

            # Node references like ["75:68", 0] (flux2 pattern): follow to the
            # PrimitiveInt node and set its scalar value.
            for dim_key in ("width", "height"):
                val = inputs.get(dim_key)
                if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                    ref_node_id = val[0]
                    ref_node = workflow.get(ref_node_id)
                    new_val = width if dim_key == "width" else height
                    if (ref_node is not None
                            and ref_node.get("class_type") == "PrimitiveInt"
                            and isinstance(new_val, int)):
                        ref_node["inputs"]["value"] = new_val
                        report.append(f"{dim_key}={new_val} → {ref_node_id} (PrimitiveInt)")

            # CR Aspect Ratio node: force custom mode so our dims take effect.
            if ct == "CR Aspect Ratio":
                if "aspect_ratio" in inputs:
                    inputs["aspect_ratio"] = "custom"
                if "swap_dimensions" in inputs:
                    inputs["swap_dimensions"] = "Off"
                report.append(f"CR Aspect Ratio → {node_id} (forced custom)")

    # 5. Set the seed on every node that carries a seed input.
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {}) or {}
        if "seed" in inputs:
            inputs["seed"] = seed
            report.append(f"seed={seed} → {node_id}")
        if "noise_seed" in inputs:
            inputs["noise_seed"] = seed
            report.append(f"noise_seed={seed} → {node_id}")

    return workflow, report


# ── ComfyUI client (submit / poll / download) ──────────────────────────────────

def _extract_seed(workflow: dict) -> Optional[int]:
    """Best-effort read of the seed that was injected (for metadata only)."""
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs", {}) or {}
        if "seed" in inputs and isinstance(inputs["seed"], int):
            return inputs["seed"]
        if "noise_seed" in inputs and isinstance(inputs["noise_seed"], int):
            return inputs["noise_seed"]
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
            resp = client.post(f"{url}/prompt", json={"prompt": workflow}, timeout=30)
        except httpx.ConnectError as e:
            raise RuntimeError(f"ComfyUI server not reachable at {url}. Is it running?") from e
        except httpx.TimeoutException as e:
            raise RuntimeError(f"ComfyUI request timed out at {url}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"ComfyUI prompt submission failed: {resp.status_code} {resp.text[:200]}"
            )

        try:
            prompt_id = resp.json()["prompt_id"]
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

            status = entry.get("status", {}) or {}

            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI generation error: {json.dumps(status)[:300]}")

            if status.get("completed"):
                # 3. Extract the first image from the node outputs and download it.
                outputs = entry.get("outputs", {}) or {}
                for _node_id, node_out in outputs.items():
                    images = (node_out or {}).get("images", []) or []
                    if images:
                        img_info = images[0]
                        filename = img_info["filename"]
                        subfolder = img_info.get("subfolder", "")
                        folder_type = img_info.get("type", "output")
                        view_url = (
                            f"{url}/view?filename={filename}"
                            f"&subfolder={subfolder}&type={folder_type}"
                        )
                        img_resp = client.get(view_url, timeout=30)
                        if img_resp.status_code == 200 and img_resp.content:
                            return img_resp.content, {"seed": _extract_seed(workflow)}
                raise RuntimeError("ComfyUI completed but no image found in outputs")

        raise TimeoutError(f"ComfyUI generation timed out after {timeout}s")
    finally:
        if own_client:
            client.close()


# ── The tool ───────────────────────────────────────────────────────────────────

@register_tool('image_gen', allow_overwrite=True)
class ImageGen(BaseTool):
    """Generate an image via ComfyUI (text prompt) or render SVG code to an image.

    Returns ``[ContentItem(image=path), ContentItem(text=caption)]`` — same shape
    as view_image, so the router's _caption_images flow adds an LLM caption later.
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
                    "Text prompt for image generation, or SVG code to render to an image."
                ),
            },
            'negative_prompt': {
                'type': 'string',
                'description': "Negative prompt to exclude elements (API generation only).",
            },
            'workflow': {
                'type': 'string',
                'description': (
                    "Full path to a ComfyUI workflow JSON file. If omitted, uses the "
                    "default workflow selected in UI settings."
                ),
            },
            'width': {'type': 'integer', 'description': "Output width in pixels (overrides workflow default)."},
            'height': {'type': 'integer', 'description': "Output height in pixels (overrides workflow default)."},
            'seed': {'type': 'integer', 'description': "Random seed for reproducibility (random if omitted)."},
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
        if _is_svg_code(prompt):
            return self._handle_svg(prompt, params)

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
            logger.exception("SVG render failed")
            return [ContentItem(text=f"ERROR: SVG parse/render error: {e}")]

        try:
            media_path = save_image_to_media(image_source=png_bytes, source_name="svg_render")
        except Exception as e:
            logger.exception("Failed to save rendered SVG image")
            return [ContentItem(text=f"ERROR: Failed to save rendered image: {e}")]

        w, h = _svg_dimensions(svg_text)
        caption = f"SVG rendered to image ({w}x{h})"
        return [ContentItem(image=media_path), ContentItem(text=caption)]

    # ------------------------------------------------------------------ #
    #  Text prompt path (ComfyUI)                                        #
    # ------------------------------------------------------------------ #

    def _handle_text_prompt(self, params: dict, kwargs: dict) -> List[ContentItem]:
        config = _get_image_gen_config()

        url = config.get('url')
        if not url:
            return [ContentItem(text=(
                "ERROR: No ComfyUI server configured. Set the image generation "
                "server URL in UI settings (config/image_gen.json)."
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
                "ERROR: No workflow specified and no default workflow configured. "
                f"Available workflows: {names}. Pass a full path via the 'workflow' "
                "parameter or set a default in UI settings."
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
                negative_prompt=params.get('negative_prompt') or "",
                width=params.get('width'),
                height=params.get('height'),
                seed=params.get('seed'),
            )
        except ValueError as e:
            return [ContentItem(text=f"ERROR: {e}")]
        logger.info("image_gen injection for %s: %s", Path(workflow_path).name, '; '.join(report))

        # ── VRAM management: save → unload → (ComfyUI) → restore in finally ──
        # The whole sequence sits under one try/finally so the restore invariant
        # holds no matter where an exception occurs (including inside the save/unload
        # setup itself). _state_saved stays False until save_instance_state returns
        # True, and the finally only restores when it is — so a failure before the
        # state was saved never triggers a spurious restore.
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
                                "[ImageGen] VRAM may be constrained; model was not unloaded before ComfyUI"
                            )

            image_bytes, _meta = _comfyui_generate(url, workflow, timeout=timeout)
        except (RuntimeError, TimeoutError) as e:
            return [ContentItem(text=f"ERROR: Image generation failed: {e}")]
        except Exception as e:
            logger.exception("Unexpected error during image generation")
            return [ContentItem(text=f"ERROR: Unexpected image generation error: {e}")]
        finally:
            # Invariant: if state was saved, ALWAYS attempt restore — even if unload
            # failed or ComfyUI raised. One retry with a 2s delay for transient issues.
            if _state_saved and instance is not None:
                self._restore_vram_state(instance, held)

        # Save the result through the media pipeline.
        try:
            media_path = save_image_to_media(image_source=image_bytes, source_name="comfyui_gen")
        except Exception as e:
            logger.exception("Failed to save generated image")
            return [ContentItem(text=f"ERROR: Failed to save generated image: {e}")]

        width = params.get('width') or 0
        height = params.get('height') or 0
        wf_name = Path(workflow_path).name
        caption = f"Generated image: {params['prompt'][:80]} ({width}x{height}, workflow={wf_name})"
        return [ContentItem(image=media_path), ContentItem(text=caption)]

    @staticmethod
    def _restore_vram_state(instance, held: dict) -> None:
        """Restore saved KV state after ComfyUI runs (one retry on failure).

        Runs in a finally context. A final restore failure is logged at ERROR but
        is non-fatal: the next LLM call triggers a fresh model load via autoloader
        JIT, so the system self-heals (the user only loses KV cache continuity).
        """
        from agent_cascade.state_ops import restore_instance_state
        for attempt in range(2):
            try:
                restore_instance_state(instance, held_endpoint_cfg=held)
                return
            except Exception as e:
                if attempt == 0:
                    logger.warning("[ImageGen] Restore attempt 1 failed, retrying: %s", e)
                    time.sleep(2)
                else:
                    logger.error(
                        "[ImageGen] State restore FAILED after ComfyUI — model may not be loaded: %s", e
                    )
