"""SSRF guard for the URL fetchers (SEC-09).

The web fetchers (``web_extractor`` -> ``save_url_to_local_work_dir`` /
``get_content_type_by_head_request``) take an agent- or user-supplied URL. Without a
guard, a crafted URL can pivot into the host: loopback (127.0.0.0/8, ::1), the LAN
(RFC1918 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), link-local (169.254.0.0/16), or
the cloud metadata service (169.254.169.254). This module refuses any fetch whose
target -- a literal IP, any of a hostname's resolved addresses, or any redirect hop --
falls in those ranges.

Fail closed: a host that cannot be resolved, resolves to no addresses, or whose address
is unparseable is BLOCKED, never passed through.

Residual boundary (documented, not a gap in the ranges above): the guard resolves a
hostname, validates, then ``requests`` independently re-resolves DNS for the actual
connection. An attacker controlling a domain's nameserver (short TTL) can answer the
guard's lookup with a public IP and the request's lookup with a private one (DNS
rebinding). The per-hop redirect re-validation below closes the redirect vector, but
full rebinding resistance would require IP-pinning (connect to the validated IP, send
the original Host) -- a larger change deferred out of this ticket's scope.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import requests

# Reserved ranges the web fetchers must never reach (SSRF guard). An IPv4-mapped
# IPv6 address (e.g. ::ffff:127.0.0.1) is normalized to its IPv4 form before the
# check so it cannot slip past the IPv4 ranges.
_BLOCKED_NETWORKS = (
    ipaddress.ip_network('0.0.0.0/8'),       # "this" network
    ipaddress.ip_network('127.0.0.0/8'),     # loopback
    ipaddress.ip_network('10.0.0.0/8'),      # RFC1918 private
    ipaddress.ip_network('172.16.0.0/12'),   # RFC1918 private
    ipaddress.ip_network('192.168.0.0/16'),  # RFC1918 private
    ipaddress.ip_network('169.254.0.0/16'),  # link-local + cloud metadata (169.254.169.254)
    ipaddress.ip_network('::/128'),          # IPv6 unspecified
    ipaddress.ip_network('::1/128'),         # IPv6 loopback
    ipaddress.ip_network('fe80::/10'),       # IPv6 link-local
    ipaddress.ip_network('fc00::/7'),        # IPv6 unique-local
)


def _is_blocked_ip(ip_str: str) -> bool:
    """True if ``ip_str`` falls in a reserved range. Fail closed on unparseable input."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _BLOCKED_NETWORKS)


def assert_public_url_egress(url: str) -> None:
    """Raise ``ValueError`` if ``url`` targets a non-public address (SSRF guard).

    A literal-IP host is checked directly; a hostname is resolved and EVERY returned
    address is checked, so a public-looking name that points at a private range is
    still refused. Fail closed: no-resolve / no-addresses / unparseable => blocked.
    """
    host = urlparse(url).hostname
    if not host:
        raise ValueError(f'Blocked fetch: {url!r} has no host')

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _is_blocked_ip(str(literal_ip)):
            raise ValueError(f'Blocked fetch to non-public address: {url!r}')
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f'Blocked fetch: cannot resolve {host!r} ({e})')
    if not infos:
        raise ValueError(f'Blocked fetch: {host!r} resolved to no addresses')
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            raise ValueError(f'Blocked fetch: {host!r} resolves to a non-public address')


class _SSRFGuardedSession(requests.Session):
    """A ``requests.Session`` that re-validates the egress target on EVERY redirect hop.

    ``requests`` follows 3xx redirects by default and each hop's ``Location`` is
    attacker-controllable. Overriding ``send()`` to re-run ``assert_public_url_egress``
    before every wire write means a public host cannot 302 the fetch into reserved
    (loopback / private / link-local / metadata) space.
    """

    def send(self, request, **kwargs):
        assert_public_url_egress(request.url)
        return super().send(request, **kwargs)


def guarded_get(url: str, headers: dict, timeout) -> 'requests.Response':
    """GET ``url`` with per-hop egress re-validation (SSRF guard).

    The initial URL and every redirect hop are validated before the wire write, so a
    public host cannot redirect the fetch into a reserved range.
    """
    session = _SSRFGuardedSession()
    try:
        return session.get(url, headers=headers, timeout=timeout)
    finally:
        session.close()
