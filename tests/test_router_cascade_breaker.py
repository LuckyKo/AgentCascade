"""Router-cascade breaker tests (reports/router-cascade-fix-plan.md §9).

Covers the approved fix for the single-GPU model-swap storm:
- Change A: normalize_api_base correctness + idempotency; scheduler conc>0 pool keying.
- Change B: breaker state machine, atomic single-probe claim under concurrency,
  consult-before-fire chain filtering (incl. Tier-4 default).
- Change C: per-(base,model) cooldown independence on a shared base.
- Change D (D1): bounded fail-fast wait, zero HTTP while open, clean ServerBusyError
  degradation (never FallbackCompressionRequired), termination-aware sleep.
- Change E: bypass gates — _detect_context_window makes zero /models GETs for a
  breaker-open base; caption_images does not fire at the busy base.
- Concurrency (M5): two agents with DIFFERENT slot pools (conc=0 + conc>0) on the
  same normalized base during half_open → exactly ONE probe fired.
- Integration (mock, no GPU): busy base (503 "Failed to load model") + healthy
  different-base endpoint → zero hammering while open, failover works, exactly one
  probe after the window, recovery resumes; hot-loop check (breaker held open past
  the cap → clean degradation).

No real LLM/GPU/network required: HTTP is mocked or a local mock server is used.
"""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.retry_policy import RetryPolicy
from agent_cascade.exceptions import ServerBusyError, FallbackCompressionRequired
from agent_cascade.api_router_pkg.normalization import (
    normalize_api_base,
)
from agent_cascade.settings import (
    BREAKER_BASE_WINDOW_SECONDS,
    SERVER_BUSY_WAIT_CAP_SECONDS,
)
from agent_cascade.llm.oai import _breaker_blocks_base

# Shared fixtures + helpers now live in tests/conftest.py so they are auto-available
# to BOTH this file and the opt-in stress file (test_router_cascade_breaker_stress.py)
# regardless of collection order — the canonical cross-module fixture-sharing pattern.
# The `router` / `mock_servers` fixtures come from conftest automatically (no local
# definition needed). Plain helper functions are re-exported below so any external
# importer keeps working and there is a single source of truth in conftest.py.
from tests.conftest import (  # noqa: E402,F401
    FAST_RETRY_POLICY,
    BUSY_ERR_TEXT,
    _busy_error,
    _add_endpoint,
    _set_max_retries,
)


# ============================================================================
# Change A — normalization
# ============================================================================

class TestNormalizeApiBase:
    def test_basic_identity(self):
        assert normalize_api_base('http://127.0.0.1:1234/v1') == 'http://127.0.0.1:1234/v1'

    def test_localhost_mapped_to_loopback(self):
        assert normalize_api_base('http://localhost:1234/v1') == 'http://127.0.0.1:1234/v1'

    def test_scheme_lowercased(self):
        assert normalize_api_base('HTTP://MyHost:8080/v1') == 'http://myhost:8080/v1'

    def test_trailing_slash_stripped(self):
        assert normalize_api_base('http://127.0.0.1:1234/v1/') == 'http://127.0.0.1:1234/v1'

    def test_path_and_port_preserved(self):
        # Distinct from state_ops._normalize_api_base which strips /v1.
        assert normalize_api_base('http://host:9/llama/api') == 'http://host:9/llama/api'

    def test_idempotent(self):
        samples = [
            'http://localhost:1234/v1/',
            'HTTP://LocalHost:8080/V1',
            'http://[::1]:5678/v1',
            'http://remote.example.com:443/api/v1/',
        ]
        for s in samples:
            once = normalize_api_base(s)
            twice = normalize_api_base(once)
            assert once == twice, f"not idempotent for {s}: {once} != {twice}"

    def test_loopback_variants_collide(self):
        # Scheme/host case and trailing slash collapse; the PATH is preserved as-is
        # (spec: only scheme + host are lowercased), so /V1 stays distinct from /v1.
        a = normalize_api_base('http://localhost:1234/v1')
        b = normalize_api_base('http://127.0.0.1:1234/v1/')
        c = normalize_api_base('HTTP://LOCALHOST:1234/v1')
        assert a == b == c

    def test_path_case_preserved(self):
        # Path is NOT lowercased — only scheme and host are.
        assert normalize_api_base('HTTP://LOCALHOST:1234/V1') == 'http://localhost:1234/V1'.replace('localhost', '127.0.0.1')

    def test_distinct_servers_stay_distinct(self):
        assert normalize_api_base('http://127.0.0.1:1234/v1') != \
               normalize_api_base('http://127.0.0.1:1235/v1')
        assert normalize_api_base('http://127.0.0.1:1234/v1') != \
               normalize_api_base('http://192.168.1.5:1234/v1')


