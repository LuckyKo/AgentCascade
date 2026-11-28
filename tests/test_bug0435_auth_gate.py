"""BUG_0435 — auth gate tests.

Two layers (see skill three-layer-proof-for-a-new-behavior-flag):

* Enforcer layer (no network, always runs): the single-source-of-truth predicate in
  agent_cascade.auth — auth_state / classify / authenticate / authorize /
  check_boot_preconditions / token-bucket — plus audit-line shape and the invariant
  that the raw key never appears in the audit trail.
* Wiring layer (create_app + TestClient): 401/403/429 status codes and the
  inert-by-default invariant. The app fixture uses a minimal stub pool; if the real
  create_app lifespan cannot boot against it the integration tests skip (the disabled-
  mode inertness guarantee is additionally proven by the existing tests/test_api_endpoints.py
  suite, which runs unchanged with no key set).
"""

import json
import os
import time

import pytest

from agent_cascade import auth


# ── Helpers ───────────────────────────────────────────────────────────────────
ADMIN = 'admin-super-secret-key-123'
VIEWER = 'viewer-readonly-key-456'
BAD = 'wrong-key-789'


@pytest.fixture
def enforced(monkeypatch):
    """Enforced mode: non-loopback bind + admin + viewer keys. Limiter reset per test."""
    monkeypatch.setenv('AGENT_CASCADE_HOST', '0.0.0.0')
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    monkeypatch.setenv('AGENT_CASCADE_VIEWER_API_KEYS', VIEWER + ',other-viewer')
    monkeypatch.delenv('AGENT_CASCADE_AUDIT_ENABLED', raising=False)
    auth.reset_rate_limiter()
    return monkeypatch


@pytest.fixture
def disabled(monkeypatch):
    """Disabled (inert) mode: loopback bind + no key."""
    monkeypatch.delenv('AGENT_CASCADE_HOST', raising=False)
    monkeypatch.delenv('AGENT_CASCADE_ADMIN_API_KEY', raising=False)
    monkeypatch.delenv('AGENT_CASCADE_VIEWER_API_KEYS', raising=False)
    auth.reset_rate_limiter()
    return monkeypatch


def _hdrs(key=None, token=None):
    h = {}
    if key is not None:
        h['X-API-Key'] = key
    q = {}
    if token is not None:
        q['token'] = token
    return h, q


# ── auth_state ──────────────────────────────────────────────────────────────
def test_auth_state_disabled_on_loopback_no_key(disabled):
    assert auth.auth_state() == 'disabled'


def test_auth_state_enforced_on_nonloopback_with_key(enforced):
    assert auth.auth_state() == 'enforced'


def test_auth_state_enforced_loopback_but_key_set(monkeypatch):
    monkeypatch.delenv('AGENT_CASCADE_HOST', raising=False)  # loopback default
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    assert auth.auth_state() == 'enforced'


def test_default_bind_host_is_loopback(disabled):
    # Inert-by-default invariant depends on the default bind being loopback.
    assert auth.effective_host() == '127.0.0.1'
    assert auth.is_loopback_bind() is True


# ── Fail-closed boot rule (enforcer) ──────────────────────────────────────────
def test_boot_refuses_nonloopback_without_key(monkeypatch):
    monkeypatch.delenv('AGENT_CASCADE_ADMIN_API_KEY', raising=False)
    monkeypatch.delenv('AGENT_CASCADE_VIEWER_API_KEYS', raising=False)
    with pytest.raises(RuntimeError):
        auth.check_boot_preconditions('0.0.0.0')


def test_boot_allows_loopback_without_key(monkeypatch):
    monkeypatch.delenv('AGENT_CASCADE_ADMIN_API_KEY', raising=False)
    monkeypatch.delenv('AGENT_CASCADE_VIEWER_API_KEYS', raising=False)
    auth.check_boot_preconditions('127.0.0.1')  # must not raise


