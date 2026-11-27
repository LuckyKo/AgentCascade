"""SEC-09: the web fetchers must not egress to non-public addresses (SSRF guard).

``ssrf_guard.assert_public_url_egress`` refuses any fetch whose target -- a literal IP,
any of a hostname's resolved addresses, or any redirect hop -- falls in loopback /
RFC1918 private / link-local / 169.254.0.0/16 (cloud metadata) space. It is wired in
front of the two network calls in ``utils.utils`` (``save_url_to_local_work_dir`` GET
via ``guarded_get``, which re-validates on every redirect hop, and
``get_content_type_by_head_request`` HEAD) so a blocked target never reaches the wire.
"""

import socket
import tempfile
from unittest.mock import patch

import pytest

from agent_cascade.utils.ssrf_guard import _is_blocked_ip, assert_public_url_egress

# Literal-IP hosts that must be refused (checked directly, no DNS involved).
_BLOCKED_LITERAL = [
    'http://127.0.0.1/',                 # loopback
    'http://127.8.9.10/x',              # any 127.0.0.0/8
    'http://10.1.2.3/',                 # RFC1918
    'http://172.16.0.1/',              # RFC1918 (bottom of 172.16.0.0/12)
    'http://172.31.255.255/',          # RFC1918 (top of 172.16.0.0/12)
    'http://192.168.1.1/',             # RFC1918
    'http://169.254.169.254/latest/meta-data/',  # cloud metadata service
    'http://169.254.0.1/',             # link-local
    'http://0.0.0.0/',                 # "this" network
    'http://[::]/',                    # IPv6 unspecified (loopback-equivalent on Linux)
    'http://[::1]/',                   # IPv6 loopback
    'http://[::ffff:127.0.0.1]/',      # IPv4-mapped IPv6 loopback
    'http://[fe80::1]/',              # IPv6 link-local
    'http://[fc00::1]/',              # IPv6 unique-local
]

# Literal-IP hosts that must be ALLOWED (public, or just outside a reserved range).
_ALLOWED_LITERAL = [
    'http://8.8.8.8/',
    'http://1.1.1.1/',
    'http://172.32.0.1/',             # just above 172.16.0.0/12
    'http://172.15.0.1/',            # just below 172.16.0.0/12
    'http://192.169.0.1/',           # just above 192.168.0.0/16
    'http://169.255.0.1/',           # just above 169.254.0.0/16
]


@pytest.mark.parametrize('url', _BLOCKED_LITERAL)
def test_literal_private_ip_is_blocked(url):
    with pytest.raises(ValueError):
        assert_public_url_egress(url)


@pytest.mark.parametrize('url', _ALLOWED_LITERAL)
def test_literal_public_ip_is_allowed(url):
    assert_public_url_egress(url)  # must not raise


# ── Hostname resolution (mock socket.getaddrinfo; no real DNS in the test) ────────

def _fake_infos(ips):
    """Build getaddrinfo-shaped tuples for a list of IPv4 strings (port 80)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in ips]


def test_hostname_resolving_to_private_is_blocked():
    with patch('agent_cascade.utils.ssrf_guard.socket.getaddrinfo',
               return_value=_fake_infos(['127.0.0.1'])):
        with pytest.raises(ValueError):
            assert_public_url_egress('http://internal.example.com/')


def test_hostname_resolving_to_public_is_allowed():
    with patch('agent_cascade.utils.ssrf_guard.socket.getaddrinfo',
               return_value=_fake_infos(['93.184.216.34'])):
        assert_public_url_egress('http://example.com/')  # must not raise


def test_hostname_with_any_private_resolution_is_blocked():
    # A public address PLUS a private one: any private result must trigger the block.
    with patch('agent_cascade.utils.ssrf_guard.socket.getaddrinfo',
               return_value=_fake_infos(['93.184.216.34', '10.0.0.5'])):
        with pytest.raises(ValueError):
            assert_public_url_egress('http://dual.example.com/')


def test_unresolvable_hostname_is_blocked_fail_closed():
    with patch('agent_cascade.utils.ssrf_guard.socket.getaddrinfo',
               side_effect=socket.gaierror('no address')):
        with pytest.raises(ValueError):
            assert_public_url_egress('http://no-such-host-xyz.invalid/')


def test_host_with_no_address_is_blocked_fail_closed():
    with patch('agent_cascade.utils.ssrf_guard.socket.getaddrinfo', return_value=[]):
        with pytest.raises(ValueError):
            assert_public_url_egress('http://empty.example.com/')


def test_hostless_url_is_blocked():
    with pytest.raises(ValueError):
        assert_public_url_egress('not-a-url-at-all')


# ── The IPv4-mapped-IPv6 normalization helper ─────────────────────────────────────

def test_ipv4_mapped_ipv6_is_normalized_before_check():
    assert _is_blocked_ip('::ffff:127.0.0.1') is True
    assert _is_blocked_ip('::ffff:8.8.8.8') is False


# ── Integration: the guard fires before any network I/O in utils.utils ────────────

def test_save_url_refuses_loopback_before_network(monkeypatch):
    from agent_cascade.utils import utils as U
    called = {}

    def _fake_guarded_get(*a, **k):
        called['yes'] = True

    monkeypatch.setattr(U, 'guarded_get', _fake_guarded_get)
    with pytest.raises(ValueError):
        U.save_url_to_local_work_dir('http://127.0.0.1/secret', save_dir=tempfile.mkdtemp())
    assert 'yes' not in called, 'guarded_get must not be called for a blocked target'


def test_guarded_session_revalidates_each_redirect_hop(monkeypatch):
    """A public host that 302s into a reserved range must be refused at the redirect hop.

    ``_SSRFGuardedSession.send`` re-runs the guard on ``request.url`` before every wire
    write, so a blocked hop raises and never reaches the underlying (parent) send.
    """
    import requests as _requests
    from agent_cascade.utils import ssrf_guard as G
    session = G._SSRFGuardedSession()
    wire_calls = []

    def _fake_parent_send(self, request, **kw):
        wire_calls.append(request.url)

    monkeypatch.setattr(_requests.Session, 'send', _fake_parent_send)
    # A blocked hop must be refused BEFORE the wire write (parent send never reached).
    blocked = _requests.Request('GET', 'http://169.254.169.254/latest/meta-data/').prepare()
    with pytest.raises(ValueError):
        session.send(blocked)
    assert not wire_calls, 'a blocked hop must not reach the wire'
    # A public hop passes through to the parent.
    public = _requests.Request('GET', 'http://8.8.8.8/').prepare()
    session.send(public)
    assert wire_calls == ['http://8.8.8.8/']


def test_head_request_refuses_metadata_before_network(monkeypatch):
    from agent_cascade.utils import utils as U
    called = {}

    def _fake_head(*a, **k):
        called['yes'] = True

    monkeypatch.setattr(U.requests, 'head', _fake_head)
    with pytest.raises(ValueError):
        U.get_content_type_by_head_request('http://169.254.169.254/latest/meta-data/')
    assert 'yes' not in called, 'requests.head must not be called for a blocked target'
