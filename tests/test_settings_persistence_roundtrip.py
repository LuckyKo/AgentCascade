"""Restart round-trip tests for persisted settings (Phase 1 exit criteria).

Verifies the set → save → fresh-load → same-value invariant, one test per
persisted SETTINGS AXIS (not one test per registry key): LLM config
(default_llm_cfg), session name, work folders (incl. empty-list clear),
compression/disabled tools, the 3 loop-recovery toggles, atomic-write
invariants, and the revision counter. Representative keys are asserted per
axis; the registry-completeness suite (test_settings_registry.py) is what
guarantees every key has a spec.
"""

import json

import pytest

from agent_cascade.agent_instance import PoolSettings


# ── LLM config (default_llm_cfg) round-trip via APIRouter ──────────────

def test_default_llm_cfg_roundtrip(tmp_path):
    """default_llm_cfg persists to api_endpoints.json and is restored on load."""
    from agent_cascade.api_router_pkg.router import APIRouter

    router = APIRouter.__new__(APIRouter)
    # Minimal init of the persistence-relevant attributes (bypass full __init__).
    import os
    import shutil
    router._config_dir = tmp_path
    router._config_path = tmp_path / 'api_endpoints.json'
    router._tmp_path = lambda: tmp_path / 'api_endpoints.json.tmp'
    router._backup_path = lambda: tmp_path / 'api_endpoints.json.bak'
    import threading
    router._lock = threading.Lock()
    router.endpoints = {}
    router.agent_priorities = {}
    router._agent_types_with_priorities = set()
    # APIRouter.__new__ bypasses __init__, so set the dirty flags _save() reads directly.
    router._dirty_endpoints = False
    router._dirty_llm_cfg = False
    router.default_llm_cfg = {
        'model': 'test-model', 'api_base': 'http://x', 'api_key': 'sk-secret',
        'temperature': 0.7, 'max_tokens': 4096, 'log_api_post': True, 'model_type': 'openai',
    }

    router._save()
    # Fresh router loads the persisted config.
    router2 = APIRouter.__new__(APIRouter)
    router2._config_dir = tmp_path
    router2._config_path = tmp_path / 'api_endpoints.json'
    router2._tmp_path = lambda: tmp_path / 'api_endpoints.json.tmp'
    router2._backup_path = lambda: tmp_path / 'api_endpoints.json.bak'
    router2._lock = threading.Lock()
    router2.endpoints = {}
    router2.agent_priorities = {}
    router2._agent_types_with_priorities = set()
    router2._dirty_endpoints = False
    router2._dirty_llm_cfg = False
    router2.default_llm_cfg = {'model': 'placeholder'}
    router2._load()
    assert router2.default_llm_cfg['model'] == 'test-model'
    assert router2.default_llm_cfg['api_key'] == 'sk-secret'
    assert router2.default_llm_cfg['log_api_post'] is True
    # BUG_0252: pin the field the bug is about — api_base must survive the round-trip.
    assert router2.default_llm_cfg['api_base'] == 'http://x'


def test_pool_settings_broadcasts_api_base_from_default_llm_cfg(tmp_path):
    """BUG_0252: the boot/stream snapshot must carry default_llm_cfg.api_base so the UI
    can rehydrate #setting-endpoint after a restart."""
    from agent_cascade.api_integration_pkg.state_builder import _add_pool_runtime_settings

    class _Router:
        default_llm_cfg = {'api_base': 'http://general-endpoint', 'model': 'm'}
    class _Pool:
        operation_manager = None
        api_router = _Router()
        _enable_async_shell_console_window = False

    ps = {}
    _add_pool_runtime_settings(_Pool(), ps)
    assert ps['api_base'] == 'http://general-endpoint'   # fails before Fix A (key absent)

    # An empty/missing default must not emit a stale value:
    class _PoolNoRouter:
        operation_manager = None; api_router = None; _enable_async_shell_console_window = False
    ps2 = {}
    _add_pool_runtime_settings(_PoolNoRouter(), ps2)
    assert ps2['api_base'] == ''                          # defined-but-empty, never undefined-stomp


