"""Tests for the AUTO RAM-floor hook on shell_cmd (execution_mode='auto').

When the caller's endpoint is a llama-autoloader AND free SYSTEM RAM is below a hard
floor (AUTO_RAM_FLOOR, see settings.SAFE_MODE_AUTO_RAM_FLOOR_MB), AUTO routes to safe mode
(unload model → run → restore KV) so a memory-hungry command can't OOM-kill Windows. When any
GPU's free VRAM is below an advisory floor (AUTO_VRAM_FLOOR), a warning is queued but safe is NOT forced.

Design invariants under test:
  - The capability gate mirrors _execute_safe's GATE A (reads _last_endpoint_config), so
    we only force safe when safe would actually save/restore KV, not degrade to sync.
  - The probe targets the HELD endpoint (resolve_held_endpoint), never a stale
    _last_endpoint_config.
  - Explicit 'sync'/'async'/'safe' win over the floor (the floor only applies to 'auto').
  - Forced-safe takes precedence over auto-async (a command about to OOM must not run
    unmonitored in the background).
  - Forced-safe queues NO pre-command tool warning (BUG_0174/commit 42956f47): such a warning
    claimed "local model unloaded" before the approval gate ran, so it was false when the
    command was denied. The reason is still logged at INFO; the user-facing notification is
    the `[safe]` note `_execute_safe` appends after the unload actually happened.
  - Best-effort: a status-fetch failure degrades to the normal path, never crashes.

Mock style mirrors tests/test_shell_cmd_safe_mode.py. The instance is a real AgentInstance-
LIKE object (real _tool_warnings list + _compression_lock RLock), NOT a bare MagicMock —
a MagicMock auto-creates a truthy _tool_warnings and a non-subscriptable list, so
_queue_tool_warning would no-op and the warning assertion would be vacuous (the footgun
called out in state_ops.kv_save_effectively_enabled's docstring).
"""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.operation_manager import OperationManager
from agent_cascade.settings import SAFE_MODE_AUTO_RAM_FLOOR_MB as AUTO_RAM_FLOOR, SAFE_MODE_AUTO_VRAM_FLOOR_MB as AUTO_VRAM_FLOOR
from agent_cascade.tools.custom.shell_cmd import ShellCmd
from jsonschema import ValidationError

# A state-save-capable llama-autoloader endpoint config (the instance's cached config).
CAPABLE_EP = {
    'state_save_enabled': True,
    'api_base': 'http://127.0.0.1:1234/v1',
    'model': 'test-model',
}

# Contract pin: these are the LIVE production defaults (imported, not hardcoded). Tests that
# assert against them must stay in sync with settings.py automatically — if the default moves,
# this import tracks it. Never reintroduce a local literal here.


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _make_instance(*, endpoint=CAPABLE_EP):
    """A stand-in AgentInstance carrying the attributes the AUTO hook + queue read.

    _tool_warnings is a REAL list and _compression_lock a real RLock so _queue_tool_warning
    actually appends (a bare MagicMock would auto-create truthy attrs and a non-subscriptable
    list, making the warning assertion vacuous — the documented footgun).
    """
    inst = MagicMock()
    inst.instance_name = 'agent-1'
    inst.agent_class = 'assistant'
    inst._state_lock = threading.RLock()
    inst._state_label = None
    inst._last_endpoint_config = endpoint
    inst._tool_warnings = []
    inst._compression_lock = threading.RLock()
    return inst


