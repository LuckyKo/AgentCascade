"""BUG_0116: compress_context must report the mode that actually ran.

Before the fix, ``handle_compress_tool`` hardcoded the label ``'manual'`` at
handler.py:1041 regardless of the ``mode`` argument it had already read at :972 and
passed to the compressor at :984. An ``auto`` call therefore printed
"manual compression complete." — the exact reported symptom.

The fix derives the label from the live ``mode``: ``comp_type = 'manual' if mode == 'manual' else 'auto'``.

Reuses the ``test_post_compression_usage`` harness (pool/engine/result stubs) so the
only thing under test is the echoed label, not the token math.
"""
import pytest
from unittest.mock import patch

from agent_cascade.compression.handler import CompressionHandler
from tests.compression.test_post_compression_usage import (
    NAME,
    _make_pool,
    _ok_result,
    _pin_patches,
    _stub_engine,
)


def _run(handler, args, inst):
    """Drive the live tool path with the compressor stubbed to succeed."""
    with _pin_patches()[0], _pin_patches()[1], _pin_patches()[2], \
         patch('agent_cascade.compression.core.compress_context', return_value=_ok_result(500)):
        return handler.handle_compress_tool(args, inst, NAME)


@pytest.mark.parametrize('mode, expected_label, forbidden_label', [
    ('auto', 'auto compression complete', 'manual compression complete'),
    ('manual', 'manual compression complete', 'auto compression complete'),
])
def test_compress_feedback_label_matches_mode(mode, expected_label, forbidden_label):
    pool = _make_pool()
    inst = pool.instances[NAME]
    handler = CompressionHandler(pool)
    handler.set_engine(_stub_engine())

    args = {'fraction': 0.5, 'mode': mode}
    # A manual run is only reachable when a summary is supplied (core.py:409 rejects
    # manual without one); supply it so the scenario is realistic even though the
    # compressor itself is stubbed.
    if mode == 'manual':
        args['summary_text'] = 'caller-provided summary'

    out = _run(handler, args, inst)

    assert expected_label in out, f"{mode}: expected label {expected_label!r} in output: {out!r}"
    assert forbidden_label not in out, f"{mode}: wrong label {forbidden_label!r} leaked into output: {out!r}"
    # The banner and token clause must be unaffected by the label fix.
    assert '[COMPRESSION]' in out, f"banner must remain: {out!r}"
