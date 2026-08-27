"""Part 2 — sanity-probe TRIGGER fix (per-connection health, NOT a global TTL).

The production bug: ``pre_validate_endpoint_chain`` probed EVERY endpoint not already
committed by this instance (per-connection marker, no TTL) on every
``call_with_fallback`` entry, so a healthy LIVE primary was re-probed on every turn AND
every engine retry → llama.cpp accept-queue exhaustion.

This suite locks in the intended model using a LOCAL HTTP stub that counts requests per
base and distinguishes the probe (GET /models) from the real call (POST /chat/completions).
The ``call_fn`` passed to ``call_with_fallback`` makes a REAL POST to the selected endpoint's
stub (mirroring the production llm.chat path), so POSTs are counted AND a hanging stub
produces a genuine connection timeout.

Covered:
  - NO re-probe of a live primary across turns or engine retries (the core flood fix).
  - Exactly ONE probe on a fresh acquisition (new instance / post-timeout).
  - Walk-until-live: head unreachable → its probe fails once + cooldown; second probed once,
    committed; the failed head is NOT re-probed immediately on retry.
  - A connection TIMEOUT releases the live marker → next acquisition re-probes from top.

Uses the REAL APIRouter + real call_with_fallback against the local stub. SANITY_PROBE_ENABLED
is left at its default (True) — that is the whole point of this fix.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import agent_cascade.api_router_pkg.router as router_mod
from tests.conftest import _add_endpoint  # noqa: F401 (shared helper; 'router' fixture from conftest)


# ============================================================================
# Local HTTP stub — counts probe GETs and chat POSTs PER BASE
# ============================================================================

class _CountingHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible stub.

    - GET  /models             → 200 (the sanity probe). Counted as a PROBE.
    - POST /chat/completions   → behavior driven by the per-server ``ref`` dict:
        * ref['timeout'] → sleep past the client read timeout then 200 (a hanging
                           connection → requests ReadTimeout on the caller side)
      Counted as a POST.

    ``ref`` is bound as a CLASS attribute on a per-server subclass (see _StubServer):
    http.server resets handler instance state between requests, so an instance attribute set
    in __init__ can be lost by do_GET/do_POST — a class attribute is always found via lookup.
    """

    def log_message(self, *a):
        pass

    def _count(self, kind):
        r = self.ref or {}
        r[kind] = r.get(kind, 0) + 1

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/models', '/v1/models'):
            self._count('probes')
            body = {'data': [{'id': 'm', 'object': 'model'}]}
            code = 200
        else:
            body = {}
            code = 404
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        if length:
            self.rfile.read(length)
        r = self.ref or {}
        # Simulate a hanging connection (client-side read timeout).
        if r.get('timeout'):
            time.sleep(12.0)
        self._count('posts')
        body = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubServer:
    """One local OpenAI-compatible endpoint with per-base request counters."""

    def __init__(self, ref):
        self.ref = ref
        ref.setdefault('probes', 0)
        ref.setdefault('posts', 0)
        ref.setdefault('timeout', False)

        _ref = ref  # alias so the class body can see the enclosing local

        class Handler(_CountingHandler):
            ref = _ref

        self.srv = ThreadingHTTPServer(('127.0.0.1', _free_port()), Handler)
        self.base = f'http://127.0.0.1:{self.srv.server_address[1]}/v1'
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass


@pytest.fixture
def stubs():
    """Two local stub servers (head + second) with independent request counters."""
    head_ref = {'probes': 0, 'posts': 0, 'timeout': False}
    second_ref = {'probes': 0, 'posts': 0, 'timeout': False}
    head = _StubServer(head_ref)
    second = _StubServer(second_ref)
    head.start()
    second.start()
    yield {'head': head, 'second': second, 'head_ref': head_ref, 'second_ref': second_ref}
    head.stop()
    second.stop()


def _wire(router, stubs):
    """Register both stub endpoints and make them the 'coder' priority chain (head first)."""
    _add_endpoint(router, 'head', stubs['head'].base, model='head-model', concurrency_limit=-1)
    _add_endpoint(router, 'second', stubs['second'].base, model='second-model', concurrency_limit=-1)
    eps = router.list_endpoints()
    head_id = next(e.id for e in eps if e.api_base == stubs['head'].base)
    second_id = next(e.id for e in eps if e.api_base == stubs['second'].base)
    router.set_agent_priorities('coder', [head_id, second_id])


