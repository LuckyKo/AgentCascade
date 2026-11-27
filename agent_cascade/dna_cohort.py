"""BUG_0401 — DNA-telemetry accumulation pipeline: deterministic core (PHASE 1).

A pure, side-effect-free analysis core that replaces the manual, LLM-in-the-loop
DNA-telemetry accumulation pass:

    scan -> count cohorts (by prompt_hash) -> pool A/B -> run the promotion gate
    -> render a decision-oriented report -> deterministically update the note.

Design contract (see plan ``BUG_0401_dna_cohort_pipeline.md`` §1-§3):
- NO LLM, NO agent import at analysis time. Pure stdlib only, so it is fully
  deterministic and unit-testable.
- The only human-in-the-loop is the promotion cut itself; PHASE 1 never writes a
  cut. ``prepare_cut`` is a documented PHASE-2 seam and raises on use.
- All gate thresholds live in ONE dict (``GATE_THRESHOLDS``) and the frozen
  baseline is a single config constant (``DEFAULT_BASELINE_HASH``) — never
  auto-rotated. Every metric delta is computed against that baseline.

The cohort key is always ``prompt_hash`` (content truth), NEVER ``dna_version``
(which can lie: many distinct hashes share one version label — surfaced as a
``LabelingGap``, never hidden).
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import statistics
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Single source of truth: the frozen baseline and every gate threshold.
# Changing the baseline is a HUMAN policy decision (the cohorts that retired as
# READY-POOLED), not an automation decision; it is never rotated at runtime.
# ---------------------------------------------------------------------------
DEFAULT_BASELINE_HASH: str = '4279a7d889d66c35'

GATE_THRESHOLDS: dict[str, float] = {
    'P1_min_N': 10.0,                   # N>=10 distinct sessions -> poolable/readable band
    'GH2_tool_acc_floor_pts': -3.0,     # tool_acc/edit_acc must not fall more than 3 pts vs baseline
    'GH2_cycles_cap_pct': 20.0,         # coder/review cycles/task must not rise more than +20%
    'GC1_cost_cap_pct': 15.0,           # cost/task within +15%...
    'GC1_efficiency_release_pct': 20.0,  # ...OR efficiency up >=20% (count metrics must fall >=20%)
    'GC2_latency_cap_pct': 10.0,        # p50 duration/task (ADVISORY only, never changes status)
    'GH1_regres_pct': 20.0,             # loops/retries/task "materially up" = >+20% (framework noise floor)
}

# The four gate statuses (exact set — no dead enum value).
ACCUMULATING = 'ACCUMULATING'
HOLD = 'HOLD'
READY = 'READY'
CUT_CANDIDATE = 'CUT_CANDIDATE'

# Metrics the gate/report consume, in fixed render order. Higher-is-better ones
# are flagged so the templated narrative can classify better/worse correctly.
HIGHER_IS_BETTER: frozenset[str] = frozenset({'tool_acc', 'edit_acc'})
METRIC_ORDER: tuple[str, ...] = (
    'tool_acc', 'edit_acc', 'llm_calls_per_task', 'coder_review_per_task',
    'waste_per_session', 'loops_per_task', 'retries_per_task',
    'latency_p50_ms', 'cost_per_task',
)

# Telemetry record types that contribute to cohort aggregation. Everything else
# (turn_start, telemetry_stats, hint_posted, session_start/end, ...) is ignored
# for metric summation — session_start/telemetry_stats are NOT sessions.
_AGG_TYPES: frozenset[str] = frozenset({
    'turn_end', 'tool_call', 'llm_call', 'agent_instance_call',
    'compression', 'loop_detected',
})

# Sentinel-delimited auto-managed region inside dna-telemetry-accumulation.md.
# Everything outside these markers is history and is NEVER deleted or rewritten.
_NOTE_BEGIN = '<!-- dna_cohort:auto:begin -->'
_NOTE_END = '<!-- dna_cohort:auto:end -->'


# ---------------------------------------------------------------------------
# Typed result dataclasses (pure, comparable -> unit-testable).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CohortRow:
    """Per-cohort (per ``prompt_hash``) aggregate. All ``*_per_*`` fields are
    normalized per task (or per session for waste); ``cost_per_task`` sums the
    ``turn_end`` token estimates at ONE level only (single-level rule, G-C1)."""
    prompt_hash: str
    dna_version_label: str          # display only; NEVER a grouping key
    n_sessions: int
    tasks: int
    tool_acc: float
    edit_acc: float
    llm_calls_per_task: float
    coder_review_per_task: float
    waste_per_session: float
    loops_per_task: float
    retries_per_task: float
    latency_p50_ms: float
    cost_per_task: float            # sum(turn_end.{input,output}_tokens_est)/tasks
    model_mix: dict[str, int]       # model -> llm_call count (P2 confound)
    ghost: bool                     # total llm_calls == 0 -> drop from A/B
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class GateDecision:
    """Outcome of ``run_gate``. ``deltas`` maps metric -> (candidate, baseline)."""
    status: str
    failing: tuple[str, ...]
    reasoning: str
    deltas: dict[str, tuple[float, float]]


@dataclass(frozen=True)
class LabelingGap:
    """One ``dna_version`` label mapping to >1 distinct ``prompt_hash``."""
    dna_version: str
    hashes: tuple[str, ...]
    proposed_bump: str


@dataclass(frozen=True)
class CutArtifact:
    """PHASE-2 placeholder (Q3). Not produced in PHASE 1 — see ``prepare_cut``."""
    prompt_hash: str
    cut_patch_path: str
    canary_plan_path: str
    gate_evidence_path: str
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_div(num: float, den: float) -> float:
    """Division guarding a zero denominator -> 0.0 (never raises)."""
    return float(num) / den if den else 0.0


def _rel_pct(new: float, old: float) -> float:
    """Relative % change of ``new`` vs ``old``. Zero baseline -> +/-inf when
    non-zero (a 'lower-is-better' rise from 0 is unambiguously worse)."""
    if old == 0:
        if new == 0:
            return 0.0
        return float('inf') if new > 0 else float('-inf')
    return (new - old) / abs(old) * 100.0


def _next_minor(version: str) -> str:
    """Next MINOR version per the dna.py bump convention (additive change ->
    MINOR). ``1.1.0`` -> ``1.2.0``. Non-semver labels fall back to a suffixed
    bump rather than raising, so a malformed label never aborts analysis."""
    parts = version.split('.')
    if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]):
        major, minor = int(parts[0]), int(parts[1])
        return f'{major}.{minor + 1}.0'
    return f'{version}+bump'


def _resolve_session_id(records: list[dict]) -> str:
    """The session_id for a file: prefer the one on ``session_start``, then any
    record that carries one; fall back to a file-derived key so multi-hash
    sessions still group deterministically (session_id is never on turn records)."""
    for t in ('session_start', 'session_end', 'telemetry_stats'):
        for r in records:
            if r.get('type') == t and r.get('session_id'):
                return r['session_id']
    for r in records:
        if r.get('session_id'):
            return r['session_id']
    return ''  # caller substitutes the file key


# ---------------------------------------------------------------------------
# LAYER 1: pure analysis core (no side effects, fully unit-testable).
# ---------------------------------------------------------------------------
def scan_telemetry_with_totals(telemetry_dir: str, *,
                               since: str | None = None) -> tuple[list[dict], int]:
    """Full-scan EVERY ``telemetry_*.jsonl`` (never a newest-N subset — that is
    the undercounting bug class this module eliminates). Malformed/blank lines
    are skipped (log-and-continue). Returns ``(records, total_seen)`` where
    ``records`` keeps only prompt_hash-labeled records (each carrying ``_file``
    and ``_session_id``, the file's session resolved from its ``session_start``)
    and ``total_seen`` counts every valid JSON-object record read — labeled or
    not — so callers can report how many unlabeled records were dropped without
    a second scan. ``since`` is an optional ISO timestamp lower bound.
    """
    records: list[dict] = []
    total_seen = 0
    # Deterministic order over an alphabetically sorted file list.
    for fp in sorted(glob.glob(os.path.join(telemetry_dir, 'telemetry_*.jsonl'))):
        file_records: list[dict] = []
        try:
            with open(fp, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except (ValueError, TypeError):
                        continue  # malformed line — never crash a full scan
                    if not isinstance(o, dict):
                        continue
                    total_seen += 1  # a real record, whether labeled or not
                    if not o.get('prompt_hash'):
                        continue  # unlabeled — counted in total_seen, not kept
                    if since and (o.get('timestamp') or '') < since:
                        continue
                    file_records.append(o)
        except OSError:
            continue  # unreadable file — skip, keep scanning
        sid = _resolve_session_id(file_records) or fp
        for o in file_records:
            o['_file'] = fp
            o['_session_id'] = sid
            records.append(o)
    return records, total_seen


def count_cohorts(records: list[dict]) -> dict[str, CohortRow]:
    """Group by ``prompt_hash`` and build one aggregate row per cohort.

    Session rule (§3-S): ``n_sessions`` = #distinct session_ids carrying >=1
    ``turn_end`` for that hash. A multi-hash session (mid-session DNA rollover)
    counts toward EACH cohort it has turn activity in — this never undercounts
    and is fully deterministic. (Majority-only attribution is a named TODO, not
    guessed.) All per-task normalization happens here.
    """
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_hash[r['prompt_hash']].append(r)

    rows: dict[str, CohortRow] = {}
    for ph, rs in by_hash.items():
        turns = [r for r in rs if r.get('type') == 'turn_end']
        tools = [r for r in rs if r.get('type') == 'tool_call']
        edits = [r for r in tools if r.get('tool_name') == 'edit_file']
        llm = [r for r in rs if r.get('type') == 'llm_call']
        cycles = sum(
            1 for r in rs
            if r.get('type') == 'agent_instance_call'
            and r.get('agent_class') in ('coder', 'reviewer')
        )

        tasks = len(turns)
        total_llm_calls = sum(r.get('llm_calls', 0) for r in turns)
        ghost = total_llm_calls == 0

        # §3-S: a session counts iff it has >=1 turn_end with this hash. Records
        # are expected from scan_telemetry_with_totals, which always stamps _session_id.
        session_ids = {r.get('_session_id') or r.get('_file', '') for r in turns}
        n_sessions = len({s for s in session_ids if s})

        tool_acc = 100.0 * sum(1 for r in tools if r.get('success')) / len(tools) if tools else 0.0
        edit_acc = 100.0 * sum(1 for r in edits if r.get('success')) / len(edits) if edits else 0.0

        waste = sum(r.get('wasted_tokens', 0) for r in llm)
        loops = (sum(r.get('loops_detected', 0) for r in turns)
                 + sum(1 for r in rs if r.get('type') == 'loop_detected'))
        retries = sum(r.get('retries', 0) for r in turns)
        durations = [r.get('duration_ms', 0) for r in turns if r.get('duration_ms') is not None]
        latency_p50 = statistics.median(durations) if durations else 0.0
        # Cost single-level rule (G-C1): turn_end token estimates ONLY.
        cost = sum(r.get('input_tokens_est', 0) + r.get('output_tokens_est', 0) for r in turns)

        ts = [r.get('timestamp') for r in rs if r.get('timestamp')]
        first_seen = min(ts) if ts else ''
        last_seen = max(ts) if ts else ''

        # dna_version is stable per hash within a run; take the deterministic min.
        versions = {r.get('dna_version') for r in rs if r.get('dna_version')}
        label = min(versions) if versions else '?'

        rows[ph] = CohortRow(
            prompt_hash=ph,
            dna_version_label=label,
            n_sessions=n_sessions,
            tasks=tasks,
            tool_acc=tool_acc,
            edit_acc=edit_acc,
            llm_calls_per_task=_safe_div(total_llm_calls, tasks),
            coder_review_per_task=_safe_div(cycles, tasks),
            waste_per_session=_safe_div(waste, n_sessions),
            loops_per_task=_safe_div(loops, tasks),
            retries_per_task=_safe_div(retries, tasks),
            latency_p50_ms=latency_p50,
            cost_per_task=_safe_div(cost, tasks),
            model_mix=dict(Counter(r.get('model') for r in llm if r.get('model'))),
            ghost=ghost,
            first_seen=first_seen,
            last_seen=last_seen,
        )
    return rows


def pool_ab(candidates: dict[str, CohortRow], baseline_hash: str
            ) -> list[tuple[CohortRow, CohortRow | None]]:
    """Pair each poolable non-baseline cohort with the frozen baseline row and
    drop ghost cohorts (P2). Returns sorted-by-hash ``(candidate, baseline | None)``
    tuples. Ghost cohorts are excluded here but still reported (N surfaced) by
    ``build_report``.
    """
    baseline = candidates.get(baseline_hash)
    pairs: list[tuple[CohortRow, CohortRow | None]] = []
    for ph, row in candidates.items():
        if ph == baseline_hash or row.ghost:
            continue
        pairs.append((row, baseline))
    return sorted(pairs, key=lambda t: t[0].prompt_hash)


def run_gate(candidate: CohortRow, baseline: CohortRow | None, thresholds: dict,
             candidate_diff_ready: bool = False, single_variable: bool = False
             ) -> GateDecision:
    """Pure gate math (framework §3). Exactly four statuses, no dead enum.

    State machine:
      ACCUMULATING  P1 fails (N < P1_min_N): poolable band not reached — count only.
      HOLD          baseline not poolable (P0), OR any hard gate (G-H1/G-H2) fails,
                    OR P3 fails on a ready diff, OR composite G-C1 fails.
      READY         all gates pass, diff not yet supplied — evidence is sufficient,
                    nothing to cut yet.
      CUT_CANDIDATE all gates pass AND candidate_diff_ready AND single_variable.

    P3 (single-variable) is a human/coder assertion the pipeline cannot infer, so
    it is an input defaulting False; a diff-ready-but-unasserted cohort -> HOLD, a
    not-yet-ready cohort -> READY. G-C2 latency is advisory and never changes status.
    """
    # ``candidate_diff_ready`` / ``single_variable`` are PHASE-2 cut-staging inputs.
    # PHASE 1's ``_decide_all`` never passes them, so a fully-passing PHASE-1
    # cohort can only reach READY; ``CUT_CANDIDATE`` below is a PHASE-2-only
    # outcome. The pure function stays complete for the framework's full state
    # machine, so these flags are kept (the gate-math test exercises them).
    # P0: baseline must be poolable (non-ghost, has tasks), else HOLD with a
    # clear reason and no delta math (never divide by a zero/absent baseline).
    if baseline is None or baseline.ghost or baseline.tasks == 0:
        reason = ('baseline not poolable (P0): no completed turns under '
                  f'{baseline_hash_hint(baseline)}; cannot compute deltas')
        return GateDecision(status=HOLD, failing=('P0:baseline-not-poolable',),
                            reasoning=reason, deltas={})

    deltas = {m: (getattr(candidate, m), getattr(baseline, m)) for m in METRIC_ORDER}

    # P1: N gate. Below the poolable band, do nothing but keep counting.
    if candidate.n_sessions < thresholds['P1_min_N']:
        return GateDecision(
            status=ACCUMULATING,
            failing=(f'P1:N<{int(thresholds['P1_min_N'])}',),
            reasoning=(f'{candidate.n_sessions} sessions < poolable floor '
                       f'{int(thresholds['P1_min_N'])}; keep accumulating.'),
            deltas=deltas,
        )

    if candidate.ghost:  # P2: ghost candidate (normally filtered in pool_ab).
        return GateDecision(
            status=HOLD, failing=('P2:candidate-ghost',),
            reasoning='candidate cohort has zero completed LLM calls (ghost).',
            deltas=deltas,
        )

    failing: list[str] = []
    # G-H1: loops/retries per task not materially up.
    loops_pct = _rel_pct(candidate.loops_per_task, baseline.loops_per_task)
    retries_pct = _rel_pct(candidate.retries_per_task, baseline.retries_per_task)
    if loops_pct > thresholds['GH1_regres_pct']:
        failing.append(f'G-H1:loops+{loops_pct:.0f}%')
    if retries_pct > thresholds['GH1_regres_pct']:
        failing.append(f'G-H1:retries+{retries_pct:.0f}%')

    # G-H2: quality floor — tool_acc/edit_acc >= -3 pts, cycles/task <= +20%.
    tool_pts = candidate.tool_acc - baseline.tool_acc
    edit_pts = candidate.edit_acc - baseline.edit_acc
    cyc_pct = _rel_pct(candidate.coder_review_per_task, baseline.coder_review_per_task)
    if tool_pts < thresholds['GH2_tool_acc_floor_pts']:
        failing.append(f'G-H2:tool_acc{tool_pts:+.1f}pts')
    if edit_pts < thresholds['GH2_tool_acc_floor_pts']:
        failing.append(f'G-H2:edit_acc{edit_pts:+.1f}pts')
    if cyc_pct > thresholds['GH2_cycles_cap_pct']:
        failing.append(f'G-H2:cycles+{cyc_pct:.0f}%')

    # G-C1 composite: cost/task within +15% OR efficiency pays (>=20% fewer
    # llm-calls/task or cycles/task). Hard-fail (G-H*) dominates if present.
    cost_pct = _rel_pct(candidate.cost_per_task, baseline.cost_per_task)
    llm_pct = _rel_pct(candidate.llm_calls_per_task, baseline.llm_calls_per_task)
    gc1_release = (llm_pct <= -thresholds['GC1_efficiency_release_pct']
                   or cyc_pct <= -thresholds['GC1_efficiency_release_pct'])
    gc1_fail = not (cost_pct <= thresholds['GC1_cost_cap_pct'] or gc1_release)
    gc1_note = (f'G-C1 cost{cost_pct:+.0f}%'
                + (', efficiency release' if gc1_release and cost_pct > thresholds['GC1_cost_cap_pct'] else ''))

    # G-C2: latency — advisory only, recorded but never changes the status.
    latency_pct = _rel_pct(candidate.latency_p50_ms, baseline.latency_p50_ms)
    advisory = (f'G-C2 latency{latency_pct:+.0f}% (> +{int(thresholds['GC2_latency_cap_pct'])}% advisory)'
                if latency_pct > thresholds['GC2_latency_cap_pct'] else '')

    # Hard gates fail -> HOLD (composite-fail is logged separately below).
    if failing:
        return GateDecision(
            status=HOLD, failing=tuple(failing),
            reasoning='hard gate(s) failed: ' + '; '.join(failing) + f' | {gc1_note}.',
            deltas=deltas,
        )
    # Hard gates pass but the composite (cost/efficiency) fails -> HOLD.
    if gc1_fail:
        return GateDecision(
            status=HOLD, failing=('G-C1:cost-efficiency',),
            reasoning=(f'{gc1_note} — cost up {cost_pct:+.0f}% without a '
                       f'>={int(thresholds['GC1_efficiency_release_pct'])}% efficiency release.'),
            deltas=deltas,
        )

    # All gates pass: decide between READY and CUT_CANDIDATE on the diff state.
    # Beyond READY only when the PHASE-2 caller supplies both flags (PHASE 1 stops at READY).
    if not candidate_diff_ready:
        return GateDecision(
            status=READY, failing=(),
            reasoning=('all gates pass at N>=' + str(int(thresholds['P1_min_N'])) +
                       ' but no candidate dna.py diff supplied — evidence ready, '
                       'nothing to cut yet.') + (f' | {advisory}' if advisory else ''),
            deltas=deltas,
        )
    if not single_variable:
        return GateDecision(
            status=HOLD, failing=('P3:single-variable-not-asserted',),
            reasoning=('diff ready but P3 (one instruction/theme) not asserted — '
                       'cannot cut an unlabeled/multi-variable drift.'),
            deltas=deltas,
        )
    return GateDecision(
        status=CUT_CANDIDATE, failing=(),
        reasoning=('all prereqs + hard gates + composites pass, diff ready and '
                   'single-variable — stage the one-click approve artifact (PHASE 2).')
                  + (f' | {advisory}' if advisory else ''),
        deltas=deltas,
    )


def baseline_hash_hint(baseline: CohortRow | None) -> str:
    """Human-readable baseline hash for reasoning text (baseline may be None)."""
    return baseline.prompt_hash if baseline else DEFAULT_BASELINE_HASH


def detect_labeling_gap(cohorts: dict[str, CohortRow]) -> list[LabelingGap]:
    """Flag every ``dna_version`` label that maps to >1 distinct ``prompt_hash``
    (the version did not bump when content changed). Proposes the corrective
    MINOR bump; the grouping key remains ``prompt_hash`` regardless.
    """
    by_label: dict[str, list[str]] = defaultdict(list)
    for ph, row in cohorts.items():
        by_label[row.dna_version_label].append(ph)
    gaps: list[LabelingGap] = []
    for label, hashes in by_label.items():
        if len(hashes) > 1:
            gaps.append(LabelingGap(
                dna_version=label,
                hashes=tuple(sorted(hashes)),
                proposed_bump=_next_minor(label),
            ))
    return sorted(gaps, key=lambda g: g.dna_version)


def build_report(rows: dict[str, CohortRow], decisions: dict[str, GateDecision],
                 gaps: list[LabelingGap], baseline_hash: str, *, generated_at: str,
                 coverage: dict | None = None) -> str:
    """Render the primary human deliverable (markdown). Per-cohort deltas vs the
    frozen baseline + a plain-English gate verdict, with "what worked / what
    didn't" sentences TEMPLATED from numeric outcomes (zero LLM). Output is
    byte-deterministic for a given set of inputs (all lists sorted, fixed field
    order, caller-supplied ``generated_at``).
    """
    out: list[str] = []
    out.append('# DNA cohort results report')
    out.append(f'- generated_at: {generated_at}')
    out.append(f'- frozen baseline: `{baseline_hash}` (never auto-rotated)')
    if coverage:
        out.append(f'- labeled coverage: {coverage.get('labeled', 0)} labeled records '
                   f'over {coverage.get('files', 0)} files scanned '
                   f'(unlabeled {coverage.get('total', 0) - coverage.get('labeled', 0)} dropped)')

    if gaps:
        out.append('\n## Labeling gap')
        for g in gaps:
            out.append(f'- label `{g.dna_version}` maps to {len(g.hashes)} distinct hashes '
                       f'[{', '.join(g.hashes)}]; proposed bump `{g.proposed_bump}`. '
                       'Group by `prompt_hash` (content truth); `dna_version` is label-only.')
    else:
        out.append('\n## Labeling gap\n- none detected')

    baseline = rows.get(baseline_hash)
    out.append('\n## Baseline cohort')
    if baseline is not None:
        out.append(_render_cohort_line(baseline) + '  (baseline)')
    else:
        out.append(f'- `{baseline_hash}` not present in this corpus — all cohorts will HOLD on P0.')

    out.append('\n## Cohorts (sorted by prompt_hash)')
    for ph in sorted(rows):
        row = rows[ph]
        out.append(f'\n### `{ph}` ({row.dna_version_label})')
        out.append(_render_cohort_line(row))
        if row.ghost:
            out.append('- **GHOST** (zero completed LLM calls): reported for N, excluded from A/B.')
            continue
        decision = decisions.get(ph)
        if decision is None or ph == baseline_hash:
            out.append('- (baseline — not A/B-compared)')
            continue
        out.append(f'- verdict: **{decision.status}**'
                   + (f'  failing: {', '.join(decision.failing)}' if decision.failing else ''))
        out.append('- deltas vs baseline (candidate -> baseline): ' + _render_deltas(decision.deltas))
        worked, not_worked = _narrate(row, baseline, decision)
        if worked:
            out.append('- what worked: ' + '; '.join(worked))
        if not_worked:
            out.append('- what didn\'t: ' + '; '.join(not_worked))
    return '\n'.join(out) + '\n'


def _fmt(v: float) -> str:
    return f'{v:.2f}'


def _render_cohort_line(row: CohortRow) -> str:
    return (f'N={row.n_sessions}  tasks={row.tasks}  tool_acc={row.tool_acc:.1f}%  '
            f'edit_acc={row.edit_acc:.1f}%  llm_calls/task={_fmt(row.llm_calls_per_task)}  '
            f'cost/task={row.cost_per_task:.0f}  waste/session={row.waste_per_session:.0f}  '
            f'loops/task={_fmt(row.loops_per_task)}  retries/task={_fmt(row.retries_per_task)}  '
            f'p50={row.latency_p50_ms:.0f}ms  models={len(row.model_mix)}')


def _render_deltas(deltas: dict[str, tuple[float, float]]) -> str:
    return '; '.join(f'{m} {cand:.2f}->{base:.2f}' for m, cand, base in
                     ((m, *deltas[m]) for m in METRIC_ORDER if m in deltas))


def _narrate(candidate: CohortRow, baseline: CohortRow,
             decision: GateDecision) -> tuple[list[str], list[str]]:
    """Templated, decision-oriented sentences from the deltas (no LLM). A metric
    is called out only when its change clears the relevant noise floor."""
    worked: list[str] = []
    not_worked: list[str] = []
    acc_floor = abs(GATE_THRESHOLDS['GH2_tool_acc_floor_pts'])
    for metric in METRIC_ORDER:
        if metric not in decision.deltas:
            continue
        cand, base = decision.deltas[metric]
        higher = metric in HIGHER_IS_BETTER
        if metric in HIGHER_IS_BETTER:
            delta_pts = cand - base
            moved = abs(delta_pts) >= acc_floor
            better = (delta_pts > 0) if higher else (delta_pts < 0)
            delta_s = f'{metric} {delta_pts:+.1f}pts'
        else:
            pct = _rel_pct(cand, base)
            floor = (GATE_THRESHOLDS['GC1_cost_cap_pct'] if metric == 'cost_per_task'
                     else GATE_THRESHOLDS['GH2_cycles_cap_pct'])
            moved = abs(pct) >= floor
            better = pct < 0  # lower is better for every non-accuracy metric here
            delta_s = f'{metric} {pct:+.0f}%'
        if not moved:
            continue
        (worked if better else not_worked).append(delta_s)
    if decision.status == HOLD and decision.failing:
        not_worked.append('gated: ' + ', '.join(decision.failing))
    return worked, not_worked


def update_note(old_note: str, rows: dict[str, CohortRow], decisions: dict[str, GateDecision],
                gaps: list[LabelingGap], *, generated_at: str) -> str:
    """Deterministically refresh the note's auto-managed "current state" block
    (last-checked, N per cohort, status, gap line). Everything outside the
    sentinel-delimited block is history and is preserved VERBATIM. On first use
    (no markers) the block is APPENDED — history above it is never deleted."""
    block = _render_note_block(rows, decisions, gaps, generated_at)
    begin = old_note.find(_NOTE_BEGIN)
    end = old_note.find(_NOTE_END)
    if begin != -1 and end != -1 and end > begin:
        head = old_note[:begin]
        tail = old_note[end + len(_NOTE_END):]
        return head + _NOTE_BEGIN + '\n' + block + _NOTE_END + tail
    # No managed region yet: keep the whole existing note, append the block.
    sep = '' if old_note.endswith('\n') or not old_note else '\n'
    return old_note + sep + '\n' + _NOTE_BEGIN + '\n' + block + _NOTE_END + '\n'


def _render_note_block(rows: dict[str, CohortRow], decisions: dict[str, GateDecision],
                       gaps: list[LabelingGap], generated_at: str) -> str:
    lines: list[str] = [f'Last checked: {generated_at} (dna_cohort deterministic pass)',
                        '- Grouping key: prompt_hash (content truth); dna_version is label-only.']
    if gaps:
        lines.append('- Labeling gap: ' + '; '.join(
            f'{len(g.hashes)} hashes share label `{g.dna_version}` (propose `{g.proposed_bump}`)'
            for g in gaps))
    else:
        lines.append('- Labeling gap: none')
    lines.append('- Cohorts: ' + '; '.join(
        f'`{ph}` N={rows[ph].n_sessions} '
        + (decisions[ph].status if ph in decisions
           else 'GHOST' if rows[ph].ghost else 'BASELINE')
        for ph in sorted(rows)) or 'none')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# LAYER 2: side-effecting prepare/write (thin, guarded, gated).
# PHASE 1: note updates only (and even those are skipped under dry_run). The
# cut artifact (Q3) is PHASE 2 and intentionally not implemented here.
# ---------------------------------------------------------------------------
def prepare_cut(candidate_hash: str, repo_dir: str, decision: GateDecision) -> CutArtifact:
    """PHASE 2 ONLY (Q3): compute a HEAD-based ``dna.py`` diff + canary plan +
    gate evidence under ``workspace/dna_cut_prep/<hash>/`` and surface it for
    human approval. NOT implemented in PHASE 1 — raises on any use so the
    manual-pipeline guardrail is never silently bypassed.
    """
    raise NotImplementedError(
        'prepare_cut is a PHASE-2 feature (HEAD-based cut staging + one-click '
        'approve); PHASE 1 ships only the deterministic analysis core + report.')


def apply_approved_cut(artifact: CutArtifact, *, commit: bool = False) -> str:
    """PHASE 2 ONLY: HUMAN-triggered ``git apply --cached`` of an approved cut
    (no commit unless ``commit=True``). Never invoked by the periodic loop."""
    raise NotImplementedError(
        'apply_approved_cut is a PHASE-2 feature; PHASE 1 ships only the '
        'deterministic analysis core + report.')


# Single-flight guard: a periodic tick (PHASE 2) and an on-demand call must not
# double-scan the same 1400-file corpus concurrently.
_RUN_LOCK = threading.Lock()
_IN_PROGRESS = '[dna_cohort] a run is already in progress; result will not double-scan.'


def run_and_report(telemetry_dir: str, *, workspace_root: str,
                   baseline_hash: str = DEFAULT_BASELINE_HASH,
                   stage_cut: bool = False,  # PHASE-2 cut-staging flag (renamed from ``prepare_cut`` to stop shadowing the module-level prepare_cut)
                   dry_run: bool = False) -> str:
    """Top-level sync orchestrator (single-flight guarded).

    ``dry_run=True`` -> report only, no note/cut writes (the manual-pipeline
    path; acceptance: "no regression to manual run"). ``stage_cut=True`` is a
    PHASE-2 seam and raises. Returns the rendered report string.
    """
    if stage_cut:
        raise NotImplementedError(
            'stage_cut=True is PHASE 2 (auto-stage the promotion cut for '
            'one-click approve); PHASE 1 runs the deterministic core only.')
    if not _RUN_LOCK.acquire(blocking=False):
        return _IN_PROGRESS
    try:
        generated_at = _now_utc_iso()
        raw, total_seen = scan_telemetry_with_totals(telemetry_dir)
        rows = count_cohorts(raw)
        gaps = detect_labeling_gap(rows)
        decisions = _decide_all(rows, baseline_hash)
        coverage = {'labeled': len(raw), 'total': total_seen, 'files': _count_files(telemetry_dir)}
        report = build_report(rows, decisions, gaps, baseline_hash,
                              generated_at=generated_at, coverage=coverage)
        if not dry_run:
            note_path = os.path.join(workspace_root, 'dna-telemetry-accumulation.md')
            old = ''
            if os.path.exists(note_path):
                with open(note_path, encoding='utf-8') as fh:
                    old = fh.read()
            new = update_note(old, rows, decisions, gaps, generated_at=generated_at)
            with open(note_path, 'w', encoding='utf-8') as fh:
                fh.write(new)
        return report
    finally:
        _RUN_LOCK.release()


def _decide_all(rows: dict[str, CohortRow], baseline_hash: str) -> dict[str, GateDecision]:
    """Run the gate for every poolable candidate vs the baseline. The baseline
    itself and ghost cohorts are skipped (ghosts are surfaced in the report)."""
    decisions: dict[str, GateDecision] = {}
    for cand, base in pool_ab(rows, baseline_hash):
        decisions[cand.prompt_hash] = run_gate(cand, base, GATE_THRESHOLDS)
    return decisions


def _count_files(telemetry_dir: str) -> int:
    return len(glob.glob(os.path.join(telemetry_dir, 'telemetry_*.jsonl')))


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
