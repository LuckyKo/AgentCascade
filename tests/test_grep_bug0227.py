#!/usr/bin/env python3
"""BUG_0227 regression tests: a count from a truncated over-cap walk must be
disclosed as a lower bound in the header, and the walk budget must follow the
caller's ``timeout``.

Incident: a count-only grep over a large tree returned
``[total: 31 matching lines in 11 file(s)]`` followed by a note saying the
over-cap file list above was incomplete — a list that does not exist in
count-only mode. The total was presented as the answer, not as a lower bound,
and the walk's 0.75 s budget ignored the caller's ``timeout``.
"""

import time
from pathlib import Path
from unittest.mock import patch

from agent_cascade.operation_manager import OperationManager
from agent_cascade.operation_manager import grep as grep_mod


def _make_om(root: Path) -> OperationManager:
    return OperationManager(base_dir=str(root))


def _truncated_walk(self, root, include, exclude, ignore_vcs, max_bytes,
                    deadline, resolver=None):
    """Pretend the over-cap enumeration ran out of budget before finishing."""
    return 0, [], True


def _capturing_walk(store):
    def walk(self, root, include, exclude, ignore_vcs, max_bytes, deadline,
             resolver=None):
        store.append(deadline - time.monotonic())
        return 0, [], False
    return walk


def test_nonzero_total_from_truncated_walk_is_flagged_in_header(tmp_path):
    """The total must not be read as complete when enumeration was cut short."""
    (tmp_path / 'a.txt').write_text('needle\nneedle\n', encoding='utf-8')
    om = _make_om(tmp_path)

    with patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _truncated_walk):
        out = om.grep('needle', '.', count_only=True)

    assert out.startswith('[search INCOMPLETE — the over-cap file enumeration stopped')
    assert 'treat the count as a lower bound' in out, out
    assert 'Count-only results' in out, out
    assert 'list of skipped files above' not in out, out
    # The header precedes the total, so the total is never read on its own.
    assert out.index('search INCOMPLETE') < out.index('[total:'), out


def test_truncated_walk_marks_per_pattern_breakdown_partial(tmp_path):
    """The breakdown is an undercount too; do not render it as authoritative."""
    (tmp_path / 'c.txt').write_text('foo bar\nfoo\nbar\n', encoding='utf-8')
    om = _make_om(tmp_path)

    with patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _truncated_walk):
        out = om.grep('foo|bar', '.', count_only=True)

    assert '[per-pattern:' in out, out
    assert 'partial, lower bound' in out, out


def test_complete_walk_has_no_partial_header(tmp_path):
    """Positive control: no truncation → historical output shape, no header."""
    (tmp_path / 'a.txt').write_text('needle\n', encoding='utf-8')
    om = _make_om(tmp_path)

    with patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _capturing_walk([])):
        out = om.grep('needle', '.', count_only=True)

    assert out.startswith('Count-only results'), out
    assert 'search INCOMPLETE' not in out, out
    assert 'lower bound' not in out, out


def test_zero_total_from_truncated_walk_is_flagged_as_lower_bound(tmp_path):
    """A zero from a cut-short walk establishes no absence."""
    (tmp_path / 'a.txt').write_text('nothing\n', encoding='utf-8')
    om = _make_om(tmp_path)

    with patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _truncated_walk):
        out = om.grep('needle', '.', count_only=True)

    assert out.startswith('0 matching lines'), out
    assert 'treat the count as a lower bound' in out, out
    assert 'list of skipped files above' not in out, out


def test_walk_budget_follows_caller_timeout(tmp_path):
    """BUG_0227: raising ``timeout`` must widen the walk, not stay at 0.75 s."""
    (tmp_path / 'a.txt').write_text('needle\n', encoding='utf-8')
    om = _make_om(tmp_path)

    wide: list = []
    with patch('agent_cascade.settings.GREP_LARGE_FILE_SCAN_BUDGET_S', 0.75), \
            patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _capturing_walk(wide)):
        om.grep('needle', '.', count_only=True, timeout=30.0)
    assert wide and wide[0] >= 28, f'walk budget should follow timeout=30; got {wide}'

    narrow: list = []
    with patch('agent_cascade.settings.GREP_LARGE_FILE_SCAN_BUDGET_S', 0.75), \
            patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _capturing_walk(narrow)):
        om.grep('needle', '.', count_only=True, timeout=0.2)
    assert narrow and 0.6 <= narrow[0] <= 0.9, \
        f'small timeout must keep the 0.75 s safety budget; got {narrow}'


def test_walk_budget_uses_settings_source(tmp_path):
    """No re-hardcoded default: the settings budget is the floor when timeout is small."""
    (tmp_path / 'a.txt').write_text('needle\n', encoding='utf-8')
    om = _make_om(tmp_path)

    captured: list = []
    with patch('agent_cascade.settings.GREP_LARGE_FILE_SCAN_BUDGET_S', 2.5), \
            patch.object(grep_mod.GrepMixin, '_over_cap_rel_paths', _capturing_walk(captured)):
        om.grep('needle', '.', count_only=True, timeout=0.1)

    assert captured and 2.3 <= captured[0] <= 2.6, captured