def _http_call(llm_cfg, *a, **k):
    """A call_fn that makes a REAL POST to the selected endpoint (like production llm.chat).

    Returns the model name on success; raises requests' exception on failure/timeout so
    call_with_fallback's per-endpoint retry/fallback machinery runs for real.
    """
    base = (llm_cfg.get('api_base') or llm_cfg.get('model_server', '')).rstrip('/')
    url = f"{base}/chat/completions"
    resp = requests.post(url, json={'messages': [{'role': 'user', 'content': 'hi'}]}, timeout=(2.0, 4.0))
    resp.raise_for_status()
    return llm_cfg.get('model')


@pytest.fixture(autouse=True)
def _probe_on():
    """Ensure the sanity probe is ENABLED for these tests (the whole point of the fix)."""
    assert router_mod.SANITY_PROBE_ENABLED is True, \
        "these tests require SANITY_PROBE_ENABLED to be True"


# ============================================================================
# Core flood fix: no re-probe of a live primary
# ============================================================================

class TestNoReprobeOnLivePrimary:
    def test_second_turn_does_not_reprobe(self, router, stubs):
        """Establish a live connection on head; a second call fires NO probe GET (only POST)."""
        _wire(router, stubs)

        # Turn 1 — fresh acquisition: exactly ONE probe to head, then the POST.
        r1 = router.call_with_fallback('coder', _http_call, agent_instance_name='instA')
        assert r1 == 'head-model'
        assert stubs['head_ref']['probes'] == 1, "fresh acquisition must probe head exactly once"
        assert stubs['head_ref']['posts'] == 1

        # Turn 2 — same instance, connection still LIVE: NO probe, only the POST.
        r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='instA')
        assert r2 == 'head-model'
        assert stubs['head_ref']['probes'] == 1, \
            "a live primary must NOT be re-probed on the next turn (flood fix)"
        assert stubs['head_ref']['posts'] == 2

    def test_engine_retry_of_live_connection_does_not_reprobe(self, router, stubs):
        """Simulate an engine retry: a call that fails once (transient) then succeeds while the
        connection is still live. The re-entry into call_with_fallback must NOT re-probe."""
        _wire(router, stubs)

        # Fresh acquisition first (records the live endpoint).
        r1 = router.call_with_fallback('coder', _http_call, agent_instance_name='instA')
        assert r1 == 'head-model'
        probes_after_first = stubs['head_ref']['probes']
        assert probes_after_first == 1

        # Engine retry: a call that fails once (non-deterministic → clears live marker) but the
        # endpoint is still reachable, so it recovers within the same call_with_fallback. After
        # recovery the live marker is re-set. A SUBSEQUENT engine-retry entry must not re-probe.
        calls = {'n': 0}

        def flaky(llm_cfg2, *a2, **k2):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('transient hiccup (non-deterministic)')
            return _http_call(llm_cfg2, *a2, **k2)

        r2 = router.call_with_fallback('coder', flaky, agent_instance_name='instA')
        assert r2 == 'head-model'
        # The transient failure + recovery happens inside ONE call_with_fallback; the probe gate
        # is only consulted at entry (once). No additional probe GETs may fire.
        assert stubs['head_ref']['probes'] == probes_after_first, \
            "engine retry of a still-live connection must not re-probe"

        # A further turn on the recovered live connection: still no probe.
        r3 = router.call_with_fallback('coder', _http_call, agent_instance_name='instA')
        assert r3 == 'head-model'
        assert stubs['head_ref']['probes'] == probes_after_first, \
            "after recovery the live connection is re-committed — no re-probe"


# ============================================================================
# Fresh acquisition: exactly one probe before the first POST
# ============================================================================

