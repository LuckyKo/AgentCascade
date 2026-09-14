"""Unit tests for ``agent_cascade.llm.base._fire_usage_callback``.

Regression: the usage callback must ALWAYS receive ``cached_tokens`` (which lives only in
``prompt_tokens_details``) even when a backend ALSO emits ``completion_tokens_details``. The old
implementation passed only ONE details dict (completion preferred), so on a both-present usage
object the prompt dict — and with it ``cached_tokens`` — was silently dropped, breaking the
authoritative prompt-cache hit/miss classification in telemetry.

The fix merges both dicts (prompt wins on key conflicts) before invoking the callback. These tests
prove cached_tokens AND reasoning_tokens both survive that merge. Pure and deterministic: no network,
no LLM calls; the thread-local callback is set and cleared per test.
"""

import pytest

from agent_cascade.llm.base import _fire_usage_callback, _set_on_usage_cb


@pytest.fixture
def usage_capture():
    """Capture the (pt, ct, details) triple the callback receives; restore thread-local afterwards."""
    captured = {}

    def _cb(pt, ct, details=None):
        captured['pt'] = pt
        captured['ct'] = ct
        captured['details'] = details

    _set_on_usage_cb(_cb)
    try:
        yield captured
    finally:
        _set_on_usage_cb(None)  # never leak the callback onto the test thread


class TestFireUsageCallbackDetailsMerge:
    def test_both_details_present_preserves_cached_and_reasoning(self, usage_capture):
        """When BOTH completion_tokens_details and prompt_tokens_details are present, the callback
        receives cached_tokens (from prompt) AND reasoning_tokens (from completion)."""
        _fire_usage_callback({
            'prompt_tokens': 1000,
            'completion_tokens': 5,
            'completion_tokens_details': {'reasoning_tokens': 3},
            'prompt_tokens_details': {'cached_tokens': 80},
        })
        assert usage_capture['pt'] == 1000
        assert usage_capture['ct'] == 5
        details = usage_capture['details']
        # cached_tokens must survive (the whole point of the hardening).
        assert details.get('cached_tokens') == 80
        # reasoning_tokens must also survive (preserved via the completion base of the merge).
        assert details.get('reasoning_tokens') == 3

    def test_prompt_only_still_passes_cached_tokens(self, usage_capture):
        """llama.cpp emits only prompt_tokens_details — cached_tokens must still reach telemetry."""
        _fire_usage_callback({
            'prompt_tokens': 1000,
            'completion_tokens': 5,
            'prompt_tokens_details': {'cached_tokens': 80},
        })
        assert usage_capture['details'].get('cached_tokens') == 80

    def test_completion_only_still_passes_reasoning_tokens(self, usage_capture):
        """A backend that emits only completion_tokens_details keeps its reasoning breakdown."""
        _fire_usage_callback({
            'prompt_tokens': 1000,
            'completion_tokens': 5,
            'completion_tokens_details': {'reasoning_tokens': 3},
        })
        assert usage_capture['details'].get('reasoning_tokens') == 3

    def test_no_details_passes_none(self, usage_capture):
        """Neither details dict present -> callback gets None (unchanged contract)."""
        _fire_usage_callback({'prompt_tokens': 10, 'completion_tokens': 2})
        assert usage_capture['details'] is None

    def test_prompt_wins_on_key_conflict(self, usage_capture):
        """Prompt wins on a conflicting key (e.g. audio_tokens in both) — the cache signal source."""
        _fire_usage_callback({
            'prompt_tokens': 1000,
            'completion_tokens': 5,
            'completion_tokens_details': {'audio_tokens': 7},
            'prompt_tokens_details': {'cached_tokens': 80, 'audio_tokens': 9},
        })
        details = usage_capture['details']
        assert details.get('cached_tokens') == 80
        # Prompt's audio_tokens (9) shadows completion's (7).
        assert details.get('audio_tokens') == 9

    def test_no_callback_registered_is_noop(self):
        """With no callback set, firing is a silent no-op (never raises)."""
        _set_on_usage_cb(None)
        # Must not raise even with a fully-populated usage object.
        _fire_usage_callback({
            'prompt_tokens': 1000,
            'completion_tokens': 5,
            'completion_tokens_details': {'reasoning_tokens': 3},
            'prompt_tokens_details': {'cached_tokens': 80},
        })

    def test_malformed_usage_is_noop(self):
        """Non-dict / empty usage is ignored without raising."""
        _set_on_usage_cb(lambda *a: (_ for _ in ()).throw(AssertionError('should not fire')))
        try:
            _fire_usage_callback(None)
            _fire_usage_callback([])
            _fire_usage_callback('garbage')
        finally:
            _set_on_usage_cb(None)
