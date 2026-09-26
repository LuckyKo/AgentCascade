"""Stale auto-skill snapshot tests for the corrected tg-dup root cause (plan v3).

plans/tg-dup-delivery_PLAN.md §0.5/§0.6 — the user reported that SEVERAL DIFFERENT
queries each returned the IDENTICAL final answer on the phone. Root cause: the
in-loop auto-skill snapshot ``_auto_skill_task_output`` is per-INSTANCE and was only
ever reset on the sub-agent reuse path (lifecycle_manager.py:157-163), never on the
main-agent run path — so ``extract_instance_output()`` returned the PREVIOUS run's
final answer for every subsequent root run.

Revert-proof: tests 1-2 FAIL on the pre-fix code and pass after F1 (the per-run reset
of the snapshot trio in ``run_agent_unified.py``); tests 3-4 guard F3 (the [TG-PUSH]
fingerprint + inst/gen logging) and FAIL pre-fix where the log line lacks those fields.

Run serially:
    python -m pytest tests/test_tg_dup_stale_snapshot.py -v -o addopts="" --timeout=120
"""

import asyncio
import logging
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure top-level imports work (mirror tests/test_tg_dup_delivery.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))


def _make_inst():
    """Minimal AgentInstance via ``__new__`` (bypasses dataclass defaults).

    Only the attributes the reset block and ``extract_instance_output()`` read are set;
    production code reads them with getattr-with-defaults anyway.
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState
    from agent_cascade.llm.schema import Message, USER

    inst = AgentInstance.__new__(AgentInstance)
    inst.instance_name = 'Maine'
    inst.parent_instance = None
    inst.conversation = [Message(role=USER, content='question 1')]
    inst.state = AgentState.IDLE
    inst._state_lock = threading.RLock()
    return inst


# --------------------------------------------------------------------------- #
# F1. Per-run reset of the auto-skill snapshot trio (root cause)
# --------------------------------------------------------------------------- #

def test_post_run_push_does_not_reuse_previous_run_snapshot():
    """The exact 18:24-18:38 condition, replayed directly (no thread driving).

    Run N set the snapshot to its final answer. Run N+1 reuses the SAME instance and
    appends a DIFFERENT answer to the conversation. After the per-run reset block,
    ``extract_instance_output`` must return the LIVE tail — not run N's stale text.
    Fails pre-fix: returns "ANSWER FROM RUN 1".
    """
    from agent_cascade.compression.helpers import extract_instance_output
    from agent_cascade.llm.schema import Message, ASSISTANT
    from agent_cascade.run_agent_unified import _reset_run_scoped_tg_state

    inst = _make_inst()
    # State as left by the previous run's in-loop auto-skill trigger (core.py:358).
    inst._auto_skill_task_output = 'ANSWER FROM RUN 1'
    inst._auto_skill_proposed = True
    inst._auto_skill_dirty_stop = False   # explicit: the natural-completion value

    # Run N+1: a different question gets a different final answer.
    inst.conversation.append(Message(role=ASSISTANT, content='ANSWER FROM RUN 2'))

    # The F1 per-run reset block (run_agent_unified.py), called directly.
    _reset_run_scoped_tg_state(inst)

    result = extract_instance_output(list(inst.conversation), 'Maine', instance=inst)
    assert result == 'ANSWER FROM RUN 2', \
        f'stale snapshot replayed: got {result!r}, expected the live tail "ANSWER FROM RUN 2"'


def test_p1_prehook_is_reachable_on_second_root_run():
    """After the per-run reset, ``_auto_skill_proposed`` must be False again.

    The flag is documented as one-shot PER RUN (agent_instance.py:259-260) but was only
    cleared on the sub-agent reuse path — so for the root agent it was effectively
    one-shot per process, permanently disabling the P1 pre-reflection push (H3).
    Fails pre-fix: still True.
    """
    from agent_cascade.run_agent_unified import _reset_run_scoped_tg_state

    inst = _make_inst()
    inst._auto_skill_task_output = 'ANSWER FROM RUN 1'
    inst._auto_skill_proposed = True
    inst._auto_skill_dirty_stop = False

    _reset_run_scoped_tg_state(inst)

    assert inst._auto_skill_proposed is False, \
        '_auto_skill_proposed must be re-armed per root run (one-shot per run semantics)'


# --------------------------------------------------------------------------- #
# F3. [TG-PUSH] log carries fingerprint + instance/run generation
# --------------------------------------------------------------------------- #

def _run(coro):
    """Run an async coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_bot():
    bot = MagicMock()

    async def _noop(**kwargs):
        return None

    bot.send_message.side_effect = lambda **kw: _noop()
    return bot


def test_tg_push_log_carries_fingerprint_and_instance(caplog):
    """[TG-PUSH] line must carry fp= (sha1[:8]), inst=, gen= — and NO message content.

    Guards F3a/F3b: fingerprint always present (same construction as the retry WARNING
    at bot.py:141); inst/gen only when both are provided. Fails pre-fix: no fp=/inst=/gen=.
    """
    from agent_cascade.telegram_bridge.bot import _safe_send

    text = 'The final answer delivered to the phone.'
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run(_safe_send(_make_bot(), chat_id=42, text=text,
                        instance_name='Maine', run_generation=7))

    delivered = [r for r in caplog.records if '[TG-PUSH] delivered' in r.getMessage()]
    assert delivered, 'expected a "[TG-PUSH] delivered" INFO line from _safe_send success path'
    line = delivered[-1].getMessage()
    import hashlib
    expected_fp = hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]
    assert f'fp={expected_fp}' in line, f'fingerprint missing from delivery log: {line!r}'
    assert 'inst=Maine' in line, f'instance name missing from delivery log: {line!r}'
    assert 'gen=7' in line, f'run generation missing from delivery log: {line!r}'
    # Content-free contract: the message text itself must never appear in any log record.
    for r in caplog.records:
        assert text not in r.getMessage(), \
            f'message content leaked into log record: {r.getMessage()!r}'


def test_tg_push_log_omits_fields_when_not_provided(caplog):
    """Backward compat: callers that pass nothing (P3/P4/bridge-internal) get fp= only.

    inst=/gen= must be ABSENT when the kwargs are omitted, so existing log lines keep
    their shape. Fails pre-fix: no fp= at all.
    """
    from agent_cascade.telegram_bridge.bot import _safe_send

    text = 'A bridge-internal notice with no run context.'
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run(_safe_send(_make_bot(), chat_id=42, text=text))

    delivered = [r for r in caplog.records if '[TG-PUSH] delivered' in r.getMessage()]
    assert delivered, 'expected a "[TG-PUSH] delivered" INFO line from _safe_send success path'
    line = delivered[-1].getMessage()
    import hashlib
    expected_fp = hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]
    assert f'fp={expected_fp}' in line, f'fingerprint missing from delivery log: {line!r}'
    assert 'inst=' not in line, f'inst= must be omitted when not provided: {line!r}'
    assert 'gen=' not in line, f'gen= must be omitted when not provided: {line!r}'