class TestSchedulerPoolKeying:
    def test_conc_gt_0_pools_share_normalized_key(self, router):
        """conc>0 pools for localhost vs 127.0.0.1 (same physical server) share one pool."""
        sched = router.scheduler
        r1 = sched.acquire('http://localhost:1234/v1', 2, instance_name='a')
        r2 = sched.acquire('http://127.0.0.1:1234/v1/', 2, instance_name='b')
        assert r1 is not None and r2 is not None
        with sched._lock:
            keys = [k for k in sched._pools.keys() if '1234' in k]
            assert len(keys) == 1, f"expected one shared pool, got {keys}"
        if r1: r1()
        if r2: r2()

    def test_conc_0_uses_shared_sequential_slot(self, router):
        sched = router.scheduler
        r = sched.acquire('http://127.0.0.1:1234/v1', 0, instance_name='a')
        assert r is not None
        with sched._lock:
            assert '_shared_sequential_slot_' in sched._pools
        r()


# ============================================================================
# Change B — breaker state machine + probe guard
# ============================================================================

class TestBreakerStateMachine:
    def test_closed_by_default(self, router):
        assert not router._breaker_should_skip('http://127.0.0.1:1234/v1')
        assert not router._breaker_is_open('http://127.0.0.1:1234/v1')

    def test_busy_error_trips_breaker(self, router):
        base = 'http://127.0.0.1:1234/v1'
        router._record_server_busy(base, _busy_error())
        br = router._server_breakers[normalize_api_base(base)]
        assert br['state'] == 'open'
        assert br['window'] == BREAKER_BASE_WINDOW_SECONDS
        assert router._breaker_should_skip(base)

    def test_real_per_model_errors_do_not_trip(self, router):
        base = 'http://127.0.0.1:1234/v1'
        for err in (Exception('404 - model not found'), Exception('400 - bad request payload')):
            assert not router._is_server_busy_loading(err)
        router._record_server_busy(base, Exception('404 - model not found'))
        assert normalize_api_base(base) not in router._server_breakers

    def test_open_to_half_open_after_window(self, router):
        base = 'http://127.0.0.1:1234/v1'
        router._record_server_busy(base, _busy_error())
        with router._lock:
            router._server_breakers[normalize_api_base(base)]['opened_at'] -= (BREAKER_BASE_WINDOW_SECONDS + 1)
        # Consult transitions to half_open and allows exactly the probe.
        assert not router._breaker_should_skip(base)
        br = router._server_breakers[normalize_api_base(base)]
        assert br['state'] == 'half_open'

    def test_probe_success_closes(self, router):
        base = 'http://127.0.0.1:1234/v1'
        router._record_server_busy(base, _busy_error())
        with router._lock:
            router._server_breakers[normalize_api_base(base)]['opened_at'] -= (BREAKER_BASE_WINDOW_SECONDS + 1)
        # Real single-step protocol: a winning consult claims the probe INLINE — this thread now holds it.
        assert not router._breaker_should_skip(base)
        assert router._caller_holds_probe(base)
        router._breaker_on_success(base)
        assert normalize_api_base(base) not in router._server_breakers

    def test_probe_failure_grows_window(self, router):
        base = 'http://127.0.0.1:1234/v1'
        router._record_server_busy(base, _busy_error())
        with router._lock:
            br = router._server_breakers[normalize_api_base(base)]
            br['opened_at'] -= (br['window'] + 1)
        assert not router._breaker_should_skip(base)
        assert router._caller_holds_probe(base)
        router._record_server_busy(base, _busy_error())  # probe failed
        br = router._server_breakers[normalize_api_base(base)]
        assert br['state'] == 'open'
        assert br['window'] == pytest.approx(BREAKER_BASE_WINDOW_SECONDS * 2)

    def test_probe_guard_released_on_failure(self, router):
        base = 'http://127.0.0.1:1234/v1'
        router._record_server_busy(base, _busy_error())
        with router._lock:
            router._server_breakers[normalize_api_base(base)]['opened_at'] -= (BREAKER_BASE_WINDOW_SECONDS + 1)
        # Winning consult claims the probe inline — this thread holds it.
        assert not router._breaker_should_skip(base)
        assert router._caller_holds_probe(base)
        router._breaker_release_probe(base)
        # After release the slot is freed (no wedged flag).
        assert not router._caller_holds_probe(base)
        # A SECOND consult in the same half_open state re-claims inline (probing was cleared):
        # it must return False AND re-hold — matching production.
        assert not router._breaker_should_skip(base)
        assert router._caller_holds_probe(base)
        router._breaker_release_probe(base)