def test_boot_allows_nonloopback_with_key(monkeypatch):
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    auth.check_boot_preconditions('0.0.0.0')  # must not raise


# ── Route classification ──────────────────────────────────────────────────────
@pytest.mark.parametrize('method,path', [
    ('GET', '/'), ('GET', '/index.html'), ('GET', '/api/health'),
    ('GET', '/api/keys'), ('POST', '/api/handshake'), ('GET', '/app.js'),
])
def test_open_routes(method, path):
    assert auth.classify(method, path) == 'open'


def test_read_and_mutating_classification():
    assert auth.classify('GET', '/api/agents') == 'read'
    assert auth.classify('GET', '/api/status') == 'read'
    assert auth.classify('POST', '/api/reset') == 'mutating'
    assert auth.classify('PUT', '/api/mcp-config') == 'mutating'
    assert auth.classify('DELETE', '/api/stacks/abc') == 'mutating'
    assert auth.classify('OPTIONS', '/api/agents') == 'open'  # CORS preflight never gated


def test_destructive_subset_recognized():
    assert auth.is_destructive('POST', '/api/reset')
    assert auth.is_destructive('POST', '/api/approve/xyz')
    assert auth.is_destructive('DELETE', '/api/stacks/123')
    assert auth.is_destructive('PUT', '/api/mcp-config')
    assert auth.is_destructive('POST', '/api/endpoints')
    assert not auth.is_destructive('GET', '/api/agents')


# ── authenticate: credential -> role ──────────────────────────────────────────
def test_admin_key_authenticates_as_admin(enforced):
    r = auth.authenticate({'X-API-Key': ADMIN}, {}, api_sessions={})
    assert r.authenticated and r.role == auth.ADMIN
    assert r.key_id != ADMIN and len(r.key_id) == 12  # hashed, never raw


def test_viewer_key_authenticates_as_viewer(enforced):
    r = auth.authenticate({'X-API-Key': VIEWER}, {}, api_sessions={})
    assert r.authenticated and r.role == auth.VIEWER


def test_bearer_and_query_key_accepted(enforced):
    assert auth.authenticate({'Authorization': 'Bearer ' + ADMIN}, {}, {})
    assert auth.authenticate({}, {'api_key': ADMIN}, {})


def test_invalid_key_is_unauthenticated(enforced):
    r = auth.authenticate({'X-API-Key': BAD}, {}, api_sessions={})
    assert r.authenticated is False and r.role is None


def test_session_token_is_admin_equivalent(enforced):
    sessions = {'tok123': b'secret'}
    r = auth.authenticate({}, {'token': 'tok123'}, api_sessions=sessions)
    assert r.authenticated and r.role == auth.ADMIN and r.key_id == auth.KEY_SESSION
    bad = auth.authenticate({}, {'token': 'nope'}, api_sessions=sessions)
    assert bad.authenticated is False


def test_no_credential_unauthenticated(enforced):
    r = auth.authenticate({}, {}, api_sessions={})
    assert r.authenticated is False and r.key_id == auth.KEY_NONE


# ── authorize: role x route -> verdict ────────────────────────────────────────
def test_authorize_read_allows_both_roles(enforced):
    admin = auth.AuthResult(auth.ADMIN, 'k', True)
    viewer = auth.AuthResult(auth.VIEWER, 'k', True)
    none = auth.AuthResult(None, 'none', False)
    assert auth.authorize('read', admin) == 'allow'
    assert auth.authorize('read', viewer) == 'allow'
    assert auth.authorize('read', none) == 'unauth'


def test_authorize_mutating_denies_viewer_and_unauth():
    admin = auth.AuthResult(auth.ADMIN, 'k', True)
    viewer = auth.AuthResult(auth.VIEWER, 'k', True)
    none = auth.AuthResult(None, 'none', False)
    assert auth.authorize('mutating', admin) == 'allow'
    assert auth.authorize('mutating', viewer) == 'forbidden'
    assert auth.authorize('mutating', none) == 'unauth'
    assert auth.authorize('open', none) == 'allow'


