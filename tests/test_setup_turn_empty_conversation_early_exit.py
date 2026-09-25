"""Regression test: empty-conversation early exit must not crash the turn.

Bug (production, 2026-09-25 — Telegram bridge "AC produced no text reply"):
``ExecutionEngine.run()`` called ``len(messages)`` / ``len(llm_messages)`` in a debug
log IMMEDIATELY after ``_setup_turn()``. But ``_setup_turn``'s documented contract
(core.py, its docstring) is to return ``(None, None, None)`` on the empty-conversation
early exit (core.py:1337). ``len(None)`` raised ``TypeError``, which was swallowed by
the generic ``except Exception`` handler (core.py:1188) that yields a spurious
``[SYSTEM ERROR: ...]`` — and, critically, it fired BEFORE the ``if not messages:``
early-exit block that is explicitly designed to drain queued user messages so they are
not lost.

Net production effect: the enqueued user message was never appended to the conversation
(lost), a spurious error was streamed, and the Telegram waiter read an empty reply.

This test drives the REAL ``ExecutionEngine.run()`` with an instance whose
``conversation`` is genuinely EMPTY (so the real ``_setup_turn`` hits the early exit and
returns ``(None, None, None)``) and a user message enqueued through the REAL pool queue.
It asserts:
  1. NO ``[SYSTEM ERROR: ...]`` message is yielded by ``run()`` (pre-fix this fired).
  2. The enqueued user message WAS appended to ``instance.conversation`` (the early-exit
     drain ran — the message is not lost).
  3. The run exits cleanly to IDLE (no crash, no spurious error yield).

NOTE on non-vacuity: existing engine tests MOCK ``_setup_turn`` (e.g.
``test_bug1_bug6_bug4_8_slot_deadlock.py`` returns ``([], [], [])`` — empty *lists*, not
``None``), so a mocked test cannot catch this bug. This test does NOT mock ``_setup_turn``;
it exercises the real method end-to-end.

Run: pytest tests/test_setup_turn_empty_conversation_early_exit.py -v
"""

import time

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.api_router import APIRouter, APIEndpoint
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.agent_pool import AgentPool
from agent_cascade.llm.schema import Message


def _build_real_pool(tmp_path) -> AgentPool:
    """A real AgentPool (real MessageQueueMixin → real enqueue/drain/has_messages)."""
    llm_cfg = {
        'model': 'mock',
        'api_base': 'http://127.0.0.1:9/v1',
        'model_server': 'http://127.0.0.1:9/v1',
        'api_key': 'EMPTY',
    }
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(tmp_path))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep = APIEndpoint(id='ep0', name='conc0', api_base=llm_cfg['api_base'],
                     model='mock', concurrency_limit=0, enabled=True)
    router.add_endpoint(ep)
    router.default_llm_cfg = ep.to_llm_cfg()
    return AgentPool(llm_cfg, agents_dir=str(tmp_path), api_router=router)


def _make_empty_instance(name: str) -> AgentInstance:
    """A real AgentInstance with a GENUINELY EMPTY conversation.

    Empty conversation is the precondition that makes the real ``_setup_turn`` hit the
    early-exit branch and return ``(None, None, None)`` — do NOT pre-seed a message.
    """
    now = time.monotonic()
    inst = AgentInstance(
        instance_name=name,
        agent_class='coder',
        conversation=[],  # ← the bug trigger: empty conversation
        created_at=now,
        last_activity=now,
        latest_marker_index=0,
        parent_instance='Main',
    )
    return inst


def _flatten_yielded(yielded):
    """Flatten run()'s yields into a flat list of Message objects."""
    msgs = []
    for item in yielded:
        if isinstance(item, tuple):  # (messages_list, is_streaming)
            msgs.extend(item[0])
        elif isinstance(item, list):
            msgs.extend(item)
        elif item is not None:
            msgs.append(item)
    return msgs


def test_empty_conversation_early_exit_no_crash_message_not_lost(tmp_path):
    """Real run() with empty conversation + enqueued user message:

    (1) no [SYSTEM ERROR] yield, (2) the user message is drained into the conversation,
    (3) the run exits cleanly to IDLE.
    """
    pool = _build_real_pool(tmp_path)
    inst = _make_empty_instance('Maine')
    pool.instances[inst.instance_name] = inst

    # Enqueue a user message exactly the way POST /api/message does.
    user_text = 'hello from telegram'
    pool.enqueue_message(inst.instance_name, user_text)
    assert pool.has_messages(inst.instance_name), 'precondition: message must be queued'

    engine = ExecutionEngine(pool)

    # Drive the REAL run() to completion (no _setup_turn mock).
    yielded = list(engine.run(inst))

    msgs = _flatten_yielded(yielded)

    # (1) No spurious [SYSTEM ERROR] was streamed. Pre-fix, len(None) raised TypeError,
    #     caught by the generic handler and yielded as "[SYSTEM ERROR: ...]".
    error_msgs = [m for m in msgs if isinstance(m, Message)
                  and isinstance(m.content, str) and '[SYSTEM ERROR' in m.content]
    assert not error_msgs, f"spurious SYSTEM ERROR yielded (bug not fixed): {error_msgs}"

    # (2) The early-exit drain ran: the enqueued user message was appended to the
    #     conversation — it is NOT lost.
    conv_contents = [m.content for m in inst.conversation if isinstance(m, Message)]
    assert any(user_text in (c or '') for c in conv_contents), (
        f"enqueued user message was LOST (early-exit drain did not run). "
        f"conversation contents: {conv_contents}"
    )

    # (3) The run exited cleanly to IDLE (no crash, no suspension).
    assert inst.state == AgentState.IDLE, f"expected IDLE, got {inst.state.name}"


def test_empty_conversation_no_queue_exits_clean(tmp_path):
    """Sanity: empty conversation with NO queued message still exits cleanly to IDLE
    with no spurious error (the early-exit path is safe even with an empty queue)."""
    pool = _build_real_pool(tmp_path)
    inst = _make_empty_instance('Maine')
    pool.instances[inst.instance_name] = inst

    engine = ExecutionEngine(pool)
    yielded = list(engine.run(inst))

    msgs = _flatten_yielded(yielded)
    error_msgs = [m for m in msgs if isinstance(m, Message)
                  and isinstance(m.content, str) and '[SYSTEM ERROR' in m.content]
    assert not error_msgs, f"spurious SYSTEM ERROR yielded: {error_msgs}"
    assert inst.state == AgentState.IDLE, f"expected IDLE, got {inst.state.name}"
