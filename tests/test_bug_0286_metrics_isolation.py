"""BUG_0286 — the skill-metrics store must never be written by a test process, and
concurrent flushes must not share a temp file.

Two defects are guarded here:

1. **Isolation.** ``SkillManager`` defaults ``_metrics_file`` to the CWD-relative
   ``agents/global/skills-metrics.json``; ``AgentPool`` builds a manager on construction and
   flushes it on stop. A test-made pool therefore overwrote the LIVE store. The autouse
   ``_isolate_skill_metrics_store`` fixture (tests/conftest.py) redirects the path via
   ``AGENT_CASCADE_SKILLS_METRICS_FILE``.
2. **Temp-name collision.** A fixed ``<name>.tmp`` is shared by every writer, so concurrent
   flushes interleave: the first ``os.replace`` consumes the temp file and the second fails
   with WinError 2 (ENOENT). ``_tmp_sibling`` makes the name unique per writer.

No network, no real LLM.
"""

import json
import os
import threading

from agent_cascade.skills.manager import (_DEFAULT_METRICS_FILE, SkillManager,
                                          _tmp_sibling)

PRODUCTION_STORE = _DEFAULT_METRICS_FILE


# ── isolation (defect 1) ─────────────────────────────────────────────────────

def test_autouse_fixture_keeps_manager_off_the_live_store(tmp_path):
    """The autouse isolation fixture must redirect every constructed manager."""
    m = SkillManager()
    assert m._metrics_file.is_relative_to(tmp_path), (
        f'manager wrote to {m._metrics_file}, outside the per-test tmp tree')
    assert m._metrics_file.resolve() != PRODUCTION_STORE.resolve()


def test_env_override_is_honoured_by_init(tmp_path, monkeypatch):
    target = tmp_path / 'custom' / 'metrics.json'
    monkeypatch.setenv('AGENT_CASCADE_SKILLS_METRICS_FILE', str(target))
    assert SkillManager()._metrics_file == target


def test_empty_env_value_falls_back_to_default(monkeypatch):
    """Empty string means 'unset' (0/empty = off), not a path of ''."""
    monkeypatch.setenv('AGENT_CASCADE_SKILLS_METRICS_FILE', '')
    assert SkillManager()._metrics_file == _DEFAULT_METRICS_FILE


def test_flush_writes_only_to_the_configured_path(tmp_path):
    """A flush must not touch any other metrics file on disk."""
    target = tmp_path / 'store' / 'skills-metrics.json'
    m = SkillManager()
    m._metrics_file = target
    m._metrics = {'demo-skill': {'total_loads': 1, 'status': 'active'}}

    m._flush_metrics_to_disk()

    assert target.exists()
    data = json.loads(target.read_text(encoding='utf-8'))
    assert data['schema_version'] == '1.3'
    assert data['skills']['demo-skill']['status'] == 'active'


# ── temp-name uniqueness (defect 2) ──────────────────────────────────────────

def test_tmp_sibling_is_unique_in_target_dir():
    dst = _DEFAULT_METRICS_FILE
    first, second = _tmp_sibling(dst), _tmp_sibling(dst)

    assert first != second, 'two writers must never share one temp name'
    assert first.parent == dst.parent, 'temp file must stay on the target volume'
    assert first.name.startswith(dst.name)
    assert first.name.endswith('.tmp')
    assert str(os.getpid()) in first.name


def test_each_flush_uses_a_distinct_temp_name(tmp_path, monkeypatch):
    """Regression for the shared-``.tmp`` race: consecutive flushes must not reuse a name."""
    import agent_cascade.skills.manager as mgr

    target = tmp_path / 'skills-metrics.json'
    m = SkillManager()
    m._metrics_file = target
    m._metrics = {'demo-skill': {'total_loads': 1, 'status': 'active'}}

    seen = []
    real = mgr._tmp_sibling

    def spy(dst):
        path = real(dst)
        seen.append(path.name)
        return path

    monkeypatch.setattr(mgr, '_tmp_sibling', spy)
    m._flush_metrics_to_disk()
    m._flush_metrics_to_disk()

    assert len(seen) == 2
    assert seen[0] != seen[1], f'both flushes used the same temp name: {seen[0]}'


def test_concurrent_flushes_leave_no_orphans_and_a_valid_file(tmp_path):
    """Two managers flushing the same store concurrently must both succeed cleanly."""
    target = tmp_path / 'skills-metrics.json'
    managers = []
    for name in ('skill-a', 'skill-b'):
        m = SkillManager()
        m._metrics_file = target
        m._metrics = {name: {'total_loads': 1, 'status': 'active'}}
        managers.append((m, name))

    barrier = threading.Barrier(len(managers))
    errors = []

    def run(manager):
        try:
            barrier.wait(timeout=10)
            manager._flush_metrics_to_disk()
        except Exception as exc:  # noqa: BLE001 — the point is to surface anything raised
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m, _ in managers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), 'flush threads hung'
    assert not errors, f'flush raised: {errors}'
    assert not list(target.parent.glob('*.tmp')), 'leftover temp file after concurrent flush'

    # Last writer wins, but the file must always be one complete valid snapshot.
    data = json.loads(target.read_text(encoding='utf-8'))
    assert data['schema_version'] == '1.3'
    assert set(data['skills']) in ({'skill-a'}, {'skill-b'})
