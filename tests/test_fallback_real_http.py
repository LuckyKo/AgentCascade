"""Part C — REAL-HTTP integration tests for the sticky-slot / fallback system.

Replaces the fake-green mock-based edge tests with tests that use a LOCAL HTTP
stub + per-base request counter and can actually catch the two production
incidents:

  (1) priority-swap hang      → TestPrioritySwapDoesNotHang
  (2) model-loader request flood → TestFloodBoundUnderContention / TestNoReprobeCommitted

The stub counts EVERY request per base, distinguishing the sanity probe
(GET /models) from the real call (POST /chat/completions). The ``call_fn`` passed
to ``call_with_fallback`` makes a REAL POST to the selected endpoint's stub
(mirroring the production llm.chat path), so timeouts are genuine connection
timeouts and every request is counted.

The engine retry layer (engine/llm_call.py::_execute_llm_call_with_retry) re-enters
``call_with_fallback`` up to ``retry_max_attempts`` times per turn; these tests drive
that same loop faithfully (a plain for-loop over the attempt count), so the
measured request counts are exactly what the engine would produce.

SANITY_PROBE_ENABLED is left at its default (True) — that is the whole point of
these tests.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import agent_cascade.api_router_pkg.router as router_mod
from agent_cascade.retry_policy import RetryPolicy
from tests.conftest import _add_endpoint  # noqa: F401 (shared helper; 'router' fixture from conftest)


# ============================================================================
# Local HTTP stub — counts probe GETs and chat POSTs PER BASE, with injectable
# failure modes (immediate success / N-failures-then-success / persistent
# timeout / 503 busy / auth failure).
# ============================================================================

class _CountingHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible stub.

    - GET  /models             → behavior driven by ref['probe_status'] (default 200).
                                 Counted as a PROBE.
    - POST /chat/completions   → behavior driven by the per-server ``ref`` dict:
         * ref['timeout']      → sleep past the client read timeout then 200
                                 (a hanging connection → requests ReadTimeout)
         * ref['fail_n']       → first N POSTs fail with ref['fail_status'],
                                 subsequent POSTs succeed (transient failure)
         * ref['status']       → fixed status for every POST (persistent mode)
       Counted as a POST.

    ``ref`` is bound as a CLASS attribute on a per-server subclass (see _StubServer):
    http.server resets handler instance state between requests, so an instance
    attribute set in __init__ can be lost by do_GET/do_POST — a class attribute is
    always found via lookup. All ref mutations are guarded by ref['lock'] because
    the ThreadingHTTPServer serves concurrent requests from multiple threads.
    """

    def log_message(self, *a):
        pass

    def _count(self, kind):
        r = self.ref or {}
        with r.get('lock') or threading.Lock():
            r[kind] = r.get(kind, 0) + 1

    def _respond(self, status, body):
        """Send the response; ignore errors from a client that already timed out and
        closed the connection (the request was still counted — that is the point)."""
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/models', '/v1/models'):
            self._count('probes')
            r = self.ref or {}
            with r.get('lock') or threading.Lock():
                status = int(r.get('probe_status', 200))
            body = {'data': [{'id': 'm', 'object': 'model'}]} if status == 200 else \
                   {'error': {'message': 'probe rejected'}}
        else:
            status = 404
            body = {}
        self._respond(status, body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        if length:
            try:
                self.rfile.read(length)
            except (ConnectionResetError, OSError):
                return
        r = self.ref or {}
        # Simulate a hanging connection (client-side read timeout). The sleep happens
        # BEFORE the response is written, so the client's read timeout fires first.
        # 6s > client read timeout (4s in _http_call); daemon threads die with the process.
        if r.get('timeout'):
            time.sleep(6.0)
        with r.get('lock') or threading.Lock():
            self._count_locked(r, 'posts')
            fail_n = int(r.get('fail_n', 0))
            fixed_status = int(r.get('status', 0))
            status = fixed_status if fixed_status else \
                     (int(r.get('fail_status', 503)) if r['posts'] <= fail_n else 200)
        body = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]} \
               if status == 200 else \
               {'error': {'message': f'simulated failure {status}'}}
        self._respond(status, body)

    @staticmethod
    def _count_locked(r, kind):
        r[kind] = r.get(kind, 0) + 1


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
        ref.setdefault('fail_n', 0)
        ref.setdefault('fail_status', 503)
        ref.setdefault('status', 0)
        ref.setdefault('probe_status', 200)
        ref.setdefault('lock', threading.Lock())

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


def _new_ref():
    return {'probes': 0, 'posts': 0, 'timeout': False, 'fail_n': 0,
            'fail_status': 503, 'status': 0, 'probe_status': 200}


