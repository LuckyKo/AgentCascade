"""BUG_0167: a regex that arrives HTML-escaped (`&lt;` for `<`) must not be a dead end.

The transcript evidence shows the LLM itself emitted `&lt;` inside the JSON tool arguments, so
this is not an AgentCascade transport escape. The fix therefore lives in grep: recover the
pattern only when it does NOT compile as-is but its HTML-unescaped form does, and tell the
caller what happened. Valid patterns that legitimately contain `&lt;` are never rewritten.
"""

import re
from pathlib import Path

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager.grep import _recover_html_escaped_pattern

LOOKBEHIND_ESCAPED = r'(?&lt;!/)device\.cu:|(?&lt;!/)stage_link\.'
LOOKBEHIND_CLEAN = r'(?<!/)device\.cu:|(?<!/)stage_link\.'

REPORT = (
    'worker.rs:12: device.cu: launch\n'
    'worker.rs:13: stage_link. wire\n'
    'worker.rs:14: /device.cu: commented path\n'
)


def _make_om(root: Path) -> OperationManager:
    return OperationManager(base_dir=str(root))


class TestRecoveryHelper:
    def test_escaped_lookbehind_is_recovered_with_a_note(self):
        pattern, note = _recover_html_escaped_pattern(LOOKBEHIND_ESCAPED)
        assert pattern == LOOKBEHIND_CLEAN
        assert re.compile(pattern)
        assert 'HTML entities' in note

    def test_double_escaped_pattern_is_recovered(self):
        pattern, note = _recover_html_escaped_pattern(r'(?&amp;lt;!/)foo')
        assert pattern == r'(?<!/)foo'
        assert note

    def test_valid_pattern_containing_an_entity_is_never_rewritten(self):
        # A caller may legitimately search for the literal text `&lt;`.
        pattern, note = _recover_html_escaped_pattern(r'&lt;tag')
        assert pattern == r'&lt;tag'
        assert note == ''

    def test_unrecoverable_pattern_is_returned_unchanged(self):
        pattern, note = _recover_html_escaped_pattern('(?<!/')
        assert pattern == '(?<!/'
        assert note == ''


class TestGrepEndToEnd:
    def test_escaped_lookbehind_searches_the_unescaped_pattern(self, tmp_path):
        (tmp_path / 'report.md').write_text(REPORT, encoding='utf-8')

        out = _make_om(tmp_path).grep(LOOKBEHIND_ESCAPED, '.', include='*.md')

        # The lookbehind excludes the third line (preceded by '/'), exactly like the clean form.
        assert out.startswith('Found 2 matches'), out
        assert 'HTML entities' in out
        assert 'device.cu' in out and 'stage_link.' in out
        assert 'commented path' not in out

    def test_single_file_path_also_recovers_the_pattern(self, tmp_path):
        target = tmp_path / 'report.md'
        target.write_text(REPORT, encoding='utf-8')

        out = _make_om(tmp_path).grep(LOOKBEHIND_ESCAPED, str(target))

        assert out.startswith('Found 2 matches'), out
        assert 'HTML entities' in out

    def test_literal_search_for_an_entity_is_not_rewritten(self, tmp_path):
        (tmp_path / 'doc.md').write_text('a &lt; b\nc < d\n', encoding='utf-8')

        out = _make_om(tmp_path).grep('&lt;', '.', literal=True)

        assert out.startswith('Found 1 matches'), out
        assert 'a &lt; b' in out
        assert 'HTML entities' not in out

    def test_valid_escaped_text_pattern_is_not_rewritten(self, tmp_path):
        (tmp_path / 'doc.md').write_text('a &lt; b\nc < d\n', encoding='utf-8')

        out = _make_om(tmp_path).grep('&lt;', '.')

        assert out.startswith('Found 1 matches'), out
        assert 'a &lt; b' in out
        assert 'HTML entities' not in out


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
