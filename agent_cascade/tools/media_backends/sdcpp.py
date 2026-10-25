"""
stable-diffusion.cpp backend for ``media_gen``.

A thin adapter over the pure helpers in :mod:`agent_cascade.tools.image_gen`
(``_resolve_sdcpp_config`` / ``_sdcpp_argv`` / ``_sdcpp_generate``). Importing
those — rather than duplicating them — is what keeps the sdcpp output byte-identical
to ``image_gen``'s sdcpp path (the plan's whole safety case). The full ``media_gen``
config dict is passed straight through: ``_resolve_sdcpp_config``/``_sdcpp_argv``
read ``cfg.get('sdcpp')``, and ``media_gen.json`` carries an ``sdcpp`` block with
the same keys (``binary``/``models_dir``/``config_dir``/``default_sdcpp_config``).
"""

import os
import tempfile

from .base import MediaBackend, MediaRequest, MediaResult, resolve_media_timeout
from .manifest import BackendManifest, FieldSpec

from agent_cascade.tools.image_gen import (
    _resolve_sdcpp_config,
    _sdcpp_argv,
    _sdcpp_generate,
    _list_sdcpp_configs,
)


class SdcppBackend(MediaBackend):
    id = 'sdcpp'
    media_types = ('image',)

    manifest = BackendManifest(
        label='stable-diffusion.cpp',
        note='stable-diffusion.cpp local CLI — image only, slow.',
        connection_fields=(
            FieldSpec(key='binary', label='Binary', kind='path', required=True,
                      placeholder='C:\\...\\sd-cli.exe'),
            FieldSpec(key='models_dir', label='Models Directory', kind='path'),
            FieldSpec(key='config_dir', label='Config Directory', kind='path',
                      placeholder='<AgentCascade_root>/config/sdcpp'),
        ),
        advanced_fields=(
            FieldSpec(key='default_sdcpp_config', label='Default Config', kind='text',
                      placeholder='z_image_turbo'),
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=30, max=1800, step=30),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='config_dir',
        agent_args=('config', 'negative_prompt', 'media_size', 'overrides'),
        overrides_keys=('seed', 'steps', 'guidance'),
        usage_hints=(
            ('image', 'prompt="<image description>", '
                      'media_size={"width": 1024, "height": 1024}'),
        ),
        defaults={'release_model_around_call': True},
    )

    def describe(self) -> str:
        return 'stable-diffusion.cpp (local sd-cli)'

    def preflight(self, req: MediaRequest) -> None:
        # reference_file is a ComfyUI-only feature (LoadImage node); reject it for
        # the local sd-cli backend, mirroring image_gen's input_image rejection.
        if req.reference_file:
            raise RuntimeError(
                'reference_file is not supported by the stable-diffusion.cpp backend.'
            )

        # Resolve the model config + model files BEFORE any VRAM work, so a missing
        # file fails fast without a wasted save/unload/restore cycle.
        model_cfg = _resolve_sdcpp_config(req.config, req.config_name)

        # Timeout: unified resolver (sdcpp.timeout > top-level timeout > 900).
        sdcpp_block = req.config.get('sdcpp') or {}
        timeout = resolve_media_timeout(req.config, sdcpp_block)

        # Create the temp output file. The caller's finally unlinks it on the path
        # where run() is never reached; _sdcpp_generate unlinks it on every path it
        # reaches. Double-unlink is guarded by try/except OSError.
        fd, out_path = tempfile.mkstemp(suffix='.png', prefix='sdcpp_')
        os.close(fd)

        overrides = req.overrides or {}
        size = req.media_size or {}
        try:
            argv = _sdcpp_argv(
                req.config, model_cfg,
                prompt=req.prompt,
                negative_prompt=req.negative_prompt or '',
                width=size.get('width'),
                height=size.get('height'),
                seed=overrides.get('seed'),
                steps=overrides.get('steps'),
                guidance=overrides.get('guidance'),
                output_path=out_path,
            )
        except Exception:
            # Remove the temp output on ANY failure (not just ValueError) so a
            # preflight raise never leaks the file; re-raise the original error.
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise

        req.prepared = {
            'model_cfg': model_cfg,
            'argv': argv,
            'out_path': out_path,
            'timeout': timeout,
            'config_name': req.config_name or sdcpp_block.get('default_sdcpp_config', ''),
        }

    def run(self, req: MediaRequest) -> MediaResult:
        image_bytes = _sdcpp_generate(req.prepared['argv'], timeout=req.prepared['timeout'])
        return MediaResult(data=image_bytes, ext='png', meta={})
