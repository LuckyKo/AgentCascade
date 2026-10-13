"""
ComfyUI backend for ``media_gen`` — IMAGE / VIDEO / MUSIC via a ComfyUI server.

The IMAGE path reuses :mod:`agent_cascade.tools.image_gen`'s pure helpers
directly (``_load_workflow`` / ``_inject_params`` / ``_inject_sampler_params`` /
``_comfyui_generate`` / ``_upload_image`` / ``_list_workflows``). Importing them
rather than reimplementing is what makes the ComfyUI IMAGE output byte-identical
to ``image_gen``'s ComfyUI path (the plan's safety case; verified by a deep-equal
test in §8.5). The IMAGE branch below is byte-for-byte the increment-1 code.

VIDEO (WAN i2v) reuses the SAME injection path as image — the workflow carries
CLIPTextEncode nodes and direct-int width/height on WanImageToVideo, so
``_inject_params`` works unchanged. The difference is OUTPUT EXTRACTION: a
SaveVideo node writes to disk, not to ``outputs[*]['images']``, so we read the
newest matching file from ``comfyui.output_dir`` (a CONFIG field — never
hardcoded) under the save node's ``filename_prefix`` subfolder.

MUSIC (YuE2) is the three ordered changes from plan §5.5, implemented HERE
(image_gen is frozen): (1) prompt placement is SKIPPED (the workflow has no
CLIP/PrimitiveString nodes, so ``_inject_params`` would hard-raise); (2) a
multi-node writer writes ``style`` and ``lyrics`` to EVERY YuE2 text node
(nodes 22 YuE2GenerateMusic + 23 YuE2GenerateABC); (3) seed injection STILL
runs (KSampler node 8 + the per-node seeds). Style/lyrics are surfaced as
separate override keys (``overrides.style`` / ``overrides.lyrics``), not a
single prompt; ``prompt`` doubles as the style when no explicit style is given.

The ``reference_file`` security seam is resolved via ``resolve_tool_path`` BEFORE
any upload/subprocess (audit defect 5): an out-of-roots path raises and the
request never reaches VRAM. Applies to image-edit and i2v video first-frame.
"""

import os
import time
from pathlib import Path

from .base import MediaBackend, MediaRequest, MediaResult, resolve_media_timeout

from agent_cascade.tools.image_gen import (
    _load_workflow,
    _inject_params,
    _inject_sampler_params,
    _comfyui_generate,
    _upload_image,
    _list_workflows,
)
from agent_cascade.utils.tool_path_resolver import resolve_tool_path

import logging

logger = logging.getLogger(__name__)

# Save-node class types that write a file to disk (video / audio) rather than
# returning bytes in the /history outputs.
_DISK_SAVE_NODES = ('SaveVideo', 'SaveAudioAdvanced', 'SaveAudio')
# YuE2 text-bearing nodes (music): style + lyrics live on these.
_MUSIC_TEXT_NODES = ('YuE2GenerateMusic', 'YuE2GenerateABC')


def _infer_media_type(workflow: dict) -> str:
    """Derive the media type from the loaded workflow (no caller media_type param).

    Order matters: a YuE2 music graph also has a SaveAudio node, so music is
    detected FIRST by its text nodes; then video by its ``SaveVideo`` node;
    otherwise it is an image graph. Verified against the real workflows in
    ``config/workflows`` (only ``yue2_full`` has YuE2 nodes; only
    ``video_wan2_2_14B_i2v`` has ``SaveVideo``; no image workflow has either).
    """
    nodes = [n for n in workflow.values() if isinstance(n, dict)]
    if any(n.get('class_type') in _MUSIC_TEXT_NODES for n in nodes):
        return 'audio'
    if any(n.get('class_type') == 'SaveVideo' for n in nodes):
        return 'video'
    return 'image'


def _find_save_node_prefix(workflow: dict) -> str | None:
    """Return the ``filename_prefix`` of the first disk-save node, or None."""
    for node in workflow.values():
        if isinstance(node, dict) and node.get('class_type') in _DISK_SAVE_NODES:
            prefix = (node.get('inputs') or {}).get('filename_prefix')
            if isinstance(prefix, str) and prefix:
                return prefix
    return None