class TestAtomicProbeClaim:
    def test_exactly_one_winner_under_concurrency(self, router):
        base = 'http://127.0.0.1:1234/v1'
        router._record_server_busy(base, _busy_error())
        with router._lock:
            router._server_breakers[normalize_api_base(base)]['opened_at'] -= (BREAKER_BASE_WINDOW_SECONDS + 1)

        n_threads = 16
        results = []
        barrier = threading.Barrier(n_threads)
        lock = threading.Lock()

        def worker():
            barrier.wait()
            won = router._breaker_claim_probe(base)
            with lock:
                results.append(won)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        assert sum(results) == 1, f"expected exactly one probe claim, got {sum(results)}"


# ============================================================================
# Change C — per-(base,model) cooldown independence
# ============================================================================

class TestPerModelCooldownIndependence:
    def test_shared_base_different_models_independent(self, router):
        base = 'http://127.0.0.1:1234/v1'
        id_m1 = _add_endpoint(router, "m1", base, model='model-a')
        id_m2 = _add_endpoint(router, "m2", base, model='model-b')
        router.set_agent_priorities('coder', [id_m1, id_m2])

        now = time.time()
        with router._lock:
            # Only model-a is cooling down.
            router._endpoint_failure_times[(normalize_api_base(base), 'model-a')] = now

        chain = router.get_endpoint_chain('coder')
        models = [c['model'] for c in chain]
        assert 'model-b' in models, "healthy model on shared base must remain in chain"
        assert 'model-a' not in models, "cooling-down (base,model) must be filtered out"

    def test_same_model_different_base_independent(self, router):
        id_s1 = _add_endpoint(router, "s1", 'http://127.0.0.1:1234/v1', model='shared-model')
        id_s2 = _add_endpoint(router, "s2", 'http://192.168.1.5:1234/v1', model='shared-model')
        router.set_agent_priorities('coder', [id_s1, id_s2])

        with router._lock:
            router._endpoint_failure_times[(normalize_api_base('http://127.0.0.1:1234/v1'), 'shared-model')] = time.time()

        chain = router.get_endpoint_chain('coder')
        bases = [c['api_base'] for c in chain]
        assert 'http://192.168.1.5:1234/v1' in bases
        assert 'http://127.0.0.1:1234/v1' not in bases


# ============================================================================
# Change B/D — consult-before-fire chain filtering (incl. Tier-4 default)
# ============================================================================