# ── §8.1 matrix (predicate-level) ─────────────────────────────────────────────
@pytest.mark.parametrize('key,expect_role', [(ADMIN, auth.ADMIN), (VIEWER, auth.VIEWER)])
@pytest.mark.parametrize('route_class', ['read', 'mutating', 'open'])
def test_matrix_roles_by_route_class(enforced, key, expect_role, route_class):
    res = auth.authenticate({'X-API-Key': key}, {}, api_sessions={})
    verdict = auth.authorize(route_class, res)
    if route_class == 'read':
        assert verdict == 'allow'
    elif route_class == 'mutating':
        assert verdict == ('allow' if expect_role == auth.ADMIN else 'forbidden')
    else:
        assert verdict == 'allow'


# ── Rate limiting (token bucket) ──────────────────────────────────────────────
def test_token_bucket_blocks_over_limit():
    auth.reset_rate_limiter()
    results = [auth.check_rate_limit('k1', 10) for _ in range(11)]
    assert all(r == 0.0 for r in results[:10])
    assert results[10] > 0.0  # 11th over the per-minute limit
    # A distinct key has its own budget.
    assert auth.check_rate_limit('k2', 10) == 0.0


def test_rate_limit_zero_disables():
    auth.reset_rate_limiter()
    for _ in range(100):
        assert auth.check_rate_limit('k', 0) == 0.0


def test_token_bucket_refills_after_interval():
    # Real refill proof: exhaust a 1-token/min bucket, advance time by a full
    # minute, confirm the token is back. (Renamed from the misnamed test that
    # only asserted immediate 2nd-call blocking, not refilling.)
    b = auth._TokenBucket(1)          # 1 token/min, capacity 1
    assert b.take() == 0.0            # spend the one token
    assert b.take() > 0.0             # exhausted → must wait
    b._last -= 60.0                   # simulate a full minute elapsed since last refill
    assert b.take() == 0.0            # refilled to capacity → allowed again


def test_is_loopback_ip_predicate():
    assert auth.is_loopback_ip('127.0.0.1')
    assert auth.is_loopback_ip('::1')
    assert not auth.is_loopback_ip('10.0.0.5')
    assert not auth.is_loopback_ip('testclient')
    assert not auth.is_loopback_ip(None)


# ── Audit trail: shape + raw-key never present ─────────────────────────────────
def test_audit_line_shape_and_no_raw_key(tmp_path):
    from agent_cascade.audit import AuditLogger
    al = AuditLogger(base_dir=str(tmp_path))
    al.enabled = True
    al.record(method='POST', route='/api/reset', result=200,
              key_id='abcd1234ef56', role='admin', ip='10.0.0.1', dur_ms=5)
    al.record(method='GET', route='/api/settings', result=401,
              key_id='none', role='none', ip='10.0.0.2')
    files = list(tmp_path.glob('audit_*.jsonl'))
    assert files
    lines = [json.loads(x) for x in files[0].read_text(encoding='utf-8').splitlines() if x.strip()]
    assert len(lines) == 2
    first = lines[0]
    for field in ('ts', 'key_id', 'role', 'ip', 'method', 'route', 'action', 'result', 'dur_ms'):
        assert field in first, f'missing audit field {field}'
    assert first['result'] == 200 and first['route'] == '/api/reset'
    assert lines[1]['result'] == 401
    # The raw key value must never be written.
    raw = files[0].read_text(encoding='utf-8')
    assert ADMIN not in raw and 'abcd1234ef56' in raw  # hashed prefix only


def test_key_id_never_equals_raw_key(enforced):
    r = auth.authenticate({'X-API-Key': ADMIN}, {}, {})
    assert ADMIN not in r.key_id
    assert auth.viewer_keys()[0] not in auth._key_id(VIEWER)