def _split_prefix(prefix: str):
    """Split a ComfyUI ``filename_prefix`` into (subfolder, base_name).

    ``"video/Wan2.2_i2v"`` → ``("video", "Wan2.2_i2v")``; a prefix with no
    separator → ``("", prefix)``.
    """
    # Normalise separators; ComfyUI uses '/' for the output subfolder.
    norm = prefix.replace('\\', '/')
    if '/' in norm:
        subfolder, base = norm.rsplit('/', 1)
        return subfolder, base
    return '', norm


def _find_newest_output(output_dir: str, filename_prefix: str,
                       submit_time: float, preexisting: set) -> tuple | None:
    """Locate the file a disk-save node just wrote, or None.

    Prefers a NEW file (name not in ``preexisting``); falls back to the newest
    matching file with ``mtime >= submit_time``. Returns ``(bytes, ext)`` with
    the extension detected at runtime (never assumed).

    LIMITATION: this assumes one output file per generation (media_gen is
    single-shot — one call → one media file). If a workflow ever emits N files
    in one run, or two generations overlap on the same prefix, the newest-file
    heuristic can mis-select. v1 does not support batch/concurrent generation;
    if that changes, replace this with an atomic unique-name (or monotonic
    counter) mechanism rather than mtime ordering.
    """
    subfolder, base = _split_prefix(filename_prefix)
    search_dir = Path(output_dir) / subfolder
    if not search_dir.is_dir():
        return None

    def _matching():
        out = []
        for f in search_dir.iterdir():
            if f.is_file() and f.name.startswith(base):
                try:
                    out.append((f.stat().st_mtime, f))
                except OSError:
                    continue
        return out

    matches = _matching()
    if not matches:
        return None
    # Prefer a file that did not exist before this run.
    new = [(mt, f) for mt, f in matches if f.name not in preexisting]
    pool = new or [(mt, f) for mt, f in matches if mt >= submit_time] or matches
    pool.sort(key=lambda x: x[0])
    newest = pool[-1][1]
    try:
        data = newest.read_bytes()
    except OSError:
        return None
    ext = newest.suffix.lstrip('.')
    return data, ext


def _inject_music_params(workflow: dict, style: str, lyrics: str,
                        seed) -> tuple:
    """Music injection (plan §5.5): skip prompt placement, write style/lyrics to
    every YuE2 text node, still inject seed. Returns (workflow, report)."""
    report = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get('class_type') not in _MUSIC_TEXT_NODES:
            continue
        inputs = node.get('inputs') or {}
        if style is not None and 'style' in inputs:
            inputs['style'] = style
            report.append(f"{node_id}.style")
        if lyrics is not None and 'lyrics' in inputs:
            inputs['lyrics'] = lyrics
            report.append(f"{node_id}.lyrics")
    # Seed injection STILL runs (downstream of the skipped prompt step).
    if seed is not None:
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get('inputs') or {}
            for key in ('seed', 'noise_seed'):
                if key in inputs and isinstance(inputs[key], int):
                    inputs[key] = seed
                    report.append(f"{node_id}.{key}")
    return workflow, report


def _comfyui_generate_to_file(url: str, workflow: dict, output_dir: str,
                              timeout: int, client=None) -> tuple:
    """Submit + poll, then read the file a disk-save node wrote.

    Returns ``(bytes, ext, meta)``. Raises RuntimeError on submit failure /
    execution error / no output file; TimeoutError on timeout.
    """
    prefix = _find_save_node_prefix(workflow)
    if not prefix:
        raise RuntimeError(
            'No disk-save node (SaveVideo/SaveAudio) found in the workflow; '
            'cannot extract a server-written file.'
        )
    subfolder, base = _split_prefix(prefix)
    search_dir = Path(output_dir) / subfolder
    preexisting = set()
    if search_dir.is_dir():
        try:
            preexisting = {f.name for f in search_dir.iterdir()
                           if f.is_file() and f.name.startswith(base)}
        except OSError:
            preexisting = set()

    own_client = client is None
    if own_client:
        import httpx
        client = httpx.Client()
    try:
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

        submit_time = time.time()
        deadline = submit_time + timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                hist_resp = client.get(f"{url}/history/{prompt_id}", timeout=10)
            except httpx.RequestError:
                continue
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
                raise RuntimeError(f"ComfyUI generation error: {__import__('json').dumps(status)[:300]}")
            if status.get('completed'):
                found = _find_newest_output(output_dir, prefix, submit_time, preexisting)
                if not found:
                    raise RuntimeError(
                        'ComfyUI completed but no output file was found under '
                        f'{output_dir} for prefix {prefix!r}.'
                    )
                data, ext = found
                return data, ext, {}
        raise TimeoutError(f"ComfyUI generation timed out after {timeout}s")
    finally:
        if own_client:
            client.close()


