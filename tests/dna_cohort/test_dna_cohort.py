"""PHASE-1 deterministic-core tests for ``agent_cascade.dna_cohort``.

All fixtures are small temp-dir corpora (never the live 1430-file telemetry and
never production config), per AGENTS.md test discipline. Constants are imported
from the code under test rather than hardcoded (single source of truth).
"""
import json
import os
import re

import pytest

from agent_cascade import dna_cohort as dc


# ── Fixture helpers ──────────────────────────────────────────────────────────

def _turn_end(ph, sid_hash=None, *, llm_calls=1, tokens_in=100, tokens_out=50,
              loops=0, retries=0, duration_ms=1000, dna_version='1.1.0'):
    return {'type': 'turn_end', 'prompt_hash': ph, 'dna_version': dna_version,
            'llm_calls': llm_calls, 'input_tokens_est': tokens_in,
            'output_tokens_est': tokens_out, 'loops_detected': loops,
            'retries': retries, 'duration_ms': duration_ms, 'timestamp': 'T'}


def _session_start(ph, session_id, dna_version='1.1.0'):
    return {'type': 'session_start', 'prompt_hash': ph, 'dna_version': dna_version,
            'session_id': session_id}


def _tool_call(ph, success=True, tool_name='read_file', dna_version='1.1.0'):
    return {'type': 'tool_call', 'prompt_hash': ph, 'dna_version': dna_version,
            'success': success, 'tool_name': tool_name}


def _llm_call(ph, model='m1', wasted=0, tokens_in=10, tokens_out=5, dna_version='1.1.0'):
    return {'type': 'llm_call', 'prompt_hash': ph, 'dna_version': dna_version,
            'model': model, 'wasted_tokens': wasted,
            'input_tokens_est': tokens_in, 'output_tokens_est': tokens_out}


def _write(fp, records):
    """Write records as JSONL. Records without ``session_id`` keep the §3-S
    file-fallback session key applied by ``scan_telemetry_with_totals``."""
    with open(fp, 'w', encoding='utf-8') as fh:
        for rec in records:
            fh.write(json.dumps(rec) + '\n')


def _corpus(tmp_path, files):
    """Write a dict {filename: [records]} into a temp telemetry dir and return
    the dir path."""
    d = str(tmp_path / 'telemetry')
    os.makedirs(d, exist_ok=True)
    for name, recs in files.items():
        _write(os.path.join(d, name), recs)
    return d


def _row(**kw):
    """A poolable, non-ghost baseline-shaped CohortRow; override per test."""
    base = dict(prompt_hash='base', dna_version_label='1.1.0', n_sessions=15, tasks=30,
                tool_acc=98.0, edit_acc=96.0, llm_calls_per_task=10.0,
                coder_review_per_task=2.0, waste_per_session=5000.0,
                loops_per_task=0.0, retries_per_task=0.1, latency_p50_ms=1000.0,
                cost_per_task=10000.0, model_mix={'m1': 100}, ghost=False,
                first_seen='T0', last_seen='T9')
    base.update(kw)
    return dc.CohortRow(**base)


def _decision(status='READY', failing=(), deltas=None):
    return dc.GateDecision(status=status, failing=tuple(failing),
                           reasoning='', deltas=deltas or {})


# ── §5 — anti-undercounting + determinism ───────────────────────────────────

def test_scan_full_no_undercount(tmp_path):
    """One cohort's sessions live in the OLDEST, MIDDLE and NEWEST files; a full
    scan must find ALL of them (kills the undercount bug class). Malformed lines
    and unlabeled records are skipped without dropping real ones."""
    d = _corpus(tmp_path, {
        'telemetry_20260101_000000.jsonl': [  # oldest
            _session_start('AAAA', 'S1'), _turn_end('AAAA'), _tool_call('AAAA')],
        'telemetry_20260102_000000.jsonl': [  # the historically "missed" middle file
            _session_start('BBBB', 'S2'), _turn_end('AAAA'),
            _turn_end('BBBB')],
        'telemetry_20260103_000000.jsonl': [  # newest
            _session_start('AAAA', 'S3'), _turn_end('AAAA')],
        'telemetry_20260104_000000.jsonl': [  # junk + unlabeled, must be tolerated
            '{not json}', '',  # malformed + blank line -> skipped
            {'type': 'turn_end', 'dna_version': '1.1.0'},  # NO prompt_hash -> dropped
            _turn_end('CCCC'),  # real labeled cohort, survives the noise
        ],
    })
    recs = dc.scan_telemetry_with_totals(d)[0]
    assert all(r.get('prompt_hash') for r in recs)  # nothing unlabeled survived
    rows = dc.count_cohorts(recs)
    # 'AAAA' spans 3 distinct session files -> 3 distinct sessions (not undercounted).
    assert rows['AAAA'].n_sessions == 3
    assert rows['BBBB'].n_sessions == 1


