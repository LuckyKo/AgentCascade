"""Regression tests for ``_resolve_fix_paths`` (BUG_0113).

The ``fix_paths`` tool parameter was silently ignored: the flag was read from the
RAW ``params`` (a JSON *string* on the normal LLM path) instead of the already-parsed
``validated_params``, so ``isinstance(params, dict)`` was False and the flag stayed
True — path auto-resolution always ran and the "please use fix_paths=false" notice
always fired.

The fix extracts a pure, Docker-free helper that reads the PARSED dict and coerces
stringly-typed false-ish values. These tests exercise the helper directly (no
container, no LLM, no network).
"""

from agent_cascade.tools.code_interpreter import _resolve_fix_paths


class TestResolveFixPathsDefault:
    """Absent key or non-dict input must keep the default (True = resolution on)."""

    def test_absent_key_defaults_true(self):
        assert _resolve_fix_paths({}) is True

    def test_code_only_dict_defaults_true(self):
        # A normal payload with no fix_paths key must not change behaviour.
        assert _resolve_fix_paths({'code': "x='N:/a\\b.png'"}) is True

    def test_non_dict_input_defaults_true(self):
        # Lenient-fallback path: raw params may be a string / None — helper is total.
        assert _resolve_fix_paths('not a dict') is True
        assert _resolve_fix_paths(None) is True


class TestResolveFixPathsRealBools:
    """A real JSON boolean is honoured exactly (the well-formed LLM path)."""

    def test_true_bool(self):
        assert _resolve_fix_paths({'fix_paths': True}) is True

    def test_false_bool(self):
        assert _resolve_fix_paths({'fix_paths': False}) is False


class TestResolveFixPathsStringly:
    """Stringly-typed values must not invert intent (the ``bool("false")`` trap)."""

    def test_string_true_is_enabled(self):
        assert _resolve_fix_paths({'fix_paths': 'True'}) is True
        assert _resolve_fix_paths({'fix_paths': 'true'}) is True

    def test_string_false_is_disabled(self):
        assert _resolve_fix_paths({'fix_paths': 'false'}) is False
        assert _resolve_fix_paths({'fix_paths': 'FALSE'}) is False
        assert _resolve_fix_paths({'fix_paths': ' False '}) is False

    def test_other_falseish_strings_disabled(self):
        for value in ('0', 'no', 'off', ''):
            assert _resolve_fix_paths({'fix_paths': value}) is False, value

    def test_other_strings_enabled(self):
        # Anything not in the documented false-ish set is treated as enabled.
        assert _resolve_fix_paths({'fix_paths': 'yes'}) is True
        assert _resolve_fix_paths({'fix_paths': '1'}) is True

    def test_non_bool_non_string_uses_truthiness(self):
        # Defensive: a non-bool/non-str value falls back to bool().
        assert _resolve_fix_paths({'fix_paths': 0}) is False
        assert _resolve_fix_paths({'fix_paths': 1}) is True
