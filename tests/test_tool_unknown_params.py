"""BUG_0024 regression tests — shell_cmd / BaseTool unknown-parameter handling.

Root cause (see .bug_tracker/BUG_0024_*.md): the shell_cmd schema had no
additionalProperties:false, so a renamed/misspelled parameter (e.g. the legacy
``async_mode`` after the execution_mode rename) was accepted and silently
discarded — the tool ran with DEFAULTS instead of the requested behaviour.

Fix under test:
- BaseTool._verify_json_format_args converts additionalProperties rejections into
  a friendly ValueError naming the offending keys and the valid parameters.
- ShellCmd.parameters carries additionalProperties: False.
- ShellCmd.call coerces the observed legacy ``async_mode`` alias to
  ``execution_mode`` (with a nudge log) and returns an ERROR string for any other
  unknown key — never runs with silently-wrong defaults.

The coercion tests monkeypatch _execute_sync / _launch_async so no real shell is
spawned; the rejection test proves the command never executes at all.
"""
import jsonschema
import pytest

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.custom.shell_cmd import ShellCmd


def test_base_gate_friendly_error_for_unknown_param():
    """The shared validation gate names offending keys + valid params (not a raw ValidationError)."""

    @register_tool('_test_unknown_param_tool', allow_overwrite=True)
    class _T(BaseTool):
        name = '_test_unknown_param_tool'
        description = 'test tool'
        parameters = {
            'type': 'object',
            'additionalProperties': False,
            'properties': {'a': {'type': 'string'}},
            'required': ['a'],
        }

        def call(self, params, **kwargs):
            return self._verify_json_format_args(params)

    with pytest.raises(ValueError) as ei:
        _T().call({'a': 'x', 'bogus_key': 1})
    msg = str(ei.value)
    assert 'unknown parameter' in msg
    assert 'bogus_key' in msg
    assert "'a'" in msg


def test_shell_cmd_schema_rejects_additional_properties():
    """The shell_cmd schema itself rejects unknown keys."""
    assert ShellCmd.parameters.get('additionalProperties') is False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'command': 'echo hi', 'async_mode': True}, ShellCmd.parameters)


def test_shell_cmd_async_mode_alias_coerced_to_execution_mode(monkeypatch):
    """Legacy async_mode=true must map to execution_mode=async (revert-proof)."""
    calls = {}

    def fake_launch(self, agent_name, command, justification, cwd, timeout, heartbeat_interval):
        calls['launched'] = True
        return 'LAUNCHED'

    monkeypatch.setattr(ShellCmd, '_launch_async', fake_launch)
    out = ShellCmd().call({'command': 'echo hi', 'justification': 't', 'async_mode': True})
    assert not out.startswith('ERROR'), out
    assert calls.get('launched') is True


def test_shell_cmd_unknown_param_errors_loudly_and_never_runs(monkeypatch):
    """Any other unknown key errors loudly and the command must NOT execute."""

    def boom(self, *a, **k):
        raise AssertionError('_execute_sync must not be called for an unknown param')

    monkeypatch.setattr(ShellCmd, '_execute_sync', boom)
    out = ShellCmd().call({'command': 'echo hi', 'justification': 't', 'wrong_key': 1})
    assert out.startswith('ERROR'), out
    assert 'unknown parameter' in out
    assert 'wrong_key' in out