def test_n_is_correct_and_stable(tmp_path):
    """Two independent full-scan runs over identical records produce identical
    N per cohort (determinism)."""
    d = _corpus(tmp_path, {
        'telemetry_a.jsonl': [_session_start('X', 'SA'), _turn_end('X'), _turn_end('X')],
        'telemetry_b.jsonl': [_session_start('Y', 'SB'), _turn_end('X'), _turn_end('Y')],
        'telemetry_c.jsonl': [_session_start('Y', 'SC'), _turn_end('Y')],
    })
    first = {h: r.n_sessions for h, r in dc.count_cohorts(dc.scan_telemetry_with_totals(d)[0]).items()}
    second = {h: r.n_sessions for h, r in dc.count_cohorts(dc.scan_telemetry_with_totals(d)[0]).items()}
    assert first == second
    assert first == {'X': 2, 'Y': 2}  # each cohort seen in two distinct session files


def test_session_identity_multi_hash_file(tmp_path):
    """One session_id carrying two hashes counts toward BOTH cohorts (§3-S) and
    is not double-counted within a single cohort."""
    d = _corpus(tmp_path, {
        'telemetry_Multi.jsonl': [
            _session_start('H1', 'S1'),
            _turn_end('H1'), _turn_end('H1'),
            _turn_end('H2'), _turn_end('H2'), _turn_end('H2'),
        ],
    })
    rows = dc.count_cohorts(dc.scan_telemetry_with_totals(d)[0])
    assert rows['H1'].n_sessions == 1   # the single session counts once for H1
    assert rows['H2'].n_sessions == 1   # ...and once for H2 (not conflated/omitted)
    assert rows['H1'].tasks == 2
    assert rows['H2'].tasks == 3


# ── §Q4 — cost single-level rule ────────────────────────────────────────────

def test_cost_single_level(tmp_path):
    """G-C1 cost/task sums turn_end token estimates ONLY — never the turn_end +
    llm_call sum (double-count)."""
    turns = [_turn_end('C', tokens_in=100, tokens_out=50),
             _turn_end('C', tokens_in=200, tokens_out=100)]
    # llm_call token estimates that MUST NOT be added into the cost metric.
    recs = [_session_start('C', 'S1')] + turns + [
        _llm_call('C', tokens_in=1000, tokens_out=500),
        _llm_call('C', tokens_in=1000, tokens_out=500),
    ]
    d = _corpus(tmp_path, {'telemetry_c.jsonl': recs})
    row = dc.count_cohorts(dc.scan_telemetry_with_totals(d)[0])['C']
    turn_only = sum(r['input_tokens_est'] + r['output_tokens_est'] for r in turns) / 2
    double_count = (sum(r['input_tokens_est'] + r['output_tokens_est'] for r in turns)
                    + sum(r['input_tokens_est'] + r['output_tokens_est'] for r in recs
                          if r['type'] == 'llm_call')) / 2
    assert row.cost_per_task == pytest.approx(turn_only)
    assert row.cost_per_task != pytest.approx(double_count)


# ── §3-S/X/Y — ghost + empty baseline ───────────────────────────────────────

def test_ghost_drop(tmp_path):
    """A ghost cohort (llm_calls==0) is dropped from A/B pooling but still has an
    aggregate row (N surfaced)."""
    d = _corpus(tmp_path, {
        'telemetry_base.jsonl': [_session_start('BASE', 'SB'), _turn_end('BASE')],
        'telemetry_ghost.jsonl': [_session_start('GHOST', 'SG'), _turn_end('GHOST', llm_calls=0)],
    })
    rows = dc.count_cohorts(dc.scan_telemetry_with_totals(d)[0])
    assert rows['GHOST'].ghost is True
    pairs = dc.pool_ab(rows, 'BASE')
    assert [p[0].prompt_hash for p in pairs] == []  # ghost excluded from A/B


def test_empty_baseline_hold(tmp_path):
    """An empty / missing baseline yields HOLD (P0), never a ZeroDivisionError."""
    cand = _row(prompt_hash='C', n_sessions=12)
    empty_base = _row(prompt_hash='BASE', tasks=0, ghost=True)
    for base in (None, empty_base):
        dec = dc.run_gate(cand, base, dc.GATE_THRESHOLDS)
        assert dec.status == dc.HOLD
        assert 'P0:baseline-not-poolable' in dec.failing


