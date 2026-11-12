"""BUG_0210 — a busy/loading 503 must not truncate the endpoint card's retry count.

Symptom: with a busy physical server (llama-autoloader model-load/queue 503, or any
OpenAI-compatible ``503``), the router gave up on the current endpoint after ONE
attempt and failed over as soon as the next chain entry lived on a different base —
even when the endpoint card said ``Retries = 1`` or ``2``. The card's retry budget is
the user's contract: N retries means N+1 attempts before failover, regardless of the
error class.

Cross-caller protection is unchanged for a genuinely BUSY base: the first base-scoped
busy error (connection cap, queue, "server busy") still trips the per-server breaker, so
every OTHER caller skips this base until the window/probe matures. A MODEL-scoped loading
failure (`Failed to load model '<id>'`) is deliberately excluded - the base is still
healthy for every other model it serves (BUG_0297). Only the caller already inside its
retry loop spends its card budget either way.

Run: pytest tests/test_retry_bug0210.py -v
"""

import pytest

import agent_cascade.api_router_pkg.router as router_mod
from agent_cascade.api_router import APIEndpoint, APIRouter
from agent_cascade.llm.base import ModelServiceError

BUSY_BASE = 'http://busy-bug0210.test:9001/v1'
NEXT_BASE = 'http://next-bug0210.test:9002/v1'
AGENT_TYPE = 'bug0210-type'


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """No network probes, no cooldowns, and deterministic zero-delay backoff."""
    monkeypatch.setattr(router_mod, 'SANITY_PROBE_ENABLED', False)
    monkeypatch.setattr(router_mod, 'ENDPOINT_COOLDOWN_SECONDS', 0)
    delays = []

    def fake_sleep(duration, pool, instance_name, interval=0.5):
        delays.append(duration)

    monkeypatch.setattr(router_mod, '_interruptible_sleep', fake_sleep)
    return delays


def make_router():
    """Fresh router with an empty endpoint registry (ignores the shared test config)."""
    router = APIRouter(default_llm_cfg={
        'model': 'global-default',
        'api_base': 'http://global-default.test:9000/v1',
        'model_type': 'qwenvl_oai',
    })
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    return router


def install_chain(router, card_retries):
    """Busy endpoint (card retries) → different-base healthy endpoint."""
    ep_busy = APIEndpoint(
        name='busy-card-ep',
        api_base=BUSY_BASE,
        model='busy-model',
        max_retries=card_retries,
    )
    ep_next = APIEndpoint(
        name='next-base-ep',
        api_base=NEXT_BASE,
        model='next-model',
        max_retries=1,
    )
    router.add_endpoint(ep_busy)
    router.add_endpoint(ep_next)
    router.set_agent_priorities(AGENT_TYPE, [ep_busy.id, ep_next.id])
    return ep_busy, ep_next


def model_load_503():
    """MODEL-scoped: this model could not be loaded; other models on the base are fine."""
    return ModelServiceError(message='Failed to load model', code=503)


def base_busy_503():
    """BASE-scoped: the server itself is saturated (connection cap)."""
    return ModelServiceError(message='Too many concurrent connections', code=503)


@pytest.mark.parametrize('card_retries', [0, 1, 2])
def test_busy_503_honors_card_retry_count_before_cross_base_failover(card_retries):
    """Card=N → exactly N+1 attempts on the busy endpoint, then failover to the next base."""
    router = make_router()
    install_chain(router, card_retries)

    busy_calls = []
    next_calls = []

    def call_fn(llm_cfg):
        if llm_cfg.get('api_base') == BUSY_BASE:
            busy_calls.append(llm_cfg.get('model'))
            raise model_load_503()
        next_calls.append(llm_cfg.get('model'))
        return 'ok'

    result = router.call_with_fallback(AGENT_TYPE, call_fn,
                                       agent_instance_name='bug0210-inst')

    assert result == 'ok'
    assert len(busy_calls) == card_retries + 1, (
        f"card Retries={card_retries} must produce {card_retries + 1} attempts on the "
        f"busy endpoint before failover, got {len(busy_calls)}")
    assert len(next_calls) == 1, 'failover must reach the different-base endpoint'


def test_busy_503_plain_text_signature_also_honors_card_retries():
    """Autoloader-style text (``503 - Failed to load model ...``) is the same contract."""
    router = make_router()
    install_chain(router, card_retries=2)

    busy_calls = []

    def call_fn(llm_cfg):
        if llm_cfg.get('api_base') == BUSY_BASE:
            busy_calls.append(llm_cfg.get('model'))
            raise Exception('503 - Failed to load model: qwen2.5 failed to start')
        return 'ok'

    router.call_with_fallback(AGENT_TYPE, call_fn, agent_instance_name='bug0210-text')

    assert len(busy_calls) == 3


def test_model_scoped_503_does_not_skip_other_models_on_same_base():
    """BUG_0297: one model refusing to load must not blacklist its whole base.

    Replays the 2026-11-12 11:51 incident shape: two models share one autoloader base,
    the first 503s with "Failed to load model '<id>'", the second is healthy. Before the
    fix the base breaker opened and the healthy model was skipped without a request.
    """
    router = make_router()
    base = 'http://shared-bug0297.test:9100/v1'
    ep_a = APIEndpoint(name='model-a-ep', api_base=base, model='model-a', max_retries=0)
    ep_b = APIEndpoint(name='model-b-ep', api_base=base, model='model-b', max_retries=0)
    router.add_endpoint(ep_a)
    router.add_endpoint(ep_b)
    router.set_agent_priorities(AGENT_TYPE, [ep_a.id, ep_b.id])

    calls = []

    def call_fn(llm_cfg):
        calls.append(llm_cfg.get('model'))
        if llm_cfg.get('model') == 'model-a':
            raise model_load_503()
        return 'ok'

    result = router.call_with_fallback(AGENT_TYPE, call_fn, agent_instance_name='bug0297-inst')

    assert result == 'ok'
    assert calls == ['model-a', 'model-b'], (
        'the healthy model on the same base must still be called')
    assert not router._breaker_is_open(base), (
        'a model-scoped load failure must not trip the per-base breaker')


def test_base_busy_503_still_trips_breaker_for_other_callers():
    """The card budget is per caller; the breaker still stops OTHER callers hammering
    a genuinely saturated base (connection cap / queue), even after BUG_0297."""
    router = make_router()
    install_chain(router, card_retries=2)

    first_busy_calls = []
    first_next_calls = []

    def first_call(llm_cfg):
        if llm_cfg.get('api_base') == BUSY_BASE:
            first_busy_calls.append(1)
            raise base_busy_503()
        first_next_calls.append(1)
        return 'ok'

    router.call_with_fallback(AGENT_TYPE, first_call, agent_instance_name='bug0210-first')
    assert len(first_busy_calls) == 3
    assert router._breaker_is_open(BUSY_BASE), 'busy 503 must still trip the breaker'

    # Second caller: the breaker-open base is skipped WITHOUT firing (no cross-caller
    # hammering), so it fails over immediately.
    second_busy_calls = []
    second_next_calls = []

    def second_call(llm_cfg):
        if llm_cfg.get('api_base') == BUSY_BASE:
            second_busy_calls.append(1)
            raise base_busy_503()
        second_next_calls.append(1)
        return 'ok'

    result = router.call_with_fallback(AGENT_TYPE, second_call,
                                       agent_instance_name='bug0210-second')

    assert result == 'ok'
    assert second_busy_calls == [], 'breaker-open base must not be hit by another caller'
    assert second_next_calls == [1]