def _make_pool(*, instance, ram_floor=AUTO_RAM_FLOOR, vram_floor=AUTO_VRAM_FLOOR, api_router=None,
               no_settings=False, get_instance=None):
    """A pool wired for the AUTO path.

    ``settings`` is a real SimpleNamespace (not a MagicMock) so the floor reads return real
    ints — a bare MagicMock would auto-create a truthy attribute chain and the floor compare
    would be against a Mock, not a number.

    ``no_settings=True`` sets ``pool.settings = None`` to exercise the getattr-default fallback.
    ``get_instance`` overrides the lookup (e.g. ``lambda name: None`` to simulate no instance).
    """
    pool = MagicMock()
    pool.get_instance = get_instance or (lambda name: instance)  # returns the instance for ANY name (incl. 'unknown')

    om = OperationManager.__new__(OperationManager)
    om.agent_pool = pool
    om.base_dir = '/tmp'
    from pathlib import Path as _Path
    om._resolve_path = lambda path, mode='ro': _Path('.')
    pool.operation_manager = om

    # Real int floors so the AUTO hook reads authoritative values.
    if no_settings:
        pool.settings = None  # exercise the getattr-default fallback
    else:
        pool.settings = SimpleNamespace(
            safe_mode_auto_ram_floor_mb=ram_floor,
            safe_mode_auto_vram_floor_mb=vram_floor,
        )
    if api_router is not None:
        pool.api_router = api_router
    return pool


def _router_held(api_base, model):
    """A pool.api_router stand-in resolving a held endpoint (for resolve_held_endpoint)."""
    router = SimpleNamespace()
    router.get_effective_slot_info = lambda *a, **kw: {'api_base': api_base, 'needs_slot': True}
    router.get_endpoint_chain = lambda *a, **kw: [{'api_base': api_base, 'model': model}]
    return router


def _make_tool(pool):
    tool = ShellCmd()
    tool.agent_pool = pool
    return tool


def _wire(*, ram_floor=AUTO_RAM_FLOOR, vram_floor=AUTO_VRAM_FLOOR, no_settings=False, get_instance=None):
    """The dominant test setup: a capable instance + held-capable endpoint + default floors.

    Returns (inst, pool, tool). Pass ram_floor/vram_floor/no_settings/get_instance to vary the
    pool; for a non-capable or custom-endpoint instance, build those with _make_instance/_make_pool
    directly instead of this helper.
    """
    inst = _make_instance()
    pool = _make_pool(instance=inst, ram_floor=ram_floor, vram_floor=vram_floor, no_settings=no_settings,
                      get_instance=get_instance,
                      api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
    return inst, pool, _make_tool(pool)


def _status(ram_free=None, gpus=None):
    """Build a /v1/status-shaped dict to drive the real ram_vram_pressure."""
    st = {}
    if ram_free is not None:
        st['ram'] = {'total_mb': 32768, 'used_mb': 32768 - ram_free, 'free_mb': ram_free, 'pct': 0}
    if gpus is not None:
        st['gpus'] = gpus
    return st


def _gpu(index, free_mb):
    return {'index': index, 'name': f'cuda:{index}', 'total_mb': 24576,
            'used_mb': 24576 - free_mb, 'free_mb': free_mb, 'utilization_pct': 0}


# ────────────────────────────────────────────────────────────────────────────
# 1. Low RAM + autoloader + capable → force safe (routes to _execute_safe), no
#    premature tool warning; the reason is logged instead.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoForcesSafe:
    def test_low_ram_forces_execute_safe_without_premature_warning(self, caplog):
        inst, pool, tool = _wire()

        # Drive the REAL ram_vram_pressure by stubbing only the status fetch.
        status = _status(ram_free=1000)  # 1000 < AUTO_RAM_FLOOR → force_safe

        with (
            patch('agent_cascade.state_ops.probe_memory_status', return_value=status) as m_probe,
            caplog.at_level(logging.INFO, logger='agent_cascade.tools.custom.shell_cmd'),
            patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe,
            patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync,
            patch.object(tool, '_launch_async', side_effect=lambda **k: 'ASYNC') as m_async,
        ):
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SAFE'
            m_safe.assert_called_once()
            m_sync.assert_not_called()
            m_async.assert_not_called()
            # Execution witness: the probe DID see low RAM and force safe, so the empty
            # warning list below proves "forced and queued nothing", not "hook skipped".
            m_probe.assert_called_once()

        # BUG_0174 / 42956f47: the queued warning claimed "local model unloaded" BEFORE the
        # approval gate, so a denied command made it false. No pre-command warning may return;
        # the accurate notification is _execute_safe's [safe] note (gated on `freed`).
        assert inst._tool_warnings == [], \
            f'forced-safe must not queue a pre-command warning: {inst._tool_warnings}'
        # The "why safe" diagnostic is still emitted, at INFO (kept by the same commit).
        assert any('Auto safe-mode: free RAM 1000 MB < floor' in rec.getMessage()
                   for rec in caplog.records), caplog.text

    def test_low_ram_beats_auto_async(self):
        """Forced-safe takes precedence over auto-async: a long-timeout auto command that
        would launch in the background is rerouted to safe when RAM is critically low."""
        inst, pool, tool = _wire()

        status = _status(ram_free=500)  # below floor

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status) as m_probe, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe, \
              patch.object(tool, '_launch_async', side_effect=lambda **k: 'ASYNC') as m_async:
            # timeout=300 > AUTO_ASYNC_TIMEOUT_THRESHOLD → auto-async would normally fire.
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it', 'timeout': 300})
            assert out == 'SAFE'
            m_safe.assert_called_once()
            m_async.assert_not_called()  # async launch skipped
            m_probe.assert_called_once()  # witness: the floor was actually evaluated
        assert inst._tool_warnings == [], \
            f'forced-safe must not queue a pre-command warning: {inst._tool_warnings}'