# ── §5 — gate math table ────────────────────────────────────────────────────

BASELINE = _row()  # frozen reference baseline for the table

@pytest.mark.parametrize('candidate,flags,expected_status,expected_failing', [
    # N below the poolable band -> ACCUMULATING (count only).
    (_row(n_sessions=6), {}, dc.ACCUMULATING, ('P1:N<10',)),
    # N=15, every hard gate + composite passes, diff ready + single-variable -> CUT.
    (_row(), dict(candidate_diff_ready=True, single_variable=True), dc.CUT_CANDIDATE, ()),
    # tool_acc -4 pts -> hard-fail G-H2.
    (_row(tool_acc=94.0), {}, dc.HOLD, ('G-H2:tool_acc',)),
    # cycles/task +25% -> hard-fail G-H2.
    (_row(coder_review_per_task=2.5), {}, dc.HOLD, ('G-H2:cycles',)),
    # loops/task materially up -> hard-fail G-H1.
    (_row(loops_per_task=0.30), {}, dc.HOLD, ('G-H1:loops',)),
    # cost +16% (just over cap) but llm-calls/task -20% -> G-C1 efficiency release PASSES.
    (_row(cost_per_task=11600.0, llm_calls_per_task=8.0), {}, dc.READY, ()),
    # control: cost +25% with only -5% efficiency -> G-C1 fails -> HOLD (discriminating).
    (_row(cost_per_task=12500.0, llm_calls_per_task=9.5), {}, dc.HOLD, ('G-C1:cost-efficiency',)),
])
def test_gate_math_table(candidate, flags, expected_status, expected_failing):
    dec = dc.run_gate(candidate, BASELINE, dc.GATE_THRESHOLDS, **flags)
    assert dec.status == expected_status
    for frag in expected_failing:
        assert any(f.startswith(frag) for f in dec.failing), (dec.failing, frag)
    # exactly four statuses are reachable; no dead enum value.
    assert dec.status in {dc.ACCUMULATING, dc.HOLD, dc.READY, dc.CUT_CANDIDATE}


# ── §Q5 — labeling gap ──────────────────────────────────────────────────────

def test_labeling_gap():
    cohorts = {}
    for i in range(5):  # five distinct hashes, all labeled "1.1.0"
        h = f'aaaa{0000000 + i:08d}'
        cohorts[h] = _row(prompt_hash=h, dna_version_label='1.1.0')
    cohorts['singlehash'] = _row(prompt_hash='singlehash', dna_version_label='1.0.0')
    gaps = dc.detect_labeling_gap(cohorts)
    assert len(gaps) == 1  # only the "1.1.0" label is over-used
    gap = gaps[0]
    assert gap.dna_version == '1.1.0'
    assert len(gap.hashes) == 5
    assert gap.proposed_bump == '1.2.0'


# ── §5 — report determinism ─────────────────────────────────────────────────

def _report_inputs():
    rows = {
        'BASE': _row(prompt_hash='BASE'),
        'AAAA': _row(prompt_hash='AAAA', n_sessions=12, tool_acc=99.5),
        'BBBB': _row(prompt_hash='BBBB', n_sessions=11, cost_per_task=12000.0),
        'GHOST': _row(prompt_hash='GHOST', n_sessions=2, ghost=True),
    }
    decisions = {
        'AAAA': _decision(dc.HOLD, ('G-H2:tool_acc+1.5pts',), {'tool_acc': (99.5, 98.0)}),
        'BBBB': _decision(dc.READY, (), {'cost_per_task': (12000.0, 10000.0)}),
    }
    gaps = [dc.LabelingGap(dna_version='1.1.0', hashes=('AAAA', 'BBBB', 'BASE'),
                           proposed_bump='1.2.0')]
    return rows, decisions, gaps


def test_report_deterministic():
    rows, decisions, gaps = _report_inputs()
    a = dc.build_report(rows, decisions, gaps, 'BASE', generated_at='2026-11-27 00:00:00Z',
                        coverage={'labeled': 100, 'total': 100, 'files': 4})
    b = dc.build_report(rows, decisions, gaps, 'BASE', generated_at='2026-11-27 00:00:00Z',
                        coverage={'labeled': 100, 'total': 100, 'files': 4})
    assert a == b  # byte-identical across runs (sorted + fixed field order)


# ── §5 — note append-only ───────────────────────────────────────────────────

