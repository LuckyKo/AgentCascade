"""OPT-IN concurrency STRESS tests for the router per-server circuit breaker.

Deferred follow-up to the router-cascade fix (commits 8f4103c + b7e4bbd). These
prove that under HIGH CONCURRENCY (24-32 concurrent agents across mixed slot pools)
hitting ONE busy base over several open -> half_open -> close cycles, the breaker
holds its invariants:

  1. exactly ONE probe fires per half-open window;
  2. HTTP traffic to the busy base stays BOUNDED (no hammering);
  3. no deadlock / no thread hangs.

Test-only change: NO production code (agent_cascade/**) is touched.

Fast, deterministic and self-contained — no GPU/network beyond local mock servers
and a pure state-machine test that needs no server at all. Target runtime < ~5-8s
when run in isolation.

EXCLUDED from the default test run by design: every test here carries
``@pytest.mark.stress`` and ``pytest.ini`` adds ``and not stress`` to the default
marker expression (exactly like live_api / skip_if_no_local / extra_*). To run them:

    python -m pytest tests/test_router_cascade_breaker_stress.py -o addopts= -q --no-header -m stress

The shared fixtures/helpers (router, mock_servers, _add_endpoint, _set_max_retries,
_busy_error, FAST_RETRY_POLICY) live in ``tests/conftest.py`` and are auto-available
to this file as conftest fixtures — no duplication and no fragile ``pytest_plugins``.
Plain helper functions are imported by name from conftest below.
"""

import threading
import time

import pytest

# Shared plain helpers (NOT fixtures) — import by name from conftest. The `router` and
# `mock_servers` FIXTURES come from tests/conftest.py automatically; they do not need to
# be imported here (and importing them would not register them as fixtures anyway).
from tests.conftest import (  # noqa: E402
    _add_endpoint,
    _set_max_retries,
    _busy_error,
)

from agent_cascade.api_router_pkg.normalization import normalize_api_base  # noqa: E402
from agent_cascade.settings import BREAKER_BASE_WINDOW_SECONDS  # noqa: E402
from agent_cascade.exceptions import ServerBusyError  # noqa: E402


# Concurrency fan-out for the stress tests.
N_THREADS = 32          # Test 1: pure state-machine race (no HTTP).
N_INTEGRATION = 24      # Test 2: real concurrent call_with_fallback traffic.

# Per-test timeout override. The repo default is --timeout=60; under parallel load a
# concurrency stress test can transiently exceed that, so give it an explicit headroom.
STRESS_TIMEOUT = 120


