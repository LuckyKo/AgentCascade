"""Duplicate-delivery tests for the Telegram bridge (plans/tg-dup-delivery_PLAN.md v2).

Revert-proof: every test in this file FAILS on the pre-fix code and passes after:

  F1 — inbound idempotency in ``telegram_bridge/bot.py``: a redelivered update
       (same ``update_id``, or same ``effective_message.message_id`` when no
       ``update_id``) must not be injected into AC a second time.
  F2 — outbound observability in the same file: ``_safe_send`` logs a content-free
       ``[TG-PUSH] delivered N chunk(s), M chars`` INFO line on success.
  F3 — P1 pre-reflection push gate in ``engine/core.py``: the hook must READ
       ``instance._tg_pushed`` before pushing (previously write-only).

Bridge tests reuse the MagicMock update idiom from ``tests/test_telegram_bridge.py``;
the engine test reuses the real-ExecutionEngine harness from ``tests/test_tg_push_model.py``.

Run serially (pytest.ini pins xdist in addopts):
    python -m pytest tests/test_tg_dup_delivery.py -v -o addopts="" --timeout=120

NOT safe for xdist parallel runs: the production code keeps a module-level
``_SEEN_TG_IDS`` set shared across all tests in this process. Even with the
autouse clear fixture, concurrent workers executing these tests can interleave
add/clear operations and produce flaky ordering (an id added by one test may be
seen as "already processed" by another). Always run this file with xdist off.
"""

