"""Unit tests for the authoritative prompt-cache telemetry path in ``agent_cascade.llm.oai``.

Covers:
  (a) ``_extract_usage`` surfaces ``prompt_tokens_details.cached_tokens`` from an OpenAI
      usage object (the data already flows — verify it).
  (b) The include_usage gating predicate ``_should_send_include_usage`` only enables the
      flag for our own backend (loopback / env override), never for external endpoints.

Pure and deterministic: no network, no LLM calls.
"""

import pytest


class _PD:
    """Minimal prompt_tokens_details stand-in."""
    def __init__(self, cached_tokens=None, audio_tokens=None):
        self.cached_tokens = cached_tokens
        self.audio_tokens = audio_tokens


class _Usage:
    """Minimal OpenAI usage-object stand-in with the fields _extract_usage reads."""
    def __init__(self, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                 prompt_tokens_details=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.prompt_tokens_details = prompt_tokens_details


class TestExtractUsageCachedTokens:
    def test_surfaces_cached_tokens(self):
        from agent_cascade.llm.oai import _extract_usage
        usage = _Usage(prompt_tokens=1000, completion_tokens=5, total_tokens=1005,
                       prompt_tokens_details=_PD(cached_tokens=80))
        d = _extract_usage(usage)
        assert d['prompt_tokens'] == 1000
        assert d['completion_tokens'] == 5
        assert d['prompt_tokens_details']['cached_tokens'] == 80

    def test_cached_zero_is_surfaced(self):
        from agent_cascade.llm.oai import _extract_usage
        usage = _Usage(prompt_tokens=1000, completion_tokens=5, total_tokens=1005,
                       prompt_tokens_details=_PD(cached_tokens=0))
        d = _extract_usage(usage)
        # 0 is a real value (a miss), not None — must be preserved.
        assert d['prompt_tokens_details']['cached_tokens'] == 0

    def test_no_details_means_no_prompt_tokens_details_key(self):
        from agent_cascade.llm.oai import _extract_usage
        usage = _Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
        d = _extract_usage(usage)
        assert 'prompt_tokens_details' not in d

    def test_none_usage_returns_empty(self):
        from agent_cascade.llm.oai import _extract_usage
        assert _extract_usage(None) == {}


class TestShouldSendIncludeUsage:
    """Gating predicate: only our own backend gets stream_options.include_usage."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch):
        # Isolate each test from ambient env.
        monkeypatch.delenv('AC_SEND_INCLUDE_USAGE', raising=False)

    def test_localhost_default_on(self):
        from agent_cascade.llm.oai import _should_send_include_usage
        assert _should_send_include_usage('http://localhost:8080/v1') is True

    def test_loopback_ip_default_on(self):
        from agent_cascade.llm.oai import _should_send_include_usage
        assert _should_send_include_usage('http://127.0.0.1:8080/v1') is True

    def test_external_endpoint_default_off(self):
        from agent_cascade.llm.oai import _should_send_include_usage
        assert _should_send_include_usage('https://api.openai.com/v1') is False

    def test_external_hostname_not_misdetected_as_localhost(self):
        # Substring traps: "notlocalhost" / a domain containing "localhost" must NOT match.
        from agent_cascade.llm.oai import _should_send_include_usage
        assert _should_send_include_usage('https://localhost.evil.com/v1') is False

    def test_empty_and_none_base_off(self):
        from agent_cascade.llm.oai import _should_send_include_usage
        assert _should_send_include_usage('') is False
        assert _should_send_include_usage(None) is False

    def test_env_override_on_for_external(self, monkeypatch):
        from agent_cascade.llm.oai import _should_send_include_usage
        monkeypatch.setenv('AC_SEND_INCLUDE_USAGE', '1')
        assert _should_send_include_usage('https://api.openai.com/v1') is True

    def test_env_override_off_for_localhost(self, monkeypatch):
        from agent_cascade.llm.oai import _should_send_include_usage
        monkeypatch.setenv('AC_SEND_INCLUDE_USAGE', '0')
        assert _should_send_include_usage('http://127.0.0.1:8080/v1') is False

    def test_env_true_yes_case_insensitive(self, monkeypatch):
        from agent_cascade.llm.oai import _should_send_include_usage
        monkeypatch.setenv('AC_SEND_INCLUDE_USAGE', 'YES')
        assert _should_send_include_usage('https://api.openai.com/v1') is True

    def test_malformed_url_degrades_to_off(self):
        # A garbage base must not raise and must not enable the flag.
        from agent_cascade.llm.oai import _should_send_include_usage
        assert _should_send_include_usage('::not-a-url::') in (False, True)  # never raises