# ── Wiring layer: ENFORCED-direction HTTP behavior on a minimal app ──────────
# These exercise the EXACT production middleware via install_auth_gate(), on a tiny
# FastAPI() with representative routes — no create_app lifespan, no pool/LLM. This
# proves 401/403/429/200 actually run (the create_app path only covered inert).
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _NoopAudit:
    """Audit stub so wiring tests don't touch the real workspace/audit dir."""
    enabled = False

    def record(self, **kw):
        pass


@pytest.fixture
def gate_app(enforced):
    """Minimal enforced app: one open, one read-only GET, one mutating+destructive POST,
    plus /api/handshake (open, but rate-limited). Installs the shared gate."""
    from agent_cascade.api_server import install_auth_gate
    enforced.setattr('agent_cascade.audit.get_audit_logger', lambda: _NoopAudit())

    app = FastAPI()

    @app.get('/api/health')
    def _health():
        return {'status': 'ok'}

    @app.get('/api/agents')
    def _agents():
        return {'agents': []}

    @app.post('/api/reset')
    def _reset():
        return {'ok': True}

    @app.post('/api/handshake')
    def _handshake():
        return {'sid': 'x'}

    install_auth_gate(app, sessions={})
    with TestClient(app) as client:
        yield client


def test_open_route_allowed_without_key_enforced(gate_app):
    # /api/health is in OPEN_SET: served unauthenticated even when enforced.
    assert gate_app.get('/api/health').status_code == 200


def test_read_401_without_key_enforced(gate_app):
    assert gate_app.get('/api/agents').status_code == 401


def test_read_admin_key_200_enforced(gate_app):
    r = gate_app.get('/api/agents', headers={'X-API-Key': ADMIN})
    assert r.status_code == 200
    assert r.json() == {'agents': []}


def test_read_viewer_key_200_enforced(gate_app):
    # Viewer may read.
    assert gate_app.get('/api/agents', headers={'X-API-Key': VIEWER}).status_code == 200


def test_mutating_401_without_key_enforced(gate_app):
    assert gate_app.post('/api/reset').status_code == 401


def test_mutating_403_viewer_enforced(gate_app):
    assert gate_app.post('/api/reset', headers={'X-API-Key': VIEWER}).status_code == 403


def test_mutating_admin_key_200_enforced(gate_app):
    assert gate_app.post('/api/reset', headers={'X-API-Key': ADMIN}).status_code == 200


def test_invalid_key_401_on_mutating_enforced(gate_app):
    assert gate_app.post('/api/reset', headers={'X-API-Key': BAD}).status_code == 401


def test_handshake_rate_limit_429_enforced(gate_app, monkeypatch):
    import agent_cascade.settings as st
    monkeypatch.setattr(st, 'RATE_LIMIT_HANDSHAKE_PER_MIN', 3)
    auth.reset_rate_limiter()
    codes = [gate_app.post('/api/handshake', json={}).status_code for _ in range(6)]
    # First 3 within the per-minute bucket, then throttled with Retry-After.
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert 429 in codes


def test_inert_direction_on_minimal_app(disabled, monkeypatch):
    # Sanity: install_auth_gate is inert when disabled (loopback bind + no key) —
    # a gated read route is served with no credential. (The existing 163-passed
    # test_api_endpoints.py suite is the authoritative inertness proof; this just
    # confirms install_auth_gate itself passes through cleanly.)
    monkeypatch.setattr('agent_cascade.audit.get_audit_logger', lambda: _NoopAudit())
    from agent_cascade.api_server import install_auth_gate
    app = FastAPI()

    @app.get('/api/agents')
    def _agents():
        return {'agents': []}

    install_auth_gate(app, sessions={})
    with TestClient(app) as c:
        assert c.get('/api/agents').status_code == 200


