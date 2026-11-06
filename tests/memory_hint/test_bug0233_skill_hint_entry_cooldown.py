"""BUG_0233 regression: a skill hint must not repeat on unrelated turns.

The reporting session had ``memory_hint_cooldown_seconds = 5`` (the setting is
documented as the MEMORY re-read window). Because the skill sub-pipeline reused that
same window, ``derive-undocumented-constant-from-repo-formula`` — a skill that sits
just above the adaptive floor on many code-reading turns — was suggested three times
in a ~23-minute session (05:15:32, 05:24:43, 05:27:11; the last two were only 551 s
and 148 s apart).

Skill hints now enforce their own entry cooldown,
``SKILL_HINT_ENTRY_COOLDOWN_SECONDS`` (30 min), applied as
``max(memory_hint_cooldown_seconds, SKILL_HINT_ENTRY_COOLDOWN_SECONDS)``. This also
acts as a regression guard against a user lowering the memory cooldown to seconds.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cascade.memory_hint import (
    MemoryHintManager,
    SKILL_HINT_ENTRY_COOLDOWN_SECONDS,
    SKILL_HINT_MIN_SCORE,
)

SKILL = 'derive-undocumented-constant-from-repo-formula'
QUERY = 'debugging a compression hang in the engine loop'
LESSONS = [
    ('compression-debug.md', 'Compression Debug',
     'How to debug compression hangs in the engine loop',
     'When compression hangs check the daemon thread and lock ordering.'),
]


def _write_lesson(vault: Path, rel_path: str, name: str, description: str, body: str) -> Path:
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntags: [test]\n---\n{body}\n",
        encoding='utf-8',
    )
    return p


def _make_om(base_dir):
    return SimpleNamespace(
        base_dir=str(base_dir),
        extra_work_folders_ro=[],
        extra_work_folders_rw=[],
    )


class _FakeInst:
    """Minimal instance exposing the memory-hint state fields + lock."""

    def __init__(self, name='w'):
        self.instance_name = name
        self.agent_class = 'test_agent'
        self.state = 'RUNNING'
        self._compression_lock = threading.RLock()
        self._tool_warnings = []
        self._memories_read = set()
        self._recently_hinted = {}
        self._last_memory_hint_turn = -1
        self._recently_skill_hinted = {}
        self._last_skill_hint_turn = -1
        self._skill_hint_counts = {}


def _make_mgr(tmp_path, cooldown_seconds):
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True)
    for rel, name, desc, body in LESSONS:
        _write_lesson(v, rel, name, desc, body)

    sm = MagicMock()
    sm.match_skills.return_value = [(SKILL, SKILL_HINT_MIN_SCORE + 0.05)]

    inst = _FakeInst()
    pool = MagicMock()
    pool.llm_cfg = {
        'memory_hint_enabled': True,
        'memory_hint_max_entries': 3,
        'memory_hint_cooldown_seconds': cooldown_seconds,
        'memory_hint_query_chars': 1000,
    }
    pool.skill_manager = sm
    pool.operation_manager = _make_om(v.parent)
    pool.get_instance.return_value = inst

    mgr = MemoryHintManager(pool)
    mgr.rescan_vaults()
    return mgr, inst, sm


def _job(turn):
    return {
        'instance_name': 'w',
        'query': QUERY,
        'agent_class': 'test_agent',
        'submitted_at': time.monotonic(),
        'turn': turn,
    }


def test_low_memory_cooldown_cannot_repeat_a_skill_hint(tmp_path):
    """BUG_0233: a 5 s memory cooldown must not let a skill repeat 10 s later."""
    mgr, inst, _sm = _make_mgr(tmp_path, cooldown_seconds=5.0)

    mgr._process_job(_job(3))
    assert len(inst._tool_warnings) == 1
    assert SKILL in inst._recently_skill_hinted
    assert inst._skill_hint_counts[SKILL] == 1

    # The skill was hinted 10 s ago: well past the 5 s memory cooldown, but not
    # past the skill entry cooldown.
    with inst._compression_lock:
        inst._recently_skill_hinted[SKILL] = time.monotonic() - 10.0

    mgr._process_job(_job(4))
    assert len(inst._tool_warnings) == 1, (
        'BUG_0233: the same skill was hinted again inside the entry cooldown')
    assert inst._skill_hint_counts[SKILL] == 1, 'a suppressed repeat must not bump the count'


@pytest.mark.parametrize('gap_seconds', [148.0, 551.0, 699.0])
def test_reported_session_gaps_are_suppressed(tmp_path, gap_seconds):
    """The real 05:15:32/05:24:43/05:27:11 gaps (148 s, 551 s, 699 s) are all inside 30 min."""
    mgr, inst, _sm = _make_mgr(tmp_path, cooldown_seconds=5.0)
    mgr._process_job(_job(3))
    assert len(inst._tool_warnings) == 1

    with inst._compression_lock:
        inst._recently_skill_hinted[SKILL] = time.monotonic() - gap_seconds

    mgr._process_job(_job(4))
    assert len(inst._tool_warnings) == 1, (
        f'BUG_0233: the reported {gap_seconds:.0f} s gap must be inside the entry cooldown')
    assert inst._skill_hint_counts[SKILL] == 1


def test_skill_repeats_after_entry_cooldown_expires(tmp_path):
    """The entry cooldown expires on schedule and lets the skill through again."""
    mgr, inst, _sm = _make_mgr(tmp_path, cooldown_seconds=5.0)

    mgr._process_job(_job(3))
    assert len(inst._tool_warnings) == 1

    with inst._compression_lock:
        inst._recently_skill_hinted[SKILL] = (
            time.monotonic() - (SKILL_HINT_ENTRY_COOLDOWN_SECONDS + 1.0))

    mgr._process_job(_job(4))
    assert len(inst._tool_warnings) == 2, 'the skill must be re-hinted after the entry cooldown'
    assert inst._skill_hint_counts[SKILL] == 2


def test_entry_cooldown_does_not_lengthen_memory_cooldown(tmp_path):
    """The fix is skill-only: memory entries still honour the (low) memory cooldown."""
    mgr, inst, _sm = _make_mgr(tmp_path, cooldown_seconds=5.0)

    mgr._process_job(_job(3))
    assert len(inst._tool_warnings) == 1
    first = inst._tool_warnings[0]
    assert 'compression-debug.md' in first
    assert SKILL in first

    # Both entries were hinted 10 s ago.
    now = time.monotonic()
    with inst._compression_lock:
        inst._recently_hinted['compression-debug.md'] = now - 10.0
        inst._recently_skill_hinted[SKILL] = now - 10.0

    mgr._process_job(_job(4))

    # Memory (5 s window) is re-hinted; the skill (30 min window) is not.
    assert len(inst._tool_warnings) == 2
    second = inst._tool_warnings[1]
    assert 'compression-debug.md' in second
    assert 'Skills you may want to load' not in second
    assert inst._skill_hint_counts[SKILL] == 1


def test_entry_cooldown_constant_is_exported_and_sane():
    """The guard is a documented module constant, not an accidental literal."""
    assert SKILL_HINT_ENTRY_COOLDOWN_SECONDS >= 1200.0, (
        'a skill entry cooldown shorter than 20 minutes cannot prevent the reported '
        '3-in-23-minutes recurrence')