# ────────────────────────────────────────────────────────────────────────────
# 2. High RAM + low VRAM → normal path (_execute_sync) + low-VRAM advisory.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoLowVramAdvisory:
    def test_high_ram_low_vram_sync_with_advisory(self):
        inst, pool, tool = _wire()

        # RAM comfortably above floor (no force_safe); GPU 0 below the VRAM floor.
        status = _status(ram_free=20000, gpus=[_gpu(0, 100)])

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status), \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()  # NOT forced safe on a VRAM advisory

        # BUG_0085: advisory reworded in 14286608 ("low VRAM" → "low free VRAM ... significant VRAM");
        # assert the current wording verbatim enough to catch future drift.
        assert any('low free VRAM on GPU(s)' in w for w in inst._tool_warnings), \
            f'expected a low-VRAM advisory: {inst._tool_warnings}'
        assert any("consider execution_mode='safe'" in w for w in inst._tool_warnings)
        assert not any('Running in SAFE mode' in w for w in inst._tool_warnings)


# ────────────────────────────────────────────────────────────────────────────
# 3. High RAM + high VRAM → normal path, no warning.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoNoPressure:
    def test_high_ram_high_vram_no_warning(self):
        inst, pool, tool = _wire()

        status = _status(ram_free=20000, gpus=[_gpu(0, 20000)])  # all above floors

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status) as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            # Execution witness: the probe DID run and returned a no-pressure status, so the
            # empty _tool_warnings below proves "ran and found no pressure", not "short-circuited".
            m_probe.assert_called_once()
        assert inst._tool_warnings == []  # no warnings queued


# ────────────────────────────────────────────────────────────────────────────
# 4. Probe returns None (status fetch fails) → normal path, no crash, no warning.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoProbeFailureDegrades:
    def test_probe_none_normal_path_no_warning(self):
        inst, pool, tool = _wire()

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=None) as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            # Execution witness: the probe DID run and returned None (fetch failed), so the
            # empty _tool_warnings proves "ran and degraded", not "short-circuited".
            m_probe.assert_called_once()
        assert inst._tool_warnings == []  # degraded, no spurious warning


# ────────────────────────────────────────────────────────────────────────────
# 5. Non-autoloader held endpoint → probe never called, normal path.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoNonAutoloader:
    def test_non_autoloader_endpoint_probe_not_called(self):
        inst = _make_instance()
        # Held endpoint resolves to a non-autoloader port → is_autoloader_endpoint False.
        pool = _make_pool(instance=inst, api_router=_router_held('http://127.0.0.1:8080/v1', 'test-model'))
        tool = _make_tool(pool)

        with patch('agent_cascade.state_ops.probe_memory_status') as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            m_probe.assert_not_called()  # autoloader gate short-circuits before probing
        assert inst._tool_warnings == []


