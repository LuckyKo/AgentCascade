"""BUG_0173 — a pool-level Stop must terminate the endpoint router's retry mechanism.

The Stop button sets ``AgentPool.stopped`` (a property over ``_stopped_event``). Before
this fix the router package never read that flag: its only cancellation was per-instance
termination (``terminated_instances`` / ``is_instance_terminated``), which a Stop does NOT
set for the root instance. So a stopped agent ran the whole cascade to exhaustion.

These tests drive ``APIRouter.call_with_fallback`` offline (no live LLM, no network) with a
controllable pool double whose ``stopped`` is a PLAIN bool (the real ``AgentPool.stopped``
is a property returning a bool; ``_stop_requested`` tests ``... is True``, so an ``Event``
or a truthy non-bool would be wrongly rejected).

Guard witnesses (see the plan's §3.2):
  G1 — call entry, before chain resolution      -> test_g1_stop_at_entry_aborts_before_chain_resolution
  G2 — top of the endpoint-cascade loop         -> test_g2_stop_terminates_endpoint_cascade
  G3 — top of the per-endpoint attempt loop     -> test_attempt_loop_guard_terminates_between_attempts
  G4 — pre-call check before the API call       -> test_g4_pre_call_check_terminates_before_http
  _interruptible_sleep tick (helpers._should_abort_locked) -> test_stop_during_retries_cuts_backoff_short_and_records_no_cooldown / test_interruptible_sleep_aborts_on_stop
"""

import contextlib
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent_cascade.api_router_pkg.router as router_mod
import agent_cascade.log as log_mod
from agent_cascade.api_router import APIEndpoint, APIRouter
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.api_router_pkg.helpers import (_interruptible_sleep, _should_abort_locked,
                                                  _should_abort_lockfree, _stop_requested)


@pytest.fixture(autouse=True)
def disable_sanity_probe(monkeypatch):
    """Dummy URLs; measure in-memory dispatch overhead, no probe HTTP."""
    monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', False)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def capture_router_logs():
    """Capture the router's DEBUG diagnostics.

    The router logs through a late-bound ``_AppLoggerProxy`` that resolves
    ``agent_cascade.log.logger`` on every call, so ``caplog`` (name-targeted) is unreliable
    here. Attach the handler to the LIVE object the proxy resolves to and restore the level
    in ``finally`` (a leaked DEBUG on the shared logger breaks later tests in the worker).
    """
    sink = log_mod.logger
    handler = _CaptureHandler()
    prev_level = sink.level
    handler.setLevel(logging.DEBUG)
    sink.addHandler(handler)
    sink.setLevel(logging.DEBUG)
    try:
        yield handler.messages
    finally:
        sink.setLevel(prev_level)
        sink.removeHandler(handler)


@contextlib.contextmanager
def neutral_backoff_sleep():
    """Neutralise the router's backoff sleep so control reaches the next attempt instead of
    blocking on the real ``_interruptible_sleep``. Restores the original in ``finally`` so a
    leaked no-op sleep never leaks into a later test in the worker."""
    orig = router_mod._interruptible_sleep

    def noop_sleep(duration, pool, instance_name, interval=0.5):
        return None

    router_mod._interruptible_sleep = noop_sleep
    try:
        yield
    finally:
        router_mod._interruptible_sleep = orig


class StopPool:
    """Pool double mirroring the real AgentPool surface the router reads.

    ``stopped`` is a plain bool (NOT an Event) — the strict ``is True`` check in
    ``_stop_requested`` requires it. ``terminated_instances`` is a real set so the
    lock-free membership term behaves like production.
    """

    def __init__(self):
        self.stopped = False
        self.terminated_instances = set()

    def get_instance(self, name):
        # Returning None makes sync_sticky_slot a no-op (it early-returns on a None
        # instance, router.py:730), so the test exercises the retry/stop guards without
        # dragging in the lifecycle-slot machinery.
        return None

    def is_instance_terminated(self, name):
        return name in self.terminated_instances


def make_router():
    """Fresh APIRouter. Wipe the persisted config file first (see api-router-test-config-dir-env-var-gotcha)
    so per-endpoint attempt counts are deterministic regardless of xdist state inheritance."""
    cfg = {'model': 'default-model', 'api_base': 'http://localhost:1111/v1', 'model_type': 'qwenvl_oai'}
    test_config_dir = os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    if test_config_dir:
        stale = Path(test_config_dir) / 'api_endpoints.json'
        if stale.exists():
            stale.unlink()
    return APIRouter(default_llm_cfg=cfg)


