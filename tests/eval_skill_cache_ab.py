#!/usr/bin/env python3
"""BUG_0404 task-aware skill-cache A/B evaluation (proving it WORKS + is EFFECTIVE).

This is a standalone, deterministic eval — NOT a pytest test — runnable via:

    python tests/eval_skill_cache_ab.py

It drives the REAL ``SkillManager`` + ``TelemetryCollector`` (the same code committed
at 7ba3cb77); it does NOT reimplement the cache. Run A is the baseline with the cache
OFF; Run B turns the cache on (commit mode) and runs the identical workload. The only
difference between the two runs is ``skill_cache_active_enabled`` in the pool's ``llm_cfg``
— both execute the same per-task loop (bump turn -> prefetch -> evict -> demand-load), so
the OFF run being inert is itself a proof of the default-off no-op contract.

───────────────────────────────────────────────────────────────────────────────────
WORKLOAD (strong temporal locality — the regime a task-aware cache exploits)
───────────────────────────────────────────────────────────────────────────────────
Universe of ``UNIVERSE`` on-disk skills, each exposing a UNIQUE rare token
(``sk{NNNN}``) in its description+trigger, plus two ubiquitous tokens (``cache skill``)
shared by every skill. Task i needs a sliding window of ``WINDOW`` skills:
``needs[i] = universe[i*STRIDE : i*STRIDE+WINDOW]`` — consecutive tasks overlap ~80%.
Each task's text is the concatenation of its window's skill phrases, so the matcher
recall against it is the cache's favourable (oracle) point.

───────────────────────────────────────────────────────────────────────────────────
METRIC — cold-loads (definition, since the real demand-load path makes this ambiguous)
───────────────────────────────────────────────────────────────────────────────────
The "active set" is defined EXACTLY as the eviction code sees it:
    active_set = {name : manager._metrics[name]['status'] == 'active'}
(``cache_evict_tick`` reads precisely this predicate, so this is not a parallel notion.)

``cold-load(task i)`` = number of ``needs[i]`` skills that were NOT in ``active_set``
at the instant the agent's demand-load begins for task i — i.e. AFTER that task's
boundary prefetch/evict (the cache's look-ahead point) and BEFORE ``resolve_load_skill_pairs``
activates them. A skill is "warm" if it was either (a) loaded in an earlier task and never
evicted, or (b) just speculatively prefetched (admitted as probation) at this boundary. So a
prefetch that warms a not-yet-loaded skill converts what would be a cold demand-load into a
warm load — which is exactly the cost the cache is supposed to cut.

Run A (cache OFF): prefetch/evict return [] (off-gate short-circuits before any telemetry),
so every first-touch skill is a cold load at demand time. Run B (cache ON, commit): prefetch
warms the upcoming window ahead of demand, then eviction bounds the active pool to TARGET.

───────────────────────────────────────────────────────────────────────────────────
top_k choice
───────────────────────────────────────────────────────────────────────────────────
Production ``SKILL_CACHE_PREFETCH_TOP_K`` is 12. With this window size and the matcher's
name-ascending tiebreak, a top-12 prefetch keeps the 12 LOWEST-indexed (already-warm) skills
of the window and drops the 3 HIGHEST-indexed — which are exactly the 3 genuinely-new (cold)
skills of each task — so a top-12 prefetch cannot warm them and the cache shows no cold-load
win. The headline run therefore sizes ``TOP_K = WINDOW`` (recall-complete: the cache's
favourable operating point under the synthetic oracle) and additionally reports a TOP_K=12
(production default) sensitivity run to show why prefetch K must cover the working window.
"""

import os
import sys
import subprocess
import tempfile
import shutil
import traceback
from pathlib import Path
from types import SimpleNamespace