import asyncio
import logging
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure top-level imports work (mirror tests/test_api_endpoints.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.telegram_bridge.bot import _safe_send  # noqa: E402
from agent_cascade.telegram_bridge.config import BridgeConfig  # noqa: E402

# Deliberately fake Telegram user ID for tests — never a real identifier.
FAKE_ALLOWED_USER_ID = 1111111111


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def fresh_manager(tmp_path):
    """Fresh SkillManager isolated to a per-test tmp dir (mirrors test_tg_push_model)."""
    from agent_cascade.skills.manager import SkillManager

    manager = SkillManager()
    manager._metrics_file = tmp_path / 'skills-metrics.json'
    base = tmp_path / 'agents' / 'global'
    manager._pending_dir = base / 'pending-skills'
    manager._candidates_dir = base / 'candidates'
    manager._production_skills_dir = base / 'skills'
    yield manager


@pytest.fixture(autouse=True)
def clean_seen_ids():
    """Reset the module-level inbound dedup set so tests never cross-contaminate.

    No-op (with a getattr guard) on the pre-fix code, where the attribute does not
    exist yet — the fixture only takes effect once F1 lands.
    """
    import agent_cascade.telegram_bridge.bot as bot_mod

    seen = getattr(bot_mod, '_SEEN_TG_IDS', None)
    if seen is not None:
        seen.clear()
    yield
    if seen is not None:
        seen.clear()


def _run(coro):
    """Run an async coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_update(user_id, text='hello', chat_id=100, update_id=None, message_id=None):
    """MagicMock update in the style of tests/test_telegram_bridge.py::_make_update,
    extended with the id fields F1 keys on.

    ``update_id``/``message_id`` default to None so a test that doesn't care about
    dedup ids gets key-less (fail-open) updates rather than truthy mock attributes.
    """
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.update_id = update_id
    if message_id is None:
        update.effective_message = None
    else:
        update.effective_message = MagicMock()
        update.effective_message.message_id = message_id
    return update


def _drain_waiters(context):
    """Await the fire-and-forget waiter task(s) so none leak out of the loop."""

    async def go():
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())


def _make_engine(fresh_manager, tmp_path, max_turns=5, min_turns=2, extra_turns=5,
                 natural_end_at=3, supervisor=None):
    """Real-ExecutionEngine harness — verbatim reuse of the pattern from
    tests/test_tg_push_model.py::_make_engine (same stubs, same AUTO_SKILL patches).

    Returns ``(engine, inst, pool, run)`` where ``run()`` drives engine.run(inst) to
    completion inside its own patch context.
    """
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.llm.schema import Message, ASSISTANT, USER

    template = MagicMock()
    template.function_map = {'tool_a': None, 'tool_b': None}

    pool = MagicMock()
    pool.settings.auto_skill_enabled = True
    pool.settings.auto_skill_min_turns = min_turns
    pool.settings.default_load_skill_mode = 'AUTO'
    pool.settings.tail_sync_check_enabled = False
    pool.get_template.return_value = template
    pool.is_instance_terminated.return_value = False
    pool.has_pending.return_value = False
    pool.has_messages.return_value = False
    pool.drain_queue.return_value = []
    # A bare MagicMock auto-attribute would be a truthy mock and the idiom would call
    # notify_user on it. Set explicitly to control the push.
    pool.telegram_supervisor = supervisor

    from agent_cascade.logger import AgentInstanceLogger
    log_inst = AgentInstanceLogger('test_agent', 'w', str(tmp_path), log_path=str(tmp_path / 'w.jsonl'))
    pool.get_logger.return_value = log_inst

    # Populate the registry via REAL discovery so load_full_instructions() finds 'skill-creator'.
    fresh_manager._cache_ttl = 0.0
    fresh_manager.discover([Path('agents/global/skills')])
    pool.skill_manager = fresh_manager

    engine = ExecutionEngine(pool)
    inst = _make_inst(max_turns)

    def fake_setup_turn(instance):
        return list(instance.conversation), [Message(role=USER, content='task')], []

    engine._setup_turn = MagicMock(side_effect=fake_setup_turn)
    engine._pre_llm_checks = MagicMock(return_value=False)
    engine._check_stop_conditions = MagicMock(return_value=False)
    engine._is_suspended_by_compression = MagicMock(return_value=False)
    engine._is_terminal_stop = MagicMock(return_value=False)
    engine._acquire_slot_with_logging = MagicMock(return_value=None)
    engine._check_stream_termination = MagicMock(return_value=None)

    _llm_call_count = {'n': 0}

    def fake_llm(inst, msgs):
        _llm_call_count['n'] += 1
        n = _llm_call_count['n']
        yield None
        yield Message(role=ASSISTANT, content=f"reply {n}")

    engine._call_llm_with_injection = MagicMock(side_effect=lambda inst, msgs: fake_llm(inst, msgs))
    engine._execute_detected_tools = MagicMock(return_value=False)

    _ptc_calls = {'n': 0}

    def _post_turn_checks_driver(*a, **k):
        _ptc_calls['n'] += 1
        # False only on the natural-end check; True everywhere else so a triggered reflection
        # tail runs its full EXTRA budget before exhausting.
        return _ptc_calls['n'] != natural_end_at

    engine._post_turn_checks = MagicMock(side_effect=_post_turn_checks_driver)

    def run():
        with patch('agent_cascade.engine.core.AUTO_SKILL_MIN_TURNS', min_turns), \
                patch('agent_cascade.engine.core.AUTO_SKILL_EXTRA_TURNS', extra_turns), \
                patch('agent_cascade.skills.manager.AUTO_SKILL_MIN_TURNS', min_turns):
            gen = engine.run(inst)
            for _ in gen:
                pass
            return gen

    return engine, inst, pool, run


def _make_inst(max_turns):
    """Minimal AgentInstance that drives the REAL ExecutionEngine.run().

    ``AgentInstance.__new__`` bypasses the dataclass defaults, so every attribute run()'s
    hot path reads must be set explicitly (same shape as test_tg_push_model._make_inst).
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState
    from agent_cascade.llm.schema import Message, USER

    inst = AgentInstance.__new__(AgentInstance)
    inst.instance_name = 'w'
    inst.agent_class = 'test_agent'
    inst.parent_instance = None          # None == root (the pre-hook gate)
    inst.conversation = [Message(role=USER, content='task')]
    inst._cached_messages = list(inst.conversation)
    inst._cached_llm_messages = list(inst.conversation)
    inst.max_turns = max_turns
    inst.state = AgentState.IDLE
    inst._compression_lock = threading.RLock()
    inst._state_lock = threading.RLock()
    inst._generate_cfg_override = None
    inst._turn_consumed = False
    inst._slot_release = None
    inst._slot_key = None
    inst._compression_suspended_at = 0.0
    inst._last_config_version = -1
    inst._last_token_count_conversation_length = -1
    inst._continue_saved_msg = None
    inst._auto_skill_proposed = False
    inst._auto_skill_dirty_stop = False
    inst._tg_pushed = False
    inst._streaming_responses = []
    return inst


# --------------------------------------------------------------------------- #
# F1. Inbound idempotency (primary fix)
# --------------------------------------------------------------------------- #

def test_duplicate_update_id_injected_once():
    """A redelivered update (same update_id) must be injected into AC exactly once.

    The incident signature: three waiters within 1.2 s from one user intent — a
    PTB long-poll reconnect / NAT expiry re-delivering the same update. Pre-fix, each
    delivery injects again and starts a fresh AC run that independently pushes its
    answer (N runs = N copies on the phone).
    """
    import agent_cascade.telegram_bridge.bot as bot_mod

    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}

    ac.inject_message.side_effect = _inject
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))

    async def go():
        update = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it', update_id=9001)
        await bot_mod.on_message(update, context)
        # Same update redelivered (fresh MagicMock, same update_id).
        update2 = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it', update_id=9001)
        await bot_mod.on_message(update2, context)
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    assert ac.inject_message.call_count == 1, \
        f'expected exactly 1 injection for a redelivered update_id, got {ac.inject_message.call_count}'


