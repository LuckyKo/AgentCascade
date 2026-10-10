"""
ComfyUI backend for ``media_gen`` (IMAGE ONLY for now).

The generation and output extraction reuse :mod:`agent_cascade.tools.image_gen`'s
pure helpers directly (``_load_workflow`` / ``_inject_params`` / ``_inject_sampler_params``
/ ``_comfyui_generate`` / ``_upload_image`` / ``_list_workflows``). Importing them —
rather than reimplementing — is what makes the ComfyUI IMAGE output byte-identical to
``image_gen``'s ComfyUI path (the plan's safety case; verified by a deep-equal test in
§8.5). ``_inject_params``/``_inject_sampler_params`` are written for image samplers and
are NOT generalised here — step 3 is image-only, so that is safe.

The ``reference_file`` security seam is resolved via ``resolve_tool_path`` BEFORE any
upload/subprocess (audit defect 5): an out-of-roots path raises and the request never
reaches VRAM.
"""

from pathlib import Path

from .base import MediaBackend, MediaRequest, MediaResult

from agent_cascade.tools.image_gen import (
    _load_workflow,
    _inject_params,
    _inject_sampler_params,
    _comfyui_generate,
    _upload_image,
    _list_workflows,
)
from agent_cascade.utils.tool_path_resolver import resolve_tool_path


class ComfyUIBackend(MediaBackend):
    id = 'comfyui'
    media_types = ('image',)
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
        try:
            timeout = int(block.get('timeout', (req.config or {}).get('timeout', 180)))
        except (TypeError, ValueError):
            timeout = 180

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
            # A true no-op (writes nothing) when both guidance and steps are None, so
            # the ComfyUI path stays byte-identical.
            _, sampler_report = _inject_sampler_params(
                workflow,
                guidance=overrides.get('guidance'),
                steps=overrides.get('steps'),
            )
            report.extend(sampler_report)
        except ValueError as e:
            raise RuntimeError(str(e)) from e

        # ── Optional reference_file: resolve (SECURITY) + upload + wire LoadImage ──
        # After parameter injection, before the VRAM block — upload failures return
        # early without saving/unloading model state. Absent param = byte-identical path.
        reference_file = req.reference_file
        if reference_file:
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

        req.prepared = {
            'url': url,
            'workflow': workflow,
            'timeout': timeout,
            'workflow_path': workflow_path,
        }

    def run(self, req: MediaRequest) -> MediaResult:
        # Byte-identical to image_gen's ComfyUI output extraction: same function.
        image_bytes, meta = _comfyui_generate(
            req.prepared['url'], req.prepared['workflow'], timeout=req.prepared['timeout']
        )
        return MediaResult(data=image_bytes, ext='png', meta=meta or {})
