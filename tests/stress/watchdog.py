"""Progress log + stall watchdog for the slot-pool liveness stress harness.

Classification (plan §3) uses ONLY externally observable signals:
  - ProgressEvent timestamps  (is the system still making progress?)
  - the harness-set `blocked_on` flag per worker thread
  - thread liveness

It never infers scheduler state, queue-head position, or hypothetical
reachability — an inference-based classifier would "explain away" the very
bug we are hunting.
"""

from __future__ import annotations

import collections
import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ── Event kinds (plan §2.1) ──────────────────────────────────────────────
ACQUIRE = 'acquire'
RELEASE = 'release'
WAIT_ENQUEUE = 'wait_enqueue'
WAIT_GRANT = 'wait_grant'
WAIT_CANCEL = 'wait_cancel'
COLL2_SYNC = 'coll2_sync'
CHAIN_RELEASE = 'chain_release'
REACQUIRE_OK = 'reacquire_ok'
REACQUIRE_FAIL = 'reacquire_fail'
DISMISS = 'dismiss'
TERMINATE = 'terminate'
REUSE = 'reuse'
TURN_BEGIN = 'turn_begin'
TURN_END = 'turn_end'
SPAWN = 'spawn'
LLM = 'llm'
LEAK_GUARD = 'leak_guard'
STICKY_SYNC = 'sticky_sync'
ACQUIRE_TIMEOUT = 'acquire_timeout'


@dataclass(frozen=True)
class ProgressEvent:
    t: float
    kind: str
    agent: str
    pool: str = ''
    detail: str = ''

    def to_dict(self) -> dict:
        return {'t': round(self.t, 4), 'kind': self.kind, 'agent': self.agent,
                'pool': self.pool, 'detail': self.detail}

    def __str__(self) -> str:
        base = f"[{self.t - self._epoch:8.2f}s] {self.kind:<16} {self.agent}"
        if self.pool:
            base += f" pool={self.pool}"
        if self.detail:
            base += f" | {self.detail}"
        return base

    # Set once by ProgressLog.record so render() can show relative time.
    _epoch: float = 0.0


class ProgressLog:
    """Thread-safe ring buffer of ProgressEvent plus per-kind counters."""

    def __init__(self, maxlen: int = 20000) -> None:
        self._events: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        self._epoch = time.monotonic()
        self._wait_pairs: List[float] = []   # (enqueue, grant) timestamps per agent
        self._pending_wait: Dict[str, float] = {}

    @property
    def epoch(self) -> float:
        return self._epoch

    def record(self, kind: str, agent: str, pool: str = '', detail: str = '') -> None:
        ev = ProgressEvent(t=time.monotonic(), kind=kind, agent=agent, pool=pool, detail=detail)
        object.__setattr__(ev, '_epoch', self._epoch)
        with self._lock:
            self._events.append(ev)
            self._counts[kind] = self._counts.get(kind, 0) + 1
            if kind == WAIT_ENQUEUE:
                self._pending_wait.setdefault(agent, ev.t)
            elif kind == WAIT_GRANT:
                start = self._pending_wait.pop(agent, None)
                if start is not None:
                    self._wait_pairs.append(ev.t - start)

    def count(self, kind: Optional[str] = None) -> int:
        with self._lock:
            if kind is None:
                return len(self._events)
            return self._counts.get(kind, 0)

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def last_ts(self) -> float:
        with self._lock:
            if not self._events:
                return self._epoch
            return self._events[-1].t

    def snapshot(self, limit: int = 200) -> List[ProgressEvent]:
        with self._lock:
            return list(self._events)[-limit:]

    def since(self, t: float) -> List[ProgressEvent]:
        with self._lock:
            return [e for e in self._events if e.t >= t]

    def wait_samples(self) -> List[float]:
        with self._lock:
            return list(self._wait_pairs)


@dataclass
class StallReport:
    seed: int
    scenario: str
    classification: str
    elapsed: float
    stall_seconds: float
    seed_repro: str
    pools: Dict[str, dict] = field(default_factory=dict)
    agents: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    sync_failures: List[str] = field(default_factory=list)
    leak_guard_hits: List[str] = field(default_factory=list)
    zombie_holders: List[str] = field(default_factory=list)
    blocked: Dict[str, str] = field(default_factory=dict)
    thread_stacks: Dict[str, str] = field(default_factory=dict)
    phase: str = 'post'
    budget_s: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            'seed': self.seed, 'scenario': self.scenario, 'phase': self.phase,
            'classification': self.classification, 'elapsed_s': round(self.elapsed, 3),
            'stall_seconds': round(self.stall_seconds, 3), 'budget_s': self.budget_s,
            'seed_repro': self.seed_repro, 'pools': self.pools, 'agents': self.agents,
            'zombie_holders': self.zombie_holders, 'sync_failures': self.sync_failures,
            'leak_guard_hits': self.leak_guard_hits, 'blocked': self.blocked,
            'thread_stacks': self.thread_stacks, 'events': self.events,
        }, indent=2, default=str)

    def render(self) -> str:
        lines = [
            f"=== STALL [{self.classification}] scenario={self.scenario} seed={self.seed} "
            f"phase={self.phase} elapsed={self.elapsed:.1f}s stall={self.stall_seconds:.1f}s ===",
            f"repro: {self.seed_repro}",
            f"zombie_holders: {self.zombie_holders or '[]'}",
            f"sync_failures: {self.sync_failures or '[]'}",
            f"leak_guard_hits: {self.leak_guard_hits or '[]'}",
            f"blocked: {self.blocked or '{}'}",
            '--- pools ---',
        ]
        for key, p in self.pools.items():
            lines.append(f"  pool {key!r} capacity={p.get('capacity')} "
                         f"running={p.get('running')} waiters={p.get('waiters')}")
        lines.append('--- agents ---')
        for a in self.agents:
            lines.append('  ' + json.dumps(a, default=str))
        lines.append('--- thread stacks ---')
        for ident, top in self.thread_stacks.items():
            lines.append(f"  tid={ident}: {top}")
        lines.append('--- last events ---')
        for e in self.events:
            lines.append('  ' + json.dumps(e, default=str))
        return '\n'.join(lines)