# Determinism guard: Python randomises str() hashing per process (PYTHONHASHSEED) unless pinned.
# The skill matcher has many equal-score ties whose top-K cut depends on set-iteration order, which
# is derived from str-hash order — so identical workloads drift across processes without a fixed seed.
# Re-exec the interpreter with the seed pinned so a plain `python tests/eval_skill_cache_ab.py` is
# byte-reproducible. (Must happen before importing agent_cascade, which builds hashed sets at import.)
# Empirically seed-sensitive: under PYTHONHASHSEED 0/1/2 the Run-B cold-loads read 62/56/59 and the
# telemetry slice swings (13/3/13 admissions/evictions/promotions at seed 0; 43/27/43 at seed 1;
# 15/2/15 at seed 2), so the seed MUST be pinned for byte-reproducible output. The committed headline
# numbers are the PYTHONHASHSEED=0 run. We pin by re-execing via subprocess (NOT os.execv): on Windows
# os.execv has no native exec — it spawns a detached child whose stdout is NOT inherited by the parent's
# redirect (so `python ... > out.txt` captures 0 bytes) and which never returns an exit code.
# subprocess.call inherits stdout/stderr and returns the child's exit code, so a plain run prints the
# table and propagates exit 0.
if os.environ.get('PYTHONHASHSEED') != '0':
    _env = dict(os.environ, PYTHONHASHSEED='0')
    _rc = subprocess.call([sys.executable, os.path.abspath(__file__)] + sys.argv[1:], env=_env)
    sys.exit(_rc)

# Make the repo importable when run as a plain script (python tests/eval_skill_cache_ab.py).
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent_cascade.skills.manager import SkillManager, _default_metrics_entry
from agent_cascade.skills import manager as _MGR
from agent_cascade.settings import SKILLS_DISABLED
from agent_cascade.telemetry import TelemetryCollector

# Reuse the on-disk skill-writing helper the test-suite uses; fall back to a byte-identical
# local mirror if importing the (large) test module pulls in pytest-only machinery.
TESTS_DIR = REPO / 'tests'
try:
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    from test_skills_system import _write_named_skill  # noqa: E402
    SKILL_WRITER = 'tests/test_skills_system._write_named_skill'
except Exception:  # noqa: BLE001 — ImportError or any module-level side effect
    def _write_named_skill(root: Path, name: str, desc: str, trigger: str) -> None:
        """Byte-identical mirror of tests/test_skills_system._write_named_skill."""
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: {desc}\nversion: "1.0.0"\n'
            f'triggers:\n  - {trigger}\n---\n# Body\n',
            encoding='utf-8')
    SKILL_WRITER = 'local mirror of _write_named_skill (fallback)'

# ── Workload constants (deterministic, no randomness) ──────────────────────────
UNIVERSE = 120   # synthetic skill universe size
T = 20           # root tasks
WINDOW = 15      # skills needed per task (sliding window)
STRIDE = 3       # window slide -> ~80% overlap between consecutive tasks
TARGET = 30      # cache active-pool target (within the [20, 200] clamp band). The working set grows to
                 # 72 unique skills over 20 tasks; a target below that keeps the pool above target at
                 # every boundary so the eviction/bounding path engages every tick. At 60 the pool only
                 # crosses 60 near the tail and demand skills are fair-window-protected, so eviction
                 # barely fires within 20 turns (see the bounding section in the report).
TOP_K = WINDOW   # headline prefetch width (recall-complete)
TOP_K_SENS = 12  # production-default prefetch width (sensitivity)

# ── Determinism: pin the wall-clock the cache scoring/classification path reads ──
# ``prefetch_for_task`` / match scoring call ``time.time()`` for recency and feed it to
# ``skill_classify`` as activity age. Across SEPARATE processes the real clock differs,
# nudging borderline scores and shifting which probation skills prefetch admits / eviction
# recycles — so the cold-load metric is clock-independent (deterministic), but the raw
# telemetry counts drift by a couple. Pinning the clock removes that last source so the WHOLE
# report is byte-reproducible. This is a test-only clock pin; production cache logic is untouched.
class _FrozenClock:
    """Stands in for the manager module's ``time`` object with a fixed clock."""
    def __init__(self, wall: float, mono: float):
        self._wall = wall
        self._mono = mono
        self._orig = _MGR.time          # delegate anything else (strftime, gmtime, ...) untouched
    def time(self): return self._wall
    def monotonic(self): return self._mono
    def perf_counter(self): return self._mono
    def __getattr__(self, n): return getattr(self._orig, n)