class TestFreshAcquisitionProbesOnce:
    def test_fresh_instance_probes_once(self, router, stubs):
        """A brand-new instance (no live connection) probes head exactly once before POSTing."""
        _wire(router, stubs)
        r = router.call_with_fallback('coder', _http_call, agent_instance_name='fresh')
        assert r == 'head-model'
        assert stubs['head_ref']['probes'] == 1
        assert stubs['head_ref']['posts'] == 1

    def test_different_instances_do_not_share_live_state(self, router, stubs):
        """Live-connection state is PER INSTANCE — a second instance must probe on its own."""
        _wire(router, stubs)
        router.call_with_fallback('coder', _http_call, agent_instance_name='instA')
        assert stubs['head_ref']['probes'] == 1

        # A DIFFERENT instance has no live connection to head → it probes once itself.
        r = router.call_with_fallback('coder', _http_call, agent_instance_name='instB')
        assert r == 'head-model'
        assert stubs['head_ref']['probes'] == 2, \
            "live-connection state must be per-instance, not global"


# ============================================================================
# Walk-until-live: head unreachable → second probed once → commit to second
# ============================================================================

class TestWalkUntilLive:
    def test_dead_head_walks_to_second(self, router, stubs):
        """Head is unreachable (closed port) → its probe fails once, it enters cooldown; the
        walk probes second once and commits there."""
        dead_port = _free_port()  # nothing listening here
        dead_base = f'http://127.0.0.1:{dead_port}/v1'
        _add_endpoint(router, 'dead', dead_base, model='dead-model', concurrency_limit=-1)
        _add_endpoint(router, 'second', stubs['second'].base, model='second-model', concurrency_limit=-1)
        eps = router.list_endpoints()
        dead_id = next(e.id for e in eps if e.api_base == dead_base)
        second_id = next(e.id for e in eps if e.api_base == stubs['second'].base)
        router.set_agent_priorities('coder', [dead_id, second_id])

        r = router.call_with_fallback('coder', _http_call, agent_instance_name='instW')
        assert r == 'second-model'
        # The dead head was probed exactly once (and failed); second was probed once and used.
        assert stubs['second_ref']['probes'] == 1, "walk must probe the live endpoint exactly once"
        assert stubs['second_ref']['posts'] == 1

        # The dead head entered cooldown → it is filtered out of the chain on the next
        # acquisition and is NOT re-probed immediately.
        from agent_cascade.api_router_pkg.normalization import normalize_api_base
        with router._lock:
            in_cooldown = (normalize_api_base(dead_base), 'dead-model') in router._endpoint_failure_times
        assert in_cooldown, "a probe-failed endpoint must enter cooldown"

        # Second acquisition: head is cooled down (no re-probe of it); second is now LIVE for
        # this instance → no re-probe of second either. Only the POST fires.
        r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='instW')
        assert r2 == 'second-model'
        assert stubs['second_ref']['probes'] == 1, \
            "after committing to second it is live — no re-probe on the next acquisition"


# ============================================================================
# Lazy probe: a live primary costs ZERO probes to its (dead) fallbacks
# ============================================================================