# ────────────────────────────────────────────────────────────────────────────
# 6. Explicit mode='sync' with low RAM → NOT forced safe (explicit wins).
# ────────────────────────────────────────────────────────────────────────────


class TestAutoExplicitWins:
    def test_explicit_sync_low_ram_not_forced_safe(self):
        inst, pool, tool = _wire()

        status = _status(ram_free=100)  # would force safe if AUTO applied

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status), \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it',
                             'execution_mode': 'sync'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()  # explicit sync wins over the RAM floor
        assert inst._tool_warnings == [], inst._tool_warnings  # BUG_0174: no queued safe warning


# ────────────────────────────────────────────────────────────────────────────
# 7. Floors are read from settings: a low floor flips the outcome.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoFloorsFromSettings:
    def test_low_floor_disables_force_safe(self):
        """Same status (free RAM 2000 MB) that WOULD force safe at the default AUTO_RAM_FLOOR
        must NOT force safe when the configured floor is below it (1000). Proves the floor
        is read from pool.settings, not hardcoded."""
        inst, pool, tool = _wire(ram_floor=1000)

        status = _status(ram_free=2000)  # 2000 > 1000 floor → not forced

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status), \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
        assert inst._tool_warnings == [], inst._tool_warnings  # BUG_0174: no queued safe warning

    def test_high_floor_forces_safe(self, caplog):
        """Same status (free RAM 2000 MB) DOES force safe when the configured floor is above
        it (8192). Pairs with the test above to bracket the floor read."""
        inst, pool, tool = _wire(ram_floor=8192)

        status = _status(ram_free=2000)  # 2000 < 8192 floor → forced

        with (
            patch('agent_cascade.state_ops.probe_memory_status', return_value=status),
            caplog.at_level(logging.INFO, logger='agent_cascade.tools.custom.shell_cmd'),
            patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe,
            patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync,
        ):
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SAFE'
            m_safe.assert_called_once()
            m_sync.assert_not_called()
        # BUG_0174: no pre-command tool warning; the configured floor is named in the log.
        assert inst._tool_warnings == [], inst._tool_warnings
        assert any('< floor 8192 MB' in rec.getMessage() for rec in caplog.records), caplog.text


# ────────────────────────────────────────────────────────────────────────────
# 8. Capability gate: not-capable endpoint → probe skipped, normal path (GATE A).
# ────────────────────────────────────────────────────────────────────────────


