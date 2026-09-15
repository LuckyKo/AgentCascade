#!/usr/bin/env python3
"""Tests for the Windows ``;`` → ``&`` translation in the chcp shell wrapper.

Background (see reports/shell_semicolon_misparse_audit.md): on Windows every shell
command is wrapped as ``chcp 65001 > nul 2>&1 & <command>`` by
``configure_windows_utf8`` (agent_cascade/shell_utils.py). cmd.exe does NOT treat
``;`` as a command separator, so ``A; B`` runs as the single command ``A`` with
``; B`` as arguments — the second command never runs. The fix translates each
UNQUOTED ``;`` to cmd's ``&`` equivalent before building the chcp string.

Two layers:
1. Unit tests (platform-independent): exercise the pure-string translator and the
   wrapper's output shape on any OS.
2. Integration tests (Windows-only, skipped elsewhere): run the wrapped command via
   subprocess and assert real cmd.exe behavior.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent_cascade.shell_utils import (_strip_leading_ampersands_for_cmd, _translate_semicolons_for_cmd,
                                       configure_windows_utf8)

ON_WINDOWS = os.name == 'nt'

# ─── Unit tests: pure string translation (run on any platform) ──────


class TestTranslateSemicolonsForCmd:
    """The translator is pure string logic — no Windows dependency."""

    def test_simple_semicolon_translated(self):
        # Unquoted `;` becomes cmd's `&`. (In-place replacement: `A; B` -> `A& B`,
        # which cmd parses as two commands — verified by the Windows integration test.)
        assert _translate_semicolons_for_cmd('echo A; echo B') == 'echo A& echo B'

    def test_cd_path_with_trailing_backslash(self):
        # `cd C:\ ; git diff` — the `;` is unquoted even though a backslash precedes it.
        assert _translate_semicolons_for_cmd('cd C:\\ ; git diff') == 'cd C:\\ & git diff'

    def test_quoted_semicolon_preserved(self):
        # `;` inside double quotes is data, not a separator — preserved.
        assert _translate_semicolons_for_cmd("python -c \"print('a;b')\"") == "python -c \"print('a;b')\""

    def test_mixed_quoted_and_unquoted(self):
        # Quoted `;` stays; the unquoted trailing `;` becomes `&`.
        assert _translate_semicolons_for_cmd('cmd /c "exit 3"; echo B') == 'cmd /c "exit 3"& echo B'

    def test_multiple_unquoted_semicolons(self):
        assert _translate_semicolons_for_cmd('echo one; echo two; echo three') == 'echo one& echo two& echo three'

    def test_single_quotes_are_literal_in_cmd(self):
        # cmd treats `'` as a literal character — it does NOT toggle quote state.
        # So the `;` here is unquoted and IS translated (only `"` protects).
        assert _translate_semicolons_for_cmd("echo 'a;b'") == "echo 'a&b'"

    def test_unbalanced_quote_treats_remainder_as_quoted(self):
        # Odd number of `"` → remainder treated as quoted (safe default: no translation).
        assert _translate_semicolons_for_cmd('echo "unclosed; more') == 'echo "unclosed; more'

    def test_no_semicolon_unchanged(self):
        assert _translate_semicolons_for_cmd('dir /s /b') == 'dir /s /b'

    def test_empty_string(self):
        assert _translate_semicolons_for_cmd('') == ''

    def test_semicolon_only(self):
        assert _translate_semicolons_for_cmd(';') == '&'


class TestStripLeadingAmpersandsForCmd:
    """The leading-`&` stripper is pure string logic — no Windows dependency (W11 fix)."""

    def test_single_leading_amp_stripped(self):
        assert _strip_leading_ampersands_for_cmd('& echo hi') == 'echo hi'

    def test_double_leading_amp_stripped(self):
        # `&&` is a full leading operator sequence — the whole run of `&` is stripped.
        assert _strip_leading_ampersands_for_cmd('&& echo hi') == 'echo hi'

    def test_triple_leading_amp_stripped(self):
        assert _strip_leading_ampersands_for_cmd('&&& echo hi') == 'echo hi'

    def test_only_amps_returns_original(self):
        # A command that is only `&` (or `&&`) would become empty if stripped; keep it as-is
        # so the wrapper still emits a valid `chcp ... & <cmd>` rather than a dangling separator.
        assert _strip_leading_ampersands_for_cmd('&') == '&'
        assert _strip_leading_ampersands_for_cmd('&&') == '&&'

    def test_no_leading_amp_unchanged(self):
        assert _strip_leading_ampersands_for_cmd('echo hi') == 'echo hi'

    def test_amp_not_at_start_is_data(self):
        # A `&` that is NOT the first character is an operator mid-command — untouched.
        assert _strip_leading_ampersands_for_cmd('echo a & echo b') == 'echo a & echo b'

    def test_quoted_string_not_stripped(self):
        # Quote-aware: only strip when the FIRST character is literally `&`. A leading quote
        # means the `&` (if any) is data — leave it alone.
        assert _strip_leading_ampersands_for_cmd('"foo & bar"') == '"foo & bar"'

    def test_whitespace_before_amp_not_stripped(self):
        # Leading whitespace means byte 0 is not `&`; the command is left untouched.
        assert _strip_leading_ampersands_for_cmd('  & echo hi') == '  & echo hi'


class TestConfigureWindowsUtf8Shape:
    """The wrapper output string shape (platform-independent)."""

    def test_prefix_shape_preserved_and_translated(self):
        wrapped, flags = configure_windows_utf8('echo A; echo B')
        assert wrapped == 'chcp 65001 > nul 2>&1 & echo A& echo B'

    def test_quoted_semicolon_kept_in_wrapper(self):
        user = 'python -c "print(\'a;b\')"'
        wrapped, _ = configure_windows_utf8(user)
        assert wrapped == 'chcp 65001 > nul 2>&1 & ' + user

    def test_chcp_prefix_untouched_for_plain_command(self):
        wrapped, _ = configure_windows_utf8('dir /s')
        assert wrapped == 'chcp 65001 > nul 2>&1 & dir /s'

    def test_leading_single_amp_stripped_in_wrapper(self):
        # W11: `& echo hi` would otherwise wrap to a double-`&` that cmd.exe rejects.
        wrapped, _ = configure_windows_utf8('& echo hi')
        assert wrapped == 'chcp 65001 > nul 2>&1 & echo hi'

    def test_leading_double_amp_stripped_in_wrapper(self):
        # `&&` at the start is a redundant operator sequence — stripped to a single wrapper `&`.
        wrapped, _ = configure_windows_utf8('&& echo hi')
        assert wrapped == 'chcp 65001 > nul 2>&1 & echo hi'

    def test_leading_amp_run_stripped_in_wrapper(self):
        # A leading run of `&` (e.g. `&&&`) is fully stripped so the wrapper's single `&` separates.
        wrapped, _ = configure_windows_utf8('&&& echo hi')
        assert wrapped == 'chcp 65001 > nul 2>&1 & echo hi'

    def test_command_not_starting_with_amp_untouched(self):
        # A mid-command `&` is a real operator and must be preserved.
        wrapped, _ = configure_windows_utf8('echo a & echo b')
        assert wrapped == 'chcp 65001 > nul 2>&1 & echo a & echo b'

    def test_quoted_leading_string_not_stripped(self):
        # Quote-aware: a command starting with `"` (not literally `&`) is left untouched.
        user = '"foo & bar"'
        wrapped, _ = configure_windows_utf8(user)
        assert wrapped == 'chcp 65001 > nul 2>&1 & ' + user

    def test_semicolon_starting_command_stripped_in_wrapper(self):
        # Transform-ordering edge: `;` translates to a leading `&`, which the stripper
        # then removes — same valid wrap as `& echo hi`. Pinned so reordering the two
        # transforms (or changing either default) cannot silently regress this path.
        wrapped, _ = configure_windows_utf8('; echo hi')
        assert wrapped == 'chcp 65001 > nul 2>&1 & echo hi'

    def test_flags_include_new_process_group(self):
        import subprocess
        _, flags = configure_windows_utf8('echo A')
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    def test_flags_include_new_console_when_requested(self):
        import subprocess
        _, flags = configure_windows_utf8('echo A', create_new_console=True)
        assert flags & subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]


# ─── Integration tests: real cmd.exe behavior (Windows-only) ────────


@pytest.mark.skipif(not ON_WINDOWS, reason='Requires Windows cmd.exe')
class TestWindowsSemicolonExecution:
    """Run the wrapped command through subprocess and assert cmd.exe behavior."""

    def _run(self, user_command):
        import subprocess
        wrapped, flags = configure_windows_utf8(user_command)
        return subprocess.run(
            wrapped,
            shell=True,
            capture_output=True,
            text=True,
            creationflags=flags,
            timeout=30,
        )

    def test_semicolon_chain_runs_both(self):
        result = self._run('echo A; echo B')
        lines = [line.strip() for line in result.stdout.splitlines()]
        assert 'A' in lines and 'B' in lines, f'expected both A and B, got {result.stdout!r}'

    def test_cd_then_echo_runs_second(self):
        # The auto-approved `cd <path> ; <cmd>` idiom must actually run the second command.
        result = self._run('cd C:\\ ; echo RAN')
        assert 'RAN' in result.stdout, f'expected RAN, got stdout={result.stdout!r} stderr={result.stderr!r}'

    def test_exit_code_propagates(self):
        # `cmd /c "exit 5"` under the wrapper must surface RC 5 (chcp's 0 does not mask it).
        result = self._run('cmd /c "exit 5"')
        assert result.returncode == 5, f'expected RC 5, got {result.returncode}'

    def test_quoted_semicolon_is_data(self):
        # `;` inside double quotes is preserved as data (both run: echo prints the literal).
        result = self._run('echo "a;b"')
        assert 'a;b' in result.stdout, f'expected a;b, got {result.stdout!r}'

    def test_leading_amp_command_runs(self):
        # W11: `& echo RAN` must strip the redundant leading `&` so cmd.exe sees
        # `chcp 65001 > nul 2>&1 & echo RAN` (single separator) — not a double-`&`.
        # Verified empirically: RC 0, stdout contains 'RAN'.
        result = self._run('& echo RAN')
        assert result.returncode == 0, f'expected RC 0, got {result.returncode} stderr={result.stderr!r}'
        assert 'RAN' in result.stdout, f'expected RAN, got stdout={result.stdout!r} stderr={result.stderr!r}'
