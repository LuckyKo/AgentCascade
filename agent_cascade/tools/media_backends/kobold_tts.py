"""
Kobold TTS backend for ``media_gen`` — STUB (increment 1).

This backend is registered so the AUTO resolver's config-ownership path is
exercised and testable (the regression test ``AUTO + config='kokoro_default'``
must resolve to ``kobold_tts``). The actual TTS generation is implemented in a
later increment (step 6); until then :meth:`run` raises a clear "not yet
implemented" error. It declares its capabilities and config ownership now so the
extension point is clean and slots in without touching the resolver.

TTS produces *audio* (one of the three canonical media types), not a 'tts' type.
"""

from .base import MediaBackend, MediaRequest, MediaResult


class KoboldTTSBackend(MediaBackend):
    id = 'kobold_tts'
    media_types = ('audio',)
    server_backed = True
    requires_vram = True

    def describe(self) -> str:
        return 'Kobold TTS server'

    def is_available(self, req: MediaRequest) -> bool:
        block = (req.config or {}).get('kobold_tts')
        if not isinstance(block, dict):
            return False
        return bool(req.server_url or block.get('url'))

    def preflight(self, req: MediaRequest) -> None:
        # Capability/config checks only; generation is deferred to step 6.
        block = (req.config or {}).get('kobold_tts') or {}
        if not (req.server_url or block.get('url')):
            raise RuntimeError('No Kobold TTS server configured.')

    def run(self, req: MediaRequest) -> MediaResult:
        raise NotImplementedError(
            'Kobold TTS generation is not implemented yet (media_gen increment 1).'
        )
