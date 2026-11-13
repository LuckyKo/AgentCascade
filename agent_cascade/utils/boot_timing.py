"""Boot-phase timing instrumentation (BUG_0310).

A zero-dependency, **pure-logging** measuring stick for the AC server boot
sequence. This module changes nothing about behavior: it only emits
``[BOOT-TIMING] <name>: <seconds>`` log lines. It exists so the boot hot steps
(MCP connect, skill discovery, agent template load, memory-hint rescan) can be
measured before/after WITHOUT introducing threading or init-order changes.

Two entry points:

- ``mark(name)``  — logs cumulative elapsed time since this module was first
  imported (``_T0``). Consecutive marks read as a cumulative boot timeline.
- ``Phase(name)`` — a context manager that logs the duration of its own block.
  Use it to time one bounded step (e.g. ``with Phase('skill_discover'): ...``).

Log lines emitted before ``init_logging()`` attaches handlers are dropped,
exactly like every other log call at that point — so the marks placed at the
very top of the entry point are recorded against ``_T0`` but may not appear in
console.log. All hot steps happen after logging is up and are always recorded.

Per-agent sub-marks use ``debug=True`` to keep normal INFO logs clean.
"""

import time

from agent_cascade.log import logger

# Single process-wide origin, captured at the first import of this module. Every
# mark is measured relative to it, so the whole boot can be read from one set of
# numbers regardless of which module (start_api_server / shared_init / pool) logs
# the marks.
_T0 = time.perf_counter()


def mark(name: str, *, debug: bool = False) -> None:
    """Log cumulative seconds since module import (``_T0``).

    Elapsed is *since start*, not since the previous mark. No return value
    (YAGNI — no caller captures it).
    """
    log = logger.debug if debug else logger.info
    log('[BOOT-TIMING] %s: %.3fs (since start)', name, time.perf_counter() - _T0)


class Phase:
    """Context manager: log ``[BOOT-TIMING] <name>: <dur-s>`` on exit.

    Unlike ``mark`` this reports the duration of the block itself, so a slow step
    stands out without subtracting adjacent cumulative marks.

        with Phase('skill_discover'):
            self.skill_manager.discover(tiers)
    """

    __slots__ = ('_name', '_log', '_start')

    def __init__(self, name: str, *, debug: bool = False):
        self._name = name
        self._log = logger.debug if debug else logger.info

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._log('[BOOT-TIMING] %s: %.3fs (duration)', self._name,
                  time.perf_counter() - self._start)
        return False  # never swallow exceptions from the timed block