class ComfyUIBackend(MediaBackend):
    id = 'comfyui'
    media_types = ('image', 'video', 'audio')  # music + tts are 'audio'
    server_backed = True
    requires_vram = True

    def describe(self) -> str:
        return 'ComfyUI server workflow'

    def is_available(self, req: MediaRequest) -> bool:
        block = (req.config or {}).get('comfyui')
        if not isinstance(block, dict):
            return False
        return bool(req.server_url or block.get('url'))

    def preflight(self, req: MediaRequest) -> None:
        block = (req.config or {}).get('comfyui') or {}
        url = req.server_url or block.get('url')
        if not url:
            raise RuntimeError(
                'No ComfyUI server configured. Set the image generation server URL '
                'in UI settings (config/media_gen/media_gen.json).'
            )
        # Timeout: unified resolver (comfyui.timeout > top-level timeout > 900).
        timeout = resolve_media_timeout(req.config, block)

        # Resolve the workflow path: param > config default > error listing available.
        workflow_path = req.config_name or block.get('default_workflow')
        if not workflow_path:
            available = _list_workflows(block.get('workflow_dir', ''))
            names = ', '.join(w['name'] for w in available) if available else 'none'
            raise RuntimeError(
                'No workflow specified and no default workflow configured. '
                f"Available workflows: {names}. Pass a full path via the 'config' "
                'parameter or set a default in UI settings.'
            )

        # A bare config stem (as the UI's grouped pulldown supplies) resolves to
        # workflow_dir/<stem>.json. A full path is used as-is (unchanged image path).
        if not Path(workflow_path).is_file():
            wd = block.get('workflow_dir')
            if wd:
                stem = workflow_path[:-5] if workflow_path.endswith('.json') else workflow_path
                candidate = Path(wd) / (stem + '.json')
                if candidate.is_file():
                    workflow_path = str(candidate)

        # Load + inject BEFORE touching VRAM, so config/format errors don't leave
        # the model unloaded with state saved.
        try:
            workflow = _load_workflow(workflow_path)
        except FileNotFoundError as e:
            available = _list_workflows(block.get('workflow_dir', ''))
            names = ', '.join(w['name'] for w in available) if available else 'none'
            raise RuntimeError(f"{e}. Available workflows: {names}") from e
        except (ValueError, OSError) as e:
            raise RuntimeError(f"Invalid workflow JSON '{workflow_path}': {e}") from e

        size = req.media_size or {}
        overrides = req.overrides or {}
        # media_type is DERIVED from the workflow (no caller param): music → audio,
        # SaveVideo → video, else image. Set on req so the caller's save/feedback path
        # sees the real type.
        media_type = _infer_media_type(workflow)
        req.media_type = media_type

        if media_type == 'audio':
            # ── MUSIC (YuE2): three ordered changes, NOT _inject_params. ──
            style = overrides.get('style') or req.prompt
            lyrics = overrides.get('lyrics')
            if lyrics is None and any(
                isinstance(n, dict) and n.get('class_type') in _MUSIC_TEXT_NODES
                for n in workflow.values()
            ):
                # YuE2 can generate from style alone, so this is a warning, not an
                # error — but make the silent-drop visible to the caller.
                logger.warning(
                    'music prompt was placed in style only; no lyrics were provided '
                    '(pass overrides.lyrics)'
                )
            workflow, report = _inject_music_params(
                workflow, style=style, lyrics=lyrics, seed=overrides.get('seed'),
            )
        else:
            # ── IMAGE / VIDEO: identical injection (video has CLIP + direct w/h). ──
            try:
                workflow, report = _inject_params(
                    workflow,
                    prompt=req.prompt,
                    negative_prompt=req.negative_prompt or '',
                    width=size.get('width'),
                    height=size.get('height'),
                    seed=overrides.get('seed'),
                )
                # Optional non-destructive cfg/steps override on KSampler-family nodes.
                _, sampler_report = _inject_sampler_params(
                    workflow,
                    guidance=overrides.get('guidance'),
                    steps=overrides.get('steps'),
                )
                report.extend(sampler_report)
            except ValueError as e:
                raise RuntimeError(str(e)) from e

        # ── Optional reference_file: resolve (SECURITY) + upload + wire LoadImage ──
        # Applies to image-edit and i2v video first-frame. Music has no LoadImage.
        reference_file = req.reference_file
        if reference_file and media_type != 'audio':
            if not isinstance(reference_file, str) or not reference_file.strip():
                raise RuntimeError("'reference_file' must be a non-empty string path.")
            try:
                local_path = resolve_tool_path(reference_file.strip(), mode='ro',
                                              agent_pool=req.agent_pool)
            except ValueError as e:
                raise RuntimeError(f"Invalid reference_file path: {e}") from e
            if not local_path.is_file():
                raise RuntimeError(f"reference_file file not found: {reference_file}")
            load_nodes = [
                node_id
                for node_id, node in workflow.items()
                if isinstance(node, dict)
                and node.get('class_type') == 'LoadImage'
                and isinstance((node.get('inputs') or {}).get('image'), str)
            ]
            if not load_nodes:
                raise RuntimeError(
                    'reference_file was provided, but the selected workflow has no '
                    'standard LoadImage node.'
                )
            if len(load_nodes) > 1:
                raise RuntimeError(
                    'reference_file supports exactly one LoadImage node, but the workflow '
                    f'has {len(load_nodes)}.'
                )
            try:
                image_ref = _upload_image(url, local_path)
            except RuntimeError as e:
                raise RuntimeError(str(e)) from e
            workflow[load_nodes[0]]['inputs']['image'] = image_ref
            report.append(f"reference_file → {load_nodes[0]} (LoadImage)")

        # ── Output-extraction contract for disk-save media (video / music). ──
        # output_dir is a CONFIG field (never hardcoded); ComfyUI creates it on
        # first run. We record the save-node prefix so run() can find the file.
        prepared = {
            'url': url,
            'workflow': workflow,
            'timeout': timeout,
            'workflow_path': workflow_path,
            'config_name': req.config_name or block.get('default_workflow', ''),
        }
        if media_type in ('video', 'audio'):
            output_dir = block.get('output_dir')
            if not output_dir:
                raise RuntimeError(
                    'No comfyui.output_dir configured; required to retrieve '
                    'video/audio output written to disk by the save node.'
                )
            prefix = _find_save_node_prefix(workflow)
            if not prefix:
                raise RuntimeError(
                    'Selected workflow has no disk-save node (SaveVideo/SaveAudio); '
                    'cannot retrieve its output.'
                )
            prepared['fetch'] = 'server_file'
            prepared['output_dir'] = output_dir
            prepared['filename_prefix'] = prefix

        req.prepared = prepared

    def run(self, req: MediaRequest) -> MediaResult:
        if req.prepared.get('fetch') == 'server_file':
            # VIDEO / MUSIC: read the file the save node wrote to disk.
            data, ext, meta = _comfyui_generate_to_file(
                req.prepared['url'], req.prepared['workflow'],
                req.prepared['output_dir'], req.prepared['timeout'],
            )
            return MediaResult(data=data, ext=ext, meta=meta or {})

        # IMAGE: byte-identical to image_gen's ComfyUI output extraction.
        image_bytes, meta = _comfyui_generate(
            req.prepared['url'], req.prepared['workflow'], timeout=req.prepared['timeout']
        )
        return MediaResult(data=image_bytes, ext='png', meta=meta or {})