@pytest.mark.stress
@pytest.mark.timeout(STRESS_TIMEOUT)
class TestBreakerStress:
    """High-concurrency invariants for the per-server circuit breaker."""

    def test_single_probe_under_heavy_concurrency_many_cycles(self, router):
        """Pure state-machine stress (no HTTP server needed — fastest).

        Over SEVERAL open -> half_open -> close cycles, N threads all race to claim
        THE single half-open probe for one normalized base. Exactly ONE may win per
        cycle; the rest must skip/fail-fast. Also proves no thread hangs.
        """
        base = 'http://127.0.0.1:1234/v1'
        n_cycles = 5

        orig_max_retries = _set_max_retries(router, 1)   # no backoff sleeps (frozen dataclass)
        try:
            for cycle in range(n_cycles):
                # Trip the breaker, then rewind opened_at so it is half_open.
                router._record_server_busy(base, _busy_error())
                with router._lock:
                    router._server_breakers[normalize_api_base(base)]['opened_at'] -= (
                        BREAKER_BASE_WINDOW_SECONDS + 1
                    )

                probes_fired = []
                probe_lock = threading.Lock()
                barrier = threading.Barrier(N_THREADS)

                def worker(tid):
                    barrier.wait()
                    # Consult-before-fire: returns False ONLY for the single winner
                    # (it transitions open->half_open and claims the probe inline).
                    if router._breaker_should_skip(base):
                        return  # loser — skip / fail fast, no HTTP
                    with probe_lock:
                        probes_fired.append(tid)
                    time.sleep(0.05)          # simulate the probe's HTTP round-trip (no lock held)
                    router._breaker_on_success(base)     # success -> close the breaker
                    router._breaker_release_probe(base)  # clear the probe slot

                threads = [threading.Thread(target=worker, args=(i,), name=f"probe-{cycle}-{i}")
                           for i in range(N_THREADS)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=15)

                # No hang: every thread must have finished.
                assert not any(t.is_alive() for t in threads), (
                    f"cycle {cycle}: a probe thread is still alive (possible deadlock)"
                )
                # Exactly ONE probe fired this cycle.
                assert len(probes_fired) == 1, (
                    f"cycle {cycle}: expected exactly ONE probe, got {len(probes_fired)} -> {probes_fired}"
                )
        finally:
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)

        # Each cycle ended on a successful probe -> breaker closed (popped from the dict).
        assert normalize_api_base(base) not in router._server_breakers, (
            "breaker should be closed after every cycle ended on success"
        )

    def test_bounded_http_no_deadlock_under_concurrency(self, router, mock_servers):
        """Full-ish integration stress: N concurrent call_with_fallback calls against a
        busy base + one healthy different-base endpoint.

        Proves bounded HTTP (no hammering), working failover, and no deadlock under
        real concurrent traffic over the short open window.
        """
        import agent_cascade.api_router_pkg.router as router_mod
        import requests as real_requests

        busy_base = mock_servers['busy']
        healthy_base = mock_servers['healthy']

        # 3 models on ONE physical (busy) server + one healthy endpoint elsewhere.
        ids = [_add_endpoint(router, f"busy_m{i}", busy_base, model=f'model-{i}') for i in range(3)]
        id_h = _add_endpoint(router, "healthy", healthy_base, model='hm')
        router.set_agent_priorities('coder', ids + [id_h])

        # Monkeypatch the underlying call_fn to hit the mock servers (mirrors the
        # existing integration test): POST {base}/chat/completions, raise on non-200.
        def fake_call(cfg, *a, **k):
            base = cfg.get('api_base')
            r = real_requests.post(
                f"{base}/chat/completions",
                json={'model': cfg['model'], 'messages': []},
                timeout=5,
            )
            if r.status_code != 200:
                raise Exception(f"{r.status_code} - {r.text}")
            return 'ok'

        orig_window = router_mod.BREAKER_BASE_WINDOW_SECONDS
        orig_cap = router_mod.SERVER_BUSY_WAIT_CAP_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # one attempt per endpoint (frozen dataclass)
        router_mod.BREAKER_BASE_WINDOW_SECONDS = 0.3     # short open window for the test
        router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = 1.0    # bounded fail-fast wait
        try:
            barrier = threading.Barrier(N_INTEGRATION)
            results = [None] * N_INTEGRATION
            exceptions = [None] * N_INTEGRATION

            def worker(i):
                barrier.wait()   # start all threads together to maximize the race
                try:
                    results[i] = router.call_with_fallback('coder', fake_call)
                except Exception as e:  # noqa: BLE001 - record, don't crash the pool
                    exceptions[i] = e

            threads = [threading.Thread(target=worker, args=(i,), name=f"agent-{i}")
                       for i in range(N_INTEGRATION)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            # No deadlock: every thread must have finished.
            assert not any(t.is_alive() for t in threads), (
                "a call_with_fallback thread is still alive (possible deadlock)"
            )

            busy_hits = mock_servers['busy_ref']['hits'].get('/v1/chat/completions', 0)
            healthy_hits = mock_servers['healthy_ref']['hits'].get('/v1/chat/completions', 0)

            # Bounded HTTP — NOT one call per thread per cycle. Evidence-based rationale for
            # the bound (measured with an instrumented replica of this test, 3 runs):
            #   * initial-race floor = N_INTEGRATION (24): every thread's very first attempt at
            #     busy_m0 lands while the breaker is still 'closed' (before any of them trips it).
            #     This is unavoidable without a global in-flight gate, so the bound MUST be >= 24.
            #   * + single-probes: at most ONE probe per half-open window (~1-2 across the short
            #     0.3s window cycles).
            #   * + bounded prober retries: the designated prober completes its probe attempt
            #     (within-endpoint retries on a re-tripped base), ~3-6 extra hits.
            #   * Worst-case observed total = 32. Naive no-breaker hammering would be
            #     N_INTEGRATION x 3 models = 72+.
            # So a hard bound comfortably above the worst case (32) and the initial-race floor
            # (24), but far below the 72 naive-hammering number. The old <= 20 was impossible
            # with N=24 concurrent threads (the initial-race floor alone is 24).
            assert busy_hits <= 40, (
                f"busy base was hammered {busy_hits} times (expected bounded; "
                f"initial-race floor={N_INTEGRATION}, worst-case observed=32, naive-hammering=72)"
            )

            # Failover still works: the healthy base got at least one hit.
            assert healthy_hits >= 1, "failover to the healthy base must have happened"

            # No thread raised an UNEXPECTED exception type. ServerBusyError / RuntimeError
            # are acceptable degradations (record them); anything else is a real bug.
            # NOTE: `exceptions` holds None for threads that SUCCEEDED — exclude those before
            # the isinstance check (isinstance(None, ...) is False, so a naive filter would
            # wrongly flag every successful thread as "unexpected").
            unexpected = [
                e for e in exceptions
                if e is not None and not isinstance(e, (ServerBusyError, RuntimeError))
            ]
            assert not unexpected, f"unexpected exception types under concurrency: {unexpected}"
        finally:
            router_mod.BREAKER_BASE_WINDOW_SECONDS = orig_window
            router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = orig_cap
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)