class TestLazyProbeSkipsUnreachedFallbacks:
    """The WinError 10055 fix: with chain [healthy_primary, dead_a, dead_b], the eager
    pre_validate_endpoint_chain used to probe dead_a AND dead_b on EVERY turn — even though
    the healthy primary is committed and the fallbacks are never reached. Lazy probing must
    touch ONLY the endpoint actually tried."""

    def test_dead_fallbacks_never_probed_when_primary_live(self, router):
        """Chain [healthy, dead_a, dead_b]: only the healthy primary is probed (turn 1) or
        fast-pathed (turn 2+); dead_a and dead_b receive ZERO requests of any kind."""
        head = _StubServer({'probes': 0, 'posts': 0, 'timeout': False})
        head.start()
        try:
            # Two dead endpoints: closed ports — nothing listening.
            dead_a_base = f'http://127.0.0.1:{_free_port()}/v1'
            dead_b_base = f'http://127.0.0.1:{_free_port()}/v1'

            _add_endpoint(router, 'head', head.base, model='head-model', concurrency_limit=-1)
            _add_endpoint(router, 'dead_a', dead_a_base, model='dead-a-model', concurrency_limit=-1)
            _add_endpoint(router, 'dead_b', dead_b_base, model='dead-b-model', concurrency_limit=-1)
            eps = router.list_endpoints()
            ids = [next(e.id for e in eps if e.api_base == b)
                   for b in (head.base, dead_a_base, dead_b_base)]
            router.set_agent_priorities('coder', ids)

            # Turn 1 — fresh acquisition: the healthy head is probed once and used. The two
            # dead fallbacks are never reached → they must receive ZERO requests (the eager
            # probe would have fired a GET /models at each of them).
            r1 = router.call_with_fallback('coder', _http_call, agent_instance_name='lazyA')
            assert r1 == 'head-model'
            assert head.ref['probes'] == 1, "fresh acquisition must probe the live primary once"
            assert head.ref['posts'] == 1

            # Turn 2 — same instance, primary still committed: NO probes at all (fast path).
            r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='lazyA')
            assert r2 == 'head-model'
            assert head.ref['probes'] == 1, "committed live primary must not be re-probed"

            # The invariant: the dead fallbacks were NEVER touched. A probe failure records a
            # cooldown entry — its absence proves each dead endpoint was never probed (a closed
            # port cannot be reached by any other request path).
            from agent_cascade.api_router_pkg.normalization import normalize_api_base
            with router._lock:
                for base, model in ((dead_a_base, 'dead-a-model'), (dead_b_base, 'dead-b-model')):
                    assert (normalize_api_base(base), model) not in router._endpoint_failure_times, \
                        f"dead fallback {base} must never be probed while the primary is live"
        finally:
            head.stop()

    def test_dead_fallbacks_never_probed_when_primary_live_multi_turn(self, router):
        """Repeated turns on a live primary: total probe count stays at exactly 1 and the dead
        fallbacks' bases never appear in the cooldown store (never probed)."""
        head = _StubServer({'probes': 0, 'posts': 0, 'timeout': False})
        head.start()
        try:
            dead_a_base = f'http://127.0.0.1:{_free_port()}/v1'
            dead_b_base = f'http://127.0.0.1:{_free_port()}/v1'

            _add_endpoint(router, 'head', head.base, model='head-model', concurrency_limit=-1)
            _add_endpoint(router, 'dead_a', dead_a_base, model='dead-a-model', concurrency_limit=-1)
            _add_endpoint(router, 'dead_b', dead_b_base, model='dead-b-model', concurrency_limit=-1)
            eps = router.list_endpoints()
            ids = [next(e.id for e in eps if e.api_base == b)
                   for b in (head.base, dead_a_base, dead_b_base)]
            router.set_agent_priorities('coder', ids)

            from agent_cascade.api_router_pkg.normalization import normalize_api_base
            dead_keys = {
                (normalize_api_base(dead_a_base), 'dead-a-model'),
                (normalize_api_base(dead_b_base), 'dead-b-model'),
            }

            for turn in range(3):
                r = router.call_with_fallback('coder', _http_call, agent_instance_name='lazyB')
                assert r == 'head-model'

            # Exactly ONE probe fired across all turns (turn 1); turns 2-3 are fast-pathed.
            assert head.ref['probes'] == 1, \
                f"live primary must be probed exactly once across {3} turns, got {head.ref['probes']}"
            assert head.ref['posts'] == 3

            # Neither dead fallback was ever probed → no cooldown entries for them.
            with router._lock:
                probed_dead = [k for k in dead_keys if k in router._endpoint_failure_times]
            assert not probed_dead, \
                f"dead fallbacks must never be probed while the primary is live: {probed_dead}"
        finally:
            head.stop()


# ============================================================================
# Timeout → slot release → next acquisition re-probes from the top
# ============================================================================

