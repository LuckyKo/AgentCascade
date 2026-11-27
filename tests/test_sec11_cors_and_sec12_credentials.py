#!/usr/bin/env python3
"""TICKET-8 (SEC-11 + SEC-12) hardening tests.

* SEC-11: the management API's CORS config drops credentials and restricts origins to
  local ones (or an explicit env-supplied list).
* SEC-12: strip_ac_credentials removes the full known-AC-secret list (not just OPENAI_*),
  while preserving harness-native keys.

Run: python -m pytest tests/test_sec11_cors_and_sec12_credentials.py -q
"""

import re

from agent_cascade.api_server import _cors_kwargs
from agent_cascade.engine.cli_agent_adapters import strip_ac_credentials


# ── SEC-11: CORS ─────────────────────────────────────────────────────────────


def test_cors_drops_credentials_and_restricts_to_local(monkeypatch):
    """Default (no env var): credentials off, only local origins allowed.

    Starlette matches ``allow_origin_regex`` with ``re.fullmatch``, so the regex must
    consume the WHOLE origin — a lookalike like ``localhost.evil.com`` must not match.
    """
    monkeypatch.delenv('AGENT_CASCADE_CORS_ORIGINS', raising=False)
    kw = _cors_kwargs()
    assert kw['allow_credentials'] is False
    assert kw['allow_origins'] == []
    # Local origins (localhost / 127.0.0.1 on any port) are allowed...
    assert re.fullmatch(kw['allow_origin_regex'], 'http://localhost:12345')
    assert re.fullmatch(kw['allow_origin_regex'], 'http://127.0.0.1:5173')
    # ...external and lookalike origins are not.
    assert re.fullmatch(kw['allow_origin_regex'], 'https://evil.example.com') is None
    assert re.fullmatch(kw['allow_origin_regex'], 'http://10.0.0.5:8080') is None
    assert re.fullmatch(kw['allow_origin_regex'], 'http://localhost.evil.com') is None


def test_cors_explicit_env_list(monkeypatch):
    """AGENT_CASCADE_CORS_ORIGINS supplies an explicit origin list; regex is ignored."""
    monkeypatch.setenv('AGENT_CASCADE_CORS_ORIGINS', 'http://localhost:5173, https://ac.internal')
    kw = _cors_kwargs()
    assert kw['allow_credentials'] is False
    assert kw['allow_origins'] == ['http://localhost:5173', 'https://ac.internal']
    assert kw['allow_origin_regex'] is None


# ── SEC-12: strip_ac_credentials ────────────────────────────────────────────


def test_strip_ac_credentials_removes_known_ac_secrets():
    env = {
        'OPENAI_API_KEY': 'sk-x',
        'OPENAI_BASE_URL': 'https://api.openai.com/v1',
        'SERPER_API_KEY': 'serper-x',
        'AGENT_CASCADE_API_KEY': 'ac-x',
        'DASHSCOPE_API_KEY': 'dash-x',
        # harness-native + non-secret: must survive
        'ANTHROPIC_API_KEY': 'anthropic-x',
        'OPENCODE_API_KEY': 'opencode-x',
        'PATH': '/usr/bin',
    }
    out = strip_ac_credentials(env)
    # every AC-owned secret is stripped
    for k in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'SERPER_API_KEY',
              'AGENT_CASCADE_API_KEY', 'DASHSCOPE_API_KEY'):
        assert k not in out, f'{k} should have been stripped'
    # harness-native + non-secret vars are preserved
    assert out['ANTHROPIC_API_KEY'] == 'anthropic-x'
    assert out['OPENCODE_API_KEY'] == 'opencode-x'
    assert out['PATH'] == '/usr/bin'


def test_strip_ac_credentials_is_case_insensitive_on_prefix():
    """A lowercase spelling of an AC-secret prefix is still stripped (defensive)."""
    out = strip_ac_credentials({'openai_api_key': 'x', 'SERPER_API_KEY': 'y', 'PATH': '/bin'})
    assert 'openai_api_key' not in out
    assert 'SERPER_API_KEY' not in out
    assert out['PATH'] == '/bin'