class TestChainFilteringWithBreaker:
    def test_tier4_default_skipped_when_breaker_open(self, router):
        """The default endpoint (Tier 4) is gated too — REVIEW M4."""
        import agent_cascade.api_router_pkg.router as router_mod
        # Hold the breaker open far past the per-call wait cap so D1 degrades fast.
        router._record_server_busy('http://default-api', _busy_error())
        with router._lock:
            router._server_breakers[normalize_api_base('http://default-api')]['window'] = 3600.0

        orig_cap = router_mod.SERVER_BUSY_WAIT_CAP_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # fast retries (frozen dataclass)
        router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = 1.0   # module global, read at call time
        try:
            calls = []
            def fn(cfg, *a, **k):
                calls.append(cfg.get('api_base'))
                return 'ok'

            with pytest.raises(ServerBusyError):
                router.call_with_fallback('coder', fn)
            assert calls == [], "no HTTP call may be fired at a breaker-open base"
        finally:
            router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = orig_cap
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)

    def test_failover_to_different_physical_server(self, router):
        id_h = _add_endpoint(router, "healthy", 'http://healthy-host:9/v1', model='hm')
        router.set_agent_priorities('coder', [id_h])
        # Busy base = the default's base.
        router._record_server_busy('http://default-api', _busy_error())

        orig_max_retries = _set_max_retries(router, 1)   # fast retries (frozen dataclass)
        calls = []
        def fn(cfg, *a, **k):
            calls.append(cfg.get('api_base'))
            if cfg.get('api_base') == 'http://default-api':
                raise _busy_error()
            return 'ok-healthy'

        try:
            result = router.call_with_fallback('coder', fn)
        finally:
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)
        assert result == 'ok-healthy'
        assert calls == ['http://healthy-host:9/v1'], "must failover immediately, zero calls to busy base"


# ============================================================================
# Change D (D1) — bounded fail-fast wait + clean degradation
# ============================================================================

class TestD1FailFast:
    def test_zero_http_and_clean_degradation(self, router):
        """Breaker held open past the cap → ServerBusyError, zero HTTP, no compression path."""
        # Hold the breaker open far past the per-call wait cap.
        router._record_server_busy('http://default-api', _busy_error())
        with router._lock:
            key = normalize_api_base('http://default-api')
            router._server_breakers[key]['window'] = 3600.0

        # Shrink the cap for a fast test (module global is read at call time).
        import agent_cascade.api_router_pkg.router as router_mod
        orig_cap = router_mod.SERVER_BUSY_WAIT_CAP_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # fast retries (frozen dataclass)
        router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = 1.0
        try:
            calls = []
            def fn(cfg, *a, **k):
                calls.append(1)
                raise _busy_error()

            t0 = time.monotonic()
            with pytest.raises(ServerBusyError) as exc_info:
                router.call_with_fallback('coder', fn)
            elapsed = time.monotonic() - t0
        finally:
            router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = orig_cap
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)

        assert calls == [], "zero HTTP requests while the breaker is open"
        assert elapsed >= 0.9, f"expected a bounded wait (~1s cap), got {elapsed:.2f}s"
        assert not isinstance(exc_info.value, FallbackCompressionRequired)
        assert "Server busy" in str(exc_info.value)

    def test_wait_is_termination_aware(self, router):
        """Terminating the instance mid-wait aborts promptly with AgentTerminatedError."""
        from agent_cascade.exceptions import AgentTerminatedError
        router._record_server_busy('http://default-api', _busy_error())
        with router._lock:
            key = normalize_api_base('http://default-api')
            router._server_breakers[key]['window'] = 3600.0

        class FakePool:
            def __init__(self): self.term = threading.Event()
            def is_instance_terminated(self, name): return self.term.is_set()
        pool = FakePool()
        router._pool = pool

        import agent_cascade.api_router_pkg.router as router_mod
        orig_cap = router_mod.SERVER_BUSY_WAIT_CAP_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # fast retries (frozen dataclass)
        router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = 10.0
        result = {}
        def worker():
            try:
                router.call_with_fallback('coder', lambda cfg, *a, **k: (_ for _ in ()).throw(_busy_error()),
                                          agent_instance_name='w1')
                result['err'] = None
            except Exception as e:
                result['err'] = e
        th = threading.Thread(target=worker)
        try:
            t0 = time.monotonic()
            th.start()
            time.sleep(1.5)
            pool.term.set()
            th.join(timeout=5)
            elapsed = time.monotonic() - t0
        finally:
            router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = orig_cap
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)
        assert isinstance(result['err'], AgentTerminatedError), f"got {type(result['err'])}"
        assert elapsed < 4.0, f"termination not honored promptly ({elapsed:.1f}s)"

    def test_recovery_after_window(self, router):
        """After the window elapses exactly one probe fires; success closes the breaker."""
        import agent_cascade.api_router_pkg.router as router_mod
        orig_window = router_mod.BREAKER_BASE_WINDOW_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # fast retries (frozen dataclass)
        router_mod.BREAKER_BASE_WINDOW_SECONDS = 0.5   # module global, read at trip time
        try:
            calls = []
            def fn(cfg, *a, **k):
                calls.append(1)
                if len(calls) == 1:
                    raise _busy_error()   # first call trips the breaker
                return 'recovered'

            result = router.call_with_fallback('coder', fn)
            assert result == 'recovered'
            assert normalize_api_base('http://default-api') not in router._server_breakers
        finally:
            router_mod.BREAKER_BASE_WINDOW_SECONDS = orig_window
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)


