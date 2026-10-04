"""Unit tests for the user-message JSONL log (t154).

Covers instance isolation, legacy fallback, exactly-once logging through the
``_send_to_user`` hook, the WS-unavailable branch, agent-to-agent exclusion,
the settings gate, never-raises behavior, format validity, truncation and
concurrency — all without a live server or LLM.

NOTE on the environment: ``tests/conftest.py`` sets AGENT_CASCADE_INSTANCE_ID at
import time (line ~40), so by default every test runs with a *suffixed* instance
dir. Tests that exercise the legacy plain-``logs/`` path must explicitly unset it
and restore it in try/finally.
"""

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent_cascade.user_message_log as uml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_KEYS = {'ts', 'seq', 'channel', 'kind', 'instance', 'content', 'delivered', 'truncated'}


def _make_pool(base_dir: Path, *, instance_id_set: bool = True):
    """Build a minimal real-ish pool whose operation_manager.base_dir is `base_dir`.

    Uses a real PoolSettings so the settings gate behaves exactly as in production.
    """
    from agent_cascade.agent_instance import PoolSettings

    op_mgr = MagicMock()
    op_mgr.base_dir = Path(base_dir)  # real Path → make_instance_dir routes correctly
    pool = MagicMock()
    pool.operation_manager = op_mgr
    pool.settings = PoolSettings()
    return pool


def _find_log_files(base_dir: Path, instance_id_set: bool):
    """Return the user_messages_*.jsonl files under the resolved (instance-aware) dir."""
    from agent_cascade.instance_id import make_instance_dir
    base = Path(base_dir) / 'logs'
    resolved = Path(make_instance_dir(str(base))) if instance_id_set else base
    if not resolved.exists():
        return []
    return sorted(resolved.glob('user_messages_*.jsonl'))


def _read_records(files):
    """Parse all JSON lines across the given files into a list of dicts."""
    records = []
    for f in files:
        with open(f, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


@pytest.fixture(autouse=True)
def _reset_uml_state():
    """Reset module-level lazy state so each test starts fresh (new file stamp + seq)."""
    uml._reset_for_tests()
    yield
    uml._reset_for_tests()


# ---------------------------------------------------------------------------
# 1. Instance isolation (default path: conftest sets the env var)
# ---------------------------------------------------------------------------

def test_instance_isolation_writes_to_suffixed_dir(tmp_path):
    """With AGENT_CASCADE_INSTANCE_ID set, a write lands in logs_<id>/, not plain logs/."""
    original = os.environ.get('AGENT_CASCADE_INSTANCE_ID')
    try:
        # Ensure a known, non-empty instance id (conftest already sets one; force a value).
        os.environ['AGENT_CASCADE_INSTANCE_ID'] = 'prod'

        pool = _make_pool(tmp_path)
        uml.log_user_message(pool, content='hello', source_instance='worker1')

        files = _find_log_files(tmp_path, instance_id_set=True)
        assert len(files) == 1, f'expected exactly one log file in suffixed dir, got {files}'
        # The resolved dir must be the suffixed one.
        assert files[0].parent.name == 'logs_prod', f"unexpected dir: {files[0].parent}"

        # Plain logs/ must NOT contain a user_messages file (isolation proof).
        plain = list((tmp_path / 'logs').glob('user_messages_*.jsonl')) if (tmp_path / 'logs').exists() else []
        assert plain == [], f'leaked into plain logs/: {plain}'
    finally:
        if original is None:
            os.environ.pop('AGENT_CASCADE_INSTANCE_ID', None)
        else:
            os.environ['AGENT_CASCADE_INSTANCE_ID'] = original


# ---------------------------------------------------------------------------
# 2. Legacy — env var unset → plain logs/
# ---------------------------------------------------------------------------

def test_legacy_plain_logs_dir_when_env_unset(tmp_path):
    """With AGENT_CASCADE_INSTANCE_ID unset, a write lands in plain logs/."""
    original = os.environ.get('AGENT_CASCADE_INSTANCE_ID')
    try:
        os.environ.pop('AGENT_CASCADE_INSTANCE_ID', None)

        pool = _make_pool(tmp_path)
        uml.log_user_message(pool, content='legacy hello', source_instance='worker1')

        files = _find_log_files(tmp_path, instance_id_set=False)
        assert len(files) == 1, f'expected one file in plain logs/, got {files}'
        assert files[0].parent.name == 'logs', f"unexpected dir: {files[0].parent}"
    finally:
        if original is None:
            os.environ.pop('AGENT_CASCADE_INSTANCE_ID', None)
        else:
            os.environ['AGENT_CASCADE_INSTANCE_ID'] = original


# ---------------------------------------------------------------------------
# 3. Exactly-once — one _send_to_user call → exactly 1 entry with correct fields
# ---------------------------------------------------------------------------

def test_exactly_once_on_success_path(tmp_path):
    """One successful _send_to_user call → exactly 1 entry with the expected fields."""
    from agent_cascade.tools.custom.send_message import SendMessage

    pool = _make_pool(tmp_path)
    ws_queue = asyncio.Queue(maxsize=10)
    ws_loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(ws_loop)
        ws_loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    pool._ws_send_queue = ws_queue
    pool._ws_loop = ws_loop

    tool = SendMessage(agent_pool=pool)
    try:
        with patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='worker1'):
            result = tool._send_to_user('Build finished successfully.')
        assert 'successfully' in result.lower()
    finally:
        ws_loop.call_soon_threadsafe(ws_loop.stop)
        loop_thread.join(timeout=2.0)

    records = _read_records(_find_log_files(tmp_path, instance_id_set=True))
    assert len(records) == 1, f'expected exactly 1 entry, got {len(records)}: {records}'
    rec = records[0]
    assert rec['instance'] == 'worker1'
    assert rec['content'] == 'Build finished successfully.'
    assert rec['delivered'] is True
    assert rec['kind'] == 'agent_message'
    assert rec['channel'] == 'ui'
    assert rec['seq'] == 1


