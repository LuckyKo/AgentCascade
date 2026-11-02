"""Tests for the one-time localStorage -> server migration (Phase 5).

Covers:
- Only missing keys are imported (server value wins on conflict)
- Server-wins keys (disabled_tools, work folders, auto_security, session_name) are never imported
- No missing keys -> the apply pipeline is not invoked at all
- The server-side sentinel (_settings_migrated_v1 in session_state.json) short-circuits
  a second migrate_settings_local request with skipped='already_migrated'
- LLM config seeded from blob only when api_endpoints.json lacks default_llm_cfg
- LLM config NOT seeded when the file already has default_llm_cfg

Coverage boundary: the migration -> persistence round-trip (does an applied key actually
land on disk, and is the router bucket written atomically) is owned by
tests/test_settings_persistence_roundtrip.py — router bucket only. This file tests the
migration handler's key-selection and short-circuit semantics only.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
def _make_pool(pool_path: Path | None = None, endpoints_path: Path | None = None):
    """Build a minimal pool mock with the attributes the handler reads.

    The handler reads *behaviour*, not paths: `persisted_pool_settings()` and
    `api_router.has_default_llm_cfg()`. A bare MagicMock answers both with a truthy
    child mock, which silently defeats the "already persisted -> skip" gating, so
    they are bound to the real temp files.
    """
    pool = MagicMock()
    pool._pool_settings_path = pool_path
    pool.persisted_pool_settings = lambda: (
        json.loads(pool_path.read_text(encoding='utf-8'))
        if pool_path and pool_path.exists() else {}
    )
    router = MagicMock()
    router._config_path = endpoints_path
    router.has_default_llm_cfg = lambda: bool(
        endpoints_path and endpoints_path.exists()
        and json.loads(endpoints_path.read_text(encoding='utf-8')).get('default_llm_cfg')
    )
    pool.api_router = router
    return pool


def _make_handler(pool):
    """Build a handler-like object with the attributes the handler method needs."""
    handler = MagicMock()
    handler.agent_pool = pool
    handler.agents = []
    handler.session = MagicMock()
    handler.broadcast_fn = AsyncMock()
    return handler


def _write_json(path: Path, data: dict):
    path.write_text(json.dumps(data), encoding='utf-8')


@pytest.fixture
def tmp_paths(tmp_path):
    """Provide temp paths for pool_settings.json and api_endpoints.json."""
    pool_path = tmp_path / 'pool_settings.json'
    endpoints_path = tmp_path / 'api_endpoints.json'
    return pool_path, endpoints_path


class TestMigrationCore:
    """Core migration semantics: missing-keys-only, server-wins, idempotency."""

    @pytest.mark.asyncio
    async def test_only_missing_keys_imported(self, tmp_paths):
        from agent_cascade.ws_handlers import WsMessageHandler

        pool_path, endpoints_path = tmp_paths
        # Server already has max_workers persisted.
        _write_json(pool_path, {'max_workers': 4})
        _write_json(endpoints_path, {'default_llm_cfg': {'model': 'x'}})

        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        handler = _make_handler(pool)

        # Blob has max_workers (server has it) + soft_turn_limit (server doesn't).
        blob = {'max_workers': 99, 'soft_turn_limit': True}

        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = (['soft_turn_limit'], [], {}, 1)
            with patch('agent_cascade.session_state_store.mark_settings_migrated'):
                await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        # Only soft_turn_limit should reach apply_settings_changes (max_workers already persisted).
        applied = mock_apply.call_args[0][2]
        assert 'soft_turn_limit' in applied
        assert 'max_workers' not in applied

    @pytest.mark.asyncio
    async def test_server_wins_keys_never_imported(self, tmp_paths):
        from agent_cascade.ws_handlers import WsMessageHandler

        pool_path, endpoints_path = tmp_paths
        # Fresh server: no pool_settings.json, no endpoints file.
        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        handler = _make_handler(pool)

        # All server-wins keys in the blob + one non-server-wins key for contrast.
        blob = {
            'disabled_tools': {'orchestrator': ['shell']},
            'work_access_folders_ro': ['/data'],
            'work_access_folders_rw': ['/work'],
            'auto_security': True,
            'session_name': 'my-session',
            'soft_turn_limit': True,
        }

        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = (['soft_turn_limit'], [], {}, 1)
            with patch('agent_cascade.session_state_store.mark_settings_migrated'):
                await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        applied = mock_apply.call_args[0][2]
        # Server-wins keys must NOT be in the applied set.
        for key in ('disabled_tools', 'work_access_folders_ro', 'work_access_folders_rw',
                    'auto_security', 'session_name'):
            assert key not in applied, f'{key} must be excluded (server wins unconditionally)'
        # The non-server-wins key should be present.
        assert 'soft_turn_limit' in applied

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        'persisted, blob',
        [
            # Key already on disk with the same value as the blob.
            ({'soft_turn_limit': True}, {'soft_turn_limit': True}),
            # Every blob key already on disk, values differing (presence, not value, gates).
            ({'soft_turn_limit': True, 'max_workers': 4},
             {'soft_turn_limit': False, 'max_workers': 2}),
        ],
        ids=['same_value_already_persisted', 'all_blob_keys_persisted'],
    )
    async def test_no_missing_keys_short_circuits_apply(self, tmp_paths, persisted, blob):
        from agent_cascade.ws_handlers import WsMessageHandler

        pool_path, endpoints_path = tmp_paths
        _write_json(pool_path, persisted)
        _write_json(endpoints_path, {'default_llm_cfg': {'model': 'x'}})

        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        handler = _make_handler(pool)

        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = ([], [], {}, 1)
            with patch('agent_cascade.session_state_store.mark_settings_migrated'):
                await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        # Handler short-circuits: no missing keys -> apply_settings_changes not called.
        mock_apply.assert_not_called()
        result = handler.broadcast_fn.call_args[0][0]
        assert result['status'] == 'ok'
        assert result['applied'] == []
        assert 'skipped' not in result  # the sentinel short-circuit did NOT fire here

    @pytest.mark.asyncio
    async def test_second_request_short_circuits_on_server_sentinel(self, tmp_paths, tmp_path, monkeypatch):
        """A second migrate_settings_local is skipped by the server-side sentinel.

        Unlike the other tests, mark_settings_migrated is NOT patched out: the
        sentinel is really written, so is_settings_migrated() short-circuits the
        second request. AGENT_CASCADE_TEST_CONFIG_DIR is pointed at this test's
        temp dir so the sentinel never touches the real config/session_state.json.
        """
        from agent_cascade.ws_handlers import WsMessageHandler
        from agent_cascade.session_state_store import is_settings_migrated

        pool_path, endpoints_path = tmp_paths
        _write_json(pool_path, {})
        _write_json(endpoints_path, {'default_llm_cfg': {'model': 'x'}})
        monkeypatch.setenv('AGENT_CASCADE_TEST_CONFIG_DIR', str(tmp_path))

        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        blob = {'soft_turn_limit': True}

        # Pass 1: the sentinel is absent, so the missing keys are applied.
        handler = _make_handler(pool)
        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = (['soft_turn_limit'], [], {}, 1)
            await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        first = handler.broadcast_fn.call_args[0][0]
        assert first['status'] == 'ok'
        assert first['applied'] == ['soft_turn_limit']
        assert 'skipped' not in first
        assert is_settings_migrated(), 'sentinel was not recorded in the isolated session_state.json'

        # Pass 2: a fresh client (its own handler/broadcast) replaying the blob is
        # short-circuited before any key is applied.
        second = _make_handler(pool)
        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            await WsMessageHandler.handle_migrate_settings_local(second, {'settings': blob})

        mock_apply.assert_not_called()
        result = second.broadcast_fn.call_args[0][0]
        assert result['status'] == 'ok'
        assert result['applied'] == []
        assert result['skipped'] == 'already_migrated'


class TestLLMConfigSeed:
    """LLM config is seeded from the blob only when api_endpoints.json lacks default_llm_cfg."""

    @pytest.mark.asyncio
    async def test_llm_seeded_when_file_lacks_default_llm_cfg(self, tmp_paths):
        from agent_cascade.ws_handlers import WsMessageHandler

        pool_path, endpoints_path = tmp_paths
        _write_json(pool_path, {})
        # Endpoints file exists but has NO default_llm_cfg key.
        _write_json(endpoints_path, {'endpoints': []})

        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        handler = _make_handler(pool)

        # Use actual LLM_CFG_KEYS names from the registry.
        blob = {'model': 'qwen2.5', 'temperature': 0.7}

        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = (['model', 'temperature'], [], {}, 1)
            with patch('agent_cascade.session_state_store.mark_settings_migrated'):
                await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        applied = mock_apply.call_args[0][2]
        assert 'model' in applied
        assert 'temperature' in applied

    @pytest.mark.asyncio
    async def test_llm_not_seeded_when_file_has_default_llm_cfg(self, tmp_paths):
        from agent_cascade.ws_handlers import WsMessageHandler

        pool_path, endpoints_path = tmp_paths
        _write_json(pool_path, {})
        # Endpoints file HAS default_llm_cfg.
        _write_json(endpoints_path, {'default_llm_cfg': {'model': 'gpt-4', 'temperature': 0.5}})

        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        handler = _make_handler(pool)

        blob = {'model': 'qwen2.5', 'temperature': 0.7}

        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = ([], [], {}, 1)
            with patch('agent_cascade.session_state_store.mark_settings_migrated'):
                await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        # Handler short-circuits (no missing keys) -> apply_settings_changes not called.
        mock_apply.assert_not_called()
        result = handler.broadcast_fn.call_args[0][0]
        assert result['status'] == 'ok'
        assert result['applied'] == []

    @pytest.mark.asyncio
    async def test_llm_seeded_when_no_endpoints_file(self, tmp_paths):
        from agent_cascade.ws_handlers import WsMessageHandler

        pool_path, endpoints_path = tmp_paths
        _write_json(pool_path, {})
        # No endpoints file at all (path doesn't exist).

        pool = _make_pool(pool_path=pool_path, endpoints_path=endpoints_path)
        handler = _make_handler(pool)

        blob = {'model': 'qwen2.5'}

        with patch('agent_cascade.settings_delta.apply_settings_changes', new_callable=AsyncMock) as mock_apply:
            mock_apply.return_value = (['model'], [], {}, 1)
            with patch('agent_cascade.session_state_store.mark_settings_migrated'):
                await WsMessageHandler.handle_migrate_settings_local(handler, {'settings': blob})

        applied = mock_apply.call_args[0][2]
        assert 'model' in applied