def failing_call_fn(pool, calls, flip=None):
    """call_fn that records the endpoint it was invoked for, then fails.

    ``flip`` selects the side effect on the FIRST invocation:
      'stop'    -> pool.stopped = True          (the user Stop signal)
      'term'    -> pool.terminated_instances.add('test-inst')  (per-instance termination)
      None     -> no side effect (negative / control path)
    """

    def call_fn(llm_cfg):
        calls.append(llm_cfg.get('model'))
        if len(calls) == 1:
            if flip == 'stop':
                pool.stopped = True
            elif flip == 'term':
                pool.terminated_instances.add('test-inst')
        raise ConnectionError('simulated connection failure')

    return call_fn


def add_failing_endpoint(router, name, port, max_retries=2):
    ep = APIEndpoint(name=name, api_base=f'http://localhost:{port}/v1', model=name + '-model',
                     max_retries=max_retries)
    router.add_endpoint(ep)
    return ep


# ── G1: call entry, before any chain resolution / probe / D1 wait ──────────────────


def test_g1_stop_at_entry_aborts_before_chain_resolution():
    """A Stop set before the call must abort at entry: no chain, no HTTP, no attempts."""
    router = make_router()
    ep = add_failing_endpoint(router, 'ep1', 9901, max_retries=2)
    router.set_agent_priorities('coder', [ep.id])
    pool = StopPool()
    pool.stopped = True  # Stop pressed before call_with_fallback
    router._pool = pool

    calls = []
    with capture_router_logs() as logs:
        with pytest.raises(AgentTerminatedError):
            router.call_with_fallback('coder', failing_call_fn(pool, calls, flip='stop'),
                                      agent_instance_name='test-inst')
    assert calls == [], f"G1 must abort before any endpoint is tried; got {calls}"
    # The entry guard's OWN line is the unique witness: if G1 were reverted, G2 would catch
    # the stop later and emit a different line, so call-count alone would stay green.
    assert any('before chain resolution' in m and '[TERMINATION]' in m for m in logs), \
        f"G1 entry guard did not fire; got {logs}"


# ── G2: top of the endpoint-cascade loop ───────────────────────────────────────────


def test_g2_stop_terminates_endpoint_cascade():
    """Stop during endpoint 1's attempt must stop the cascade moving to endpoint 2."""
    router = make_router()
    ep1 = add_failing_endpoint(router, 'ep1', 9902, max_retries=0)
    ep2 = add_failing_endpoint(router, 'ep2', 9903, max_retries=0)
    router.set_agent_priorities('coder', [ep1.id, ep2.id])
    pool = StopPool()
    router._pool = pool

    calls = []
    with capture_router_logs() as logs:
        with pytest.raises(AgentTerminatedError):
            router.call_with_fallback('coder', failing_call_fn(pool, calls, flip='stop'),
                                      agent_instance_name='test-inst')
    # Only ep1's single attempt ran; ep2 was never reached (no "All endpoints exhausted").
    assert calls == ['ep1-model'], f"G2 must stop the cascade at the next endpoint; got {calls}"
    # Unique witness: the cascade guard's own line, not the attempt guard's.
    assert any('Aborting endpoint cascade' in m and '[TERMINATION]' in m for m in logs), \
        f"G2 cascade guard did not fire; got {logs}"


# ── Core symptom: Stop cuts the retry backoff short, records no cooldown ─────────────