# ---------------------------------------------------------------------------
# 4. WS-unavailable branch → 1 entry with delivered=false
# ---------------------------------------------------------------------------

def test_ws_unavailable_logs_delivered_false(tmp_path):
    """No WS queue/loop on the pool → still exactly 1 entry, delivered=False."""
    from agent_cascade.tools.custom.send_message import SendMessage

    pool = _make_pool(tmp_path)
    # No _ws_send_queue / _ws_loop attributes → getattr returns None → early-return branch.
    tool = SendMessage(agent_pool=pool)
    with patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='worker1'):
        result = tool._send_to_user('Task complete!')

    assert 'Warning' in result or 'not delivered' in result.lower() or 'WebSocket unavailable' in result

    records = _read_records(_find_log_files(tmp_path, instance_id_set=True))
    assert len(records) == 1, f'expected exactly 1 entry, got {len(records)}: {records}'
    assert records[0]['delivered'] is False
    assert records[0]['content'] == 'Task complete!'


# ---------------------------------------------------------------------------
# 5. Agent→agent excluded — _send_to_agent produces 0 entries
# ---------------------------------------------------------------------------

def test_agent_to_agent_produces_zero_entries(tmp_path):
    """_send_to_agent must NOT write to the user-message log."""
    from agent_cascade.tools.custom.send_message import SendMessage
    from agent_cascade.agent_instance import AgentState

    pool = _make_pool(tmp_path)
    # Provide a target instance so the send succeeds (exercises the full path).
    target = MagicMock()
    target.state = AgentState.RUNNING
    pool._pool_lock = threading.RLock()
    pool.instances = {'agentB': target}
    enqueued = []
    pool.enqueue_message = lambda name, msg: enqueued.append((name, msg))

    tool = SendMessage(agent_pool=pool)
    with patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='agentA'):
        result = tool._send_to_agent('agentB', 'Hey agentB')

    assert 'sent successfully' in result.lower()
    assert len(enqueued) == 1

    # No user-message log file should have been created at all.
    files = _find_log_files(tmp_path, instance_id_set=True)
    assert files == [], f'agent-to-agent must not write the user log, but found {files}'


# ---------------------------------------------------------------------------
# 6. Flag off → 0 entries, no exception
# ---------------------------------------------------------------------------

def test_flag_off_produces_zero_entries(tmp_path):
    """user_message_log_enabled=False → no entries and no exception."""
    pool = _make_pool(tmp_path)
    pool.settings.user_message_log_enabled = False

    uml.log_user_message(pool, content='should not be logged', source_instance='worker1')

    files = _find_log_files(tmp_path, instance_id_set=True)
    assert files == [], f'flag off must write nothing, but found {files}'


# ---------------------------------------------------------------------------
# 7. Never raises — writer failure is swallowed; _send_to_user still returns normally
# ---------------------------------------------------------------------------

def test_never_raises_on_write_failure(tmp_path):
    """If the file write raises OSError, log_user_message swallows it and _send_to_user succeeds."""
    from agent_cascade.tools.custom.send_message import SendMessage

    pool = _make_pool(tmp_path)
    ws_queue = asyncio.Queue(maxsize=10)
    ws_loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(ws_loop)
        ws_loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    pool._ws_send_queue = ws_queue
    pool._ws_loop = ws_loop

    tool = SendMessage(agent_pool=pool)
    try:
        with patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='worker1'), \
             patch('builtins.open', side_effect=OSError('disk full')):
            result = tool._send_to_user('must not crash')

        # The tool must still report success (the WS push happened before the log write).
        assert 'successfully' in result.lower()
    finally:
        ws_loop.call_soon_threadsafe(ws_loop.stop)
        loop_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 8. Format validity — every line parses as JSON with required keys
# ---------------------------------------------------------------------------

def test_format_validity(tmp_path):
    """Every written line parses as JSON and carries all required keys."""
    pool = _make_pool(tmp_path)
    for i in range(3):
        uml.log_user_message(pool, content=f'msg {i}', source_instance='worker1')

    records = _read_records(_find_log_files(tmp_path, instance_id_set=True))
    assert len(records) == 3
    for rec in records:
        assert REQUIRED_KEYS.issubset(rec.keys()), f'missing keys: {REQUIRED_KEYS - set(rec.keys())}'
    # seq must be monotonically increasing from 1.
    assert [r['seq'] for r in records] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 9. Truncation — >64 KB content truncated with truncated:true
