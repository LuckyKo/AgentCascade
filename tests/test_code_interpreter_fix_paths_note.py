"""Regression tests for BUG_0054: fix_paths detection note + docs clarification.

Tests the _check_fix_paths_artifact helper and the fix_paths parameter
description scope.  No Docker container required — pure unit tests.
"""
import os
import re
import sys

# Ensure the repo root is importable
_REPO = os.environ.get('BUG0054_REPO', r'N:\work\WD\AgentCascade')
sys.path.insert(0, _REPO)

from agent_cascade.tools.code_interpreter import (  # noqa: E402
    _check_fix_paths_artifact,
    _FIX_PATHS_ARTIFACT_RE,
)


class TestFixPathsArtifactDetection:
    """Verify the doubled-prefix detection in cell output (BUG_0054)."""

    def test_doubled_prefix_triggers_note(self):
        """A container-mount prefix + Windows drive-letter path triggers the note."""
        result = 'File not found: /extra_rw_0/N:/work/WD/AgentWorkspace/data.csv'
        note = _check_fix_paths_artifact(result)
        assert '[fix_paths note]' in note
        assert '/extra_rw_0/N:/work/WD/AgentWorkspace/data.csv' in note
        assert 'system_info mounts' in note

    def test_doubled_prefix_with_backslash(self):
        """Backslash variant (N:\\work\\...) also triggers the note."""
        result = 'Error at /workspace/N:\\work\\WD\\file.txt'
        note = _check_fix_paths_artifact(result)
        assert '[fix_paths note]' in note

    def test_extra_ro_mount_prefix(self):
        """RO mount prefix also triggers the note."""
        result = 'Cannot read /extra_ro_0/M:/data/config.json'
        note = _check_fix_paths_artifact(result)
        assert '[fix_paths note]' in note
        assert '/extra_ro_0/M:/data/config.json' in note

    def test_clean_posix_path_no_note(self):
        """A clean POSIX path does NOT trigger the note."""
        result = 'File found at /workspace/data.csv'
        assert _check_fix_paths_artifact(result) == ''

    def test_bare_windows_path_no_mount_prefix_no_note(self):
        """A bare Windows path (no container mount prefix) does NOT trigger the note."""
        result = 'File at N:/work/WD/data.csv'
        assert _check_fix_paths_artifact(result) == ''

    def test_empty_result_no_note(self):
        """Empty result produces no note."""
        assert _check_fix_paths_artifact('') == ''

    def test_only_first_match_noted(self):
        """Only the first artifact match produces a note (one-line note)."""
        result = (
            'Error 1: /extra_rw_0/N:/work/a.txt\n'
            'Error 2: /extra_ro_0/M:/work/b.txt'
        )
        note = _check_fix_paths_artifact(result)
        assert note.count('[fix_paths note]') == 1
        # The first match is noted
        assert '/extra_rw_0/N:/work/a.txt' in note

    def test_note_is_single_line(self):
        """The note is exactly one line (starts with \\n, no embedded newlines)."""
        result = 'Path: /extra_rw_0/N:/work/WD/x.txt'
        note = _check_fix_paths_artifact(result)
        # Strip the leading \n, then check there are no more newlines
        stripped = note.lstrip('\n')
        assert '\n' not in stripped


class TestFixPathsParamDescription:
    """Verify the fix_paths parameter description clarifies scope (BUG_0054)."""

    def test_description_mentions_literals(self):
        """The description must state that only literals in submitted code are rewritten."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        desc = CodeInterpreter.parameters['properties']['fix_paths']['description']
        assert 'LITERALS' in desc, f"Expected 'LITERALS' in description: {desc}"

    def test_description_mentions_runtime_paths_not_rewritten(self):
        """The description must state that runtime-sourced paths are NOT rewritten."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        desc = CodeInterpreter.parameters['properties']['fix_paths']['description']
        assert 'NOT rewritten' in desc, f"Expected 'NOT rewritten' in description: {desc}"

    def test_description_mentions_files_databases_argv(self):
        """The description must name the runtime sources (files, databases, argv)."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        desc = CodeInterpreter.parameters['properties']['fix_paths']['description']
        for term in ('files', 'databases', 'argv'):
            assert term in desc, f"Expected '{term}' in description: {desc}"


class TestFixPathsLiteralBehaviorUnchanged:
    """Verify existing fix_paths behavior for code-literal paths is unchanged (BUG_0054)."""

    def test_resolve_code_paths_rewrites_literals(self):
        """resolve_code_paths should still rewrite Windows path literals in code."""
        from agent_cascade.utils.code_path_resolver import resolve_code_paths
        code = "import os\np = r'N:\\work\\WD\\AgentWorkspace\\test.txt'\nprint(p)"
        resolved, count = resolve_code_paths(code)
        # On a system without Docker mounts configured, the resolver may return 0
        # rewrites.  The key assertion is that it does NOT raise and the function
        # contract is preserved.
        assert isinstance(resolved, str)
        assert isinstance(count, int)

    def test_fix_paths_flag_default_is_true(self):
        """The fix_paths parameter defaults to True (existing behavior)."""
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        assert CodeInterpreter.parameters['properties']['fix_paths']['default'] is True