def _freeze_clock() -> None:
    # Wall clock fixed; monotonic fixed at 0 (it only feeds the TTL + disk-flush timers, both
    # driven explicitly per run and irrelevant to the in-process telemetry we read).
    _MGR.time = _FrozenClock(wall=1_700_000_000.0, mono=0.0)


def _phrases(idx):
    """Task text = the window's skill phrases; each carries its unique rare token sk{NNNN}."""
    return ' '.join(f'cache skill sk{j:04d}' for j in idx)


def _build_universe(root: Path):
    """Write UNIVERSE discoverable skills, each with a unique rare token sk{NNNN}."""
    names = []
    for j in range(UNIVERSE):
        name = f'cache-skill-{j:04d}'
        # desc + trigger expose the rare token (df=1 -> high IDF) plus 2 common tokens,
        # so a window task text matches this skill on >=2 terms and ranks it by sk{NNNN}.
        _write_named_skill(root, name, f'cache skill sk{j:04d}', f'cache skill sk{j:04d}')
        names.append(name)
    return names


def _make_run(tmp: Path, universe, *, cache_on: bool, top_k: int, commit: bool):
    """Build a hermetic SkillManager (cold pool) + a fresh TelemetryCollector-backed pool.

    Identical for Run A and Run B except ``cache_on`` (which flips ``skill_cache_active_enabled``).
    The active pool starts fully cold: every skill on disk but inactive (status=inactive, reason
    unset -> revivable) and disabled, so the first demand-load genuinely warms it.
    """
    base = tmp / 'agents' / 'global'
    skills_root = tmp / 'skills'
    for sub in ('pending-skills', 'candidates', 'skills'):
        (base / sub).mkdir(parents=True, exist_ok=True)

    m = SkillManager()
    # Redirect every write root to this run's tmp tree (mirrors make_hermetic_skill_manager)
    # so we never touch the real agents/global/skills-metrics.json store.
    m._metrics_file = base / 'skills-metrics.json'
    m._pending_dir = base / 'pending-skills'
    m._candidates_dir = base / 'candidates'
    m._production_skills_dir = base / 'skills'
    with m._metrics_lock:
        m._metrics = {}
    m._disabled_names = set(SKILLS_DISABLED)
    m._global_activity_turns = 0
    m.pool = None

    # Discover EXACTLY once, deterministically. ttl=0 forces the scan past the TTL guard on the
    # explicit call (with a huge ttl the very first discover would short-circuit to 0 skills).
    # Immediately after, freeze ttl huge so _ensure_discovered() (and any background reload)
    # short-circuits for the rest of the run — discovery never re-runs, so the matcher/registry/
    # servable indices are a fixed function of the synthetic universe (the determinism proof).
    # Disabled skills still resolve mid-run via the servable index built here (the soft-eviction
    # path), exactly as production — no re-scan is required.
    m._cache_ttl = 0.0
    m.discover([skills_root])
    m._cache_ttl = 1e9

    # Cold-start the active pool: mark every skill inactive + disabled (reason unset, so the
    # cache / demand path may still revive it). status=inactive matches exactly what
    # cache_evict_tick counts as the active set.
    with m._metrics_lock:
        m._metrics = {n: _default_metrics_entry('inactive') for n in universe}
    m._disabled_names = set(SKILLS_DISABLED) | {n.lower() for n in universe}

    # A fresh, isolated telemetry collector per run (so the skill_cache slice accumulates
    # only this run's decisions). Fixed log dir under tmp; nothing else writes events here.
    tel = TelemetryCollector(log_dir=str(tmp / 'telemetry'), instance_id='ab-eval')
    llm_cfg = {
        'skill_cache_active_enabled': cache_on,
        'skill_cache_commit_enabled': commit,
        'skill_cache_target_size': TARGET,
        'skill_cache_prefetch_top_k': top_k,
    }
    m.pool = SimpleNamespace(llm_cfg=llm_cfg, telemetry=tel, settings=None)

    def active_set():
        with m._metrics_lock:
            return {str(k).lower() for k, v in m._metrics.items()
                    if isinstance(v, dict) and v.get('status') == 'active'}

    return m, tel, active_set