# ---------------------------------------------------------------------------

def test_truncation_over_64kb(tmp_path):
    """Content over 64 KB is truncated and flagged truncated=True."""
    pool = _make_pool(tmp_path)
    big = 'a' * (70 * 1024)  # 70 KB > 64 KB
    uml.log_user_message(pool, content=big, source_instance='worker1')

    records = _read_records(_find_log_files(tmp_path, instance_id_set=True))
    assert len(records) == 1
    rec = records[0]
    assert rec['truncated'] is True
    # Stored content must be <= 64 KB (byte length).
    assert len(rec['content'].encode('utf-8')) <= 64 * 1024
    assert rec['content'] == 'a' * (64 * 1024)


def test_no_truncation_under_64kb(tmp_path):
    """Content under the cap is stored verbatim with truncated=False."""
    pool = _make_pool(tmp_path)
    small = 'x' * 100
    uml.log_user_message(pool, content=small, source_instance='worker1')

    records = _read_records(_find_log_files(tmp_path, instance_id_set=True))
    assert records[0]['truncated'] is False
    assert records[0]['content'] == small


# ---------------------------------------------------------------------------
# 10. Concurrency — 8 threads × 50 calls → 400 well-formed lines
# ---------------------------------------------------------------------------

def test_concurrency_8x50(tmp_path):
    """8 threads × 50 concurrent writes → exactly 400 well-formed, non-corrupted lines."""
    pool = _make_pool(tmp_path)
    n_threads, per_thread = 8, 50

    def worker(tidx):
        for i in range(per_thread):
            uml.log_user_message(pool, content=f't{tidx}-m{i}', source_instance='worker1')

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    records = _read_records(_find_log_files(tmp_path, instance_id_set=True))
    assert len(records) == n_threads * per_thread, f'expected 400 lines, got {len(records)}'
    for rec in records:
        assert REQUIRED_KEYS.issubset(rec.keys())
    # seq must be a permutation of 1..400 (each increment happened under the lock).
    seqs = sorted(r['seq'] for r in records)
    assert seqs == list(range(1, n_threads * per_thread + 1))


# ---------------------------------------------------------------------------
# Robustness — agent_pool is None or lacks operation_manager (fallback to DEFAULT_WORKSPACE)
# ---------------------------------------------------------------------------

def test_none_pool_does_not_raise():
    """log_user_message(None, ...) must not raise (falls back to DEFAULT_WORKSPACE)."""
    # Never-raises contract: even with a degenerate pool it must swallow any error.
    uml.log_user_message(None, content='degenerate pool', source_instance='worker1')  # no exception


def test_pool_without_operation_manager_does_not_raise():
    """A pool lacking operation_manager falls back to DEFAULT_WORKSPACE without raising."""
    pool = MagicMock(spec=[])  # no operation_manager attribute at all
    uml.log_user_message(pool, content='no opmgr', source_instance='worker1')  # no exception


# ---------------------------------------------------------------------------
# Settings plumbing — flag is a real PoolSettings field with default ON, and the
# config handler toggles it (mirrors test_telegram_bridge_supervisor.py patterns).
# ---------------------------------------------------------------------------

def test_settings_field_default_on_and_persist_roundtrip():
    """user_message_log_enabled defaults to True and survives to_dict/from_dict."""
    from agent_cascade.agent_instance import PoolSettings

    assert PoolSettings().user_message_log_enabled is True
    d = PoolSettings(user_message_log_enabled=False).to_dict()
    assert d['user_message_log_enabled'] is False
    restored = PoolSettings.from_dict({'user_message_log_enabled': True})
    assert restored.user_message_log_enabled is True
    # Missing key → default.
    assert PoolSettings.from_dict({}).user_message_log_enabled is True


def test_config_handler_registered_and_toggles():
    """The config handler is registered and toggles the settings field."""
    from agent_cascade.config_handlers import CONFIG_HANDLERS, POOL_SETTINGS_KEYS
    from agent_cascade.agent_instance import PoolSettings

    assert 'user_message_log_enabled' in POOL_SETTINGS_KEYS
    assert 'user_message_log_enabled' in CONFIG_HANDLERS

    handler = CONFIG_HANDLERS['user_message_log_enabled']
    pool = MagicMock()
    pool.settings = PoolSettings(user_message_log_enabled=True)
    handler({'user_message_log_enabled': False}, pool, [])
    assert pool.settings.user_message_log_enabled is False

    # None pool must not raise.
    handler({'user_message_log_enabled': True}, None, [])  # no exception


def test_config_handler_default_true_when_key_missing():
    """When the UI payload omits the key, the handler keeps it ON (default)."""
    from agent_cascade.config_handlers import CONFIG_HANDLERS
    from agent_cascade.agent_instance import PoolSettings

    pool = MagicMock()
    pool.settings = PoolSettings()
    CONFIG_HANDLERS['user_message_log_enabled']({}, pool, [])
    assert pool.settings.user_message_log_enabled is True