def test_stop_during_retries_cuts_backoff_short_and_records_no_cooldown():
    """The reported symptom. A Stop raised inside the first attempt must cut the retry
    backoff sleep short (not sit out the full delay), run exactly one attempt, never try
    the second endpoint, and record no endpoint cooldown for a user-initiated stop."""
    router = make_router()
    ep = add_failing_endpoint(router, 'ep1', 9904, max_retries=2)
    router.set_agent_priorities('coder', [ep.id])
    pool = StopPool()
    router._pool = pool

    # Make the intended backoff delay large so "cut short" is observable; use the REAL
    # _interruptible_sleep so we can prove it aborts early via helpers._should_abort_locked.
    monkey_backoff = lambda *a, **k: 2.0
    orig = router_mod.calculate_backoff
    router_mod.calculate_backoff = monkey_backoff
    try:
        calls = []
        import time
        t0 = time.monotonic()
        with pytest.raises(AgentTerminatedError):
            router.call_with_fallback('coder', failing_call_fn(pool, calls, flip='stop'),
                                      agent_instance_name='test-inst')
        elapsed = time.monotonic() - t0
    finally:
        router_mod.calculate_backoff = orig

    assert calls == ['ep1-model'], f"expected exactly one attempt; got {calls}"
    assert 'default-model' not in calls, 'second endpoint must never be tried after Stop'
    assert elapsed < 1.0, f"backoff (2.0s intended) was not cut short; elapsed={elapsed:.2f}s"

    # No cooldown recorded for a stop-abort (state hygiene): the post-loop cooldown write is skipped.
    from agent_cascade.api_router_pkg.normalization import normalize_api_base
    ep_key = (normalize_api_base('http://localhost:9904/v1'), 'ep1')
    assert ep_key not in router._endpoint_failure_times, 'a user Stop must not penalise a healthy endpoint'


# ── G3/G4: top of the per-endpoint attempt loop (cancellation between attempts) ─────


def test_attempt_loop_guard_terminates_between_attempts():
    """Stop between attempts must terminate the within-endpoint retry loop. Backoff sleep
    is neutralised so control reaches the next attempt and the loop-top guard is the exit."""
    router = make_router()
    ep = add_failing_endpoint(router, 'ep1', 9905, max_retries=2)
    router.set_agent_priorities('coder', [ep.id])
    pool = StopPool()
    router._pool = pool

    calls = []
    with neutral_backoff_sleep():
        with capture_router_logs() as logs:
            with pytest.raises(AgentTerminatedError):
                router.call_with_fallback('coder', failing_call_fn(pool, calls, flip='stop'),
                                          agent_instance_name='test-inst')

    assert calls == ['ep1-model'], f"expected one attempt then abort at the next attempt top; got {calls}"
    # Unique witness: the attempt-loop guard's own line, not the pre-call guard's.
    assert any('Aborting LLM call' in m and '[TERMINATION]' in m for m in logs), \
        f"G3 attempt-loop guard did not fire; got {logs}"


class _CountingStoppedPool:
    """Pool whose ``stopped`` flips True on the Nth read of the attribute.

    Used to pin a SPECIFIC guard as the first to observe the stop. With no rate-limiting
    and a neutralised backoff sleep, the ``stopped`` reads occur in order G1, G2, G3, G4,
    so ``flip_at_read=4`` makes G4 (the pre-call check) the first guard to see True —
    G1/G2/G3 all see False. This is the only way to reach G4 without G3 firing first.
    """

    def __init__(self, flip_at_read):
        self._flip_at_read = flip_at_read
        self._reads = 0
        self.terminated_instances = set()

    @property
    def stopped(self):
        self._reads += 1
        return self._reads >= self._flip_at_read

    def get_instance(self, name):
        return None

    def is_instance_terminated(self, name):
        return name in self.terminated_instances


def test_g4_pre_call_check_terminates_before_http():
    """The pre-call guard (G4) must abort before any HTTP fires when the stop appears strictly
    between the attempt-loop top and the call — a state G3 cannot see. Proves G4 is not dead
    code masquerading as G3."""
    router = make_router()
    ep = add_failing_endpoint(router, 'ep1', 9910, max_retries=2)
    router.set_agent_priorities('coder', [ep.id])
    # flip_at_read=4: G1(1)=F, G2(2)=F, G3(3)=F, G4(4)=True -> abort at the pre-call check.
    pool = _CountingStoppedPool(flip_at_read=4)
    router._pool = pool

    calls = []
    with neutral_backoff_sleep():
        with capture_router_logs() as logs:
            with pytest.raises(AgentTerminatedError):
                router.call_with_fallback('coder', failing_call_fn(pool, calls, flip=None),
                                          agent_instance_name='test-inst')

    # No HTTP at all: the pre-call guard stopped it before the endpoint was tried.
    assert calls == [], f"G4 must abort before any HTTP; got {calls}"
    assert any('before API call' in m and '[TERMINATION]' in m for m in logs), \
        f"G4 pre-call guard did not fire; got {logs}"


