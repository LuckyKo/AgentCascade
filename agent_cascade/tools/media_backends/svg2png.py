"""
SVG-to-PNG backend for ``media_gen``.

Renders SVG code (passed in ``prompt``) to a PNG via the ``cairosvg`` library.
This is the simplest possible backend: fully local — no VRAM, no external server,
no subprocess, no config/workflow directories. The whole render is one in-process
library call.

``cairosvg`` is imported lazily inside :meth:`run` so the backend registers and
its manifest builds even when the library is not installed; a missing library
surfaces as a plain ``ImportError`` at call time (the tool's caller handles it),
and a malformed SVG surfaces as cairosvg's own parse error — both propagate
unchanged, matching the "let backend errors reach the caller" convention.

The ``timeout`` advanced field is NOT applied inside ``run`` (an in-process call
has no subprocess to bound); it is honoured by ``media_gen.dms_bound`` exactly as
for every other backend, bounding the tool call's wall-clock. The VRAM save/unload
dance is opted out in the config block (``release_model_around_call=false``) because
there is no model to free — the same "opt out via the block, keep the field" pattern
kobold_tts uses. The field itself stays declared (a system-wide convention every
backend honours), so a caller who enables it gets the normal save/unload/restore.
"""

from .base import MediaBackend, MediaRequest, MediaResult
from .manifest import BackendManifest, FieldSpec


class Svg2PngBackend(MediaBackend):
    id = 'svg2png'
    media_types = ('image',)

    manifest = BackendManifest(
        label='SVG to PNG',
        note='cairosvg local render — SVG code in prompt → PNG.',
        # No external resources: nothing to configure in the connection tab.
        connection_fields=(),
        advanced_fields=(
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=5, max=120, step=5),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='none',
        agent_args=('prompt', 'media_size'),
        overrides_keys=(),
        defaults={'release_model_around_call': True},
        usage_hints=(
            ('image', 'prompt="<svg>...</svg>", '
                      'media_size={"width": 512, "height": 512}'),
        ),
    )

    def describe(self) -> str:
        return 'SVG to PNG (cairosvg)'

    def preflight(self, req: MediaRequest) -> None:
        # The SVG source is the prompt itself — reject an empty one before any work.
        if not req.prompt or not req.prompt.strip():
            raise RuntimeError('svg2png requires a non-empty prompt (SVG code).')
        # reference_file is an input-image seam (ComfyUI LoadImage / TTS voice / ASR
        # audio); there is nothing to do with it here.
        if req.reference_file:
            raise RuntimeError(
                'reference_file is not supported by the svg2png backend.'
            )
        # No config resolution — cairosvg is a library, not a server or CLI.
        req.prepared = {'svg_text': req.prompt}

    def run(self, req: MediaRequest) -> MediaResult:
        import cairosvg  # lazy: the backend registers without the library installed

        kwargs = {'bytestring': req.prepared['svg_text'].encode('utf-8')}
        size = req.media_size or {}
        if size.get('width'):
            kwargs['output_width'] = int(size['width'])
        if size.get('height'):
            kwargs['output_height'] = int(size['height'])

        png_bytes = cairosvg.svg2png(**kwargs)
        return MediaResult(data=png_bytes, ext='png', meta={})
