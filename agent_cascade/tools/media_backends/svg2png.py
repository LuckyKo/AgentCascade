"""
SVG-to-PNG backend for ``media_gen``.

Renders SVG to a PNG via the ``cairosvg`` library. The SVG source is either raw
SVG code in ``prompt`` or a path to an existing ``.svg`` file in ``reference_file``
(read at preflight time); ``reference_file`` takes priority when both are given.
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

from pathlib import Path

from agent_cascade.utils.tool_path_resolver import resolve_tool_path

from .base import MediaBackend, MediaRequest, MediaResult
from .manifest import BackendManifest, FieldSpec


class Svg2PngBackend(MediaBackend):
    id = 'svg2png'
    media_types = ('image',)

    manifest = BackendManifest(
        label='SVG to PNG',
        note='cairosvg local render — SVG code in prompt or file path via reference_file → PNG.',
        # No external resources: nothing to configure in the connection tab.
        connection_fields=(),
        advanced_fields=(
            FieldSpec(key='timeout', label='Timeout (s)', kind='number',
                      min=5, max=120, step=5),
            FieldSpec(key='release_model_around_call',
                      label='Free VRAM around the call', kind='checkbox'),
        ),
        availability='none',
        # reference_file may supply the SVG source, so a prompt is not strictly required;
        # preflight() owns the "neither supplied" rejection.
        prompt_optional_with_reference_file=True,
        agent_args=('prompt', 'reference_file', 'media_size'),
        overrides_keys=(),
        defaults={'release_model_around_call': True},
        usage_hints=(
            ('image', 'prompt="<svg>...</svg>" OR reference_file="<path/to/file.svg>", '
                      'media_size={"width": 512, "height": 512}'),
        ),
    )

    def describe(self) -> str:
        return 'SVG to PNG (cairosvg)'

    def preflight(self, req: MediaRequest) -> None:
        # Resolve the SVG source: a file path via reference_file (priority) or raw
        # SVG code via prompt. At least one is required.
        if req.reference_file:
            ref_str = req.reference_file.strip()
            if not ref_str:
                raise RuntimeError(
                    'svg2png requires either reference_file (path to .svg) '
                    'or prompt (SVG code).'
                )
            # Extension check on the user-supplied path (clearer error before resolution).
            # Not the security seam — resolve_tool_path enforces allowed-dirs on the *resolved*
            # path regardless. Known exotic gap: a symlink without a .svg name is rejected, and a
            # .svg-named symlink to non-SVG content passes here then fails at read/parse.
            if Path(ref_str).suffix.lower() != '.svg':
                raise RuntimeError(
                    f'svg2png reference_file must be a .svg file, got: {ref_str!r}'
                )
            # SECURITY seam: resolve against allowed paths (path-traversal guard).
            try:
                resolved = resolve_tool_path(
                    ref_str, mode='ro', agent_pool=req.agent_pool
                )
            except ValueError as e:
                raise RuntimeError(f'Invalid reference_file path: {e}') from e
            # Read the file; a missing/unreadable file raises naturally (caller handles it).
            svg_text = Path(resolved).read_text(encoding='utf-8')
        elif req.prompt and req.prompt.strip():
            svg_text = req.prompt
        else:
            raise RuntimeError(
                'svg2png requires either reference_file (path to .svg) '
                'or prompt (SVG code).'
            )
        # No config resolution — cairosvg is a library, not a server or CLI.
        req.prepared = {'svg_text': svg_text}

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