def test_boot_fail_enforced_nonloopback_no_key(monkeypatch):
    # Fail-closed boot rule (enforcer).
    monkeypatch.delenv('AGENT_CASCADE_ADMIN_API_KEY', raising=False)
    monkeypatch.delenv('AGENT_CASCADE_VIEWER_API_KEYS', raising=False)
    with pytest.raises(RuntimeError):
        auth.check_boot_preconditions('0.0.0.0')


# ── BUG_0435 BUILD-review #1: ADMIN_ONLY_READ gate (sole control, no masking) ─
_SENTINEL = 'sk-PROVIDER-SECRET-DO-NOT-LEAK'


def test_classify_admin_only_read():
    assert auth.classify('GET', '/api/endpoints') == 'admin_read'
    assert auth.classify('GET', '/api/mcp-config') == 'admin_read'
    assert auth.classify('GET', '/api/telemetry/export') == 'admin_read'
    assert auth.classify('POST', '/api/endpoints') == 'mutating'  # POST not admin_read


def test_authorize_admin_read_viewer_forbidden_admin_allowed():
    assert auth.authorize('admin_read', auth.AuthResult(auth.VIEWER, 'v', True)) == 'forbidden'
    assert auth.authorize('admin_read', auth.AuthResult(auth.ADMIN, 'a', True)) == 'allow'
    assert auth.authorize('admin_read', auth.AuthResult(auth.SESSION_TOKEN_ROLE, 's', True)) == 'allow'


def test_masking_helpers_removed():
    # Regression guard: the response-masking helpers corrupted MCP env secrets and
    # provider api_keys on a GET -> PUT -> save round-trip (the WebUI editor PUTs the
    # GET payload back verbatim). They must stay deleted; the ADMIN_ONLY_READ gate is
    # the single control for these reads.
    import agent_cascade.api_server as asrv
    assert not hasattr(asrv, '_mask_endpoints')
    assert not hasattr(asrv, '_mask_mcp_env')
    assert not hasattr(asrv, '_SECRET_REDACTION')


@pytest.fixture
def roundtrip_app(enforced):
    """Minimal app faithful to the POST-FIX production handlers: GET returns the raw
    store verbatim, PUT persists the body verbatim — exactly the GET -> PUT -> re-read
    path that broke, exercised without booting the create_app lifespan."""
    from agent_cascade.api_server import install_auth_gate
    enforced.setattr('agent_cascade.audit.get_audit_logger', lambda: _NoopAudit())
    app = FastAPI()
    store = {
        'mcp': {'srv': {'args': ['-y', 'mcp'], 'env': {'TOKEN': _SENTINEL}}},
        'endpoints': {'endpoints': [{'name': 'gpt', 'api_key': _SENTINEL}], 'agent_priorities': {}},
    }

    @app.get('/api/mcp-config')
    def _mcp_get():
        return dict(store['mcp'])

    @app.put('/api/mcp-config')
    async def _mcp_put(data: dict):
        store['mcp'] = data
        return {'ok': True}

    @app.get('/api/endpoints')
    def _ep_get():
        return dict(store['endpoints'])

    @app.put('/api/endpoints')
    async def _ep_put(data: dict):
        store['endpoints'] = data
        return {'ok': True}

    install_auth_gate(app, sessions={})
    with TestClient(app) as c:
        yield c, store


def test_mcp_config_roundtrip_preserves_secret_enforced(roundtrip_app):
    # Discriminating test: opening the MCP editor (GET) then saving (PUT) must not
    # overwrite the env secret with a masked placeholder.
    c, _ = roundtrip_app
    got = c.get('/api/mcp-config', headers={'X-API-Key': ADMIN}).json()
    assert got['srv']['env']['TOKEN'] == _SENTINEL          # raw, not REDACTED
    assert c.put('/api/mcp-config', json=got, headers={'X-API-Key': ADMIN}).status_code == 200
    again = c.get('/api/mcp-config', headers={'X-API-Key': ADMIN}).json()
    assert again['srv']['env']['TOKEN'] == _SENTINEL        # preserved after save
    assert 'REDACTED' not in json.dumps(again)


