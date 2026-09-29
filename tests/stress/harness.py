"""Stress harness: real SlotPool / EndpointScheduler / ToolDispatcher, fake engine.

Design (plan §1 PRIMARY): drive the REAL production slot machinery and let the
engine loop be a script interpreter. Everything the harness substitutes lives
in the TEST PROCESS only:
  * `tool_dispatcher.run_child_core`   -> `_fake_run_child_core` (the patch seam)
  * `AgentPool.register_async_call`    -> tracked worker thread (the SECOND seam:
    pool/slots.py:88 imports run_child_core LOCALLY, so patching the
    tool_dispatcher symbol alone would let the async path run the real engine)
  * `APIRouter.call_with_fallback`     -> scripted stub (records progress, sleeps)

No production file is modified.

Fidelity checklist for `_fake_turn` vs ExecutionEngine.run():
  1. LEAK_GUARD check/clear at entry (clear-without-release)
  2. bounded acquire (2s) so a stall surfaces as a timeout, not a hang
  3. scripted action dispatch through the REAL `execute_tool`
  4. canonical release via `release_slot_permit`
  5. `finally` marks the worker thread complete
"""

from __future__ import annotations

import os
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── Test-process constants (never read by production code) ──────────────
# QUEUE_WAIT_TIMEOUT must be GREATER than the watchdog's no_progress_s (10s) so
# that a thread stuck in SlotPool.acquire is classified by the watchdog BEFORE
# the timeout fires. If it were shorter, the acquire_timeout event would reset
# the idle timer and mask the deadlock as a benign timeout.
QUEUE_WAIT_TIMEOUT = 30.0   # bounded queue wait -> a stall surfaces, not a hang
MAX_TURNS = 20               # depth cap; generous vs AGENT_MAX_NESTING_DEPTH
JOIN_TIMEOUT = 30.0
WAIT_SETTLE = 1.0

CLASSES = ('stress_a', 'stress_b', 'stress_c')
DEFAULT_EP = 'ep_stress_a'


# ── Instrumentation state ───────────────────────────────────────────────

class Flags:
    """Per-thread `blocked_on` marker used by the DEADLOCK classifier.

    Set immediately around SlotPool.acquire and
    ToolDispatcher._reacquire_caller_slot in a try/finally so it clears even on
    the exception path (a failed acquire is a return, not a hang).
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._registry: Dict[int, str] = {}
        self._lock = threading.Lock()

    def set(self, what: str) -> None:
        tid = threading.get_ident()
        self._local.what = what
        with self._lock:
            self._registry[tid] = what

    def clear(self) -> None:
        self._local.what = ''
        tid = threading.get_ident()
        with self._lock:
            self._registry.pop(tid, None)

    def current(self) -> str:
        return getattr(self._local, 'what', '') or ''

    def snapshot(self) -> Dict[int, str]:
        with self._lock:
            return dict(self._registry)

    @property
    def registry(self) -> Dict[int, str]:
        return self._registry


@dataclass
class Result:
    scenario: str
    seed: int
    classification: str = 'NONE'
    elapsed: float = 0.0
    turns: int = 0
    max_waiter_age_s: float = 0.0
    error: str = ''
    counts: Dict[str, int] = field(default_factory=dict)
    dump: str = ''

    def line(self) -> str:
        return (f'[{self.scenario:<20}] seed={self.seed:<11} {self.classification:<11} '
                f'turns={self.turns:<4} waiter_max={self.max_waiter_age_s:5.2f}s '
                f'elapsed={self.elapsed:6.2f}s {self.error}')


class _StubTemplate:
    """Minimal stand-in for an `Assistant` template.

    `ToolDispatcher.handle_call_agent` only needs `pool.get_template(cls)` to be
    non-None; nothing in the slot path touches template internals. A real
    Assistant would drag in provider clients and disk templates.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.tagline = f'stress template {name}'
        self.description = 'template stub for slot liveness stress'
        self.llm = None
        self.function_map: Dict[str, Callable] = {}

    def get_tools(self) -> List[dict]:
        return []