def test_update_default_llm_cfg_persists(tmp_path):
    """update_default_llm_cfg() must call _save() so changes survive a restart."""
    from agent_cascade.api_router_pkg.router import APIRouter
    import threading

    router = APIRouter.__new__(APIRouter)
    router._config_dir = tmp_path
    router._config_path = tmp_path / 'api_endpoints.json'
    router._tmp_path = lambda: tmp_path / 'api_endpoints.json.tmp'
    router._backup_path = lambda: tmp_path / 'api_endpoints.json.bak'
    router._lock = threading.Lock()
    router.endpoints = {}
    router.agent_priorities = {}
    router._agent_types_with_priorities = set()
    router._dirty_endpoints = False
    router._dirty_llm_cfg = False
    router.default_llm_cfg = {'model': 'a'}
    router.update_default_llm_cfg({'model': 'b', 'max_tokens': 100})
    assert (tmp_path / 'api_endpoints.json').exists()
    on_disk = json.loads((tmp_path / 'api_endpoints.json').read_text())
    assert on_disk['default_llm_cfg']['model'] == 'b'
    assert on_disk['default_llm_cfg']['max_tokens'] == 100


# ── Session name round-trip ─────────────────────────────────────────────

def test_session_name_roundtrip(monkeypatch, tmp_path):
    """save_session_name → load_session_name returns the same value."""
    import agent_cascade.session_state_store as ss

    real_path = ss._session_state_path()
    monkeypatch.setattr(ss, '_session_state_path', lambda: tmp_path / 'session_state.json')
    assert ss.load_session_name() == 'Maine'  # no file yet → default
    assert ss.save_session_name('Rex') is True
    assert ss.load_session_name() == 'Rex'
    # Merge, not replace: a second save preserves the key.
    ss.save_session_name('Nova')
    assert ss.load_session_name() == 'Nova'


# ── Work folders (incl. empty-list clear) round-trip ────────────────────

def _make_pool(tmp_path, ro=(), rw=()):
    from agent_cascade.pool.config_persist import ConfigPersistMixin
    import threading

    class _Om:
        # Mirrors OperationManager.set_extra_work_folders (the live-state seam that
        # _apply_pending_config drives). Records the last-applied folders so tests can
        # assert the startup restore reached the live OM state.
        def set_extra_work_folders(self, ro, rw):
            self.extra_work_folders_ro = list(ro or [])
            self.extra_work_folders_rw = list(rw or [])

    om = _Om()
    om.extra_work_folders_ro = list(ro)
    om.extra_work_folders_rw = list(rw)
    om.base_dir = str(tmp_path)

    class _Pool(ConfigPersistMixin):
        pass

    pool = _Pool()
    pool.settings = PoolSettings()
    pool.llm_cfg = {}
    pool._ui_disabled_tools = {}
    pool._ui_disabled_tools_lock = threading.Lock()
    pool.operation_manager = om
    pool._enable_async_shell_console_window = False
    pool._settings_save_lock = threading.Lock()
    pool.auto_security = False
    pool._pending_work_folders = None
    pool._pending_default_workspace = None
    pool._pool_settings_path = tmp_path / 'pool_settings.json'
    return pool


def test_work_folders_roundtrip(tmp_path):
    pool = _make_pool(tmp_path, ro=['C:\\ro'], rw=['C:\\rw'])
    pool._save_pool_settings()
    on_disk = json.loads(pool._pool_settings_path.read_text())
    assert on_disk['work_access_folders_ro'] == ['C:\\ro']
    assert on_disk['work_access_folders_rw'] == ['C:\\rw']

    # Fresh load restores the folders (deferred to _pending_work_folders, applied
    # post-init by _apply_pending_config once operation_manager exists).
    pool2 = _make_pool(tmp_path)
    pool2._load_pool_settings()
    assert pool2._pending_work_folders == {'ro': ['C:\\ro'], 'rw': ['C:\\rw']}


def test_empty_work_folders_clear_roundtrips(tmp_path):
    """An intentional clear (empty lists) must persist AND round-trip through load."""
    pool = _make_pool(tmp_path, ro=[], rw=[])
    pool._save_pool_settings()
    on_disk = json.loads(pool._pool_settings_path.read_text())
    # Both keys present with empty lists (not omitted).
    assert on_disk['work_access_folders_ro'] == []
    assert on_disk['work_access_folders_rw'] == []
    # Load path preserves the clear (presence-based, not truthiness).
    pool2 = _make_pool(tmp_path)
    pool2._load_pool_settings()
    assert pool2._pending_work_folders == {'ro': [], 'rw': []}