def test_termination_only_still_aborts_between_attempts():
    """Regression guard for the G3/G4 rewrite: a per-instance termination (stop flag False)
    still aborts via the bare terminated_instances membership the guards preserve."""
    router = make_router()
    ep = add_failing_endpoint(router, 'ep1', 9906, max_retries=2)
    router.set_agent_priorities('coder', [ep.id])
    pool = StopPool()  # stopped stays False
    router._pool = pool

    calls = []
    with neutral_backoff_sleep():
        with pytest.raises(AgentTerminatedError):
            router.call_with_fallback('coder', failing_call_fn(pool, calls, flip='term'),
                                      agent_instance_name='test-inst')

    assert calls == ['ep1-model'], f"termination-only path must still abort; got {calls}"
    assert pool.stopped is False, 'termination-only path must not require the stop flag'


# ── Negative control: no Stop -> retries follow policy ──────────────────────────────


def test_without_stop_retries_still_follow_policy():
    """No Stop: the attempt loop must still run max_retries+1 = 3 attempts per endpoint
    and reach the exhaustion raise. Proves the guards do not change non-stopped behaviour."""
    router = make_router()
    ep = add_failing_endpoint(router, 'ep1', 9907, max_retries=2)
    router.set_agent_priorities('coder', [ep.id])
    pool = StopPool()  # never stopped
    router._pool = pool

    calls = []
    with neutral_backoff_sleep():
        with pytest.raises(RuntimeError, match='All API endpoints exhausted'):
            router.call_with_fallback('coder', failing_call_fn(pool, calls, flip=None),
                                      agent_instance_name='test-inst')

    assert calls.count('ep1-model') == 3, f"expected 3 attempts (max_retries=2); got {calls}"


# ── Pool doubles without the attributes are safe (strict is True + getattr defaults) ─


def test_predicates_are_safe_with_attributeless_and_magicmock_pools():
    """The new predicates must never crash or spuriously abort on pool doubles that lack the
    attributes (SimpleNamespace) or expose truthy auto-attributes (MagicMock), nor on None.
    This witnesses the ``getattr(..., 'stopped', False) is True`` strictness and the
    ``getattr(..., 'terminated_instances', None)`` guard at the new entry guard.
    (memory: magicmock-engine-defeats-terminal-stop-gate)"""
    # None pool: every predicate must be False (no crash).
    assert _stop_requested(None) is False
    assert _should_abort_lockfree(None, 'x') is False
    assert _should_abort_locked(None, 'x') is False

    # Attribute-less pool: the LOCK-FREE predicate (used at the entry guard) must apply
    # getattr defaults and not crash. The LOCK-TAKING _should_abort_locked is only ever called at
    # the sleep tick against a real pool, so it is NOT held to this contract — it delegates
    # to _check_termination, which requires is_instance_terminated. That split is the whole
    # point of the design, so we assert it here.
    bare = SimpleNamespace()
    assert _stop_requested(bare) is False
    assert _should_abort_lockfree(bare, 'x') is False

    # MagicMock pool: every attribute is a truthy Mock; strict ``is True`` must reject them
    # so the LOCK-FREE predicate never spuriously aborts.
    mm = MagicMock()
    assert _stop_requested(mm) is False
    assert _should_abort_lockfree(mm, 'x') is False

    # A real stop is still detected, and only a real True counts.
    stopped = SimpleNamespace(stopped=True)
    assert _stop_requested(stopped) is True
    assert _should_abort_lockfree(stopped, 'x') is True
    assert _should_abort_locked(stopped, 'x') is True
    truthy_not_true = SimpleNamespace(stopped='yes')  # truthy but not True
    assert _stop_requested(truthy_not_true) is False


# ── helpers._should_abort_locked: the interruptible-sleep tick honours a Stop ──────────────


def test_interruptible_sleep_aborts_on_stop():
    """The sleep tick must abort on a pool Stop (not only per-instance termination)."""
    pool = StopPool()
    pool.stopped = True
    import time
    t0 = time.monotonic()
    with pytest.raises(AgentTerminatedError):
        _interruptible_sleep(5.0, pool, 'x', interval=0.1)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"sleep must abort immediately on Stop; elapsed={elapsed:.2f}s"
