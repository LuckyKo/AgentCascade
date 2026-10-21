"""Regression tests for ``_detect_multiline_python`` (BUG_0012).

On a Windows host, a multi-line ``python -c`` script is split by cmd.exe (which treats
an embedded newline as a command separator), so the child never receives the full script
and the tool reports success with no output. The tool already had a detector for this
case, but its regex body ``[^"]*\\n[^"]*`` could not match a ``-c`` argument containing
an inner double-quote — the common case — so the "use semicolons" hint never fired.

The fix makes detection quote-state-aware. These tests are pure (no host I/O) and run
identically on Windows and POSIX hosts: the detector itself is platform-independent, and
the call-site wiring test patches ``ON_WINDOWS`` so it exercises the Windows branch
deterministically without spawning a real cmd.exe.
"""

import subprocess
from unittest.mock import MagicMock

import pytest

from agent_cascade.operation_manager.shell import ShellMixin


# ────────────────────────────────────────────────────────────────────────────────
# 1. Detector correctness (pure, platform-independent)
# ────────────────────────────────────────────────────────────────────────────────
NL = '\n'

DETECTED = [
    pytest.param('python -c "import sys' + NL + 'print(sys.version)"', id='plain-double-quoted'),
    pytest.param('python -c "print(\'a\')' + NL + 'print(\'b\')"', id='inner-single-quotes'),
    # The exact regression: inner DOUBLE quote inside a double-quoted -c script.
    pytest.param('python -c "x = "hi"' + NL + 'print(x)"', id='inner-double-quote'),
    pytest.param('py -c "print(1)' + NL + 'print(2)"', id='py-launcher'),
    pytest.param('python3 -c "print(1)' + NL + 'print(2)"', id='python3-launcher'),
    pytest.param("python -c 'print(1)" + NL + "print(2)'", id='single-quoted-arg'),
    pytest.param('python -u -c "print(1)' + NL + 'print(2)"', id='intervening-flag'),
]

NOT_DETECTED = [
    pytest.param('python -c "print(1)"', id='single-line'),
    pytest.param('python -c "a;b"', id='semicolon-no-newline'),
    pytest.param('python -c "print(1)"' + NL + 'python -c "print(2)"', id='two-single-line-stages'),
    pytest.param('python -c "print(1)" && echo hi' + NL + 'foo', id='newline-in-later-stage'),
    pytest.param('copy -c "x"' + NL + 'y', id='py-substring-of-copy'),
    pytest.param('python script.py', id='no-c-flag'),
    pytest.param('ls -la', id='not-python'),
    pytest.param('python -c "print(1)', id='unterminated-single-line'),
]


class TestDetectMultilinePython:
    """The detector is a pure predicate: True only when a -c/-e argument spans a newline."""

    @pytest.mark.parametrize('command', DETECTED)
    def test_detected(self, command):
        assert ShellMixin._detect_multiline_python(command) is True, command

    @pytest.mark.parametrize('command', NOT_DETECTED)
    def test_not_detected(self, command):
        assert ShellMixin._detect_multiline_python(command) is False, command


class TestHintText:
    """The hint text is unchanged by this fix (only WHEN it appears changes)."""

    def test_hint_mentions_multiline_and_semicolons(self):
        hint = ShellMixin._multiline_python_hint()
        assert 'Multi-line python -c detected' in hint
        assert 'semicolons' in hint


# ────────────────────────────────────────────────────────────────────────────────
# 2. Call-site wiring: the empty-output branch must surface the hint on Windows.
#    Fully controlled — no real process, no real approval prompt, no cmd.exe.
# ────────────────────────────────────────────────────────────────────────────────
class _EmptyProc:
    """A clean, output-less process: returncode 0, empty stdout/stderr."""
    returncode = 0
    pid = 0

    class _Pipe:
        def read(self, size=-1):
            return ''

    def __init__(self):
        self.stdout = self._Pipe()
        self.stderr = self._Pipe()

    def wait(self, timeout=None):
        return 0


def _make_om():
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager.__new__(OperationManager)
    om.base_dir = '/tmp'
    om.agent_pool = MagicMock()  # bare MagicMock pool → not restricted (verified behaviour)
    return om


class TestEmptyOutputBranchAppendsHint:
    """A Windows multi-line -c command that produces no output must carry the hint,
    not a bare 'No output produced.' (the BUG_0012 symptom)."""

    def test_empty_output_includes_hint(self, monkeypatch):
        om = _make_om()
        # Force the Windows branch on, deterministically, regardless of the host.
        monkeypatch.setattr('agent_cascade.operation_manager.shell.ON_WINDOWS', True)
        # Auto-approve (skip the interactive prompt) and stub out path resolution + spawn.
        monkeypatch.setattr(ShellMixin, 'evaluate_shell_command_safety',
                            lambda self, *a, **k: (True, []))
        monkeypatch.setattr('agent_cascade.utils.tool_path_resolver.resolve_tool_path',
                            lambda *a, **k: '/tmp')
        monkeypatch.setattr('subprocess.Popen', lambda *a, **k: _EmptyProc())

        result = om.execute_shell_command(
            command='python -c "x = "hi"' + NL + 'print(x)"',
            justification='test', agent_name='normal')

        assert 'Multi-line python -c detected' in result
        # The bare symptom must be replaced by the hinted form.
        assert 'No output produced.' in result