def test_endpoints_roundtrip_preserves_api_key_enforced(roundtrip_app):
    c, _ = roundtrip_app
    got = c.get('/api/endpoints', headers={'X-API-Key': ADMIN}).json()
    assert got['endpoints'][0]['api_key'] == _SENTINEL
    assert c.put('/api/endpoints', json=got, headers={'X-API-Key': ADMIN}).status_code == 200
    again = c.get('/api/endpoints', headers={'X-API-Key': ADMIN}).json()
    assert again['endpoints'][0]['api_key'] == _SENTINEL
    assert 'REDACTED' not in json.dumps(again)


@pytest.fixture
def secrets_app(enforced):
    """Minimal app exposing the secret-bearing GETs (gate-deny for VIEWER) and the
    read-only-effect POSTs (VIEWER-allowed). The ADMIN_ONLY_READ gate is the sole
    control — there is no response masking to verify here (it would corrupt a save)."""
    from agent_cascade.api_server import install_auth_gate
    enforced.setattr('agent_cascade.audit.get_audit_logger', lambda: _NoopAudit())
    app = FastAPI()

    @app.get('/api/endpoints')
    def _ep():
        return {'endpoints': [{'name': 'gpt', 'api_key': _SENTINEL}]}

    @app.get('/api/mcp-config')
    def _mcp():
        return {'srv': {'env': {'TOKEN': _SENTINEL}}}

    @app.get('/api/telemetry/export')
    def _tl():
        return {'rows': []}

    @app.post('/api/find_file')
    def _ff():
        return {'paths': []}

    @app.post('/api/parse')
    def _ps():
        return {'parsed': True}

    install_auth_gate(app, sessions={})
    with TestClient(app) as c:
        yield c


def test_admin_only_read_viewer_403_enforced(secrets_app):
    for p in ('/api/endpoints', '/api/mcp-config', '/api/telemetry/export'):
        assert secrets_app.get(p, headers={'X-API-Key': VIEWER}).status_code == 403, p


def test_admin_only_read_admin_200_enforced(secrets_app):
    for p in ('/api/endpoints', '/api/mcp-config', '/api/telemetry/export'):
        assert secrets_app.get(p, headers={'X-API-Key': ADMIN}).status_code == 200, p


def test_viewer_allowed_mutating_enforced(secrets_app):
    # POST /api/find_file + /api/parse are read-only-effect → VIEWER allowed (200).
    assert secrets_app.post('/api/find_file', headers={'X-API-Key': VIEWER}).status_code == 200
    assert secrets_app.post('/api/parse', headers={'X-API-Key': VIEWER}).status_code == 200


def test_classify_viewer_allowed_mutating():
    assert auth.classify('POST', '/api/find_file') == 'read'
    assert auth.classify('POST', '/api/parse') == 'read'


# ── BUG_0435 BUILD-review #2: reverse-proxy client IP (TRUST_PROXY) ──────────
def test_best_client_ip_default_is_socket_peer(monkeypatch):
    monkeypatch.delenv('AGENT_CASCADE_TRUST_PROXY', raising=False)
    assert auth.best_client_ip({'X-Forwarded-For': '203.0.113.5'}, '127.0.0.1') == '127.0.0.1'


def test_best_client_ip_trust_proxy_uses_leftmost_xff(monkeypatch):
    monkeypatch.setenv('AGENT_CASCADE_TRUST_PROXY', '1')
    assert auth.best_client_ip({'X-Forwarded-For': '203.0.113.5, 10.0.0.2'}, '127.0.0.1') == '203.0.113.5'


