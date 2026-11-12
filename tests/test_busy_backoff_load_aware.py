"""Load-aware busy-503 retry backoff (router inner retry budget).

A SERVER_BUSY_LOADING 503 (model JIT load) must NOT be retried on the generic
~1s backoff — the per-endpoint budget can never span a real load. These tests
pin the busy-aware delay: a configured floor, honouring Retry-After, bounded by
the call deadline, and byte-identical to the generic backoff when disabled.

No real LLM/GPU/network required; the delay is computed, never slept.
"""

import random
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock

import pytest

import agent_cascade.api_router_pkg.router as router_mod
from agent_cascade.api_router_pkg.router import _retry_after_seconds
from agent_cascade.llm.base import ModelServiceError
from agent_cascade.retry_policy import calculate_backoff

# Busy (base-saturated) and model-scoped (per-model refusal) error helpers, plus
# the fast retry policy, come from the shared conftest — single source of truth.
from tests.conftest import _busy_error, _model_load_error, FAST_RETRY_POLICY


def _err_with_headers(headers):
    """Build an error whose ``.response.headers`` is the given mapping.

    Mirrors the httpx/requests shape the helper is written to handle (no assumption
    about which HTTP client produced it).
    """
    err = ModelServiceError(message='503 - Too many concurrent connections')
    resp = MagicMock()
    resp.headers = headers
    err.response = resp
    return err


def _busy_error_with_retry_after(value):
    """Busy 503 carrying ``Retry-After: <value>``, like an autoloader recovery 503."""
    inner = _err_with_headers({'Retry-After': value})
    return ModelServiceError(exception=inner, code='503')


class TestBusyAwareDelay:

    def test_knob_zero_matches_generic_backoff_without_retry_after(self, router, monkeypatch):
        # Disabled knob + no Retry-After + a non-binding deadline → identical to the
        # generic backoff. (A busy 503 that DOES carry Retry-After is not byte-identical
        # — the header is honoured even at knob=0; see test_retry_after_header_wins.)
        # Seed is defensive: FAST_RETRY_POLICY is jitter-free, so the delay is already
        # deterministic, but pinning both keeps the equivalence robust.
        monkeypatch.setattr(router_mod, 'SERVER_BUSY_RETRY_DELAY_SECONDS', 0.0)
        monkeypatch.setattr(router_mod, 'LLM_CALL_DEADLINE_SECONDS', 2400)
        policy = router.policy
        seed = 1234567

        random.seed(seed)
        expected = calculate_backoff(2, policy)
        random.seed(seed)
        got = router._busy_aware_delay(_busy_error(), 2)

        assert got == expected

    def test_knob_floor_applied(self, router, monkeypatch):
        monkeypatch.setattr(router_mod, 'SERVER_BUSY_RETRY_DELAY_SECONDS', 30.0)
        monkeypatch.setattr(router_mod, 'LLM_CALL_DEADLINE_SECONDS', 2400)

        got = router._busy_aware_delay(_busy_error(), 1)

        assert got == 30.0

    def test_retry_after_header_wins(self, router, monkeypatch):
        monkeypatch.setattr(router_mod, 'SERVER_BUSY_RETRY_DELAY_SECONDS', 0.0)
        monkeypatch.setattr(router_mod, 'LLM_CALL_DEADLINE_SECONDS', 2400)

        got = router._busy_aware_delay(_busy_error_with_retry_after('45'), 1)

        assert got == 45.0

    def test_max_of_knob_and_retry_after(self, router, monkeypatch):
        # The larger of the floor and Retry-After governs; here the floor wins.
        monkeypatch.setattr(router_mod, 'SERVER_BUSY_RETRY_DELAY_SECONDS', 50.0)
        monkeypatch.setattr(router_mod, 'LLM_CALL_DEADLINE_SECONDS', 2400)

        got = router._busy_aware_delay(_busy_error_with_retry_after('20'), 1)

        assert got == 50.0

    def test_retry_after_wins_over_floor(self, router, monkeypatch):
        # RA-wins direction of the max (complements the floor-wins case above): when
        # Retry-After exceeds the floor, the header governs.
        monkeypatch.setattr(router_mod, 'SERVER_BUSY_RETRY_DELAY_SECONDS', 10.0)
        monkeypatch.setattr(router_mod, 'LLM_CALL_DEADLINE_SECONDS', 2400)

        got = router._busy_aware_delay(_busy_error_with_retry_after('45'), 1)

        assert got == 45.0

    def test_model_scoped_refusal_excluded_from_busy_gate(self, router):
        # The gate that selects the busy-aware path must reject a per-model refusal
        # (so it keeps the generic backoff — never a long load-spanning wait), while
        # admitting a base-saturated 503. The else-branch (generic backoff) is the
        # unchanged path already covered by the broader router/retry suite.
        assert router._is_server_busy_loading(_model_load_error()) is False
        assert router._is_server_busy_loading(_busy_error()) is True

    def test_knob_capped_by_deadline(self, router, monkeypatch):
        monkeypatch.setattr(router_mod, 'SERVER_BUSY_RETRY_DELAY_SECONDS', 999.0)
        monkeypatch.setattr(router_mod, 'LLM_CALL_DEADLINE_SECONDS', 60)

        got = router._busy_aware_delay(_busy_error(), 1)

        assert got == 60.0


class TestRetryAfterSeconds:

    def test_integer_seconds(self):
        assert _retry_after_seconds(_err_with_headers({'Retry-After': '45'})) == 45.0

    def test_float_seconds(self):
        assert _retry_after_seconds(_err_with_headers({'Retry-After': '1.5'})) == 1.5

    def test_case_insensitive_key(self):
        assert _retry_after_seconds(_err_with_headers({'retry-after': '12'})) == 12.0

    def test_http_date(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        header = {'Retry-After': format_datetime(future, usegmt=True)}
        got = _retry_after_seconds(_err_with_headers(header))
        assert got is not None
        assert 25.0 <= got <= 31.0

    def test_past_date_clamped_to_zero(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        header = {'Retry-After': format_datetime(past, usegmt=True)}
        assert _retry_after_seconds(_err_with_headers(header)) == 0.0

    def test_absent_returns_none(self):
        assert _retry_after_seconds(_err_with_headers({})) is None

    def test_unparseable_returns_none(self):
        bad = {'Retry-After': 'not-a-number-or-date-zzz'}
        assert _retry_after_seconds(_err_with_headers(bad)) is None

    def test_reads_wrapped_exception(self):
        # The OAI backend stores the raw OpenAIError in ModelServiceError.exception.
        inner = _err_with_headers({'Retry-After': '7'})
        outer = ModelServiceError(exception=inner)
        assert _retry_after_seconds(outer) == 7.0

    def test_no_response_returns_none(self):
        assert _retry_after_seconds(Exception('boom')) is None