@pytest.fixture
def stubs(request):
    """Up to three local stub servers with independent request counters.

    Usage: ``stubs(1)`` / ``stubs(2)`` / ``stubs(3)`` returns a dict with keys
    'head', 'second' (, 'third') plus '<name>_ref' per server. Servers are created
    lazily on first request and stopped at test end.
    """
    created = {}

    def factory(n):
        for name in ('head', 'second', 'third'):
            if name not in created:
                ref = _new_ref()
                srv = _StubServer(ref)
                srv.start()
                created[name] = (srv, ref)
        out = {}
        for i in range(1, n + 1):
            name = ('head', 'second', 'third')[i - 1]
            srv, ref = created[name]
            out[name] = srv
            out[f'{name}_ref'] = ref
        return out

    yield factory
    for srv, _ref in created.values():
        srv.stop()


def _wire(router, stub_map, names):
    """Register the given stub endpoints and make them the 'coder' priority chain."""
    ids = []
    for name in names:
        base = stub_map[name].base
        _add_endpoint(router, name, base, model=f'{name}-model', concurrency_limit=-1)
        eps = router.list_endpoints()
        eid = next(e.id for e in eps if e.api_base == base)
        ids.append(eid)
    router.set_agent_priorities('coder', ids)
    return ids


def _http_call(llm_cfg, *a, **k):
    """A call_fn that makes a REAL POST to the selected endpoint (like production llm.chat).

    Returns the model name on success; raises requests' exception on failure/timeout so
    call_with_fallback's per-endpoint retry/fallback machinery runs for real.
    """
    base = (llm_cfg.get('api_base') or llm_cfg.get('model_server', '')).rstrip('/')
    url = f"{base}/chat/completions"
    resp = requests.post(url, json={'messages': [{'role': 'user', 'content': 'hi'}]},
                         timeout=(2.0, 4.0))
    resp.raise_for_status()
    return llm_cfg.get('model')


@pytest.fixture(autouse=True)
def _probe_on():
    """Ensure the sanity probe is ENABLED for these tests (the whole point of the fix)."""
    assert router_mod.SANITY_PROBE_ENABLED is True, \
        "these tests require SANITY_PROBE_ENABLED to be True"


# ============================================================================
# 1. Flood bound under contention — catches the WinError-10053/10055 flood incident
# ============================================================================

class TestFloodBoundUnderContention:
    """Chain of >=3 endpoints, head persistently down (timeout), later ones recover.
    Drive call_with_fallback through a faithful engine-retry loop and assert the TOTAL
    probe GETs + POSTs to the dead base is BOUNDED — not growing unboundedly."""

    def _engine_retry_loop(self, router, inst_name, attempts):
        """Faithful mirror of engine/llm_call.py::_execute_llm_call_with_retry: re-enter
        call_with_fallback up to `attempts` times, returning on first success."""
        last_exc = None
        for _ in range(attempts):
            try:
                return router.call_with_fallback('coder', _http_call,
                                                 agent_instance_name=inst_name)
            except Exception as e:
                last_exc = e
        raise last_exc

    def test_dead_head_request_count_is_bounded(self, router, stubs):
        """3-endpoint chain; head hangs on POST (persistent timeout). The engine-retry
        loop must succeed via the live second/third endpoints while the total request
        count to the dead head stays BOUNDED (small constant × attempts)."""
        sm = stubs(3)
        _wire(router, sm, ['head', 'second', 'third'])

        # Head: probe passes (GET /models 200) but every POST hangs → ReadTimeout.
        sm['head_ref']['timeout'] = True
        # Second: first POST fails with 503 (transient), then succeeds — simulates the
        # "later ones recover" model-load race.
        sm['second_ref']['fail_n'] = 1

        attempts = router.policy.retry_max_attempts  # default 3
        result = self._engine_retry_loop(router, 'floodA', attempts)
        assert result in ('second-model', 'third-model'), \
            f"Call must succeed on a live endpoint, got {result!r}"

        head_total = sm['head_ref']['probes'] + sm['head_ref']['posts']
        bound = 2 * attempts
        assert head_total <= bound, (
            f"FLOOD: dead head received {head_total} requests "
            f"(probes={sm['head_ref']['probes']}, posts={sm['head_ref']['posts']}) "
            f"across {attempts} engine retries — must be bounded by {bound}"
        )
        # The live endpoints were actually used.
        assert sm['second_ref']['posts'] >= 1 or sm['third_ref']['posts'] >= 1

    def test_dead_head_bounded_when_all_live_endpoints_also_flaky(self, router, stubs):
        """Worst case: head hangs AND second is flaky (fails every first POST). Even then
        the dead head's request count must stay bounded across the retry loop."""
        sm = stubs(3)
        _wire(router, sm, ['head', 'second', 'third'])

        sm['head_ref']['timeout'] = True
        # Second fails its first N POSTs (N >= attempts) then recovers; third is solid.
        sm['second_ref']['fail_n'] = 5

        attempts = router.policy.retry_max_attempts
        result = self._engine_retry_loop(router, 'floodB', attempts)
        assert result in ('second-model', 'third-model')

        head_total = sm['head_ref']['probes'] + sm['head_ref']['posts']
        bound = 2 * attempts
        assert head_total <= bound, (
            f"FLOOD: dead head received {head_total} requests across {attempts} retries "
            f"— must be bounded by {bound}"
        )


