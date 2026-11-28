"""BUG_0435 — secure remote control of the AC server: the auth gate.

Single source of truth for authentication + route classification. The global
HTTP middleware in ``api_server.create_app``, the WS ``/ws/chat`` gate, and the
test suite all call *this* module — no duplicated key-compare / route-classify
logic anywhere else.

Design (locked decisions, see research/BUG_0435_secure-remote-control-plan.md):

* Transport-agnostic. No mesh/proxy vendor dependency: the gate keys only on the
  bind host (``AGENT_CASCADE_HOST``) and the configured credential.
* INERT by default. When the bind is loopback AND no server key is configured,
  ``auth_state() == "disabled"`` and the middleware passes every request through
  unchanged (byte-identical to pre-BUG_0435 behavior).
* Fail-closed boot. A non-loopback bind with no key must refuse to start —
  ``check_boot_preconditions`` raises so the entrypoints abort before binding.
* Constant-time key comparison (``hmac.compare_digest``) — no timing oracle.
* The raw key is never retained here beyond reading env; only a sha256-prefix
  ``key_id`` is exposed for the audit trail.

The gate is a small pure-Python surface (no FastAPI import) so it is trivially
testable in isolation; the middleware supplies the normalized inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

# ── Route classification ────────────────────────────────────────────────────
# OPEN: reachable WITHOUT a credential in every mode. These are the bootstrap /
# health / static assets only. Anything under /api/ that is not explicitly open
# requires a credential once enforcement is active (fail-closed for routes added
# later). Static SPA assets (anything NOT under /api/ and not a WS upgrade) are
# served by the path-contained ``/{path:path}`` handler and are OPEN by design —
# they carry no data and are ``_is_within``-contained server-side.
OPEN_EXACT = frozenset({
    '/',
    '/index.html',
    '/api/health',       # liveness probe (new; see plan §3)
    '/api/keys',         # server X25519 pubkey — needed before a session exists
    '/api/handshake',    # issues the session_token (bootstraps; rate-limited instead)
})

# Mutating verbs. Any of these on a non-open /api/ path requires a controller
# credential; a VIEWER key is refused with 403 (authenticated but under-privileged).
MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
# Read verbs allowed for both ADMIN and VIEWER.
READ_METHODS = frozenset({'GET', 'HEAD'})

# Destructive subset (controller-only). Listed explicitly for the test matrix and
# for audit labelling; authorization already follows from "VIEWER denied on every
# mutation", but naming it documents the highest-severity surface. All entries are
# (method, path) pairs; ``/api/endpoints`` uses the ``POST`` method and the rest are
# prefix-matched below to cover path-parameter forms (e.g. /api/approve/{rid}).
DESTRUCTIVE_EXACT = frozenset({
    ('POST', '/api/reset'),
    ('POST', '/api/restart'),
    ('POST', '/api/session/restore'),
    ('POST', '/api/endpoints'),
    ('POST', '/api/endpoints/bulk'),
    ('PUT', '/api/mcp-config'),
    ('POST', '/api/dna_cohort'),
    ('POST', '/api/dna_cohort/approve'),
})
# Prefix-matched destructive routes (path-parameter / nested forms).
DESTRUCTIVE_PREFIXES = (
    '/api/approve/',
    '/api/reject/',
    '/api/endpoints/',   # PUT/DELETE /api/endpoints/{id}
    '/api/stacks/',      # DELETE /api/stacks/{id}, .../snapshots/{sid}
)

# Admin-only READ routes: a GET, but the response carries SECRETS (provider
# api_keys / MCP env), so a VIEWER is refused 403 while ADMIN and a
# session_token are allowed. A distinct class lets the gate deny VIEWER access
# without touching the handlers' own auth path. This gate is the SOLE control for
# these secret-bearing reads — there is deliberately NO response masking (the
# WebUI editor PUTs the GET payload back verbatim, so masking would overwrite the
# on-disk secrets with the sentinel; see .agent_lessons
# /bug0435-response-masking-and-predicate-collaborator-test-traps.md).
ADMIN_ONLY_READ = frozenset({
    ('GET', '/api/endpoints'),
    ('GET', '/api/mcp-config'),
    ('GET', '/api/telemetry/export'),
})

# Mutating verbs that are read-only-effect (a file search / a parse): a VIEWER
# is allowed on these even though the verb is POST (plan §1:86). Classified as
# 'read' so both roles pass; they share the slow-route rate bucket.
VIEWER_ALLOWED_MUTATING = frozenset({
    ('POST', '/api/find_file'),
    ('POST', '/api/parse'),
})

# Rate-limit group prefixes (the numeric per-minute ceilings live in settings;
# this only maps a route to its group).
APPROVE_REJECT_PREFIXES = ('/api/approve/', '/api/reject/')
SLOW_ROUTES = ('/api/parse', '/api/media', '/api/upload', '/api/find_file')


def normalize_path(path: str) -> str:
    """Lowercase, collapse duplicate slashes, strip a trailing slash (keep '/').

    Defense-in-depth so a differently-cased or slash-stacked path cannot slip
    past an OPEN/prefix match into a gated route. Starlette matches routes
    case-sensitively, so this never widens what actually routes — it only stops
    a path from being misclassified by the gate.
    """
    p = path.lower()
    while '//' in p:
        p = p.replace('//', '/')
    if len(p) > 1 and p.endswith('/'):
        p = p.rstrip('/')
    return p


def route_rate_group(method: str, path: str) -> Optional[str]:
    """Rate-limit group for a request, or ``None`` for no explicit throttle.

    Returns ``handshake`` | ``approve`` | ``slow`` | ``mutating``. The middleware
    maps the group to a per-minute ceiling from settings and builds the bucket
    key from it; a single source keeps the route→limit mapping in auth.py.
    """
    method = method.upper()
    path = normalize_path(path)
    if path == '/api/handshake':
        return 'handshake'
    if path.startswith(APPROVE_REJECT_PREFIXES):
        return 'approve'
    if path.startswith(SLOW_ROUTES):
        return 'slow'
    if method in MUTATING_METHODS and not is_open(path):
        return 'mutating'
    return None

# ── Role constants ────────────────────────────────────────────────────────────
ADMIN = 'admin'
VIEWER = 'viewer'
SESSION_TOKEN_ROLE = 'session_token'  # audit label for session_token sessions

# Audit key-id labels (NEVER the raw key).
KEY_NONE = 'none'
KEY_SESSION = 'session_token'

_LOOPBACK_BIND_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1', '[::1]'})


def effective_host() -> str:
    """The bind host that determines whether enforcement applies.

    Defaults to loopback (the safe, local-first default — replaces the old
    hard-coded ``0.0.0.0``). Read live from ``AGENT_CASCADE_HOST`` at call time
    so a test can flip the mode with ``monkeypatch.setenv`` without re-import.
    """
    return (os.getenv('AGENT_CASCADE_HOST', '127.0.0.1').strip() or '127.0.0.1')


def _is_loopback_bind(host: str) -> bool:
    """True for loopback bind addresses (127/8, localhost, ::1)."""
    h = host.strip().lower().strip('[]')
    return h in _LOOPBACK_BIND_HOSTS or h.startswith('127.')


def admin_key() -> Optional[str]:
    """The controller REST key, or None when unset (inert on loopback)."""
    return (os.getenv('AGENT_CASCADE_ADMIN_API_KEY', '').strip() or None)


def viewer_keys() -> Tuple[str, ...]:
    """Comma-separated read-only keys, empty tuple when unset."""
    raw = os.getenv('AGENT_CASCADE_VIEWER_API_KEYS', '')
    return tuple(k.strip() for k in raw.split(',') if k.strip())


def keys_configured() -> bool:
    """True when at least one server REST key is configured."""
    return bool(admin_key() or viewer_keys())


def auth_state() -> str:
    """``"disabled"`` (inert, today's behavior) or ``"enforced"``.

    Disabled == loopback bind AND no key configured. Anything else (non-loopback
    bind, or any key set) enforces credentials. The non-loopback + no-key case is
    the boot-fail condition handled by :func:`check_boot_preconditions`.
    """
    if _is_loopback_bind(effective_host()) and not keys_configured():
        return 'disabled'
    return 'enforced'


def check_boot_preconditions(host: Optional[str] = None) -> None:
    """Fail-closed boot gate: a non-loopback bind with no key refuses to start.

    Raises ``RuntimeError`` when the bind is not loopback and neither the admin
    nor a viewer key is configured — the only way to honour "no plain-HTTP open
    port + auth on every mutating route" without silently shipping the current
    vuln. Loopback binds always pass. Called by the entrypoints before binding.
    """
    h = effective_host() if host is None else host
    if not _is_loopback_bind(h) and not keys_configured():
        raise RuntimeError(
            f"BUG_0435 boot refused: AGENT_CASCADE_HOST={h!r} is not loopback but "
            'no server credential is configured. Set AGENT_CASCADE_ADMIN_API_KEY '
            '(and/or AGENT_CASCADE_VIEWER_API_KEYS) to start a non-loopback bind, '
            'or bind loopback (AGENT_CASCADE_HOST=127.0.0.1) for local control.'
        )


# ── Route classification ────────────────────────────────────────────────────
def is_open(path: str) -> bool:
    """True for bootstrap / health / static paths (no credential required)."""
    path = normalize_path(path)
    if path in OPEN_EXACT:
        return True
    # SPA static assets: anything not under /api/ and not the WS upgrade path.
    if path.startswith('/api/') or path == '/ws/chat' or path.startswith('/ws/'):
        return False
    return True


def is_destructive(method: str, path: str) -> bool:
    """True for the highest-severity (controller-only) mutating routes."""
    method = method.upper()
    path = normalize_path(path)
    if (method, path) in DESTRUCTIVE_EXACT:
        return True
    return method in MUTATING_METHODS and path.startswith(DESTRUCTIVE_PREFIXES)


def classify(method: str, path: str) -> str:
    """One of ``"open"`` | ``"read"`` | ``"admin_read"`` | ``"mutating"``.

    * ``"open"`` — no credential required in any mode.
    * ``"read"`` — a GET/HEAD under /api/, or a read-only-effect POST
      (:data:`VIEWER_ALLOWED_MUTATING`) (ADMIN or VIEWER; 401 otherwise).
    * ``"admin_read"`` — a GET that returns secrets (:data:`ADMIN_ONLY_READ`);
      ADMIN / session_token allowed, VIEWER → 403.
    * ``"mutating"`` — a POST/PUT/PATCH/DELETE under /api/ (ADMIN or a
      session_token; VIEWER → 403; no credential → 401).
    """
    method = method.upper()
    path = normalize_path(path)
    if is_open(path):
        return 'open'
    if (method, path) in VIEWER_ALLOWED_MUTATING:
        # read-only-effect POST — both roles (same verdict as a GET 'read')
        return 'read'
    if (method, path) in ADMIN_ONLY_READ:
        return 'admin_read'
    if method in MUTATING_METHODS:
        return 'mutating'
    if method in READ_METHODS:
        return 'read'
    # OPTIONS preflight and any other verb are not gated here; CORS answers
    # preflight. Treat as open so auth never blocks a CORS preflight.
    return 'open'


# ── The gate predicate (single source of truth) ──────────────────────────────
class AuthResult:
    """Outcome of authentication: who (role) and what label (key_id for audit)."""

    __slots__ = ('role', 'key_id', 'authenticated')

    def __init__(self, role: Optional[str], key_id: str, authenticated: bool):
        self.role = role
        self.key_id = key_id
        self.authenticated = authenticated

    def __repr__(self) -> str:
        return f'AuthResult(role={self.role!r}, key_id={self.key_id!r}, authenticated={self.authenticated})'


def _constant_time_equals(secret: Optional[str], candidate: str) -> bool:
    """Constant-time compare against a possibly-None configured secret."""
    if secret is None or not isinstance(candidate, str) or not candidate:
        return False
    return hmac.compare_digest(secret, candidate)


def _key_id(secret: str) -> str:
    """Short, safe identifier for a key: sha256(secret)[:12]. Never the raw key."""
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()[:12]


def extract_credential(
    headers: Dict[str, str],
    query: Dict[str, str],
) -> Tuple[Optional[str], Optional[str], bool]:
    """Pull the (api_key, session_token, has_session_flag) from a request.

    ``api_key``: ``X-API-Key`` header, ``Authorization: Bearer <k>`` header, or an
    ``api_key`` query param (in that priority).
    ``session_token``: the ``token`` query param the existing session_token-gated
    routes already read (so those routes stay inert when the gate accepts it).

    Values are matched case-insensitively on header names against a plain dict so
    callers may pass any mapping; the WS handler passes ``websocket.headers``.
    """
    lower = {k.lower(): v for k, v in headers.items()} if headers else {}
    api_key = None
    header_key = lower.get('x-api-key')
    if header_key:
        api_key = header_key
    else:
        authz = lower.get('authorization', '')
        if authz[:7].lower() == 'bearer ':
            api_key = authz[7:].strip()
    if not api_key and query and 'api_key' in query:
        api_key = query.get('api_key')

    session_token = query.get('token') if query else None
    return api_key, session_token, bool(session_token)


def authenticate(
    headers: Dict[str, str],
    query: Dict[str, str],
    api_sessions: Optional[Dict[str, object]] = None,
) -> AuthResult:
    """Resolve a credential to a role. THE single source of truth for "who".

    Accepts EITHER an existing ``session_token`` (preserves the WebUI / Telegram
    bridge / phase-1 approve-reject paths — controller-equivalent) OR a static API
    key. Returns an :class:`AuthResult`; ``authenticated`` is False when no
    credential resolves (the caller then returns 401). A valid VIEWER key gets
    ``role=VIEWER``; everything else that authenticates is ``role=ADMIN``.
    """
    api_key, session_token, has_session = extract_credential(headers or {}, query or {})

    # 1) Existing session_token — controller-equivalent (legacy, preserved).
    if has_session and api_sessions is not None:
        if session_token in api_sessions:
            return AuthResult(ADMIN, KEY_SESSION, True)

    # 2) Static API keys (new remote-REST credential), constant-time.
    if api_key is not None:
        if _constant_time_equals(admin_key(), api_key):
            return AuthResult(ADMIN, _key_id(api_key), True)
        for vk in viewer_keys():
            if _constant_time_equals(vk, api_key):
                return AuthResult(VIEWER, _key_id(api_key), True)
        # Provided a key but it matches nothing → unauthenticated (401, not 200).
        return AuthResult(None, _key_id(api_key), False)

    return AuthResult(None, KEY_NONE, False)


def authorize(route_class: str, result: AuthResult) -> str:
    """Authorization verdict for a classified route: ``"allow"`` | ``"unauth"`` | ``"forbidden"``.

    * ``open`` → allow (no credential needed).
    * ``read`` / ``mutating`` with no credential → ``unauth`` (401).
    * ``mutating`` with a VIEWER role → ``forbidden`` (403 — authenticated but
      under-privileged). ``read`` accepts both roles.
    """
    if route_class == 'open':
        return 'allow'
    if not result.authenticated:
        return 'unauth'
    if route_class in ('mutating', 'admin_read') and result.role == VIEWER:
        return 'forbidden'
    return 'allow'


# ── Rate limiting (in-process token bucket; no external dep) ───────────────────
# Applied to /api/handshake and every mutating gated route. Loopback clients are
# exempted by the caller (the middleware knows the client IP); this primitive is
# purely "may this key spend one token from a per-minute bucket".


class _TokenBucket:
    """Classic token bucket: ``capacity`` tokens, refilled at ``per_min``/60 /s."""

    __slots__ = ('rate', 'capacity', '_tokens', '_last', '_lock')

    def __init__(self, per_min: float):
        self.rate = per_min / 60.0          # tokens per second
        self.capacity = per_min             # full-minute burst allowance
        self._tokens = per_min
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, cost: float = 1.0) -> float:
        """Try to spend ``cost`` tokens. Returns 0.0 when allowed, else the
        seconds until enough tokens accrue (caller surfaces as ``Retry-After``)."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= cost:
                self._tokens -= cost
                return 0.0
            return (cost - self._tokens) / self.rate


class RateLimiter:
    """Per-credential (or per-IP) registry of token buckets."""

    def __init__(self) -> None:
        self._buckets: Dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()
        self._last_prune = time.monotonic()

    def _prune_idle(self, now: float) -> None:
        """Drop buckets idle > 5 min so ``_buckets`` can't grow unbounded.

        Called under ``self._lock``. A bucket's ``_last`` only advances on a
        take(), so an entry nobody touches for 5 minutes is safe to reclaim — it
        just gets recreated (full allowance) on the next request.
        """
        if now - self._last_prune < 300.0:
            return
        self._last_prune = now
        stale = [k for k, b in self._buckets.items() if now - b._last > 300.0]
        for k in stale:
            del self._buckets[k]

    def acquire(self, key: str, per_min: float, cost: float = 1.0) -> float:
        """Allow one request for ``key`` under a ``per_min`` limit. Returns the
        seconds-to-retry (0.0 == allowed). A configured limit of 0 disables it."""
        if per_min <= 0:
            return 0.0
        with self._lock:
            self._prune_idle(time.monotonic())
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _TokenBucket(per_min)
                self._buckets[key] = bucket
        return bucket.take(cost)

    def reset(self) -> None:
        """Drop all buckets (for tests)."""
        with self._lock:
            self._buckets.clear()


# Process-wide limiter shared by the HTTP middleware and any WS callers.
_LIMITER = RateLimiter()


def check_rate_limit(key: str, per_min: float) -> float:
    """Shared-limiter convenience wrapper; returns seconds-to-retry (0.0 == allowed)."""
    return _LIMITER.acquire(key, per_min)


def reset_rate_limiter() -> None:
    """Clear the process-wide limiter (for tests / isolation)."""
    _LIMITER.reset()


# ── Public loopback predicates (consumed by the middleware + WS gate) ──────────
def is_loopback_bind(host: Optional[str] = None) -> bool:
    """Whether the *effective* (or given) bind host is loopback.

    Drives the WS gate: loopback binds leave ``/ws/chat`` open (WebUI inert);
    non-loopback binds require a credential before ``accept()``.
    """
    return _is_loopback_bind(effective_host() if host is None else host)


def is_loopback_ip(ip: Optional[str]) -> bool:
    """Whether a *client* IP is loopback — loopback clients are rate-limit exempt."""
    if not ip:
        return False
    h = ip.strip().lower().strip('[]')
    return h in _LOOPBACK_BIND_HOSTS or h.startswith('127.')


_TRUST_PROXY_TRUE = frozenset({'1', 'true', 'yes', 'on'})


def trust_proxy_enabled() -> bool:
    """``AGENT_CASCADE_TRUST_PROXY`` opt-in (default OFF). Read live from env so a
    test can flip it with ``monkeypatch.setenv`` without re-import."""
    return os.getenv('AGENT_CASCADE_TRUST_PROXY', '').strip().lower() in _TRUST_PROXY_TRUE


def best_client_ip(headers: Optional[Dict[str, str]], client_host: Optional[str]) -> str:
    """Client IP for the rate-limit key + audit: reverse-proxy aware.

    When ``AGENT_CASCADE_TRUST_PROXY`` is set, take the leftmost ``X-Forwarded-For``
    entry (the original client, appended by the first trusted proxy) — otherwise a
    localhost reverse proxy (the documented Caddy path) makes every remote client
    look like 127.0.0.1, which would defeat BOTH the loopback rate-limit exemption
    and the audit IP. When unset (default / direct mesh bind) return the socket
    peer host, preserving today's behavior exactly.
    """
    if trust_proxy_enabled():
        lower = {k.lower(): v for k, v in (headers or {}).items()}
        xff = lower.get('x-forwarded-for')
        if xff:
            first = xff.split(',')[0].strip()
            if first:
                return first
    return client_host or 'unknown'


def ws_reject_reason(
    headers: Optional[Dict[str, str]],
    query: Optional[Dict[str, str]],
    api_sessions: Optional[Dict[str, object]] = None,
) -> Optional[str]:
    """Pre-``accept()`` decision for ``/ws/chat``: ``None`` to accept, else a close reason.

    Loopback binds accept unconditionally (inert WebUI, today's behavior). A
    non-loopback/mesh bind requires the same credential as REST (api_key or
    session_token); missing/invalid → the handler closes with 1008. Keeping the
    whole accept/close decision here (not split across the handler) makes it a
    single source that the handler and the tests both exercise.
    """
    if _is_loopback_bind(effective_host()):
        return None
    res = authenticate(headers or {}, query or {}, api_sessions)
    if not res.authenticated:
        return 'authentication required'
    return None
