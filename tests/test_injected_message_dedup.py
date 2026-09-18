"""Regression tests for injected system-message dedup in the LLM working view.

Bug (todo.md line 138): when an agent that went through extended turns is recalled,
injected system messages (turn-limit warnings, final-turn warning, Skill Reflection
prompt, soft-continue nudge) appeared DUPLICATED in the LLM request view. Root cause:
the injection sites called _append_and_log() — which already appends to
instance._cached_llm_messages via append_message() — and THEN did an explicit
llm_messages.append(msg). Because llm_messages is normally the SAME object as
_cached_llm_messages (see _setup_turn), that double-appended.

Fix: a single identity-checked helper _append_and_log_to_llm() guarantees exactly-once
presence in the LLM view regardless of object identity. These tests exercise the REAL
unbound engine methods bound to a minimal fake instance — no full LLM loop required.
"""

import threading
from types import SimpleNamespace

from agent_cascade.engine.core import ExecutionEngine


# ---------------------------------------------------------------------------
# Fakes: minimal engine + instance exposing only what the two helpers touch
# ---------------------------------------------------------------------------


def _make_fake_instance(cached_llm_messages=None):
    """Build a fake AgentInstance exposing only what the injection helpers need.

    Mirrors the real append_message() contract that matters here: appending to
    conversation AND _cached_llm_messages atomically (agent_instance.append_message).
    """
    inst = SimpleNamespace(
        instance_name='test',
        agent_class='coder',
        conversation=[],
        _cached_llm_messages=cached_llm_messages if cached_llm_messages is not None else [],
        _compression_lock=threading.RLock(),
    )

    def append_message(message):
        inst.conversation.append(message)
        inst._cached_llm_messages.append(message)

    inst.append_message = append_message
    return inst


def _make_engine(logged):
    """Build a fake engine with the REAL unbound helper methods bound to it.

    self.pool.get_logger(...) is stubbed to return a logger that records .log_message,
    so we test actual production logic (not a copy) without touching the filesystem.
    """
    fake_logger = SimpleNamespace(log_message=lambda msg: logged.append(msg))
    engine = SimpleNamespace(
        pool=SimpleNamespace(get_logger=lambda inst_name, agent_class: fake_logger),
    )
    # Bind the REAL unbound methods so we exercise production code, not a re-implementation.
    engine._append_and_log = ExecutionEngine._append_and_log.__get__(engine)
    engine._append_and_log_to_llm = ExecutionEngine._append_and_log_to_llm.__get__(engine)
    return engine


def _make_msg(content='warn'):
    # SimpleNamespace has identity-based equality (no __eq__), so list.count() counts
    # exactly the copies of this object — ideal for asserting "exactly once".
    return SimpleNamespace(role='user', content=content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAppendAndLogToLLM:
    """Exactly-once injection into the LLM working view (identity-checked)."""

    def test_same_object_no_double_append(self):
        """(a) Core regression: llm_messages IS _cached_llm_messages -> exactly once.

        Before the fix this produced TWO copies (append_message + explicit append).
        """
        logged = []
        engine = _make_engine(logged)
        inst = _make_fake_instance()
        llm_messages = inst._cached_llm_messages  # SAME object (the normal case)
        msg = _make_msg('halfway warning')

        engine._append_and_log_to_llm(inst, msg, llm_messages)

        assert llm_messages.count(msg) == 1, 'message double-appended into LLM view'
        assert inst.conversation.count(msg) == 1
        assert len(logged) == 1 and logged[0] is msg

    def test_divergent_object_appends_both_once(self):
        """(b) Desync case: llm_messages is a DIFFERENT list -> appended to both, once each.

        Simulates the forced-compression consolidation rebuild where _cached_llm_messages
        was replaced while the loop was suspended. The helper must NOT drop the message
        from either view.
        """
        logged = []
        engine = _make_engine(logged)
        inst = _make_fake_instance()
        llm_messages = []  # DIFFERENT object (diverged from _cached_llm_messages)
        msg = _make_msg('final turn warning')

        engine._append_and_log_to_llm(inst, msg, llm_messages)

        assert inst._cached_llm_messages.count(msg) == 1, 'message dropped from _cached_llm_messages'
        assert llm_messages.count(msg) == 1, 'message not mirrored into divergent LLM view'
        assert inst.conversation.count(msg) == 1
        assert len(logged) == 1 and logged[0] is msg

    def test_repeated_calls_stay_consistent(self):
        """(c) Each helper call appends exactly one copy; N calls -> N copies (no drift)."""
        logged = []
        engine = _make_engine(logged)
        inst = _make_fake_instance()
        llm_messages = inst._cached_llm_messages  # SAME object

        for i in range(3):
            engine._append_and_log_to_llm(inst, _make_msg(f'warn {i}'), llm_messages)

        assert len(llm_messages) == 3
        assert len(inst.conversation) == 3
        assert len(logged) == 3