# ============================================================================
# 2. No re-probe of a live/committed endpoint (call_with_fallback level)
# ============================================================================

class TestNoReprobeCommitted:
    """Establish success on head, then run again (and simulate an engine retry while
    still committed) → assert 0 probe GETs to that base on subsequent turns.

    Complements test_probe_trigger.py at the call_with_fallback level with a
    3-endpoint chain and explicit engine-retry re-entry."""

    def test_subsequent_turn_and_engine_retry_fire_zero_probes(self, router, stubs):
        sm = stubs(3)
        _wire(router, sm, ['head', 'second', 'third'])

        # Turn 1 — fresh acquisition: exactly ONE probe to head, then the POST.
        r1 = router.call_with_fallback('coder', _http_call, agent_instance_name='commitA')
        assert r1 == 'head-model'
        assert sm['head_ref']['probes'] == 1
        assert sm['head_ref']['posts'] == 1

        # Turn 2 — same instance, connection still committed: NO probe GETs.
        r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='commitA')
        assert r2 == 'head-model'
        assert sm['head_ref']['probes'] == 1, \
            "a committed live endpoint must NOT be re-probed on the next turn"

        # Simulated engine retry while still committed: a flaky call that fails once
        # (non-deterministic) then succeeds. The re-entry into call_with_fallback must
        # not re-probe head either.
        calls = {'n': 0}

        def flaky(llm_cfg2, *a2, **k2):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('transient hiccup (non-deterministic)')
            return _http_call(llm_cfg2, *a2, **k2)

        r3 = router.call_with_fallback('coder', flaky, agent_instance_name='commitA')
        assert r3 == 'head-model'
        assert sm['head_ref']['probes'] == 1, \
            "engine retry of a still-committed connection must not re-probe"

        # And one more plain turn: still zero probes.
        r4 = router.call_with_fallback('coder', _http_call, agent_instance_name='commitA')
        assert r4 == 'head-model'
        assert sm['head_ref']['probes'] == 1
        assert sm['head_ref']['posts'] == 4


# ============================================================================
# 3. Priority-swap does NOT hang — regression test for the cursor fix at
#    integration level (catches incident #1)
# ============================================================================

class TestPrioritySwapDoesNotHang:
    """A worker thread runs a real call loop; mid-flight, the main thread calls the REAL
    set_agent_priorities to reorder. The worker must COMPLETE within a timeout:
    t.join(timeout=15); assert not t.is_alive(). Sync via threading.Event (no sleep)."""

    def test_swap_mid_flight_worker_completes(self, router, stubs):
        sm = stubs(3)
        ids = _wire(router, sm, ['head', 'second', 'third'])
        head_id, second_id, third_id = ids

        # Make head flaky: first POST fails (503), then succeeds. This gives the worker a
        # real in-flight failure window in which the main thread performs the swap —
        # with the pre-fix stale positional cursor this topology hung.
        sm['head_ref']['fail_n'] = 1

        reached_retry_point = threading.Event()
        results, errors = [], []

        def call_fn(llm_cfg, *a, **k):
            base = (llm_cfg.get('api_base') or llm_cfg.get('model_server', '')).rstrip('/')
            url = f"{base}/chat/completions"
            resp = requests.post(url, json={'messages': [{'role': 'user', 'content': 'hi'}]},
                                 timeout=(2.0, 4.0))
            if resp.status_code != 200:
                # Signal the main thread: the worker is now at its retry point (the failed
                # head attempt is done, call_with_fallback is about to back off / fail over).
                reached_retry_point.set()
                raise requests.HTTPError(f'{resp.status_code} simulated failure')
            return llm_cfg.get('model')

        def worker():
            try:
                attempts = router.policy.retry_max_attempts
                for _ in range(attempts):
                    r = router.call_with_fallback('coder', call_fn,
                                                  agent_instance_name='swapW')
                    results.append(r)
                    if r is not None:
                        break
            except Exception as e:
                errors.append(f'{type(e).__name__}: {e}')

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Wait for the worker to actually reach its retry point (Event sync — no sleep).
        assert reached_retry_point.wait(timeout=10), \
            "worker never reached its retry point within 10s"

        # Mid-flight: the REAL set_agent_priorities reorders the chain.
        router.set_agent_priorities('coder', [second_id, head_id, third_id])

        t.join(timeout=15)
        assert not t.is_alive(), \
            "PRIORITY-SWAP HANG: worker did not complete within 15s after a live reorder"
        assert not errors, f"worker raised: {errors}"
        # The success must be on an endpoint of the (possibly reordered) chain.
        assert results and results[0] in ('head-model', 'second-model', 'third-model')

    def test_swap_to_shorter_chain_worker_completes(self, router, stubs):
        """Swap to a SHORTER chain mid-flight (stale cursor would be out of range)."""
        sm = stubs(3)
        ids = _wire(router, sm, ['head', 'second', 'third'])
        head_id, second_id, third_id = ids

        sm['head_ref']['fail_n'] = 1
        reached_retry_point = threading.Event()
        results, errors = [], []

        def call_fn(llm_cfg, *a, **k):
            base = (llm_cfg.get('api_base') or llm_cfg.get('model_server', '')).rstrip('/')
            resp = requests.post(f"{base}/chat/completions",
                                 json={'messages': [{'role': 'user', 'content': 'hi'}]},
                                 timeout=(2.0, 4.0))
            if resp.status_code != 200:
                reached_retry_point.set()
                raise requests.HTTPError(f'{resp.status_code} simulated failure')
            return llm_cfg.get('model')

        def worker():
            try:
                for _ in range(router.policy.retry_max_attempts):
                    r = router.call_with_fallback('coder', call_fn,
                                                  agent_instance_name='swapW2')
                    results.append(r)
                    if r is not None:
                        break
            except Exception as e:
                errors.append(f'{type(e).__name__}: {e}')

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert reached_retry_point.wait(timeout=10), "worker never reached its retry point"

        # Reorder to a shorter chain (2 endpoints instead of 3).
        router.set_agent_priorities('coder', [second_id, head_id])

        t.join(timeout=15)
        assert not t.is_alive(), \
            "PRIORITY-SWAP HANG: worker did not complete within 15s after swap to shorter chain"
        assert not errors, f"worker raised: {errors}"


