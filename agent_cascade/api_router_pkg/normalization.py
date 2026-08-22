"""api_base normalization for SERVER IDENTITY keys.

Distinct from ``state_ops._normalize_api_base`` (which strips ``/v1`` and is used
for KV-state labels). This helper KEEPS the path/port and produces a canonical
physical-server identity used ONLY as a dict key / comparison value:

- scheme and host lowercased
- ``localhost`` / ``[::1]`` / ``::1`` → ``127.0.0.1``
- trailing slashes stripped
- port and path preserved (``/v1`` stays)

Example: ``http://localhost:1234/v1/`` → ``http://127.0.0.1:1234/v1``.

NEVER use the result as a wire URL — only the original api_base goes over the wire.
"""

# ── Circuit-breaker tuning (Change B/D of reports/router-cascade-fix-plan.md) ──
BREAKER_BASE_WINDOW_SECONDS = 20.0   # Initial open-state window
BREAKER_MAX_WINDOW_SECONDS = 120.0   # Cap for exponential window growth
BREAKER_WINDOW_GROWTH = 2.0          # Window multiplier on repeated failed probes
SERVER_BUSY_WAIT_CAP_SECONDS = 30.0  # Per-call cap for the fail-fast wait (D1)


def _split_host_port(authority: str):
    """Split 'host[:port]' (IPv6-safe) into (host, port_suffix)."""
    if authority.startswith('['):
        end = authority.find(']')
        if end != -1:
            return authority[:end + 1], authority[end + 1:]
    if ':' in authority:
        host, _, port = authority.rpartition(':')
        if port.isdigit() or port == '':
            return host, ':' + port
    return authority, ''


def normalize_api_base(api_base: str) -> str:
    """Normalize an api_base URL into a canonical server-identity key.

    Keeps port and path; lowercases scheme and host; maps loopback aliases to
    ``127.0.0.1``; strips trailing slashes. Idempotent:
    ``normalize(normalize(x)) == normalize(x)``.
    """
    if not api_base or not isinstance(api_base, str):
        return api_base or ''

    base = api_base.strip()
    if not base:
        return ''

    # Lowercase the scheme only (everything up to but not including '://').
    sep = base.find('://')
    if sep != -1:
        base = base[:sep].lower() + base[sep:]
        scheme_end = sep + 3
    else:
        scheme_end = 0

    # Split off the path so the authority can be canonicalized safely
    # (a '/' inside a path must NOT be stripped).
    authority = base[scheme_end:]
    slash_idx = authority.find('/')
    path = ''
    if slash_idx != -1:
        path = authority[slash_idx:]
        authority = authority[:slash_idx]

    authority = authority.lower()
    host, port = _split_host_port(authority)
    if host in ('localhost', '[::1]', '::1'):
        authority = '127.0.0.1' + port

    if path.endswith('/'):
        # Strip exactly one trailing slash (idempotent; a '//' path segment is
        # normalized to single-slash form on the first pass).
        path = path[:-1]

    return f"{base[:scheme_end]}{authority}{path}"