# ============================================================================
# Change E — bypass gates
# ============================================================================

class TestBypassGates:
    @pytest.fixture(autouse=True)
    def _isolate_breaker_gate(self):
        """The breaker_gate registry is module-level (weakrefs to ALL live routers).
        Other tests' routers may still be alive and hold breakers for the same base,
        which would spuriously gate this test. Snapshot and restore the registry."""
        from agent_cascade.api_router_pkg import breaker_gate
        saved = list(breaker_gate._routers)
        with breaker_gate._lock:
            breaker_gate._routers[:] = []
        yield
        with breaker_gate._lock:
            breaker_gate._routers[:] = saved

    def test_detect_context_window_zero_gets_when_open(self, router):
        """Breaker open → _detect_context_window makes zero /models GETs for that base."""
        from agent_cascade.llm.oai import TextChatAtOAI
        model = TextChatAtOAI({'api_base': 'http://127.0.0.1:1234/v1', 'model': 'm'})
        router._record_server_busy('http://127.0.0.1:1234/v1', _busy_error())

        with patch('agent_cascade.llm.oai.requests.get') as mock_get:
            model._detect_context_window('http://127.0.0.1:1234/v1', 'EMPTY')
            assert mock_get.call_count == 0, "no /models GET may be fired at a busy base"

    def test_detect_context_window_fires_when_closed(self, router):
        """Breaker closed → detection proceeds normally (gate is not over-blocking)."""
        from agent_cascade.llm.oai import TextChatAtOAI
        model = TextChatAtOAI({'api_base': 'http://127.0.0.1:1234/v1', 'model': 'm'})

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {'data': [{'id': 'm', 'context_length': 8192}]}
        with patch('agent_cascade.llm.oai.requests.get', return_value=fake_resp) as mock_get:
            model._detect_context_window('http://127.0.0.1:1234/v1', 'EMPTY')
            assert mock_get.call_count == 1

    def test_caption_images_does_not_fire_at_busy_base(self, router):
        id_v = _add_endpoint(router, "vision", 'http://busy-vision:9/v1', model='v-model')
        router.set_agent_priorities('coder', [id_v])
        router._record_server_busy('http://busy-vision:9/v1', _busy_error())

        from agent_cascade.llm.schema import Message, ContentItem
        msgs = [Message(role='user', content=[ContentItem(image='data:image/png;base64,AAAA')])]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_images(msgs, agent_type='coder')
            assert mock_gcm.call_count == 0, "no captioning call may fire at a busy base"
        # Image got the placeholder caption.
        item = msgs[0].content[0]
        cap = item.get('caption') if isinstance(item, dict) else getattr(item, 'caption', None)
        assert cap == '[Image]'


