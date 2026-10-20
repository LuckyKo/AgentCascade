#!/usr/bin/env python3
"""Regression tests for the shell_cmd Windows dialect pre-check (BUG_0053/0092).

Covers:
- Blocked: POSIX text tools / PowerShell cmdlets invoked as a stage COMMAND.
- Allowed: the same names appearing as arguments, flags, file names or string literals,
  and explicit ``powershell``/``pwsh`` invocations (PowerShell program text, not cmd.exe).
- The ``ON_WINDOWS`` gate: a no-op on non-Windows hosts.
- Message shape: cmd.exe's own ``'X' is not recognized as an internal or external command``
  wording (the ``tool_loop_detect`` failure signature), correct ``Select-String`` display
  casing, and honest phrasing when no cmd.exe equivalent exists.
- Placement: the check fires before any security approval request.

The gate is the module-level ``ON_WINDOWS`` constant imported from
``agent_cascade.operation_manager.shell``, so tests monkeypatch that name ON the
shell_cmd module. Patching ``os.name`` has no effect on the check (it no longer reads
``os.name``); the tests patch ``ON_WINDOWS`` explicitly so they behave identically
on Windows and POSIX hosts.

No LLM or network connections required.
"""

from unittest.mock import MagicMock, patch

import pytest

import agent_cascade.tools.custom.shell_cmd as shell_cmd_module
from agent_cascade import tool_loop_detect


@pytest.fixture
def shell_cmd_tool():
    from agent_cascade.tools.custom.shell_cmd import ShellCmd
    return ShellCmd()


@pytest.fixture
def on_windows(monkeypatch):
    """Force the dialect gate on, regardless of the host running the tests."""
    monkeypatch.setattr(shell_cmd_module, 'ON_WINDOWS', True)


@pytest.fixture
def not_on_windows(monkeypatch):
    """Force the dialect gate off (simulates a POSIX host)."""
    monkeypatch.setattr(shell_cmd_module, 'ON_WINDOWS', False)


# ────────────────────────────────────────────────────────────────────────────────
# 1. Blocked: the tool/cmdlet is the command being invoked
# ────────────────────────────────────────────────────────────────────────────────
BLOCKED = [
    ('grep foo bar.txt', 'grep'),
    ('echo hi | Select-String hi', 'Select-String'),
    ("awk '{print $1}' file", 'awk'),
    ("sed 's/a/b/' file", 'sed'),
    ('cd N:\\work && grep -r TODO .', 'grep'),
    ('cd N:\\work&&grep -r TODO .', 'grep'),
    ('dir & grep x', 'grep'),                      # lone & is a cmd.exe command separator
    ('grep x | powershell -Command "Get-Object"', 'grep'),  # PS stage exempt, others checked
    ('dir /s | findstr x | tee-object out.txt', 'Tee-Object'),
    ('type a.txt | select-string foo', 'Select-String'),
    ('"Select-String" foo', 'Select-String'),          # quoted command token
    ('echo hi | SELECT-STRING hi', 'Select-String'),   # case-insensitive match
]


class TestBlockedDialect:
    """POSIX tools / PowerShell cmdlets in command position must be rejected."""

    @pytest.mark.parametrize('command,display', BLOCKED)
    def test_rejected(self, on_windows, command, display):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        result = ShellCmd._check_windows_dialect(command)
        assert result is not None, f"expected rejection for {command!r}"
        assert f"'{display}'" in result, f"expected {display!r} named in: {result}"
        # cmd.exe's own wording — the tool_loop_detect failure signature.
        assert 'is not recognized as an internal or external command' in result


class TestLoopDetectSignature:
    """A missed dialect error must be classifiable by tool_loop_detect."""

    @pytest.mark.parametrize('signature_res', [
        pytest.param(tool_loop_detect.TERMINAL_ERROR_RES, id='terminal-error'),
        pytest.param(tool_loop_detect._FAILURE_OUTPUT_RES, id='failure-output'),
    ])
    @pytest.mark.parametrize('command', ['grep foo bar.txt', 'echo hi | Select-String hi'])
    def test_signatures_match(self, on_windows, command, signature_res):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        msg = ShellCmd._check_windows_dialect(command)
        assert msg is not None
        assert any(rx.search(msg) for rx in signature_res), \
            f"loop-detect signature does not match: {msg}"


# ────────────────────────────────────────────────────────────────────────────────
# 2. Allowed: the name is an argument, a flag, a file name, string data,
#    or the interpreter is PowerShell itself
# ────────────────────────────────────────────────────────────────────────────────
ALLOWED = [
    'git grep pattern',                    # git built-in
    'git log --grep=fix',                 # git flag
    'type grep.txt',                      # file argument
    'findstr /i grep file.txt',           # the suggested alternative itself
    'python -c "print(\'sed is a stream editor\')"',  # string literal
    'powershell -Command "git log | Select-String foo"',  # PowerShell program text
    'pwsh -Command "ls | Where-Object { $_.Name -match x }"',
    'dir',
    'git status',
    'findstr /c:"awk" notes.txt',
    'echo "grep is missing on Windows"',   # quoted data containing a tool name
    'python -c "import re; print(re.sub(\'a\', \'b\', \'sed\'))"',  # quoted pipe/args
    'git show-ref --head',                # flag named like a POSIX tool
]