def test_startup_restore_work_folders_into_live_state(tmp_path):
    """A fresh load of pool_settings.json restores extra work folders into the live OM state.

    Regression (2026-11-03): the load path's ``hasattr(self, '_pending_work_folders')`` guard
    was self-referential — the attribute was only ever created by the load path itself — so on
    a fresh pool it was always False, the pending dict was never set, and
    ``_apply_pending_config`` skipped the folders. Persisted work folders therefore never
    reached the live OM state after a restart (the UI's extra-folders list came back empty).
    The fix initializes the attribute in ``__init__`` (core.py) and drops the guard
    (config_persist.py); this test drops the attribute to simulate a fresh pool and proves the
    load path restores the folders into the live OM state.
    """
    # Pool 1: set work folders and persist them to disk.
    pool1 = _make_pool(tmp_path, ro=['C:\\ro'], rw=['C:\\rw'])
    pool1._save_pool_settings()

    # Pool 2: simulate a fresh restart. _make_pool initializes _pending_work_folders = None
    # (mirroring the __init__ init added by the fix); drop it to prove the load path itself
    # creates the attribute and _apply_pending_config applies it to the live OM state.
    pool2 = _make_pool(tmp_path)
    del pool2._pending_work_folders
    pool2._load_pool_settings()
    pool2._apply_pending_config()

    # The live OM state must carry the restored folders.
    assert [str(p) for p in pool2.operation_manager.extra_work_folders_ro] == ['C:\\ro']
    assert [str(p) for p in pool2.operation_manager.extra_work_folders_rw] == ['C:\\rw']


# ── disabled_tools + compression round-trip ─────────────────────────────

def test_disabled_tools_and_compression_roundtrip(tmp_path):
    import threading
    pool = _make_pool(tmp_path)
    pool._ui_disabled_tools = {'coder': ['grep', 'shell_cmd']}
    pool._ui_disabled_tools_lock = threading.Lock()
    pool._save_pool_settings()
    on_disk = json.loads(pool._pool_settings_path.read_text())
    assert on_disk['disabled_tools'] == {'coder': ['grep', 'shell_cmd']}
    assert 'compression_fraction' in on_disk
    assert 'auto_security' in on_disk


# ── loop-recovery toggle round-trip ────────────────────────────────────────

def test_loop_recovery_toggles_roundtrip(tmp_path):
    """The 3 loop-recovery toggles (now declared PoolSettings fields) persist and restore."""
    pool = _make_pool(tmp_path)
    pool.settings.loop_exact_rollback_enabled = False
    pool.settings.loop_fuzzy_warning_enabled = False
    pool.settings.tool_loop_fuzzy_rollback_enabled = True
    pool._save_pool_settings()
    on_disk = json.loads(pool._pool_settings_path.read_text())
    assert on_disk['loop_exact_rollback_enabled'] is False
    assert on_disk['loop_fuzzy_warning_enabled'] is False
    assert on_disk['tool_loop_fuzzy_rollback_enabled'] is True
    # Fresh load restores the values onto the dataclass fields.
    pool2 = _make_pool(tmp_path)
    pool2._load_pool_settings()
    assert pool2.settings.loop_exact_rollback_enabled is False
    assert pool2.settings.loop_fuzzy_warning_enabled is False
    assert pool2.settings.tool_loop_fuzzy_rollback_enabled is True


# ── Atomic-write invariant ──────────────────────────────────────────────

def test_pool_settings_write_is_atomic(tmp_path):
    """After a save, no stray .tmp file remains and the .json is valid JSON."""
    pool = _make_pool(tmp_path)
    pool._save_pool_settings()
    assert (tmp_path / 'pool_settings.json').exists()
    assert not (tmp_path / 'pool_settings.json.tmp').exists()
    # Valid JSON (not truncated).
    json.loads((tmp_path / 'pool_settings.json').read_text())


def test_revision_bumps_on_save(tmp_path):
    """Every settings persist path bumps the single shared revision counter."""
    from agent_cascade.runtime_state import state as _runtime_state
    pool = _make_pool(tmp_path)
    r0 = _runtime_state.settings_revision
    pool._save_pool_settings()
    r1 = _runtime_state.settings_revision
    assert r1 == r0 + 1
    # The other two persist paths route through the same counter.
    from agent_cascade.session_state_store import save_session_name
    save_session_name('Maine')
    assert _runtime_state.settings_revision == r1 + 1