# ============================================================================
# Concurrency (M5) — different slot pools, same base, half_open → ONE probe
# ============================================================================

class TestM5SingleProbeAcrossSlotPools:
    def test_exactly_one_probe_two_pools(self, router):
        """Agent A (conc=0 shared sequential pool) and Agent B (conc>0 per-base pool)
        both target the same normalized base during half_open → exactly one probe."""
        import agent_cascade.api_router_pkg.router as router_mod
        base = 'http://127.0.0.1:1234/v1'
        sched = router.scheduler

        orig_max_retries = _set_max_retries(router, 1)   # no backoff sleeps (frozen dataclass)

        # Trip then elapse the window so the breaker is half_open.
        router._record_server_busy(base, _busy_error())
        with router._lock:
            router._server_breakers[normalize_api_base(base)]['opened_at'] -= (BREAKER_BASE_WINDOW_SECONDS + 1)

        # Both agents acquire slots from DIFFERENT pools on the same base.
        release_a = sched.acquire('http://localhost:1234/v1', 0, instance_name='agentA')   # shared sequential pool
        release_b = sched.acquire('http://127.0.0.1:1234/v1/', 2, instance_name='agentB')  # conc>0 normalized pool
        assert release_a is not None and release_b is not None

        probes_fired = []
        probe_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def agent(name):
            barrier.wait()
            # Real single-step protocol: a winning consult claims the probe INLINE (this thread holds it);
            # losers return True and skip. Mirrors production's call_with_fallback path exactly.
            if router._breaker_should_skip(base):
                return  # loser — skip/fail fast, no HTTP
            with probe_lock:
                probes_fired.append(name)   # this thread won the single-probe claim
            # Probe HTTP would go here (no lock held). Simulate outcome:
            time.sleep(0.1)
            router._breaker_on_success(base)
            router._breaker_release_probe(base)

        threads = [threading.Thread(target=agent, args=('A',)), threading.Thread(target=agent, args=('B',))]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)

        assert len(probes_fired) == 1, f"expected exactly ONE probe, got {probes_fired}"
        release_a()
        release_b()
        object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)


# ============================================================================
# Integration (mock HTTP, no GPU) — busy base + healthy different-base endpoint
# ============================================================================

# The `mock_servers` fixture and the `_MockLLMHandler` class now live in tests/conftest.py
# (shared with the opt-in stress file). `mock_servers` is auto-available as a conftest
# fixture; no local definition is needed here.