class TestAllowedCommands:
    """Clean commands must return None — no false positives from substring matching."""

    @pytest.mark.parametrize('command', ALLOWED)
    def test_allowed(self, on_windows, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        assert ShellCmd._check_windows_dialect(command) is None, \
            f"unexpected rejection for {command!r}"


class TestDocumentedLimitations:
    """Known false negatives of the stage parser. Pinned so any change is a decision, not drift.

    All are accepted as low value: the stderr-signature catch-all (a scoped follow-up) is the
    mechanism that would catch them. See the "Known misses" note in _split_command_stages.
    """

    @pytest.mark.parametrize('command', [
        r'C:\bin\grep.exe foo bar',            # path-qualified invocation is not matched
        'cmd /c "grep x" & echo done',         # wrapper is the command position
        'start /b grep x',
        "git commit -m it's broken | grep x",  # unbalanced ' opens a quote that never closes
    ])
    def test_known_misses(self, on_windows, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        assert ShellCmd._check_windows_dialect(command) is None


class TestNonWindowsNoOp:
    """The check must be inert on non-Windows hosts."""

    @pytest.mark.parametrize('command', ['grep foo bar.txt', "sed 's/a/b/' file",
                                         'echo hi | Select-String hi'])
    def test_not_blocked_on_posix(self, not_on_windows, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        assert ShellCmd._check_windows_dialect(command) is None


# ────────────────────────────────────────────────────────────────────────────────
# 3. Message content: alternatives, no-equivalent honesty, cmdlet display casing
# ────────────────────────────────────────────────────────────────────────────────
class TestMessageContent:

    def test_grep_suggests_findstr(self, on_windows):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        msg = ShellCmd._check_windows_dialect('grep foo bar.txt')
        assert "Use 'findstr' instead." in msg

    def test_no_equivalent_phrasing_for_sed(self, on_windows):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        msg = ShellCmd._check_windows_dialect("sed 's/a/b/' file")
        assert 'There is no cmd.exe equivalent' in msg
        # The parenthetical placeholder must never leak into a "Use '...' instead." slot.
        assert "Use '(" not in msg
        assert 'instead. (' not in msg

    def test_cmdlet_display_is_title_case(self, on_windows):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        msg = ShellCmd._check_windows_dialect('dir | select-string foo')
        assert "'Select-String'" in msg
        assert 'Select-string' not in msg

    def test_cmdlet_identified_as_powershell(self, on_windows):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        msg = ShellCmd._check_windows_dialect('dir | select-object *')
        assert 'PowerShell cmdlet' in msg
        assert 'There is no cmd.exe equivalent' in msg

    def test_hint_with_alternative(self):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        hint = ShellCmd._dialect_hint('findstr')
        assert "Use 'findstr' instead." in hint
        assert 'There is no cmd.exe equivalent' not in hint

    def test_hint_without_alternative(self):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        hint = ShellCmd._dialect_hint(None)
        assert hint.startswith('There is no cmd.exe equivalent')
        assert 'grep/read_file' in hint

    @pytest.mark.parametrize('command', ["sed 's/a/b/' file", 'dir | select-object *'])
    def test_advice_not_duplicated(self, on_windows, command):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        msg = ShellCmd._check_windows_dialect(command)
        assert msg.count('grep/read_file') == 1, f"advice duplicated in: {msg}"
        assert msg.count('Python snippet') == 1, f"advice duplicated in: {msg}"


# ────────────────────────────────────────────────────────────────────────────────
# 4. Stage splitting / command-name extraction (the Fix 2 machinery)
# ────────────────────────────────────────────────────────────────────────────────
class TestStageSplitting:

    @pytest.mark.parametrize('command,expected', [
        ('git status', ['git status']),
        ('a | b', ['a ', ' b']),
        ('a || b', ['a ', ' b']),
        ('cd x && grep y', ['cd x ', ' grep y']),
        ('a&&grep y', ['a', 'grep y']),        # no space after '&&'
        ('a||grep y', ['a', 'grep y']),        # no space after '||'
        ('a & b', ['a ', ' b']),              # lone '&' is a cmd.exe command separator
        ('dir 2>&1', ['dir 2>', '1']),        # redirection fragments are harmless here
        ('a; b', ['a', ' b']),
        ('a&&&grep x', ['a', '', 'grep x']),   # empty stage from a degenerate operator run
        ('a|&b', ['a', '', 'b']),
        ('python -c "x | y"', ['python -c "x | y"']),   # quoted pipe is data
        ("awk '{print $1}' f | sort", ["awk '{print $1}' f ", ' sort']),
    ])
    def test_split(self, command, expected):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        assert ShellCmd._split_command_stages(command) == expected

    @pytest.mark.parametrize('stage,expected', [
        ('grep foo', 'grep'),
        ('  head -n 5', 'head'),
        ('-E foo', 'foo'),          # leading flags skipped
        ('"Select-String" x', 'select-string'),
        ('', None),
        ('   ', None),
        ('-only -flags', None),
    ])
    def test_command_name(self, stage, expected):
        from agent_cascade.tools.custom.shell_cmd import ShellCmd
        assert ShellCmd._stage_command_name(stage) == expected


# ────────────────────────────────────────────────────────────────────────────────
# 5. Placement: rejected before any security approval is requested
# ────────────────────────────────────────────────────────────────────────────────
class TestCallSitePlacement:

    def test_rejected_before_security(self, shell_cmd_tool, on_windows):
        pool = MagicMock()
        shell_cmd_tool.agent_pool = pool
        shell_cmd_tool.agent_name = 'test-agent'
        with patch('subprocess.Popen', side_effect=AssertionError('executed a command')):
            result = shell_cmd_tool.call('{"command": "grep foo bar.txt", '
                                         '"justification": "search"}')
        assert 'is not recognized as an internal or external command' in result
        pool.operation_manager.request_user_approval.assert_not_called()
        pool.operation_manager.evaluate_shell_command_safety.assert_not_called()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