def _run_workload(m, active_set, universe, *, n_tasks=T, trace_prefetch=False):
    """Drive ``n_tasks`` tasks. Returns (total_cold, active_trace, prefetch_calls, evict_calls).

    Same loop for Run A and Run B: bump turn -> prefetch -> evict -> measure cold -> demand-load.
    The cache flag (in llm_cfg) makes prefetch/evict no-op on Run A, which is the no-op proof.

    Window indices use modulo UNIVERSE. For the headline n_tasks=20 (max index 71 < 120) this is
    the plain ``universe[i*3 : i*3+15]`` slice the spec asks for — no wrap. For the longer
    bounding-demonstration run it cycles the universe so the workload continues past the
    fair-window horizon (see main).
    """
    total_cold = 0
    trace = []
    prefetch_calls = 0
    evict_calls = 0
    commit = m._cache_commit_enabled()

    for i in range(n_tasks):
        idx = [(i * STRIDE + j) % UNIVERSE for j in range(WINDOW)]
        needs = [universe[k] for k in idx]
        m.bump_activity_turn()                    # production: activity clock ticks at root boundary

        task_text = _phrases(idx)
        pref = m.prefetch_for_task(task_text, commit=commit)
        evict = m.cache_evict_tick(commit=commit)
        prefetch_calls += len(pref) if trace_prefetch else 1
        evict_calls += len(evict) if trace_prefetch else 1

        # Cold-load = needed skills NOT warm at demand-load start (after boundary prefetch/evict).
        warm = active_set()
        cold = sum(1 for s in needs if s.lower() not in warm)
        total_cold += cold
        trace.append(len(warm))

        # Real deliberate demand-load: activates any not-yet-warm needed skill (soft re-enable)
        # AND, for skills just prefetched as probation, promotes them to demand (+telemetry).
        m.resolve_load_skill_pairs(needs)

    return total_cold, trace, prefetch_calls, evict_calls