class TestIntegrationMockServers:
    def test_no_hammering_failover_single_probe_recovery(self, router, mock_servers):
        """Full D1+B integration: N models on the busy base + healthy different base.

        Asserts: zero hammering at the busy base while open; failover to the healthy
        base works; exactly one probe after the window; recovery resumes service.
        """
        import agent_cascade.api_router_pkg.router as router_mod
        busy_base = mock_servers['busy']
        healthy_base = mock_servers['healthy']

        # N models on ONE physical (busy) server + one healthy endpoint elsewhere.
        ids = [_add_endpoint(router, f"busy_m{i}", busy_base, model=f'model-{i}') for i in range(3)]
        id_h = _add_endpoint(router, "healthy", healthy_base, model='hm')
        router.set_agent_priorities('coder', ids + [id_h])

        # Monkeypatch execute_api_call's underlying call_fn to hit the mock servers.
        import requests as real_requests
        def fake_call(cfg, *a, **k):
            base = cfg.get('api_base')
            r = real_requests.post(f"{base}/chat/completions", json={'model': cfg['model'], 'messages': []}, timeout=5)
            if r.status_code != 200:
                raise Exception(f"{r.status_code} - {r.text}")
            return 'ok'

        orig_window = router_mod.BREAKER_BASE_WINDOW_SECONDS
        orig_cap = router_mod.SERVER_BUSY_WAIT_CAP_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # one attempt per endpoint (frozen dataclass)
        router_mod.BREAKER_BASE_WINDOW_SECONDS = 1.0   # short window for the test
        router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = 2.0
        try:
            t0 = time.monotonic()
            result = router.call_with_fallback('coder', fake_call)
            elapsed = time.monotonic() - t0
        finally:
            router_mod.BREAKER_BASE_WINDOW_SECONDS = orig_window
            router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = orig_cap
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)

        assert result == 'ok'
        busy_hits = mock_servers['busy_ref']['hits'].get('/v1/chat/completions', 0)
        healthy_hits = mock_servers['healthy_ref']['hits'].get('/v1/chat/completions', 0)
        # Zero hammering: at most one attempt per model before the breaker trips (3) +
        # the single probe after the window (1). Hard bound — NOT one call per model per cycle.
        assert busy_hits <= 4, f"busy base was hammered {busy_hits} times"
        assert healthy_hits >= 1, "failover to the healthy base must have happened"

        # ── Recovery: after the open window elapses, exactly ONE probe fires at the
        # busy base; once it succeeds (server flipped healthy) the breaker closes and
        # service resumes there.
        with router._lock:
            br = router._server_breakers.get(normalize_api_base(busy_base))
            assert br is not None, "breaker must still be open after the first call"
            # Elapse the window so the next consult transitions to half_open.
            br['opened_at'] -= (br['window'] + 1)
        mock_servers['busy_ref']['busy'] = False   # server recovered

        calls2 = []
        def fake_call2(cfg, *a, **k):
            calls2.append(cfg.get('api_base'))
            return fake_call(cfg, *a, **k)

        result2 = router.call_with_fallback('coder', fake_call2)
        assert result2 == 'ok'
        busy_hits2 = mock_servers['busy_ref']['hits'].get('/v1/chat/completions', 0) - busy_hits
        # Exactly one probe at the recovered base (single-probe guard), then done.
        assert busy_hits2 == 1, f"expected exactly ONE probe after the window, got {busy_hits2}"
        # Recovery: breaker closed after a successful probe.
        assert normalize_api_base(busy_base) not in router._server_breakers

    def test_hot_loop_held_open_past_cap_degrades(self, router, mock_servers):
        """Breaker held open past the wait cap → clean ServerBusyError degradation.

        The whole chain (Tier-1 busy endpoint + Tier-4 default) must sit on the SAME
        breaker-open physical server for D1 to engage: if a different-base default
        remained, failover would succeed instead of degrading. So point the default at
        the busy base too — this is the real "single-GPU storm" scenario where every
        endpoint shares one sequential loader.
        """
        busy_base = mock_servers['busy']
        router.default_llm_cfg = {
            'api_base': busy_base, 'model': 'default-model', 'max_tokens': 2048,
        }
        id_b = _add_endpoint(router, "busy_m0", busy_base, model='model-0')
        router.set_agent_priorities('coder', [id_b])

        import agent_cascade.api_router_pkg.router as router_mod
        orig_cap = router_mod.SERVER_BUSY_WAIT_CAP_SECONDS
        orig_max_retries = _set_max_retries(router, 1)   # fast retries (frozen dataclass)
        router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = 1.0   # module global, read at call time
        try:
            # Hold the breaker open far past the cap (never recovers).
            router._record_server_busy(busy_base, _busy_error())
            with router._lock:
                router._server_breakers[normalize_api_base(busy_base)]['window'] = 3600.0

            hits_before = sum(mock_servers['busy_ref']['hits'].values())
            t0 = time.monotonic()
            with pytest.raises(ServerBusyError):
                router.call_with_fallback('coder', lambda cfg, *a, **k: (_ for _ in ()).throw(_busy_error()))
            elapsed = time.monotonic() - t0
            hits_after = sum(mock_servers['busy_ref']['hits'].values())
        finally:
            router_mod.SERVER_BUSY_WAIT_CAP_SECONDS = orig_cap
            object.__setattr__(router.policy, 'endpoint_max_retries', orig_max_retries)

        assert elapsed >= 0.9, "bounded wait must be honored"
        assert hits_after == hits_before, "zero HTTP to the busy base while held open"
