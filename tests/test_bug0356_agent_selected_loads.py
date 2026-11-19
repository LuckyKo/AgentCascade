"""BUG_0356 — agent_selected_loads is a deliberate subset of total_loads.

Covers the WRITER side and the additive-field mechanics:
  - the ledger bumps ``agent_selected_loads`` only when ``agent_selected=True``;
  - readers tolerate a record that lacks the additive field (.get(...,0)) and the flush
    keeps ``schema_version == '1.3'`` (no schema bump / no migration);
  - ``_load_skill_bodies`` threads ``agent_selected=True`` only for a DELIBERATE skill list
    (native list AND JSON-encoded string), and False for AUTO.

The precision/bandit reader re-ranking is covered in the two memory_hint test files; these
tests pin the producer contract and the no-migration guarantee. See [[bug0189-skill-hint-precision-ledger-wiring]].
"""
import json

from agent_cascade.skills.manager import SkillManager, _default_metrics_entry, _is_explicit_skill_list


def _isolated_manager(tmp_path):
    """A SkillManager whose metrics file points at tmp_path so any buffered flush is
    hermetic. The counter-only tests do not discover skills, so resetting _metrics is enough."""
    m = SkillManager()
    m._metrics_file = tmp_path / 'skills-metrics.json'
    with m._metrics_lock:
        m._metrics = {}
    return m


def test_agent_selected_loads_bumps_only_when_deliberate(tmp_path):
    """agent_selected=True bumps BOTH total_loads and agent_selected_loads; False bumps
    total_loads only — the whole point of the deliberate-subset counter."""
    m = _isolated_manager(tmp_path)

    m._increment_load_count('s', '1.0.0', agent_selected=True)    # deliberate (load_skill tool / explicit list)
    m._increment_load_count('s', '1.0.0', agent_selected=False)   # harness auto-load
    m._increment_load_count('s', '1.0.0', agent_selected=False)   # harness auto-load

    e = m.get_metrics('s')
    assert e['total_loads'] == 3, 'total_loads counts every load (superset)'
    assert e['agent_selected_loads'] == 1, 'agent_selected_loads counts ONLY deliberate loads'
    assert e['by_version']['1.0.0'] == 3, 'per-version bucket still counts all loads'


def test_missing_agent_selected_field_reads_zero_and_flush_keeps_schema_1_3(tmp_path):
    """A legacy hand-built record WITHOUT the additive field must not crash: the writer
    increments via .get(...,0) and the flush round-trips with schema_version still '1.3'
    (BUG_0356 added an additive field, NO schema bump and NO migration rename)."""
    m = _isolated_manager(tmp_path)

    # A fresh canonical entry already carries the field (additive, default 0).
    assert _default_metrics_entry()['agent_selected_loads'] == 0, \
        'the default entry must include the additive field'

    # Simulate a legacy / hand-built record that predates the field (BUG_0312 pattern).
    with m._metrics_lock:
        m._metrics['legacy'] = {'total_loads': 7, 'hints': 2, 'by_version': {}}

    # Bumping the deliberate counter on that field-missing record must not KeyError.
    m._increment_load_count('legacy', '1.0.0', agent_selected=True)

    e = m.get_metrics('legacy')
    assert e['total_loads'] == 8, 'total_loads still increments on a legacy record'
    assert e['agent_selected_loads'] == 1, '.get(...,0) must tolerate the missing field on write'

    m._flush_metrics_to_disk()
    data = json.loads(m._metrics_file.read_text(encoding='utf-8'))
    assert data['schema_version'] == '1.3', 'BUG_0356: additive field — schema stays 1.3, no bump'
    assert data['skills']['legacy']['agent_selected_loads'] == 1, \
        'the new per-skill key rides along in the deepcopy flush verbatim'


def test_load_skill_bodies_threads_explicit_only_not_auto(tmp_path, monkeypatch):
    """The two deliberate counters are gated at the _load_skill_bodies seam: a DELIBERATE
    skill list (native list OR a JSON-encoded string list) passes agent_selected=True;
    AUTO passes False. Records the per-call agent_selected flag by wrapping the loader."""
    m = _isolated_manager(tmp_path)

    calls = []

    def fake_resolve(value, task_text='', context_text=''):
        return ['s']   # pretend the name resolves to exactly one skill

    def fake_load(name, count_load=True, agent_selected=False):
        calls.append({'name': name, 'count_load': count_load, 'agent_selected': agent_selected})
        return 'body'

    monkeypatch.setattr(m, '_resolve_skill_names', fake_resolve)
    monkeypatch.setattr(m, 'load_full_instructions', fake_load)

    # native list → deliberate
    calls.clear()
    m._load_skill_bodies(['s'])
    assert calls and all(c['agent_selected'] is True for c in calls), \
        'native list must load with agent_selected=True'

    # JSON-encoded string array (the LLM sometimes emits '["s"]') → deliberate
    calls.clear()
    m._load_skill_bodies('["s"]')
    assert calls and all(c['agent_selected'] is True for c in calls), \
        'JSON-string list must load with agent_selected=True (predicate string branch)'

    # AUTO keyword match → NOT deliberate
    calls.clear()
    m._load_skill_bodies('AUTO')
    assert calls and all(c['agent_selected'] is False for c in calls), \
        'AUTO must load with agent_selected=False'


def test_is_explicit_skill_list_predicate(tmp_path):
    """Pin the canonical predicate the seam relies on (shared with engine.system_agent):
    native list and JSON-string array are explicit; bare AUTO/NONE/None/invalid are not."""
    assert _is_explicit_skill_list(['a', 'b']) is True
    assert _is_explicit_skill_list('["a"]') is True
    assert _is_explicit_skill_list('AUTO') is False
    assert _is_explicit_skill_list('NONE') is False
    assert _is_explicit_skill_list('not json') is False
    assert _is_explicit_skill_list(None) is False
