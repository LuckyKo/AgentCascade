"""BUG_0336 — WS message-envelope schema (MsgType + WsEnvelope) guarantees.

These tests are the byte-for-byte safety net for the DRY refactor:
(a) every ``MsgType`` member equals its original wire string,
(b) ``json.dumps`` of a type field is byte-identical (str-mixin invariant),
(c) a plain-string ``type`` still resolves to the correct handler,
(d) every ``WsEnvelope`` field-name ClassVar equals its original key string.
"""
import asyncio
import json
import threading

import pytest

from agent_cascade.ws_handlers import MsgType, WsEnvelope, WsMessageHandler


# Original literals, verbatim — the schema must match these byte-for-byte.
EXPECTED_MSG_TYPES = {
    # Incoming dispatch keys.
    'MESSAGE': 'message', 'CONTINUE': 'continue', 'STOP': 'stop', 'KV_STATE': 'kv_state',
    'PAUSE': 'pause', 'RESUME_ALL': 'resume_all', 'RESUME': 'resume',
    'TERMINATE_AGENT_INSTANCE': 'terminate_agent_instance',
    'TERMINATE_SUB_AGENT': 'terminate_sub_agent', 'TERMINATE_TOOLS': 'terminate_tools',
    'RETRY': 'retry', 'RESET': 'reset',
    'REFRESH_SOULS': 'refresh_souls', 'REFRESH_AGENTS': 'refresh_agents',
    'RESTART_SERVER': 'restart_server', 'UPDATE_CONFIG': 'update_config',
    'UPDATE_SETTINGS': 'update_settings', 'SET_WORK_FOLDERS': 'set_work_folders',
    'UPDATE_ENDPOINTS': 'update_endpoints', 'UPDATE_API_PRIORITIES': 'update_api_priorities',
    'APPROVE': 'approve', 'REJECT': 'reject', 'ASK_SECURITY': 'ask_security',
    'SET_AUTO_SECURITY': 'set_auto_security', 'SET_CLI_AGENT_KEY': 'set_cli_agent_key',
    'CLI_AGENT_OAUTH_MANUAL_CMD': 'cli_agent_oauth_manual_cmd',
    'CLI_AGENT_OAUTH_RECHECK': 'cli_agent_oauth_recheck', 'SET_AFK': 'set_afk',
    'EDIT_MESSAGE': 'edit_message', 'DELETE_MESSAGES': 'delete_messages',
    'SELECT_AGENT': 'select_agent', 'SET_SESSION_NAME': 'set_session_name',
    'LOAD_SESSION': 'load_session', 'INJECT': 'inject', 'DISMISS_QUEUE': 'dismiss_queue',
    'EXPORT_SETTINGS': 'export_settings', 'IMPORT_SETTINGS': 'import_settings',
    'MIGRATE_SETTINGS_LOCAL': 'migrate_settings_local', 'REQUEST_STATE': 'request_state',
    # Outgoing-only response types (+ kv_state_result from handle_kv_state).
    'STATE': 'state', 'STREAM_UPDATE': 'stream_update', 'ERROR': 'error',
    'REFRESH_AGENTS_RESULT': 'refresh_agents_result', 'KV_STATE_RESULT': 'kv_state_result',
    'SETTINGS_APPLIED': 'settings_applied', 'SETTINGS_CHANGED': 'settings_changed',
    'INFO': 'info', 'WARNING': 'warning', 'CLI_AGENT_OAUTH_FAILED': 'cli_agent_oauth_failed',
    'CLI_AGENT_OAUTH_STATUS': 'cli_agent_oauth_status',
    # S4/S5 fleet stack-swap envelope (BUG_0323).
    'STACKS_LIST': 'stacks_list', 'STACKS_RESULT': 'stacks_result',
    'STACKS_SNAPSHOT': 'stacks_snapshot', 'STACKS_SWAP': 'stacks_swap',
    'STACKS_NEW': 'stacks_new', 'STACKS_DELETE': 'stacks_delete',
}

EXPECTED_ENVELOPE_FIELDS = {
    'TYPE': 'type', 'SESSION_NAME': 'session_name', 'MESSAGE': 'message', 'ERROR': 'error',
    'INSTANCE_NAME': 'instance_name', 'STATUS': 'status', 'GENERATE_CFG': 'generate_cfg',
    'API_ROUTER': 'api_router', 'INSTANCE': 'instance', 'HARNESS': 'harness',
    'COMMAND': 'command', 'AUTHENTICATED': 'authenticated', 'REASON': 'reason',
    'ACTION': 'action', 'RETRYABLE': 'retryable', 'REVISION': 'revision',
    'BASE_REVISION': 'base_revision', 'SETTINGS': 'settings', 'MESSAGE_INDEX': 'message_index',
}


def test_msgtype_is_str_mixin():
    """str-mixin is the invariant that keeps both wire format and dispatch lookup intact."""
    assert issubclass(MsgType, str)
    assert issubclass(MsgType, __import__('enum', fromlist=['Enum']).Enum)


def test_every_msgtype_value_matches_original_literal():
    # No missing/extra members: exactly the expected name→value pairs.
    actual = {m.name: m.value for m in MsgType}
    assert actual == EXPECTED_MSG_TYPES


@pytest.mark.parametrize('value', sorted({v for v in EXPECTED_MSG_TYPES.values()}))
def test_type_field_serializes_byte_identically(value):
    """The wire frame must be byte-identical to a plain string value."""
    member = MsgType(value)
    assert json.dumps({'type': member}) == json.dumps({'type': value})


def test_field_names_match_original_literals():
    actual = {k: getattr(WsEnvelope, k) for k in EXPECTED_ENVELOPE_FIELDS}
    assert actual == EXPECTED_ENVELOPE_FIELDS


def test_plain_string_dispatch_resolves_to_handler():
    """A plain-string 'type' still resolves against the MsgType-keyed table (str hash/eq)."""
    session = {
        'session_name': 'Maine', 'agent_index': 0, 'agents': [], 'generating': False,
        'generation_id': 0, 'last_turn_snapshots': [],
    }

    async def _noop(*a, **k):
        return None

    handler = WsMessageHandler(
        session=session, agent_pool=None, agents=[], send_queue=None,
        broadcast_fn=_noop, build_state_fn=lambda *a, **k: {},
        start_gen_fn=lambda *a, **k: None, session_lock=threading.Lock(), app=None,
    )

    called = {}

    async def _rec(data):
        called['data'] = data

    handler.handle_pause = _rec
    # dispatch() reads data.get('type') → a plain str; the str-mixin makes the lookup hit.
    asyncio.run(handler.dispatch({'type': 'pause', 'foo': 'bar'}))
    assert called == {'data': {'type': 'pause', 'foo': 'bar'}}

    # The MsgType-keyed table also resolves plain strings (the alias entry too).
    table = handler._dispatch_table
    assert table['pause'] is _rec
    assert table[MsgType.PAUSE] is _rec
    assert table.get('terminate_sub_agent').__func__ is WsMessageHandler.handle_terminate
    assert table.get('terminate_agent_instance').__func__ is WsMessageHandler.handle_terminate
    assert table.get('message').__func__ is WsMessageHandler.handle_message