class Harness:
    """One scenario, one pool, one dispatcher. Not reusable across scenarios."""

    def __init__(self, scenario, log, tmp_dir, root: str = '.', phase: str = 'post') -> None:
        self.scenario = scenario
        self.log = log
        self.root = root
        self.phase = phase
        self.tmp_dir = tmp_dir
        self.rng = random.Random(scenario.seed)

        self.flags = Flags()
        self.threads: Dict[str, threading.Thread] = {}
        self.thread_done: Dict[int, bool] = {}
        self.async_children: List[threading.Thread] = []
        self.sync_failures: List[str] = []
        self.leak_guard_hits: List[str] = []
        self._name_lock = threading.RLock()
        self._name_locks: Dict[str, threading.RLock] = {}
        self._used_names: set = set()
        self._turns = 0
        self._turns_lock = threading.Lock()
        self._errors: List[str] = []

        self._patches: List[Any] = []
        self._stop = threading.Event()

        self.pool = None
        self.router = None
        self.engine = None
        self.dispatcher = None

    # ── progress helpers ──
    def rec(self, kind: str, agent: str, pool: str = '', detail: str = '') -> None:
        self.log.record(kind, agent, pool, detail)

    def _bump(self) -> None:
        with self._turns_lock:
            self._turns += 1

    def _fail(self, where: str, exc: BaseException) -> None:
        with self._name_lock:
            self._errors.append(f'{where}: {type(exc).__name__}: {exc}')

    # ── build ──
    def setup(self) -> None:
        from agent_cascade.api_router_pkg.router import APIRouter
        from agent_cascade.api_router_pkg.endpoints import APIEndpoint
        from agent_cascade.pool.core import AgentPool
        from agent_cascade.tool_dispatcher import ToolDispatcher

        os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = str(self.tmp_dir)
        self._quiet_agent_cascade_logging()

        self.llm_cfg = {'model': 'stress-mock', 'api_base': f'http://127.0.0.1:9/{DEFAULT_EP}',
                        'model_server': f'http://127.0.0.1:9/{DEFAULT_EP}', 'api_key': 'EMPTY'}
        self.router = APIRouter(default_llm_cfg=self.llm_cfg, config_dir=str(self.tmp_dir))
        with self.router._lock:
            self.router.endpoints.clear()
            self.router.agent_priorities.clear()
            self.router._agent_types_with_priorities.clear()
        for ep_id, conc in (('ep_stress_a', 0), ('ep_stress_b', 0), ('ep_stress_c', 1)):
            self.router.add_endpoint(APIEndpoint(
                id=ep_id, name=ep_id, api_base=f'http://127.0.0.1:9/{ep_id}',
                model='stress-model', concurrency_limit=conc, enabled=True,
            ))
        self.router.default_llm_cfg = dict(self.llm_cfg)
        for cls, ep_id in (('stress_a', 'ep_stress_a'),
                           ('stress_b', 'ep_stress_b'),
                           ('stress_c', 'ep_stress_c')):
            self.router.set_agent_priorities(cls, [ep_id])

        self.pool = AgentPool(self.llm_cfg, agents_dir=str(self.tmp_dir), api_router=self.router)
        for cls in CLASSES:
            self.pool.templates[cls] = _StubTemplate(cls)

        self.engine = _FakeEngine(self.pool)
        self.dispatcher = ToolDispatcher(self.pool)
        self.dispatcher.set_engine(self.engine)

        self._apply_patches()

    def _apply_patches(self) -> None:
        from unittest.mock import patch

        from agent_cascade import tool_dispatcher as td
        from agent_cascade.api_router_pkg.router import APIRouter
        from agent_cascade.pool.slots import SlotsMixin
        from agent_cascade.slot_queue import SlotPool
        from agent_cascade.tool_dispatcher import ToolDispatcher

        # NOTE: bound methods are NOT descriptors, so assigning `self._x` to a
        # class attribute would make the patched function receive NO `self`.
        # Every patch below therefore goes through a plain closure, which IS a
        # descriptor and therefore receives the real `self`/instance.

        # ── Seam 1: the synchronous child seam (module-level import at :23) ──
        self._patches.append(patch.object(
            td, 'run_child_core',
            lambda *a, **k: self._fake_run_child_core(*a, **k)))

        # ── Seam 2: the async seam (pool/slots.py:88 local import) ──
        self._patches.append(patch.object(
            SlotsMixin, 'register_async_call',
            lambda _s, *a, **k: self._register_async_call(*a, **k)))

        # ── LLM: scripted stub; records progress, never touches the network ──
        self._patches.append(patch.object(
            APIRouter, 'call_with_fallback',
            lambda _s, *a, **k: self._fake_call_with_fallback(*a, **k)))

        # ── Instrumentation wraps (real code runs inside) ──
        self._patches.append(patch.object(
            SlotPool, 'acquire', lambda _s, *a, **k: self._wrap_acquire(_s, *a, **k)))
        self._patches.append(patch.object(
            SlotPool, 'release', lambda _s, *a, **k: self._wrap_release(_s, *a, **k)))
        self._patches.append(patch.object(
            ToolDispatcher, '_reacquire_caller_slot',
            lambda _s, *a, **k: self._wrap_reacquire(_s, *a, **k)))

        for p in self._patches:
            p.start()

        # Bound the queue wait so a genuine stall surfaces as a timeout in
        # ~QUEUE_WAIT_TIMEOUT seconds instead of hanging for the default 300.
        import agent_cascade.api_router_pkg.scheduler as _sched
        import agent_cascade.slot_queue as _sq
        self._saved_timeouts = (_sq.QUEUE_WAIT_TIMEOUT, _sched.QUEUE_WAIT_TIMEOUT)
        _sq.QUEUE_WAIT_TIMEOUT = QUEUE_WAIT_TIMEOUT
        _sched.QUEUE_WAIT_TIMEOUT = QUEUE_WAIT_TIMEOUT

    def _quiet_agent_cascade_logging(self) -> None:
        """Drop the agent_cascade logger to CRITICAL for the duration.

        Slot contention is logged at WARNING on every queue event; the harness
        records the same information in the ProgressLog and prints it in the
        stall dump, so leaving it on buries a real finding in thousands of
        expected-contention lines.
        """
        import logging
        lg = logging.getLogger('agent_cascade')
        self._saved_log_level = lg.level
        lg.setLevel(logging.CRITICAL)

    def _restore_logging(self) -> None:
        import logging
        if getattr(self, '_saved_log_level', None) is not None:
            logging.getLogger('agent_cascade').setLevel(self._saved_log_level)
            self._saved_log_level = None

    def teardown(self) -> None:
        self._restore_logging()
        import agent_cascade.api_router_pkg.scheduler as _sched
        import agent_cascade.slot_queue as _sq
        if getattr(self, '_saved_timeouts', None) is not None:
            _sq.QUEUE_WAIT_TIMEOUT, _sched.QUEUE_WAIT_TIMEOUT = self._saved_timeouts
            self._saved_timeouts = None
        for p in reversed(self._patches):
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self._patches.clear()

    # ── patched implementations ──
    def _fake_call_with_fallback(self, agent_type, call_fn, *args, **kwargs):
        agent = getattr(self.engine._current_agent, 'instance_name', '?') \
            if self.engine._current_agent is not None else '?'
        self.rec('llm', agent, detail=agent_type)
        time.sleep(self.rng.uniform(0.001, 0.006))
        return {'ok': True}

    def _wrap_acquire(self, pool_self, instance_name, agent_class, timeout=None, **kwargs):
        self.rec('wait_enqueue', instance_name, pool=pool_self.key)
        self.flags.set(f'acquire:{pool_self.key}')
        try:
            rel = _orig_acquire(pool_self, instance_name, agent_class, timeout=timeout, **kwargs)
        finally:
            self.flags.clear()
        if rel is None:
            self.rec('wait_cancel', instance_name, pool=pool_self.key, detail='timeout/None')
            self.rec('acquire_timeout', instance_name, pool=pool_self.key)
            return None
        self.rec('wait_grant', instance_name, pool=pool_self.key)
        self.rec('acquire', instance_name, pool=pool_self.key)
        return rel

    def _wrap_release(self, pool_self, holder):
        self.rec('release', getattr(holder, 'instance_name', str(holder)),
                 pool=getattr(pool_self, 'key', '?'))
        return _orig_release(pool_self, holder)

    def _wrap_reacquire(self, dispatcher_self, instance, context='sync-child', quiet=True):
        name = getattr(instance, 'instance_name', '?')
        self.flags.set(f'reacquire:{name}')
        try:
            ok = _orig_reacquire(dispatcher_self, instance, context, quiet)
        finally:
            self.flags.clear()
        if ok:
            self.rec('reacquire_ok', name)
        else:
            self.rec('reacquire_fail', name)
            self.sync_failures.append(name)
        return ok

    def _register_async_call(self, instance_name, function_id=None, agent_class=None,
                            child_instance_name=None, args=None, caller=None, nest_depth=0):
        """Replacement for SlotsMixin.register_async_call.

        The real implementation calls `run_child_core` via a FUNCTION-SCOPE
        import (pool/slots.py:88), so patching `tool_dispatcher.run_child_core`
        does NOT intercept it. We wrap it explicitly and route through the same
        fake child implementation used by the sync path.
        """
        self.rec('spawn', caller or '?', detail=f'async:{instance_name}:{function_id}')
        target = child_instance_name or f'{instance_name}_async'
        spec = self.scenario.by_name().get(target)
        if spec is None:
            spec = _implicit_child_spec(target, agent_class or CLASSES[0])
            self.scenario.agents.append(spec)
        script = spec.script

        def _worker() -> None:
            self.flags.set(f'async_worker:{target}')
            try:
                self._run_child_body(instance_name, target,
                                     agent_class or CLASSES[0], caller, nest_depth, script)
            except Exception as exc:  # noqa: BLE001
                self._fail(f'async[{target}]', exc)
            finally:
                self.flags.clear()
                self.thread_done[threading.get_ident()] = True

        th = threading.Thread(target=_worker, name=f'async-{target}', daemon=True)
        with self._name_lock:
            self.async_children.append(th)
        th.start()
        return None

    def _fake_run_child_core(self, engine, pool, agent_class, instance_name, args,
                             caller_name=None, child_depth=0, **kw):
        """Stub for agent_cascade.child_runner.run_child_core.

        Signature mirrors child_runner.py:72-83 exactly. SYNC children run in
        the CALLER thread (as the real one does), which is what makes the
        COLL-2 release/reacquire ordering observable.
        """
        self.rec('spawn', caller_name or '?', detail=f'sync:{instance_name}:{agent_class}')
        spec = self.scenario.by_name().get(instance_name)
        if spec is None:
            spec = _implicit_child_spec(instance_name, agent_class)
            with self._name_lock:
                self.scenario.agents.append(spec)
        self._run_child_body(caller_name, instance_name, agent_class,
                             caller_name, child_depth, spec.script)
        return f'[status=success] async call to {instance_name} registered'

    def _run_child_body(self, caller_name, child_name, agent_class, parent, depth, script) -> None:
        with self._name_lock_for(child_name):
            child = self._make_instance(child_name, agent_class, parent, depth)
            try:
                self._fake_turn(child, script, depth)
            finally:
                self._teardown_instance(child)

    # ── instance lifecycle ──
    def _name_lock_for(self, name: str) -> threading.RLock:
        """One lock per instance name.

        Two concurrent synchronous spawns of the SAME name would race
        create/dismiss and cancel each other's ticket. Serializing per name
        removes that self-inflicted interference without weakening the
        contention we actually want (all names share one slot pool).
        """
        with self._name_lock:
            lk = self._name_locks.get(name)
            if lk is None:
                lk = threading.RLock()
                self._name_locks[name] = lk
            return lk

    def _make_instance(self, name, agent_class, parent, depth):
        self._dismiss_if_present(name)
        inst = self.pool.create_instance(name, agent_class,
                                         parent_instance=parent, max_turns=MAX_TURNS)
        inst._nest_depth = depth
        self.rec('reuse' if name in self._used_names else 'spawn', name, detail=f'cls={agent_class}')
        with self._name_lock:
            self._used_names.add(name)
        return inst

    def _dismiss_if_present(self, name: str) -> None:
        existing = self.pool.instances.get(name)
        if existing is None:
            return
        try:
            self.pool.terminate_instance(name, set_global_stopped=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.pool.dismiss_instance(name)
        except Exception:  # noqa: BLE001
            pass
        self.rec('dismiss', name, detail='replaced')

    def _teardown_instance(self, inst) -> None:
        name = inst.instance_name
        self._release_held(inst, context='turn-exit', action='drop-exit')
        try:
            self.pool.dismiss_instance(name)
        except Exception:  # noqa: BLE001
            pass

    def _release_held(self, inst, context: str, action: str = 'drop-exit') -> None:
        from agent_cascade.slot_queue import release_slot_permit
        if getattr(inst, '_slot_release', None) is None:
            return
        key = getattr(inst, '_slot_key', None)
        if release_slot_permit(inst, inst.instance_name, action=action, context=context):
            self.rec('release', inst.instance_name, pool=key, detail=context)

    # ── the fake engine turn (mirrors ExecutionEngine.run lifecycle) ──
    def _fake_turn(self, inst, script: List, depth: int = 0) -> None:
        name = inst.instance_name
        if self._stop.is_set():
            return
        # 1. LEAK_GUARD: a permit left over from a previous run of this name is
        #    cleared WITHOUT releasing (matches core.py:859-864).
        if getattr(inst, '_slot_release', None) is not None:
            self.leak_guard_hits.append(name)
            self.rec('leak_guard', name, detail='cleared-without-release')
            inst._slot_release = None

        self.rec('turn_begin', name, detail=f'depth={depth}')
        self._bump()
        self.engine._current_agent = inst
        try:
            # 2. bounded acquire
            slot = self._acquire(inst, 'initial')
            if slot is None:
                return
            # 3. scripted actions through the REAL dispatcher
            for action in script:
                if self._stop.is_set():
                    break
                self._run_action(inst, action, depth)
        except Exception as exc:  # noqa: BLE001
            self._fail(f'turn[{name}]', exc)
        finally:
            # 4. canonical release
            self._release_held(inst, context='turn-exit', action='drop-exit')
            self.rec('turn_end', name)
            self.engine._current_agent = None

    def _acquire(self, inst, context: str):
        # `SlotsMixin._acquire_slot` takes no timeout; the bound comes from the
        # module-level QUEUE_WAIT_TIMEOUT, which setup() shortens.
        #
        # A SlotCancelled or a queue TimeoutError here is an EXPECTED outcome of
        # a deliberately-shortened wait under heavy contention, not a defect —
        # it is recorded as progress and the turn ends. Only unexpected
        # exception types count as errors.
        from agent_cascade.slot_queue import SlotCancelled, SlotQueueTimeout  # noqa: F401
        try:
            rel = self.pool._acquire_slot(inst.agent_class, inst.instance_name)
        except (SlotCancelled, SlotQueueTimeout) as exc:
            self.rec('acquire_timeout', inst.instance_name,
                     detail=f'{type(exc).__name__}: {exc}')
            return None
        except Exception as exc:  # noqa: BLE001
            self._fail(f'acquire[{inst.instance_name}]', exc)
            return None
        if rel is None:
            return None
        inst._slot_release = rel
        try:
            info = self.router.get_effective_slot_info(inst.agent_class,
                                                       instance_name=inst.instance_name)
            inst._slot_key = info.get('slot_key')
        except Exception:  # noqa: BLE001
            inst._slot_key = None
        self.rec('acquire', inst.instance_name, pool=inst._slot_key, detail=context)
        return rel

    def _run_action(self, inst, action, depth: int) -> None:
        name = inst.instance_name
        if action.sleep_ms:
            time.sleep(action.sleep_ms / 1000.0)
        kind = action.kind

        if kind == 'llm':
            self._stub_llm(inst)
            return
        if kind == 'tool':
            self._stub_llm(inst, tool=action.tool_name or 'read_file')
            return
        if kind == 'sticky_sync':
            self.rec('sticky_sync', name)
            try:
                self.router.sync_sticky_slot(inst)
            except Exception as exc:  # noqa: BLE001
                self._fail(f'sticky[{name}]', exc)
            return
        if kind == 'sleep':
            self._release_held(inst, context='sleep transition', action='drop-sleep')
            time.sleep(0.005)
            self._acquire(inst, 'after-sleep')
            return

        if kind in ('spawn', 'reentrant_sync', 'spawn_burst'):
            n = int(action.args.get('count', 1)) if kind == 'spawn_burst' else 1
            for i in range(n):
                target = action.target if n == 1 else f'{action.target}_{i}'
                self._call_agent(inst, target, depth)
            return
        if kind == 'spawn_async':
            self._call_agent(inst, action.target, depth, asynchronous=True)
            return
        if kind in ('dismiss', 'dismiss_repeat', 'dismiss_grandparent', 'reuse_dismissed'):
            self._dismiss(inst, action)
            return
        raise ValueError(f'unknown action kind {kind!r}')

    def _stub_llm(self, inst, tool: str = '') -> None:
        self.rec('llm', inst.instance_name, detail=tool)

    def _call_agent(self, inst, target, depth, asynchronous: bool = False) -> None:
        cls = self.scenario.by_name().get(target)
        agent_class = cls.agent_class if cls is not None else CLASSES[1]
        self.dispatcher.execute_tool(
            inst, 'call_agent',
            {'instance_name': target, 'agent_class': agent_class,
             'message': f'hi {target}'},
            [],
        )

    def _dismiss(self, inst, action) -> None:
        target = action.target
        self.dispatcher.execute_tool(inst, 'dismiss_agent', {'instance_name': target}, [])
        self.rec('dismiss', target, detail=f'by={inst.instance_name}')

        if action.kind not in ('dismiss_repeat', 'reuse_dismissed'):
            return

        # The known-open bug under test: dismiss/terminate does not release the
        # slot permit, so re-using the SAME name finds the pool still occupied.
        #
        # The re-use MUST go through the real `call_agent` path: the harness must
        # not hold its own permit while the child runs, or the child starves and
        # the test measures the harness instead of the product.
        spec = _implicit_child_spec(target, CLASSES[1])
        with self._name_lock:
            if target not in self.scenario.by_name():
                self.scenario.agents.append(spec)
        self._call_agent(inst, target, 1)

    # ── execution ──
    def run(self, on_stall=None) -> Result:
        self.setup()
        started = time.monotonic()
        try:
            self._launch_roots()
            self._join_all()
        finally:
            elapsed = time.monotonic() - started
            self._stop.set()
        result = Result(scenario=self.scenario.name, seed=self.scenario.seed,
                        elapsed=elapsed, turns=self._turns,
                        counts=self.log.counts(),
                        error='; '.join(self._errors[:5]))
        self.teardown()
        return result

    def _launch_roots(self) -> None:
        for spec in self.scenario.roots():
            th = threading.Thread(target=self._root_worker, args=(spec,),
                                  name=f'root-{spec.name}', daemon=True)
            self.threads[spec.name] = th
            th.start()

    def _root_worker(self, spec) -> None:
        inst = self._make_instance(spec.name, spec.agent_class, None, 0)
        try:
            self._fake_turn(inst, spec.script, 0)
        except Exception as exc:  # noqa: BLE001
            self._fail(f'root[{spec.name}]', exc)
        finally:
            self.thread_done[threading.get_ident()] = True
            self._teardown_instance(inst)

    def _join_all(self) -> None:
        deadline = time.monotonic() + self.scenario.budget_s
        pending = list(self.threads.values()) + list(self.async_children)
        while pending and time.monotonic() < deadline:
            pending = [t for t in pending if t.is_alive()]
            for t in pending:
                t.join(timeout=0.05)
        alive = [t for t in pending if t.is_alive()]
        if alive:
            self._errors.append(f'{len(alive)} worker thread(s) still alive at drain: '
                                + ','.join(t.name for t in alive))
        # Let waiters/permits settle so a late grant is attributed, not lost.
        time.sleep(WAIT_SETTLE)

    # ── collection for the watchdog ──
    def live_worker_count(self) -> int:
        n = sum(1 for t in self.threads.values() if t.is_alive())
        n += sum(1 for t in list(self.async_children) if t.is_alive())
        return n

    def all_workers_done(self) -> bool:
        """True when NO worker threads are alive.

        The watchdog uses `not all_workers_done()` to mean "some workers are
        still alive" — the precondition for a DEADLOCK (all blocked on slot
        waits). If no workers are alive, a stall is UNEXPLAINED (a thread died).
        """
        return self.live_worker_count() == 0

    def max_waiter_age(self) -> float:
        oldest = 0.0
        now = time.monotonic()
        for pool in self._sched_pools():
            for ticket in list(getattr(pool, '_waiters', []) or []):
                created = getattr(ticket, 'created_at', None)
                if created is not None:
                    oldest = max(oldest, now - created)
        return oldest

    def zombie_holders(self) -> List[str]:
        """Permits held in _running for instances that no longer exist."""
        out: List[str] = []
        for pool in self._sched_pools():
            for name in list(getattr(pool, '_running', {}) or {}):
                if self.pool.instances.get(name) is None:
                    out.append(f'{getattr(pool, 'key', '?')}:{name}')
        return out

    def _sched_pools(self):
        sched = getattr(self.router, '_sched', None)
        pools = getattr(sched, '_pools', None) if sched is not None else None
        return list((pools or {}).values())

    def thread_stacks(self) -> Dict[int, str]:
        import sys
        out: Dict[int, str] = {}
        for t in list(self.threads.values()) + list(self.async_children):
            if t.is_alive() and t.ident in sys._current_frames():
                frames = sys._current_frames()
                out[t.ident] = ''.join(
                    __import__('traceback').format_stack(frames[t.ident])[-2:]).strip()
        return out

    def collect(self) -> dict:
        pools: Dict[str, dict] = {}
        for pool in self._sched_pools():
            key = str(getattr(pool, 'key', '?'))
            holders = list(getattr(pool, '_running', {}) or {})
            waiters = []
            oldest = 0.0
            now = time.monotonic()
            for ticket in list(getattr(pool, '_waiters', []) or []):
                age = now - getattr(ticket, 'created_at', now)
                oldest = max(oldest, age)
                waiters.append({'holder': getattr(ticket, 'instance_name', '?'),
                                'age_s': round(age, 3)})
            pools[key] = {
                'capacity': getattr(pool, 'capacity', '?'),
                'running': holders,
                'waiters': waiters,
                'max_waiter_age_s': round(oldest, 3),
            }

        agents = []
        for name, inst in list(self.pool.instances.items()):
            with inst._state_lock:
                held = getattr(inst, '_slot_release', None) is not None
            parent = getattr(inst, 'parent_instance', None)
            chain = []
            cur = parent
            for _ in range(MAX_TURNS):
                if not isinstance(cur, str):
                    break
                chain.append(cur)
                p = self.pool.instances.get(cur)
                cur = getattr(p, 'parent_instance', None) if p is not None else None
            agents.append({'name': name, 'cls': getattr(inst, 'agent_class', '?'),
                           'state': str(getattr(inst, 'state', '?')),
                           'holds_permit': held,
                           'slot_key': getattr(inst, '_slot_key', None),
                           'parent': parent, 'chain': chain})

        max_age = max([p['max_waiter_age_s'] for p in pools.values()] or [0.0])
        return {
            'pools': pools,
            'agents': agents,
            'max_waiter_age_s': max_age,
            'zombie_holders': self.zombie_holders(),
            'sync_failures': list(self.sync_failures),
            'leak_guard_hits': list(self.leak_guard_hits),
            'live_worker_count': self.live_worker_count(),
        }


class _FakeEngine:
    """Stands in for ExecutionEngine where ToolDispatcher needs one.

    `reacquire_for` is the REAL unbound method, bound to this object, so the
    COLL-2 reacquire step exercises production code (30s fast window, FIFO
    re-queue) rather than a mock.
    """

    def __init__(self, pool) -> None:
        from agent_cascade.execution_engine import ExecutionEngine
        self.pool = pool
        self._current_agent = None
        self._telemetry = None
        self._compression_lock = threading.RLock()
        self.reacquire_for = lambda instance, holder_name, context='reacquire': \
            ExecutionEngine.reacquire_for(self, instance, holder_name, context)
        self._resolve_placeholders = lambda args, instance_name, tool_name: args
        self._cache_tool_args = lambda *a, **k: None

    @property
    def compression_handler(self):
        return None

    @property
    def tool_dispatcher(self):
        return self.pool._stress_dispatcher


def _implicit_child_spec(name: str, agent_class: str):
    from .workload import Action, AgentSpec
    return AgentSpec(name=name, agent_class=agent_class,
                     script=[Action('llm', sleep_ms=2)], is_child=True)


# Module-level originals captured BEFORE any patch is applied.
_orig_acquire = None
_orig_release = None
_orig_reacquire = None


def _capture_originals() -> None:
    global _orig_acquire, _orig_release, _orig_reacquire
    from agent_cascade.slot_queue import SlotPool
    from agent_cascade.tool_dispatcher import ToolDispatcher
    _orig_acquire = SlotPool.acquire
    _orig_release = SlotPool.release
    _orig_reacquire = ToolDispatcher._reacquire_caller_slot


_capture_originals()