def test_dedup_falls_back_to_message_id_when_update_id_absent():
    """With no update_id, dedup must key on effective_message.message_id.

    Guards the fallback branch of the F1 guard: an update carrying only a message_id
    (e.g. a channel-post-shaped stub) is still deduplicated when redelivered.
    """
    import agent_cascade.telegram_bridge.bot as bot_mod

    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}

    ac.inject_message.side_effect = _inject
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))

    async def go():
        update = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it',
                              update_id=None, message_id=4242)
        await bot_mod.on_message(update, context)
        update2 = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it',
                               update_id=None, message_id=4242)
        await bot_mod.on_message(update2, context)
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    assert ac.inject_message.call_count == 1, \
        f'expected exactly 1 injection when deduping on message_id, got {ac.inject_message.call_count}'


def test_dedup_drop_logged_at_warning(caplog):
    """BUG_0023: a dedup drop must be auditable — logged at WARNING (not DEBUG only).

    Pre-fix the only log on the dedup-hit path was ``logger.debug``, which is
    disabled in normal operation, so a false-positive drop left zero trace.
    This test FAILS pre-fix (no WARNING record exists) and passes post-fix.
    """
    import agent_cascade.telegram_bridge.bot as bot_mod

    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}

    ac.inject_message.side_effect = _inject
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))

    async def go():
        update = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it', update_id=9101)
        await bot_mod.on_message(update, context)
        update2 = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='do it', update_id=9101)
        await bot_mod.on_message(update2, context)

    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _run(go())

    drops = [r for r in caplog.records if r.levelno >= logging.WARNING and 'duplicate Telegram event' in r.getMessage()]
    assert drops, 'expected a WARNING-level log on the dedup-drop path (DEBUG-only is unauditable)'