def main() -> int:
    _freeze_clock()  # pin the clock the cache scores against (see _FrozenClock above)
    root = Path(tempfile.mkdtemp(prefix='skill_cache_ab_'))
    try:
        universe = _build_universe(root / 'skills')
        print('BUG_0404 task-aware skill-cache A/B evaluation')
        print(f'  repo          : {REPO}')
        print(f'  skill writer  : {SKILL_WRITER}')
        print(f'  universe={UNIVERSE}  tasks={T}  window={WINDOW}  stride={STRIDE} '
              f'(overlap ~{100*(WINDOW-STRIDE)//WINDOW}%)')
        print(f'  target={TARGET}  top_k={TOP_K} (headline) / {TOP_K_SENS} (prod default) '
              f'  commit=True (Run B)\n')

        # ---- Run A: baseline (cache OFF) ----
        a_root = root / 'runA'
        a_root.mkdir()
        shutil.copytree(root / 'skills', a_root / 'skills')
        m_a, tel_a, as_a = _make_run(a_root, universe, cache_on=False, top_k=TOP_K, commit=True)
        cold_a, trace_a, pfx_a, ev_a = _run_workload(m_a, as_a, universe, trace_prefetch=True)

        # No-op proof: cache methods returned nothing and no cache telemetry was written.
        sum_a = tel_a.get_skill_cache_summary()
        no_prefetch_applied = (pfx_a == 0)
        no_evict_applied = (ev_a == 0)
        # evict_by_category is a dict, so test it explicitly (a {} != 0 would break a generic v==0 sweep).
        no_telemetry = (sum_a['prefetch_admissions'] == 0 and sum_a['evict_victims'] == 0
                        and not sum_a['evict_by_category'] and sum_a['probation_promotions'] == 0
                        and sum_a['active_set_size'] == 0 and sum_a['shadow_decisions'] == 0
                        and sum_a['commit_decisions'] == 0)

        # ---- Run B: cache ON (commit), headline top_k ----
        b_root = root / 'runB'
        b_root.mkdir()
        shutil.copytree(root / 'skills', b_root / 'skills')
        m_b, tel_b, as_b = _make_run(b_root, universe, cache_on=True, top_k=TOP_K, commit=True)
        cold_b, trace_b, pfx_b, ev_b = _run_workload(m_b, as_b, universe)
        sum_b = tel_b.get_skill_cache_summary()

        # ---- Run B': cache ON (commit), production-default top_k (sensitivity) ----
        bs_root = root / 'runB_prodK'
        bs_root.mkdir()
        shutil.copytree(root / 'skills', bs_root / 'skills')
        m_bs, tel_bs, as_bs = _make_run(bs_root, universe, cache_on=True, top_k=TOP_K_SENS, commit=True)
        cold_bs, trace_bs, _, _ = _run_workload(m_bs, as_bs, universe)
        sum_bs = tel_bs.get_skill_cache_summary()

        # ---- Run C: bounding demonstration (cache ON, with the fair-window gate RELAXED) ----
        # The headline (T=20) active set is NOT bounded to TARGET, and that is CORRECT behaviour,
        # not a bug: cache_evict_tick only retires a demand skill once it is older than
        # SKILL_FAIR_WINDOW_TURNS (=50) turns, to protect the recently-loaded working set. With only
        # 20 turns no demand skill is that old, so eviction can only recycle probation and the pool
        # grows to the cumulative working set regardless of TARGET. To prove the bounded-eviction path
        # is genuinely live, Run C RELAXES that same gate (SKILL_FAIR_WINDOW_TURNS -> short) so demand
        # skills become evictable; the demand-LRU path then pulls the pool down toward TARGET.
        orig_fair_window = getattr(_MGR, 'SKILL_FAIR_WINDOW_TURNS', None)
        _MGR.SKILL_FAIR_WINDOW_TURNS = 5
        t_long = 40
        try:
            c_root = root / 'runC_long'
            c_root.mkdir()
            shutil.copytree(root / 'skills', c_root / 'skills')
            m_c, tel_c, as_c = _make_run(c_root, universe, cache_on=True, top_k=TOP_K, commit=True)
            cold_c, trace_c, _, _ = _run_workload(m_c, as_c, universe, n_tasks=t_long)
            sum_c = tel_c.get_skill_cache_summary()
        finally:
            _MGR.SKILL_FAIR_WINDOW_TURNS = orig_fair_window  # restore the production default
        demand_evict_c = sum_c['evict_by_category'].get('demand', 0)
        max_c = max(trace_c)
        final_c = trace_c[-1]

        total_demands = T * WINDOW
        hr_a = (total_demands - cold_a) / total_demands
        hr_b = (total_demands - cold_b) / total_demands
        hr_bs = (total_demands - cold_bs) / total_demands
        reduction = (cold_a - cold_b) / cold_a * 100 if cold_a else 0.0

        # ---- Comparison table ----
        line = '-' * 78
        print(line)
        print('COMPARISON (cold-loads: fewer = better; hit-rate = warm-needed / total-needed)')
        print(line)
        print(f'{'metric':<34}{'Run A (cache OFF)':>18}{'Run B (ON, K=%d)' % TOP_K:>18}'
              f'{'Run B prod-K=%d' % TOP_K_SENS:>19}')
        print(line)
        print(f'{'total cold-loads':<34}{cold_a:>18}{cold_b:>18}{cold_bs:>19}')
        print(f'{'hit-rate (warm-needed frac)':<34}{hr_a:>18.3f}{hr_b:>18.3f}{hr_bs:>19.3f}')
        print(f'{'active-set size: start->end':<34}'
              f'{f"{trace_a[0]}->{max(trace_a)}":>18}'
              f'{f"{trace_b[0]}->{max(trace_b)}":>18}'
              f'{f"{trace_bs[0]}->{max(trace_bs)}":>19}')
        print(f'{'active-set max (target=%d)' % TARGET:<34}'
              f'{max(trace_a):>18}{max(trace_b):>18}{max(trace_bs):>19}')
        print(line)

        print('\nTelemetry slice (Run B, top_k=%d) — get_skill_cache_summary()' % TOP_K)
        print(line)
        print(f'  prefetch_admissions  : {sum_b['prefetch_admissions']}')
        print(f'  evict_victims        : {sum_b['evict_victims']}')
        print(f'  evict_by_category    : {sum_b['evict_by_category']}')
        print(f'  probation_promotions : {sum_b['probation_promotions']}')
        print(f'  active_set_size      : {sum_b['active_set_size']}  (target {TARGET})')
        print(f'  commit_decisions     : {sum_b['commit_decisions']}')
        print(f'  shadow_decisions     : {sum_b['shadow_decisions']}')
        print(line)

        # ---- Bounding demonstration (Run C): why the headline active-set exceeds target ----
        print('\nBounding / eviction-path demonstration (Run C: cache ON, fair-window gate relaxed to 5)')
        print(line)
        print(f'  HEADLINE (Run B, T={T} turns, fair-window={orig_fair_window}): active-set max={max(trace_b)} vs target {TARGET}.')
        print(f'    The active set EXCEEDS target — and that is correct, not a cache defect. cache_evict_tick')
        print(f'    only retires a DEMAND skill once it is older than SKILL_FAIR_WINDOW_TURNS ({orig_fair_window}) turns,')
        print(f'    deliberately protecting the recently-loaded working set. With only {T} turns no demand skill is')
        print(f'    that old, so eviction can only recycle probation. Run B evicted {sum_b['evict_victims']} '
              f'skills, all {sum_b['evict_by_category']} '
              f'({sum_b['evict_by_category'].get('demand', 0)} demand victims) — probation only, as expected.')
        print(f'  Run C (T={t_long} turns, SKILL_FAIR_WINDOW_TURNS relaxed 50 -> 5): demand skills now age')
        print(f'    PAST the protection window and become evictable -> the demand-LRU eviction path engages.')
        print(f'    Result: evictions={sum_c['evict_victims']} ({sum_c['evict_by_category']}) with '
              f'demand-victims={demand_evict_c}; active-set max={max_c}, final={final_c} vs target {TARGET}.')
        print(f'    This isolates the bounded-eviction mechanism: when the fair window allows retirement, the')
        print(f'    pool is pulled down toward target; when it does not (the headline), demand is protected.')
        print(line)

        # ---- Verdicts ----
        working = (sum_b['prefetch_admissions'] > 0 and sum_b['evict_victims'] > 0
                   and sum_b['probation_promotions'] > 0)
        effective = cold_b < cold_a

        print('\nVERDICTS')
        print(line)
        if working:
            print('WORKING  : YES — cache telemetry is populated: '
                  f'{sum_b['prefetch_admissions']} admissions, {sum_b['evict_victims']} evictions '
                  f'({sum_b['evict_by_category']}), {sum_b['probation_promotions']} probation->demand '
                  f'promotions, active_set={sum_b['active_set_size']} (band target {TARGET}).')
        else:
            print('WORKING  : NO — cache telemetry not exercised '
                  f'(admissions={sum_b['prefetch_admissions']}, evictions={sum_b['evict_victims']}, '
                  f'promotions={sum_b['probation_promotions']}).')
        if effective:
            print(f'EFFECTIVE: YES — cold-loads {cold_a} -> {cold_b} = '
                  f'{reduction:.1f}% reduction vs the cache-OFF baseline. '
                  f'hit-rate {hr_a:.3f} -> {hr_b:.3f}.')
        else:
            print(f'EFFECTIVE: NO — cold-loads {cold_a} -> {cold_b} (no reduction).')
        print(line)
        print(f'Default-off no-op: prefetch applied={not no_prefetch_applied}, '
              f'evict applied={not no_evict_applied}, cache telemetry all-zero={no_telemetry} '
              f'(Run A: cold={cold_a}, sum={sum_a}).')
        if not (working and effective):
            print('\nNOTE: numbers came out unexpected — inspect above / telemetry / logs.')

        return 0 if (working and effective and no_prefetch_applied and no_evict_applied
                     and no_telemetry) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