def test_note_append_only():
    old_note = ('# DNA-Telemetry Accumulation State\n\n'
                '## History\n- old pass N=1\n- older pass N=2\n')
    rows, decisions, gaps = _report_inputs()
    updated = dc.update_note(old_note, rows, decisions, gaps, generated_at='2026-11-27 00:00:00Z')
    # History is never deleted.
    assert '- old pass N=1' in updated
    assert '- older pass N=2' in updated
    # A state block was added with cohort status.
    assert dc._NOTE_BEGIN in updated and dc._NOTE_END in updated
    assert '`AAAA` N=12' in updated
    # Idempotent: re-running on the already-managed note keeps history and is stable.
    again = dc.update_note(updated, rows, decisions, gaps, generated_at='2026-11-27 00:00:00Z')
    assert '- old pass N=1' in again and '- older pass N=2' in again
    assert again.count(dc._NOTE_BEGIN) == 1  # in-place refresh, no duplicated block


# ── §5 — dry-run leaves the disk untouched (no manual-pipeline regression) ──

def test_run_and_report_dry_run_no_writes(tmp_path):
    d = _corpus(tmp_path, {
        'telemetry_base.jsonl': [_session_start('BASE', 'SB'), _turn_end('BASE')],
        'telemetry_cand.jsonl': [_session_start('CAND', 'SC'),
                                  _turn_end('CAND'), _turn_end('CAND')],
    })
    workspace = str(tmp_path / 'workspace')
    os.makedirs(workspace, exist_ok=True)
    note_path = os.path.join(workspace, 'dna-telemetry-accumulation.md')
    sentinel = 'PRE-EXISTING HISTORY — MUST NOT CHANGE\n'
    with open(note_path, 'w', encoding='utf-8') as fh:
        fh.write(sentinel)

    report = dc.run_and_report(d, workspace_root=workspace, baseline_hash='BASE',
                               stage_cut=False, dry_run=True)
    assert isinstance(report, str)
    assert '# DNA cohort results report' in report
    # Note untouched, no managed block written, no cut-prep dir created.
    with open(note_path, encoding='utf-8') as fh:
        assert fh.read() == sentinel
    assert not os.path.exists(os.path.join(workspace, 'dna_cut_prep'))


def test_prepare_cut_is_phase2_notimplemented(tmp_path):
    """PHASE-2 seams must refuse to run rather than silently no-op a cut."""
    with pytest.raises(NotImplementedError):
        dc.prepare_cut('hash', str(tmp_path), _decision())
    with pytest.raises(NotImplementedError):
        dc.apply_approved_cut(dc.CutArtifact('h', 'p', 'c', 'e', 'NEW'))
    with pytest.raises(NotImplementedError):
        dc.run_and_report(str(tmp_path), workspace_root=str(tmp_path), stage_cut=True)


def test_coverage_reports_real_total_including_unlabeled(tmp_path):
    """The 'unlabeled N dropped' line must reflect a REAL total, not the old
    always-zero case where total was hard-set == labeled. Coverage numbers are
    DERIVED from the rendered report (not injected), against a fixture of
    3 labeled + 2 unlabeled (prompt_hash-less) records -> total=5, labeled=3,
    dropped=2."""
    labeled = [_session_start('BASE', 'SB'), _turn_end('BASE'), _turn_end('BASE')]
    unlabeled = [  # valid JSON dicts but NO prompt_hash -> dropped, still counted
        {'type': 'turn_end', 'session_id': 'X1', 'timestamp': 'T'},
        {'type': 'tool_call', 'session_id': 'X2', 'success': True},
    ]
    d = _corpus(tmp_path, {'telemetry_mixed.jsonl': labeled + unlabeled})

    # Helper-level: returns (kept_records, total_records_seen).
    kept, total_seen = dc.scan_telemetry_with_totals(d)
    assert len(kept) == 3          # only prompt_hash-labeled records are kept
    assert total_seen == 5          # every valid dict record is counted
    # Report-level: coverage is derived from the rendered line.
    report = dc.run_and_report(d, workspace_root=str(tmp_path),
                               baseline_hash='BASE', dry_run=True)
    m = re.search(r'coverage: (\d+) labeled records over (\d+) files scanned '
                  r'\(unlabeled (\d+) dropped\)', report)
    assert m is not None, 'coverage line missing from report'
    labeled_n, files_n, dropped_n = (int(x) for x in m.groups())
    assert labeled_n == 3
    assert dropped_n == 2           # non-zero: the previously-always-zero line
    assert labeled_n + dropped_n == total_seen  # total = labeled + dropped
    assert files_n == 1
