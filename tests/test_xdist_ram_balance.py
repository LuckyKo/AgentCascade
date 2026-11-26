"""BUG_0418 — unit tests for RAM-proportional pytest-xdist worker count.

Covers ``tests/xdist_ram_balance.py``:
- ``compute_xdist_n`` is the pure core; we feed synthetic ``available_bytes`` so no
  real RAM is touched (proportional, floor, ceiling, ceiling<floor, divide-by-zero guard).
- ``resolve_xdist_n`` env-override (``AGENT_CASCADE_XDIST_N``) and psutil-missing
  deferral (returns ``None`` -> hook falls through to xdist's CPU-count default).
"""

import sys

import pytest

from tests.xdist_ram_balance import compute_xdist_n, resolve_xdist_n

_MIB = 1024 * 1024
_GIB = 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# compute_xdist_n — pure core (bytes in, int out)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'available_gib, expected',
    [
        (32, 16),   # this host idle: raw=32 -> capped at nproc=16
        (12, 12),   # a resident model used ~20 GiB: auto-reduces to 12 (mitigates OOM)
        (8, 8),
        (4, 4),
    ],
)
def test_proportional(available_gib, expected):
    assert compute_xdist_n(
        available_bytes=available_gib * _GIB,
        nproc=16,
        per_worker_bytes=_GIB,
    ) == expected


@pytest.mark.parametrize('available_gib', [1, 2])
def test_floor(available_gib):
    # raw <= 2, but floor=2 keeps minimal isolation (never serial).
    assert compute_xdist_n(
        available_bytes=available_gib * _GIB,
        nproc=16,
        per_worker_bytes=_GIB,
    ) == 2


def test_ceiling_caps_at_nproc():
    # Huge RAM but only 16 cores -> never exceed cores.
    assert compute_xdist_n(
        available_bytes=1000 * _GIB,
        nproc=16,
        per_worker_bytes=_GIB,
    ) == 16


def test_ceiling_below_floor_clamps_to_nproc():
    # 1-core machine, plenty of RAM -> ceiling (nproc=1) wins over floor=2.
    assert compute_xdist_n(
        available_bytes=8 * _GIB,
        nproc=1,
        per_worker_bytes=_GIB,
    ) == 1


def test_zero_per_worker_returns_ceiling():
    # Guard: per_worker_bytes=0 must not raise ZeroDivisionError; returns ceiling.
    assert compute_xdist_n(
        available_bytes=8 * _GIB,
        nproc=16,
        per_worker_bytes=0,
    ) == 16


# ---------------------------------------------------------------------------
# resolve_xdist_n — env override + psutil-missing deferral
# ---------------------------------------------------------------------------
@pytest.fixture
def _clean_env(monkeypatch):
    """Strip every BUG_0418 env knob so resolver behavior is deterministic."""
    for name in (
        'AGENT_CASCADE_XDIST_N',
        'AGENT_CASCADE_XDIST_RAM_BUDGET_MB',
        'AGENT_CASCADE_XDIST_FLOOR',
        'AGENT_CASCADE_XDIST_CEILING',
    ):
        monkeypatch.delenv(name, raising=False)


def test_env_override_wins(monkeypatch, _clean_env):
    monkeypatch.setenv('AGENT_CASCADE_XDIST_N', '5')
    assert resolve_xdist_n(log=False) == 5


def test_env_override_non_integer_falls_through(monkeypatch, _clean_env):
    # A malformed override must be ignored (guarded parse), not crash: with psutil
    # present the RAM math runs and yields some int (never None).
    monkeypatch.setenv('AGENT_CASCADE_XDIST_N', 'not-an-int')
    result = resolve_xdist_n(log=False)
    assert isinstance(result, int)


def test_psutil_missing_returns_none(monkeypatch, _clean_env):
    # sys.modules["psutil"] = None makes `import psutil` raise ImportError, so the
    # resolver defers (returns None) and the hook falls back to xdist's CPU default.
    monkeypatch.setitem(sys.modules, 'psutil', None)
    assert resolve_xdist_n(log=False) is None