class TestAutoCapabilityGate:
    def test_not_capable_endpoint_probe_not_called(self):
        """The capability gate mirrors _execute_safe's GATE A: a non-capable _last_endpoint_config
        must short-circuit before resolve_held_endpoint/probe, so safe is never forced."""
        inst = _make_instance(endpoint={'state_save_enabled': False,
                                        'api_base': 'http://127.0.0.1:1234/v1',
                                        'model': 'test-model'})
        pool = _make_pool(instance=inst, api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
        tool = _make_tool(pool)

        status = _status(ram_free=100)  # would force safe if the gate let it through

        with patch('agent_cascade.state_ops.probe_memory_status') as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            m_probe.assert_not_called()  # capability gate short-circuits before probing
        assert inst._tool_warnings == []


# ────────────────────────────────────────────────────────────────────────────
# 9. Schema: 'auto' is expressed by OMISSION (no literal 'auto'); unknown still rejected.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoSchema:
    def test_unknown_mode_still_rejected(self):
        pool = _make_pool(instance=_make_instance(), api_router=_router_held(CAPABLE_EP['api_base'], CAPABLE_EP['model']))
        tool = _make_tool(pool)
        with pytest.raises(ValidationError):
            tool.call({'command': 'ls', 'justification': 'x', 'execution_mode': 'bogus'})


# ────────────────────────────────────────────────────────────────────────────
# 10. Defensive edge cases the task called out: instance None, settings missing,
#     status lacking ram/free_mb keys, empty gpus / gpu missing index, explicit async.
# ────────────────────────────────────────────────────────────────────────────


class TestAutoDefensiveEdges:
    def test_instance_none_normal_path_no_crash(self):
        """No instance resolvable → capability gate short-circuits; command still runs."""
        inst, pool, tool = _wire(get_instance=lambda name: None)

        with patch('agent_cascade.state_ops.probe_memory_status') as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            m_probe.assert_not_called()  # no instance → no probe

    def test_settings_missing_uses_default_floor(self, caplog):
        """pool.settings is None → getattr fallback uses the default AUTO_RAM_FLOOR, so low RAM
        still forces safe. Proves the fallback path, not a hardcoded skip."""
        inst, pool, tool = _wire(no_settings=True)

        status = _status(ram_free=1000)  # 1000 < default AUTO_RAM_FLOOR → forced

        with (
            patch('agent_cascade.state_ops.probe_memory_status', return_value=status),
            caplog.at_level(logging.INFO, logger='agent_cascade.tools.custom.shell_cmd'),
            patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe,
            patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync,
        ):
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SAFE'
            m_safe.assert_called_once()
            m_sync.assert_not_called()
        # BUG_0174: no pre-command tool warning; the DEFAULT floor is named in the log.
        assert inst._tool_warnings == [], inst._tool_warnings
        assert any(f'< floor {AUTO_RAM_FLOOR} MB' in rec.getMessage()
                   for rec in caplog.records), caplog.text

    def test_status_missing_ram_keys_not_forced(self):
        """A non-empty status dict that lacks a usable ram.free_mb must NOT force safe and must
        NOT crash — drives the REAL ram_vram_pressure defensive branches (not the `not status`
        early-return)."""
        inst, pool, tool = _wire()

        # Non-empty (so it passes the `if not status` guard) but ram has no free_mb.
        status = {'ram': {'total_mb': 32768}, 'gpus': []}

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status) as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            m_probe.assert_called_once()  # probe ran; the missing-key handling is what we assert
        assert inst._tool_warnings == []

    def test_empty_gpus_and_missing_index_no_advisory(self):
        """gpus: [] yields no low-VRAM advisory; a GPU below the VRAM floor but missing an
        'index' key is also skipped (index is required to report a GPU)."""
        inst, pool, tool = _wire()

        # High RAM (no force), one GPU below the VRAM floor but with NO 'index' key.
        status = {'ram': {'free_mb': 20000}, 'gpus': [{'free_mb': 100}]}

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status) as m_probe, \
              patch.object(tool, '_execute_sync', side_effect=lambda **k: 'SYNC') as m_sync, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it'})
            assert out == 'SYNC'
            m_sync.assert_called_once()
            m_safe.assert_not_called()
            m_probe.assert_called_once()
        assert not any('low VRAM' in w for w in inst._tool_warnings)  # GPU without index is skipped

    def test_explicit_async_low_ram_launches_async(self):
        """Explicit execution_mode='async' wins over the RAM floor: the command launches in
        the background even when RAM is critically low (the floor only applies to 'auto')."""
        inst, pool, tool = _wire()

        status = _status(ram_free=100)  # would force safe if AUTO applied

        with patch('agent_cascade.state_ops.probe_memory_status', return_value=status) as m_probe, \
              patch.object(tool, '_launch_async', side_effect=lambda **k: 'ASYNC') as m_async, \
              patch.object(tool, '_execute_safe', side_effect=lambda **k: 'SAFE') as m_safe:
            out = tool.call({'command': 'python heavy.py', 'justification': 'run it',
                             'execution_mode': 'async'})
            assert out == 'ASYNC'
            m_async.assert_called_once()
            m_safe.assert_not_called()
            m_probe.assert_not_called()  # explicit async → AUTO hook skipped entirely
        assert inst._tool_warnings == [], inst._tool_warnings  # BUG_0174: no queued safe warning