def test_dedup_message_id_namespaced_by_chat():
    """BUG_0023: the message_id fallback key must be namespaced by chat_id.

    Telegram message_id is unique PER CHAT, not globally. Pre-fix the bare int was
    stored flat, so two different chats sharing the same numeric message_id would
    collide and the second message would be dropped as a false "duplicate".
    Post-fix both messages are processed (two injections). FAILS pre-fix.
    """
    import agent_cascade.telegram_bridge.bot as bot_mod

    cfg = BridgeConfig(enabled=True, bot_token='t', allowed_users=[FAKE_ALLOWED_USER_ID])
    ac = MagicMock()

    async def _inject(text, target=None):
        return {'status': 'success', 'queued': True, 'target': target or 'Maine'}

    ac.inject_message.side_effect = _inject
    context = MagicMock()
    context.bot_data = {'config': cfg, 'ac_client': ac}
    context.bot.send_message = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))

    async def go():
        # Two DIFFERENT chats, same numeric message_id (42) on the fallback path.
        update_a = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='from A',
                                chat_id=100, update_id=None, message_id=42)
        await bot_mod.on_message(update_a, context)
        update_b = _make_update(user_id=FAKE_ALLOWED_USER_ID, text='from B',
                                chat_id=200, update_id=None, message_id=42)
        await bot_mod.on_message(update_b, context)
        for t in list(context.bot_data.get('waiters', ()) or ()):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    _run(go())
    assert ac.inject_message.call_count == 2, \
        f'expected both chats to be processed (message_id namespaced by chat), got {ac.inject_message.call_count} injection(s)'


# --------------------------------------------------------------------------- #
# F2. Outbound observability
# --------------------------------------------------------------------------- #

def test_safe_send_logs_delivery(caplog):
    """_safe_send logs a content-free [TG-PUSH] delivered INFO line on success.

    This is the missing success-side log (plan §2.3) that made the ×4 incident
    undiagnosable: the line carries chunk count + char length ONLY — never message
    content, so asserting its absence of content is part of the contract.
    """
    text = 'The final answer delivered to the phone.'

    bot = MagicMock()

    async def _noop(**kwargs):
        return None

    bot.send_message.side_effect = lambda **kw: _noop()

    # The bridge logs via the shared 'agent_cascade_logger' (agent_cascade/log.py);
    # caplog captures by propagation to root, so target that logger explicitly.
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        asyncio.run(_safe_send(bot, chat_id=42, text=text))

    delivered = [r for r in caplog.records if '[TG-PUSH] delivered' in r.getMessage()]
    assert delivered, 'expected a "[TG-PUSH] delivered" INFO line from _safe_send success path'
    line = delivered[-1].getMessage()
    assert '1 chunk(s)' in line, f'chunk count missing from delivery log: {line!r}'
    assert f'{len(text)} chars' in line, f'char length missing from delivery log: {line!r}'
    # Content-free contract: the message text itself must never appear in any log record.
    for r in caplog.records:
        assert text not in r.getMessage(), \
            f'message content leaked into log record: {r.getMessage()!r}'


# --------------------------------------------------------------------------- #
# F3. P1 pre-reflection push gate reads _tg_pushed
# --------------------------------------------------------------------------- #

def test_p1_does_not_push_when_tg_pushed_already_set(fresh_manager, tmp_path):
    """A reused instance carrying a stale _tg_pushed=True must NOT re-push at P1.

    The pre-hook previously only WROTE the flag (on success) and never read it, so an
    instance whose one-shot reflection guard was reset on reuse (lifecycle_manager)
    could push the same conversation tail a second time. With F3 the gate reads the
    flag and suppresses the duplicate — while still running the reflection turns.
    """
    supervisor = MagicMock()
    # notify_user returns True so that, if the (buggy) pre-hook pushes, it would set
    # _tg_pushed=True — mirroring test_tg_push_model.test_pre_and_post_dedup's setup.
    supervisor.notify_user.return_value = True

    engine, inst, pool, run = _make_engine(fresh_manager, tmp_path, max_turns=5, min_turns=2,
                                           extra_turns=5, natural_end_at=3, supervisor=supervisor)
    assert inst.parent_instance is None          # root — the P1 gate's first condition holds
    inst._tg_pushed = True                       # stale flag from a previous reflection run
    run()

    # Reflection still fired (F3 suppresses the PUSH, not the reflection itself).
    assert inst._auto_skill_proposed is True, 'reflection should have fired'
    supervisor.notify_user.assert_not_called(), \
        'P1 must not push when _tg_pushed is already True (flag was write-only pre-fix)'
