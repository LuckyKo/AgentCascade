"""BUG_0418 — RAM-proportional pytest-xdist worker count.

Background (see plans/BUG_0418_xdist_ram_balance_plan.md): the suite runs under
``-n auto`` (``pytest.ini`` addopts).  xdist's own ``auto`` default is CPU-count only
(``xdist/plugin.py``) and performs no RAM logic, so on a host whose RAM is partly
consumed by a co-resident model it spawns too many workers and one of them can die
with ``MemoryError`` during the heavy ``tests/conftest.py`` import (BUG_0078).

This module supplies the RAM-aware value that the ``pytest_xdist_auto_num_workers``
hook in ``tests/conftest.py`` returns:

- ``compute_xdist_n`` is the pure core: bytes in, int out, side-effect-free, fully
  unit-testable without touching real RAM.
- ``resolve_xdist_n`` is the thin boundary that reads env + ``psutil`` and delegates
  to the pure core.  If ``psutil`` is missing it returns ``None`` so the hook defers
  to xdist's built-in CPU-count default (today's behavior) — graceful degradation,
  never a crash.

Explicit numeric ``-n`` bypasses this entirely: xdist only invokes
``pytest_xdist_auto_num_workers`` when ``numprocesses in ("auto", "logical")``, so a
user-forced ``-n N`` is honored with no detection logic on our side.
"""

import os


def compute_xdist_n(
    available_bytes: int,
    nproc: int,
    per_worker_bytes: int,
    floor: int = 2,
    ceiling: int | None = None,
) -> int:
    """RAM-proportional xdist worker count, clamped to [floor, min(ceiling, nproc)].

    Pure and side-effect-free. ``available_bytes`` is free RAM in bytes; ``nproc``
    is the logical CPU count (the ceiling source); ``per_worker_bytes`` is the RAM
    budget per worker. ``floor`` keeps at least minimal isolation, ``ceiling`` (None
    -> nproc) never lets us exceed cores.
    """
    ceiling = nproc if ceiling is None else ceiling
    ceiling = max(1, min(ceiling, nproc))                 # never exceeds cores
    if per_worker_bytes <= 0:
        return ceiling                                    # guard: avoid ZeroDivisionError
    raw = int(available_bytes // per_worker_bytes)
    return min(max(raw, floor), ceiling)                  # clamp; floor can't exceed ceiling


def _env_int(name: str, default):
    """Parse an env var as an int with a guarded fallback to ``default``.

    Unset/empty -> ``default``; present-but-unparseable -> ``default`` (a bad value
    must never crash the test session). Mirrors the guarded style at
    ``agent_cascade/telemetry.py:2599``.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def resolve_xdist_n(log: bool = True):
    """Resolve the RAM-proportional xdist worker count, or ``None`` to defer.

    Returns ``None`` when ``psutil`` is unavailable so the hook falls through to
    xdist's built-in CPU-count default. When ``AGENT_CASCADE_XDIST_N`` is set to a
    valid int it is returned directly (manual full override of ``auto``), bypassing
    the RAM math.

    Emits the one-shot ``[conftest][BUG_0418]`` transparency line here rather than in
    the hook: the hook receives only the final int and cannot format the available /
    per_worker / nproc / floor / ceiling / derived fields, all of which are computed
    inside this function. Pass ``log=False`` to suppress that print (unit tests); the
    conftest hook calls this with the default ``log=True`` so the real run logs.
    """
    # Manual full override: honor AGENT_CASCADE_XDIST_N=<int> directly (bypass RAM math).
    raw_override = os.environ.get('AGENT_CASCADE_XDIST_N')
    if raw_override is not None and raw_override.strip() != '':
        try:
            override_n = int(raw_override)
        except (ValueError, TypeError):
            override_n = None                             # bad value -> fall through to RAM math
        if override_n is not None:
            if log:
                print(f"\n[conftest][BUG_0418] xdist -n={override_n} (source: env-override)")
            return override_n

    try:
        import psutil
    except ImportError:
        if log:
            print('\n[conftest][BUG_0418] psutil unavailable — deferring to xdist CPU-count default')
        return None

    available = psutil.virtual_memory().available
    nproc = psutil.cpu_count(logical=True) or os.cpu_count() or 1

    # 2 GiB/worker default (measured, 2026-11-27): per-xdist-worker peak RSS is
    # ~1.2 GiB under heavy fixture load (agent/stress/session tests; only ~0.2 GiB
    # for the conftest import alone), so a 2 GiB budget keeps a ~68% safety margin
    # while letting the scaler reach the full core ceiling (e.g. 42 GiB / 2 GiB
    # -> 21 -> capped at nproc=16). A 1 GiB budget would over-provision (16+
    # workers -> thrash + the documented BUG_0078 worker-death).
    budget_mb = _env_int('AGENT_CASCADE_XDIST_RAM_BUDGET_MB', 2048)
    per_worker_bytes = budget_mb * 1024 * 1024
    floor = _env_int('AGENT_CASCADE_XDIST_FLOOR', 2)
    ceiling = _env_int('AGENT_CASCADE_XDIST_CEILING', None)  # None -> nproc (inside compute_xdist_n)

    n = compute_xdist_n(available, nproc, per_worker_bytes, floor, ceiling)

    # Transparency line mirrors compute_xdist_n's internals (raw + effective ceiling).
    eff_ceiling = nproc if ceiling is None else max(1, min(ceiling, nproc))
    derived = eff_ceiling if per_worker_bytes <= 0 else int(available // per_worker_bytes)
    available_gib = available / (1024 ** 3)
    per_worker_mib = per_worker_bytes // (1024 * 1024)
    if log:
        print(
            f"\n[conftest][BUG_0418] xdist -n={n} | "
            f"available={available_gib:.1f} GiB | per_worker={per_worker_mib} MiB | "
            f"nproc={nproc} | floor={floor} | ceiling={eff_ceiling} | derived={derived} (psutil)"
        )
    return n
