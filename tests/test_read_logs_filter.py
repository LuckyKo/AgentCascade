"""Tests for the read_logs `filter` parameter (entry selection by text).

The filter exists so a huge log can be probed without paging it: e.g.
``filter="--- CONTEXT COMPRESSED"`` lists every compaction marker. Matching runs
over the whole entry (before truncation), so it also finds text that
``max_chars_per_message`` would cut out of the simple preview.

While a filter is active ``range`` counts MATCHES, and the header switches from
``entries first-last/total`` to ``matches first-last/<matches> of <total> entries``
so the omitted non-matching entries stay recoverable (re-read without the filter).
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agent_cascade.tools.custom.read_logs import DEFAULT_TAIL_ENTRIES, ReadLogs

MARKER = '--- CONTEXT COMPRESSED (70% of history summarized) ---'


def _write_jsonl(tmp_path: Path, name: str, entries: list) -> Path:
    p = tmp_path / name
    with open(p, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry) + '\n')
    return p


def _read(path: Path, **params) -> str:
    pool = MagicMock()
    pool._logger.log_dir = str(path.parent)
    tool = ReadLogs(agent_pool=pool)
    return tool.call({'log_file': path.name, **params})


def _fenced_body(result: str) -> list:
    return result.split('```')[1].strip().split('\n')


class TestReadLogsFilter:

    def test_literal_filter_is_case_insensitive_by_default(self):
        """The todo use case: find compaction markers without matching their case."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [
                {'role': 'user', 'content': 'hello'},
                {'role': 'user', 'content': MARKER},
                {'role': 'assistant', 'content': 'bye'},
            ])

            result = _read(p, filter='context compressed')

            header = result.split('\n')[0]
            assert 'matches 1-1/1 of 3 entries' in header, header
            assert "filter 'context compressed'" in header, header
            assert 'CONTEXT COMPRESSED' in result      # the matched entry is shown
            assert 'bye' not in result                 # non-matching entries are not

    def test_case_sensitive_filter_excludes_other_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': MARKER}])

            result = _read(p, filter='context compressed', filter_case_sensitive=True)

            assert 'matches 0 of 1 entries' in result, result
            assert 'CONTEXT COMPRESSED' not in result
            assert '```' not in result, 'an empty match set must not emit a body fence'

    def test_literal_filter_does_not_interpret_regex_metacharacters(self):
        """'a.b' as a literal matches only 'a.b'; as a regex it also matches 'axb'."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [
                {'role': 'user', 'content': 'axb'},
                {'role': 'user', 'content': 'a.b'},
            ])

            literal = _read(p, filter='a.b')
            assert 'matches 1-1/1 of 2 entries' in literal.split('\n')[0]
            assert 'axb' not in literal

            as_regex = _read(p, filter='a.b', filter_regex=True)
            assert 'matches 1-2/2 of 2 entries' in as_regex.split('\n')[0]
            assert 'axb' in as_regex

    def test_regex_filter_is_case_insensitive_unless_asked_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': MARKER}])

            insensitive = _read(p, filter=r'context\s+compressed', filter_regex=True)
            assert 'matches 1-1/1 of 1 entries' in insensitive.split('\n')[0]

            sensitive = _read(p, filter=r'context\s+compressed', filter_regex=True,
                              filter_case_sensitive=True)
            assert 'matches 0 of 1 entries' in sensitive

    def test_invalid_regex_returns_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': 'x'}])

            result = _read(p, filter='(', filter_regex=True)

            assert result.startswith('Error: invalid filter regex'), result
            assert 'filter_regex=false' in result

    def test_filter_matches_text_hidden_by_preview_truncation(self):
        """Matching is done on the full entry, not the truncated simple preview."""
        with tempfile.TemporaryDirectory() as tmp:
            long_content = ('x' * 3000) + 'NEEDLE' + ('y' * 1000)
            p = _write_jsonl(Path(tmp), 't.jsonl', [
                {'role': 'user', 'content': long_content},
                {'role': 'user', 'content': 'unrelated'},
            ])

            result = _read(p, filter='NEEDLE', max_chars_per_message=50)

            assert 'matches 1-1/1 of 2 entries' in result.split('\n')[0]
            # The preview itself is still truncated (the only 'NEEDLE' left is the
            # filter label in the header) — the filter is the discovery tool...
            assert result.count('NEEDLE') == 1, result
            assert '[TRUNCATED:' in result
            # ...and the entry label keeps the source locatable for a by_line re-read.
            assert '⟨E 1 | L 1⟩' in result

    def test_filter_default_window_is_tail_of_matches(self):
        """No range + filter -> the last DEFAULT_TAIL_ENTRIES matches (mirrors the default)."""
        with tempfile.TemporaryDirectory() as tmp:
            entries = [{'role': 'user', 'content': f"m{i}" + (' HIT' if i >= 5 else '')}
                       for i in range(30)]
            p = _write_jsonl(Path(tmp), 't.jsonl', entries)

            result = _read(p, format='raw', filter='HIT')

            header = result.split('\n')[0]
            # Entries 1-5 do not match, so the 25 matches are entries 6..30 -> tail of 20
            # shows matches 6-25.
            assert f'matches 6-{DEFAULT_TAIL_ENTRIES + 5}/25 of 30 entries' in header, header
            assert len(_fenced_body(result)) == DEFAULT_TAIL_ENTRIES

    def test_filter_range_selects_among_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = [{'role': 'user', 'content': f"m{i}" + (' HIT' if i >= 5 else '')}
                       for i in range(30)]
            p = _write_jsonl(Path(tmp), 't.jsonl', entries)

            result = _read(p, format='raw', filter='HIT', range='1:3')

            assert 'matches 1-3/25 of 30 entries' in result.split('\n')[0]
            body = _fenced_body(result)
            assert len(body) == 3
            # First three matches are entries 6..8 / file lines 6..8.
            assert [line.split(':')[0] for line in body] == ['L6', 'L7', 'L8']

    def test_filter_without_matches_reports_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': 'm'} for _ in range(30)])

            result = _read(p, filter='no-such-text')

            assert 'matches 0 of 30 entries' in result
            assert "filter 'no-such-text'" in result
            assert '```' not in result

    def test_empty_filter_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': 'm1'},
                                                    {'role': 'user', 'content': 'm2'}])

            result = _read(p, format='raw', filter='')

            header = result.split('\n')[0]
            assert 'entries 1-2/2' in header, header
            assert 'filter' not in header, 'an empty filter must not change the header'

    def test_by_line_window_scopes_the_filter(self):
        """by_line is a hard window; the filter narrows it and the header cites the window."""
        with tempfile.TemporaryDirectory() as tmp:
            entries = [{'role': 'user',
                        'content': f"m{i}" + (' MARK' if i in (3, 5) else '')}
                       for i in range(1, 11)]
            p = _write_jsonl(Path(tmp), 't.jsonl', entries)

            result = _read(p, format='raw', by_line=True, range='3:5', filter='MARK')

            header = result.split('\n')[0]
            assert 'matches 1-2/2 in file lines 3-5' in header, header
            assert "filter 'MARK'" in header
            body = _fenced_body(result)
            assert len(body) == 2
            assert [line.split(':')[0] for line in body] == ['L3', 'L5']

    def test_by_line_window_with_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_jsonl(Path(tmp), 't.jsonl', [{'role': 'user', 'content': f"m{i}"}
                                                    for i in range(1, 6)])

            result = _read(p, by_line=True, range='2:4', filter='MARK')

            assert 'matches 0 in file lines 2-4' in result, result
            assert '```' not in result