class TestTimeoutReleasesLive:
    def test_timeout_clears_live_marker_and_reprobes(self, router, stubs):
        """A connection timeout (non-deterministic failure) releases the live marker, so the
        NEXT fresh acquisition re-probes the endpoint from the top."""
        _wire(router, stubs)

        # Establish a live connection on head.
        r1 = router.call_with_fallback('coder', _http_call, agent_instance_name='instT')
        assert r1 == 'head-model'
        assert stubs['head_ref']['probes'] == 1

        # Now make head HANG (read timeout). The POST times out → non-deterministic failure →
        # live marker cleared. Second is a healthy fallback, so the call still succeeds there.
        stubs['head_ref']['timeout'] = True
        try:
            r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='instT')
        finally:
            stubs['head_ref']['timeout'] = False
        assert r2 == 'second-model', "after head times out the call must fail over to second"

        # The live marker for head must have been released by the timeout (it is now second, or
        # absent — either way head is no longer marked live).
        from agent_cascade.api_router_pkg.normalization import normalize_api_base
        with router._lock:
            committed = router._instance_committed_endpoint.get('instT')
        assert committed != (normalize_api_base(stubs['head'].base), 'head-model'), \
            "a connection timeout must release the endpoint's live marker"

        # The exhausted-retries path ALSO records head into _endpoint_failure_times (cooldown) —
        # that is correct per the model: an endpoint that timed out is not re-probed until its
        # cooldown expires. To test the "re-probe from the top" behavior we simulate cooldown
        # expiry by clearing head's failure record (head is healthy again now).
        _head_key = (normalize_api_base(stubs['head'].base), 'head-model')
        with router._lock:
            router._endpoint_failure_times.pop(_head_key, None)

        # Next fresh acquisition: head is no longer live for this instance AND its cooldown has
        # expired → the walk re-probes head from the top. Head is healthy again now, so it
        # passes and is used. This proves the timeout released the slot (no stale fast-path).
        r3 = router.call_with_fallback('coder', _http_call, agent_instance_name='instT')
        assert r3 == 'head-model'
        # A new probe GET to head fired (re-acquisition from the top after release + cooldown expiry).
        assert stubs['head_ref']['probes'] >= 2, \
            "after a timeout releases the slot, the next acquisition must re-probe from the top"


# ============================================================================
# Sticky-slot RELEASE clears the committed-endpoint marker (regression)
# ============================================================================

class TestStickySlotReleaseClearsCommittedMarker:
    """Regression: releasing a held sticky permit via the sync_sticky_slot drop path
    (which calls _drop_held_permit) MUST clear this instance's committed-endpoint probe
    fast-path marker. Otherwise a re-acquired instance would fast-path (skip the sanity
    probe) against a connection that no longer exists — firing a real call on a dead
    endpoint without probing."""

    def test_drop_held_permit_clears_committed_marker_and_reprobes(self, router, stubs):
        """Drive the REAL drop path (sync_sticky_slot → _drop_held_permit) and prove:
          1. committing to head sets the marker;
          2. releasing the sticky slot clears it;
          3. a fresh acquisition re-probes head (no stale fast-path)."""
        from agent_cascade.api_router_pkg.normalization import normalize_api_base

        _wire(router, stubs)
        head_key = (normalize_api_base(stubs['head'].base), 'head-model')

        # ── Step 1: commit to head via a real successful call → marker set. ──
        r1 = router.call_with_fallback('coder', _http_call, agent_instance_name='instR')
        assert r1 == 'head-model'
        with router._lock:
            committed = router._instance_committed_endpoint.get('instR')
        assert committed == head_key, \
            "a successful call must commit the endpoint (probe fast-path marker set)"

        # ── Step 2: release the sticky slot via the REAL path that calls _drop_held_permit. ──
        # A lightweight instance exposing exactly the state sync_sticky_slot touches. The held
        # key (head's normalized base) differs from the desired key (None → conc>0/-1, no slot),
        # so sync takes the "stay-slotless" drop branch and invokes _drop_held_permit.
        import threading

        class _FakeInstance:
            agent_class = 'coder'
            instance_name = 'instR'

        inst = _FakeInstance()
        inst._state_lock = threading.Lock()
        inst._slot_key = head_key[0]  # holds the per-base permit for head
        inst._slot_release = lambda: None  # release is a no-op; only the marker matters here

        router.sync_sticky_slot(inst, desired_key=None, origin='sticky')

        with inst._state_lock:
            assert inst._slot_release is None and inst._slot_key is None, \
                "_drop_held_permit must nullify the held permit state"
        with router._lock:
            committed_after = router._instance_committed_endpoint.get('instR')
        assert committed_after is None, \
            "releasing the sticky slot (_drop_held_permit) must clear the committed-endpoint marker"

        # ── Step 3: a fresh acquisition must RE-PROBE head (no stale fast-path). ──
        probes_before = stubs['head_ref']['probes']
        r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='instR')
        assert r2 == 'head-model'
        assert stubs['head_ref']['probes'] > probes_before, \
            "after the sticky slot is released, the next acquisition must re-probe head (no fast-path)"