def _top_frame(thread: threading.Thread) -> str:
    """Best-effort single top frame for a thread (never blocks)."""
    ident = thread.ident
    if ident is None:
        return '<no ident>'
    frame = sys._current_frames().get(ident)
    if frame is None:
        return '<frame unavailable>'
    return ''.join(traceback.format_stack(frame)[-2:]).strip()


class Watchdog:
    """Polls the ProgressLog; declares a stall and classifies it.

    Never blocks on a lock the workload holds — that would hide the bug.
    """

    TICK = 0.25

    def __init__(self,
                 log: ProgressLog,
                 budget_s: float,
                 no_progress_s: float,
                 waiter_starve_s: float,
                 on_stall: Callable[[StallReport], None],
                 collect: Callable[[], dict],
                 blocked_flags: Callable[[], Dict[int, str]] = lambda: {},
                 all_alive: Callable[[], bool] = lambda: True,
                 thread_stacks: Callable[[], Dict[int, str]] = lambda: {},
                 scenario: str = '',
                 seed: int = 0,
                 phase: str = 'post',
                 seed_repro: str = '') -> None:
        self.log = log
        self.budget_s = budget_s
        self.no_progress_s = no_progress_s
        self.waiter_starve_s = waiter_starve_s
        self.on_stall = on_stall
        self.collect = collect
        self.blocked_flags = blocked_flags
        self.all_alive = all_alive
        self.thread_stacks = thread_stacks
        self.scenario = scenario
        self.seed = seed
        self.phase = phase
        self.seed_repro = seed_repro

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._stalled: Optional[StallReport] = None
        self._lock = threading.Lock()
        self._start_ts = 0.0
        self._max_waiter_age = 0.0

    # ── lifecycle ──
    def start(self) -> None:
        self._start_ts = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name='stress-watchdog', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)
            assert not t.is_alive(), 'watchdog thread did not stop within 5s'

    @property
    def stalled(self) -> Optional[StallReport]:
        with self._lock:
            return self._stalled

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_ts if self._start_ts else 0.0

    @property
    def max_waiter_age_s(self) -> float:
        return self._max_waiter_age

    # ── polling loop ──
    def _loop(self) -> None:
        while not self._stop.wait(self.TICK):
            try:
                self._tick()
            except Exception:  # pragma: no cover - watchdog must never die silently
                traceback.print_exc()

    def _tick(self) -> None:
        if self._stalled is not None:
            return
        now = time.monotonic()
        elapsed = now - self._start_ts
        idle = now - self.log.last_ts()
        payload = self.collect() or {}
        self._max_waiter_age = max(self._max_waiter_age, float(payload.get('max_waiter_age_s') or 0.0))

        no_progress = self.no_progress_s > 0 and idle > self.no_progress_s
        over_budget = elapsed > self.budget_s
        if not (no_progress or over_budget):
            return

        if over_budget and not no_progress:
            # Budget blown but progress still flowing → perf finding, not liveness.
            classification = 'TIMEOUT'
        elif self._max_waiter_age > self.waiter_starve_s:
            # F-3 fix: a waiter older than the starvation threshold → STARVATION.
            # This is a FAIRNESS failure (the system is making progress, but one
            # waiter has been starved past the threshold). It does NOT require a
            # zombie holder — that's a separate (stronger) condition for ZOMBIE.
            # Previously unreachable: no STARVATION branch existed and the
            # post-hoc check ran on a broken age signal (F-1).
            classification = 'STARVATION'
        elif self.all_alive():
            # Some workers are still alive → potential DEADLOCK or ZOMBIE.
            # Note: sync children run in caller threads and are NOT counted in
            # live_worker_count, so the flag count can exceed it. We require at
            # least one flagged thread AND that the flag count covers all live
            # workers (>=), which is sufficient: if any live worker were NOT
            # blocked on a slot wait, the flag count would be < live_worker_count.
            flags = self.blocked_flags()
            zombies = list(payload.get('zombie_holders') or [])
            if flags and len(flags) >= int(payload.get('live_worker_count') or 0):
                classification = 'ZOMBIE' if zombies else 'DEADLOCK'
            elif zombies and no_progress:
                classification = 'ZOMBIE'
            else:
                classification = 'UNEXPLAINED'
        else:
            # All workers are done (no live threads). A stall with no live
            # workers is not a deadlock — something died or the scenario ended
            # abnormally. Classify as UNEXPLAINED so it is never silently passed.
            classification = 'UNEXPLAINED'

        report = StallReport(
            seed=self.seed,
            scenario=self.scenario,
            classification=classification,
            elapsed=elapsed,
            stall_seconds=idle if no_progress else 0.0,
            seed_repro=self.seed_repro,
            phase=self.phase,
            budget_s=self.budget_s,
            pools=payload.get('pools', {}),
            agents=payload.get('agents', []),
            events=[e.to_dict() for e in self.log.snapshot(200)],
            sync_failures=list(payload.get('sync_failures') or []),
            leak_guard_hits=list(payload.get('leak_guard_hits') or []),
            zombie_holders=list(payload.get('zombie_holders') or []),
            blocked={str(k): v for k, v in (self.blocked_flags() or {}).items()},
            thread_stacks={str(k): v for k, v in (self.thread_stacks() or {}).items()},
        )
        with self._lock:
            self._stalled = report
        self.on_stall(report)