def test_default_llm_cfg_save_bumps_revision(tmp_path):
    """APIRouter._save() (default_llm_cfg path) bumps the same shared counter."""
    from agent_cascade.api_router_pkg.router import APIRouter
    from agent_cascade.runtime_state import state as _runtime_state
    import threading
    router = APIRouter.__new__(APIRouter)
    router._config_dir = tmp_path
    router._config_path = tmp_path / 'api_endpoints.json'
    router._tmp_path = lambda: tmp_path / 'api_endpoints.json.tmp'
    router._backup_path = lambda: tmp_path / 'api_endpoints.json.bak'
    router._lock = threading.Lock()
    router.endpoints = {}
    router.agent_priorities = {}
    router._agent_types_with_priorities = []
    router.default_llm_cfg = {'model': 'm', 'api_base': 'http://x'}
    router._dirty_endpoints = False
    router._dirty_llm_cfg = True  # this _save() is a default_llm_cfg (settings-bucket) write
    r0 = _runtime_state.settings_revision
    router._save()
    assert _runtime_state.settings_revision == r0 + 1


# ── frozenset regression: set-valued llm_cfg keys must not veto a save (Todo.md:134) ──

def test_set_valued_key_does_not_break_pool_settings_save(tmp_path):
    """A frozenset in llm_cfg must not veto the whole pool_settings bucket.

    Regression (Todo.md:134): a frozenset stored in the shared llm_cfg made
    atomic_write_json raise TypeError inside _save_pool_settings()'s try, so the
    entire file (work folders, disabled tools, ...) was silently lost.
    """
    pool = _make_pool(tmp_path, ro=['C:\\ro'], rw=['C:\\rw'])
    pool.llm_cfg['skill_always_protected'] = frozenset({'a', 'b'})
    pool._save_pool_settings()
    assert pool._pool_settings_path.exists()
    on_disk = json.loads(pool._pool_settings_path.read_text())
    # The bucket's keys are written, and the offending key lands as a sorted list.
    assert on_disk['work_access_folders_ro'] == ['C:\\ro']
    assert on_disk['skill_always_protected'] == ['a', 'b']


def test_skill_always_protected_roundtrip(tmp_path):
    """Ingress normalization stores the canonical string; a fresh load re-parses the same set."""
    from agent_cascade.settings import format_skill_always_protected, parse_skill_always_protected
    pool = _make_pool(tmp_path)
    pool.llm_cfg['skill_always_protected'] = format_skill_always_protected('B, A')
    pool._save_pool_settings()
    on_disk = json.loads(pool._pool_settings_path.read_text())
    assert on_disk['skill_always_protected'] == 'a,b'
    # A fresh pool loads the string form; the consumer re-parses the same semantic set.
    pool2 = _make_pool(tmp_path)
    pool2._load_pool_settings()
    assert parse_skill_always_protected(pool2.llm_cfg['skill_always_protected']) == frozenset({'a', 'b'})


def test_endpoint_card_survives_frozenset_in_llm_cfg(tmp_path):
    """add_endpoint() persists the card even when default_llm_cfg carries a frozenset.

    Direct regression (Todo.md:134): pool/core.py aliases llm_cfg into
    router.default_llm_cfg, so a frozenset there made _save() raise and the
    endpoint card was silently dropped from api_endpoints.json.
    """
    from agent_cascade.api_router_pkg.router import APIRouter
    from agent_cascade.api_router_pkg.endpoints import APIEndpoint
    import threading

    router = APIRouter.__new__(APIRouter)
    router._config_dir = tmp_path
    router._config_path = tmp_path / 'api_endpoints.json'
    router._tmp_path = lambda: tmp_path / 'api_endpoints.json.tmp'
    router._backup_path = lambda: tmp_path / 'api_endpoints.json.bak'
    router._lock = threading.Lock()
    router.endpoints = {}
    router.agent_priorities = {}
    router._agent_types_with_priorities = set()
    router._dirty_endpoints = False
    router._dirty_llm_cfg = False
    router.default_llm_cfg = {'skill_always_protected': frozenset({'x'})}

    ep_id = router.add_endpoint(APIEndpoint(name='test', api_base='http://x', model='m'))

    on_disk = json.loads((tmp_path / 'api_endpoints.json').read_text())
    assert on_disk['endpoints'][0]['id'] == ep_id
    assert on_disk['default_llm_cfg']['skill_always_protected'] == ['x']