def test_trust_proxy_client_not_loopback_exempt_enforced(monkeypatch):
    # Behind a localhost proxy the socket peer is 127.0.0.1; with TRUST_PROXY on, the
    # X-Forwarded-For client must STILL be throttled (loopback exemption must not
    # silently disable throttling for every remote client).
    monkeypatch.setenv('AGENT_CASCADE_HOST', '0.0.0.0')
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    monkeypatch.setenv('AGENT_CASCADE_TRUST_PROXY', '1')
    monkeypatch.delenv('AGENT_CASCADE_AUDIT_ENABLED', raising=False)
    monkeypatch.setattr('agent_cascade.audit.get_audit_logger', lambda: _NoopAudit())
    import agent_cascade.settings as st
    monkeypatch.setattr(st, 'RATE_LIMIT_HANDSHAKE_PER_MIN', 3)
    auth.reset_rate_limiter()
    from agent_cascade.api_server import install_auth_gate
    app = FastAPI()

    @app.post('/api/handshake')
    def _hs():
        return {'sid': 'x'}

    install_auth_gate(app, sessions={})
    with TestClient(app) as c:
        codes = [c.post('/api/handshake', json={},
                        headers={'X-Forwarded-For': '203.0.113.5'}).status_code for _ in range(6)]
    assert 429 in codes  # throttled despite the loopback socket peer


# ── BUG_0435 BUILD-review #7: WS gate predicate + session_token/admin interaction ─
def test_ws_reject_reason_nonloopback_no_credential(monkeypatch):
    monkeypatch.setenv('AGENT_CASCADE_HOST', '0.0.0.0')
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    assert auth.ws_reject_reason({}, {}) == 'authentication required'


def test_ws_reject_reason_nonloopback_accepts_key_or_token(monkeypatch):
    monkeypatch.setenv('AGENT_CASCADE_HOST', '0.0.0.0')
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    assert auth.ws_reject_reason({'X-API-Key': ADMIN}, {}, {'st': object()}) is None
    assert auth.ws_reject_reason({}, {'token': 'st'}, {'st': object()}) is None  # session_token too


def test_ws_reject_reason_loopback_is_inert(disabled):
    assert auth.ws_reject_reason({}, {}) is None  # loopback bind: accept, no credential


def test_admin_key_and_session_token_both_map_to_admin(enforced):
    sessions = {'valid-token': object()}
    r_admin = auth.authenticate({'X-API-Key': ADMIN}, {}, sessions)
    assert r_admin.role == auth.ADMIN and r_admin.authenticated
    r_tok = auth.authenticate({}, {'token': 'valid-token'}, sessions)
    assert r_tok.role == auth.ADMIN and r_tok.authenticated
    r_none = auth.authenticate({}, {'token': 'bogus'}, sessions)
    assert not r_none.authenticated


def test_main_style_nonloopback_bind_enforces_ws_gate(monkeypatch):
    """#2 REQUIRED: the __main__ path binds on the SAME host the gate enforces on.

    Regression guarded: __main__ used to bind CLI args.host (e.g. 0.0.0.0) while the
    gate read effective_host() from the (unset) env -> loopback -> the WS gate
    accepted ANY credential-less remote client on the shell-capable /ws/chat.
    __main__ now seeds AGENT_CASCADE_HOST from --host, so bind and gate agree. We
    simulate that seed: after it, a non-loopback bind MUST make the WS gate enforced
    (reject credential-less), never loopback-accept-all.
    """
    monkeypatch.setenv('AGENT_CASCADE_ADMIN_API_KEY', ADMIN)
    # Simulate api_server.__main__: seed the env var the gate reads from the CLI host.
    bind_host = '0.0.0.0'
    monkeypatch.setenv('AGENT_CASCADE_HOST', bind_host)
    # Single source of truth: the host we bind on is exactly what the gate evaluates.
    assert auth.effective_host() == bind_host
    assert auth.auth_state() == 'enforced'
    # A credential-less remote WS must be rejected, NOT loopback-accepted.
    assert auth.ws_reject_reason({}, {}) == 'authentication required'
