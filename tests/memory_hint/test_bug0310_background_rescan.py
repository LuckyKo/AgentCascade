"""BUG_0310 R2 — the boot vault rescan is deferred off the boot thread.

Covers ``MemoryHintManager.rescan_vaults_background``: the initial index build now runs
on a one-shot daemon thread, so it no longer blocks boot. The ONLY observable change is
that the index is briefly EMPTY right after the call; it must become populated within a
bounded window. Tests that:
  * ``rescan_vaults_background()`` returns immediately (deferral — no boot-thread block);
  * the index populates shortly after (eventual consistency, bounded window);
  * the population happens on a NON-main daemon thread named 'memory-hint-initial-rescan'
    (so a hung vault walk can never stall interpreter exit).

Deliberately does NOT call ``start()``: without the worker there is no idle-tick
auto-rescan, so any index population is provably from the background thread we launched.
"""

import time
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_cascade.memory_hint.manager import MemoryHintManager


def _make_mgr(tmp_path) -> MemoryHintManager:
    """MemoryHintManager over a one-vault fake pool (mirrors tests/memory_hint fixtures)."""
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True)
    (v / 'lesson.md').write_text(
        '# Debugging\nboot slowness from a synchronous vault rescan on the boot thread\n',
        encoding='utf-8')
    pool = MagicMock()
    pool.operation_manager = SimpleNamespace(
        base_dir=str(v.parent), extra_work_folders_ro=[], extra_work_folders_rw=[])
    return MemoryHintManager(pool)


def test_background_rescan_defers_then_populates(tmp_path):
    mgr = _make_mgr(tmp_path)
    # Synchronous build is gone: nothing indexed until the background thread finishes.
    assert len(mgr._vault_indexes) == 0

    observed = {}
    orig = mgr.rescan_vaults

    def _spy():
        t = threading.current_thread()
        observed['name'] = t.name
        observed['daemon'] = t.daemon
        observed['is_main'] = t is threading.main_thread()
        orig()

    mgr.rescan_vaults = _spy  # rescan_vaults_background() looks this up at thread start

    t0 = time.perf_counter()
    mgr.rescan_vaults_background()
    returned_in = time.perf_counter() - t0

    # Returned immediately ⇒ the work was deferred off the boot thread, not run inline.
    assert returned_in < 0.1, f'rescan_vaults_background blocked {returned_in:.3f}s (not deferred)'

    # The brief empty-index window must be bounded: index populates within a short wait.
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline and len(mgr._vault_indexes) == 0:
        time.sleep(0.02)
    assert len(mgr._vault_indexes) >= 1, \
        'background rescan did not populate the index within the bounded window'

    # Proven to have run off the boot thread, on a daemon worker.
    assert observed.get('is_main') is False, 'rescan ran on the main (boot) thread'
    assert observed.get('daemon') is True, 'rescan thread is not a daemon (could stall exit)'
    assert observed.get('name') == 'memory-hint-initial-rescan'