# ============================================================================
# 4. Walk-until-live ordering
# ============================================================================

class TestWalkUntilLiveOrdering:
    """Head down (probe fails), second up → commit lands on second, head entered cooldown
    (not re-probed immediately), and total probes == number of endpoints actually tried."""

    def test_head_down_walks_to_second(self, router, stubs):
        sm = stubs(2)
        ids = _wire(router, sm, ['head', 'second'])

        # Head is DOWN at the probe level (GET /models → 503).
        sm['head_ref']['probe_status'] = 503

        r = router.call_with_fallback('coder', _http_call, agent_instance_name='walkA')
        assert r == 'second-model', \
            f"commit must land on the live second endpoint, got {r!r}"

        # Total probes == number of endpoints actually tried (head + second).
        total_probes = sm['head_ref']['probes'] + sm['second_ref']['probes']
        assert total_probes == 2, \
            f"total probes must equal endpoints tried (2), got {total_probes}"
        assert sm['head_ref']['probes'] == 1, "down head probed exactly once"
        assert sm['second_ref']['probes'] == 1, "live second probed exactly once"

        # Head entered cooldown → NOT re-probed immediately on the next acquisition.
        from agent_cascade.api_router_pkg.normalization import normalize_api_base
        with router._lock:
            in_cooldown = (normalize_api_base(sm['head'].base), 'head-model') \
                          in router._endpoint_failure_times
        assert in_cooldown, "probe-failed head must enter cooldown"

        # Second acquisition: head cooled down (no re-probe), second committed (no probe).
        r2 = router.call_with_fallback('coder', _http_call, agent_instance_name='walkA')
        assert r2 == 'second-model'
        assert sm['head_ref']['probes'] == 1, \
            "cooled-down head must not be re-probed immediately"
        assert sm['second_ref']['probes'] == 1, \
            "committed second must not be re-probed"

    def test_head_down_post_only_probe_passes(self, router, stubs):
        """Variant: head's probe PASSES but its POST hangs (timeout). The walk still lands
        on second; the dead head is probed once and enters cooldown via the real-call
        failure path."""
        sm = stubs(2)
        _wire(router, sm, ['head', 'second'])

        sm['head_ref']['timeout'] = True  # POST hangs → ReadTimeout

        r = router.call_with_fallback('coder', _http_call, agent_instance_name='walkB')
        assert r == 'second-model'
        assert sm['head_ref']['probes'] == 1
        assert sm['second_ref']['probes'] == 1
        total_probes = sm['head_ref']['probes'] + sm['second_ref']['probes']
        assert total_probes == 2

        # Head (timeout) entered cooldown via the exhaustion path.
        from agent_cascade.api_router_pkg.normalization import normalize_api_base
        with router._lock:
            in_cooldown = (normalize_api_base(sm['head'].base), 'head-model') \
                          in router._endpoint_failure_times
        assert in_cooldown, "timeout-exhausted head must enter cooldown"
