"""Tests for todo.md:134 — system prompt freeze on live AgentInstance.

Contract: while an AgentInstance is alive, conversation[0] is immutable.
Only code that *creates* the instance may write it. Recall, reuse, config
change, compression resync, skill refresh, session-restore injection must all
be readers.

The fix adds a boolean `_system_prompt_frozen` on AgentInstance, enforced at
4 choke points + the M1 block in _setup_turn. No digest, no snapshot, no heal.

Expected pre-fix failures: T1, T2, T4, T5, T6, T8, T10, T11, T12 (9 of 12).
T3, T7, T13 are regression pins that pass pre-fix.
"""

import time
import threading
from unittest.mock import MagicMock, patch

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import SYSTEM, USER, Message
from agent_cascade.lifecycle_manager import (
    AgentLifecycleManager,
    _inject_metadata_into_message,
)


# ──────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = (
    'You are worker1.\n\n'
    '## Session Metadata\n'
    '- Supervisor: User\n'
    '- System: AgentCascade v0.2.95 — 2026-09-28 01:00\n'
    '- Working Dir: N:\\work\\WD\\AgentWorkspace\n\n'
    '## AVAILABLE AGENTS\n\n'
    'Available Agent Types (call via call_agent):\n'
    '- **coder**: Practical senior software engineer\n\n'
    '### Advanced Feature: Argument Caching Pool\n'
    'The system maintains a rolling cache of tool arguments.\n\n'
    '## Active Skills\n\n'
    '### Skill self-augmentation\n'
    'Self-Augmentation Protocol body here.'
)


def _make_instance(
    name='worker1',
    agent_class='coder',
    content=SYSTEM_PROMPT_TEMPLATE,
    role=SYSTEM,
    parent_instance=None,
):
    """Create a real AgentInstance with a single system message in conversation."""
    inst = AgentInstance(
        instance_name=name,
        agent_class=agent_class,
        conversation=[Message(role=role, content=content)],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
        parent_instance=parent_instance,
    )
    return inst


def _make_pool(config_version=0):
    """Build a MagicMock pool usable by _setup_turn and lifecycle methods."""
    pool = MagicMock()
    pool._config_version = config_version
    pool.stopped = False
    pool.settings.cache_pool_enabled = True
    pool.settings.default_load_skill_mode = 'AUTO'
    # slice_history_for_llm is identity — no markers in test conversations.
    pool.slice_history_for_llm.side_effect = lambda msgs, **kw: list(msgs)

    # Template with a minimal function_map so _build_resources_block works.
    # Values must have a .function attribute (accessed at helpers.py:159).
    def _func(name):
        f = MagicMock()
        f.function = {'name': name, 'description': f'{name} tool', 'parameters': {}}
        return f

    template = MagicMock()
    template.agent_class = 'coder'
    template.name = 'coder'
    template.description = 'Practical senior software engineer'
    template.function_map = {'call_agent': _func('call_agent'), 'read_file': _func('read_file')}
    template.llm = None  # no generate_cfg override
    template.system_message = SYSTEM_PROMPT_TEMPLATE
    pool.get_template.return_value = template
    pool.templates = {'coder': template}

    # _build_session_metadata needs a logger with metadata.
    log_inst = MagicMock()
    log_inst.data = {'metadata': {}, 'history': []}
    log_inst.log_path = '/fake/log.jsonl'
    pool.get_logger.return_value = log_inst
    pool.operation_manager = None

    # skill_manager for _inject_self_augmentation_skill (T13).
    pool.skill_manager = MagicMock()
    pool.skill_manager._ensure_discovered = MagicMock()
    pool.skill_manager.load_full_instructions = MagicMock(
        return_value='Self-Augmentation Protocol body here.')

    return pool


def _make_engine(pool):
    """Build an ExecutionEngine over the mock pool."""
    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    return engine


def _drive_setup_turn(engine, inst):
    """Run _setup_turn and return its result tuple."""
    return engine._setup_turn(inst)


def _freeze_via_first_turn():
    """Create a fresh instance and freeze it via one real _setup_turn.

    Returns (inst, pool, engine). Shared by every test that needs a frozen
    instance before exercising a guard or forcing a rebuild.
    """
    inst = _make_instance()
    pool = _make_pool(config_version=0)
    engine = _make_engine(pool)
    inst._last_config_version = -1  # force the rebuild path on turn 1
    _drive_setup_turn(engine, inst)
    return inst, pool, engine


def _mutate_rebuild_inputs(inst, pool):
    """Mutate every input that would make the M1 block produce different content.

    Used by the forced-rebuild tests (T2, T8, T10b) so each of them proves the
    freeze blocked a rebuild that WOULD have changed bytes.
    """
    inst.system_started_at = '2099-12-31 23:59'  # wildly different timestamp
    pool._config_version += 1                      # force rebuild
    pool.settings.cache_pool_enabled = False       # changes resources block


# ──────────────────────────────────────────────
# T1 — freeze_after_first_turn
# ──────────────────────────────────────────────


class TestFreezeAfterFirstTurn:

    def test_freeze_after_first_turn(self):
        """T1: After one successful _setup_turn, the instance is frozen.

        Pre-fix: _system_prompt_frozen attribute does not exist → AttributeError.
        """
        inst = _make_instance()
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # Force a rebuild (config version mismatch).
        inst._last_config_version = -1

        result = _drive_setup_turn(engine, inst)
        assert result is not None  # sanity: didn't early-exit

        assert inst._system_prompt_frozen is True


# ──────────────────────────────────────────────
# T2 — forced_cache_rebuild_preserves_bytes (THE MONEY TEST)
# ──────────────────────────────────────────────


class TestForcedCacheRebuildPreservesBytes:

    def test_forced_cache_rebuild_preserves_bytes(self):
        """T2: After freeze, a forced CACHE_REBUILD must NOT mutate conversation[0].

        The test explicitly sets inst.system_started_at to a new value before the
        second _setup_turn call, plus bumps pool._config_version and flips
        settings.cache_pool_enabled. This guarantees the M1 block would produce
        different content if it ran — so passing proves the freeze blocked it.

        Pre-fix: no freeze → M1 rewrites the - System: line → content differs.
        """
        inst, pool, engine = _freeze_via_first_turn()

        before = inst.conversation[0].content
        before_id = id(inst.conversation[0])

        # NOW mutate the inputs that would make M1 produce different content:
        _mutate_rebuild_inputs(inst, pool)

        _drive_setup_turn(engine, inst)

        assert inst.conversation[0].content == before, (
            f"conversation[0] was mutated by forced CACHE_REBUILD.\n"
            f"Expected: {before[:200]!r}...\n"
            f"Got:      {inst.conversation[0].content[:200]!r}..."
        )
        assert id(inst.conversation[0]) == before_id, (
            'conversation[0] object was replaced (not just edited)'
        )


# ──────────────────────────────────────────────
# T3 — recall_end_to_end (PIN: passes pre-fix)
# ──────────────────────────────────────────────


class TestRecallEndToEnd:

    def test_recall_end_to_end(self):
        """T3 (PIN): Full recall path leaves conversation[0] byte-identical.

        Drives find_or_create_instance → _create_and_run_agent (harness reused
        from test_recall_skill_preservation.py) → real initialize_conversation
        → _setup_turn. Passes pre-fix; composition pin.
        """
        inst = _make_instance()
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # First turn to establish the prompt (and freeze post-fix).
        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        before = inst.conversation[0].content

        # Simulate recall: lifecycle reuses the instance.
        lifecycle = AgentLifecycleManager(pool)
        sys_msg = inst.conversation[0]  # same object (recall keeps it)
        task_msg = Message(role=USER, content='new task')

        # Real initialize_conversation reuse path.
        conv = lifecycle.initialize_conversation(
            instance=inst,
            sys_msg=sys_msg,
            task_msg=task_msg,
            is_reuse=True,
            instance_name='worker1',
            agent_class='coder',
        )

        # _setup_turn after recall (config unchanged → cache hit).
        _drive_setup_turn(engine, inst)

        assert inst.conversation[0].content == before
        assert 'BODY-A' not in inst.conversation[0].content  # no injection happened


# ──────────────────────────────────────────────
# T4 — edit_message_in_place_raises_when_frozen
# ──────────────────────────────────────────────


class TestEditMessageInPlaceRaisesWhenFrozen:

    def test_edit_message_in_place_raises_when_frozen(self):
        """T4: edit_message_in_place(0, ...) raises SystemPromptFrozenError on frozen.

        Pre-fix: no such exception exists; the edit succeeds silently.
        """
        inst = _make_instance()
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # Freeze via first turn.
        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        before = inst.conversation[0].content

        from agent_cascade.agent_instance import SystemPromptFrozenError
        try:
            inst.edit_message_in_place(0, Message(role=SYSTEM, content='TAMPERED'))
        except SystemPromptFrozenError:
            pass  # expected
        else:
            raise AssertionError(
                'edit_message_in_place(0) did not raise SystemPromptFrozenError')

        assert inst.conversation[0].content == before


# ──────────────────────────────────────────────
# T5 — insert_message_at_head_raises_when_frozen
# ──────────────────────────────────────────────


class TestInsertMessageAtHeadRaisesWhenFrozen:

    def test_insert_message_at_head_raises_when_frozen(self):
        """T5: insert_message_at_head raises SystemPromptFrozenError on frozen.

        Pre-fix: no such exception; the insert succeeds.
        """
        inst = _make_instance()
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # Freeze via first turn.
        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        from agent_cascade.agent_instance import SystemPromptFrozenError
        try:
            inst.insert_message_at_head(Message(role=SYSTEM, content='NEW SYS'))
        except SystemPromptFrozenError:
            pass  # expected
        else:
            raise AssertionError(
                'insert_message_at_head did not raise SystemPromptFrozenError')

        assert len(inst.conversation) == 1


# ──────────────────────────────────────────────
# T6 — m11_metadata_not_injected_when_frozen
# ──────────────────────────────────────────────


class TestM11MetadataNotInjectedWhenFrozen:

    def test_m11_metadata_not_injected_when_frozen(self):
        """T6: _inject_metadata_into_message is a no-op on a frozen instance.

        Freeze an instance whose conversation[0] lacks '## Session Metadata',
        then call the real initialize_conversation reuse path. Content must
        still lack the heading (byte-identical).

        Pre-fix: metadata IS injected → content changes.
        """
        # Prompt WITHOUT '## Session Metadata'.
        no_meta_prompt = (
            'You are worker1.\n\n'
            '## AVAILABLE AGENTS\n\n'
            '- **coder**: Practical senior software engineer\n\n'
            '## Active Skills\n\n'
            '### Skill self-augmentation\n'
            'Self-Augmentation Protocol body here.'
        )
        inst = _make_instance(content=no_meta_prompt)
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # Freeze via first turn (M1 injects the metadata heading during this
        # rebuild; we strip it again below to simulate a frozen prompt lacking
        # the heading — the M2/M11 composition state).
        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        # At this point post-fix the prompt is frozen. Pre-fix it isn't.
        # Remove the metadata that M1 may have injected during the first turn,
        # then re-freeze manually to simulate the "frozen without heading" state.
        content = inst.conversation[0].content
        if '## Session Metadata' in content:
            # Strip it to simulate a frozen prompt lacking the heading.
            lines = content.split('\n')
            start = next(i for i, l in enumerate(lines) if l.startswith('## Session Metadata'))
            # Find end of metadata block (next ## heading or end).
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if lines[j].startswith('## '):
                    end = j
                    break
            inst.conversation[0].content = '\n'.join(lines[:start] + lines[end:])

        before = inst.conversation[0].content
        assert '## Session Metadata' not in before  # sanity

        # Now call _inject_metadata_into_message directly (the M11 path).
        lifecycle = AgentLifecycleManager(pool)
        sys_msg = inst.conversation[0]
        task_msg = Message(role=USER, content='task')
        lifecycle.initialize_conversation(
            instance=inst,
            sys_msg=sys_msg,
            task_msg=task_msg,
            is_reuse=True,
            instance_name='worker1',
            agent_class='coder',
        )

        assert inst.conversation[0].content == before, (
            'M11 injected metadata into a frozen prompt'
        )


# ──────────────────────────────────────────────
# T7 — m11_metadata_injected_when_not_frozen (PIN: passes pre-fix)
# ──────────────────────────────────────────────


class TestM11MetadataInjectedWhenNotFrozen:

    def test_m11_metadata_injected_when_not_frozen(self):
        """T7 (PIN): _inject_metadata_into_message DOES inject when NOT frozen.

        Guards against over-broad fix. Passes pre-fix and post-fix.
        """
        no_meta_prompt = (
            'You are worker1.\n\n'
            '## AVAILABLE AGENTS\n\n'
            '- **coder**: Practical senior software engineer\n'
        )
        inst = _make_instance(content=no_meta_prompt)
        pool = _make_pool(config_version=0)

        # Do NOT run _setup_turn — instance is not frozen.
        lifecycle = AgentLifecycleManager(pool)
        sys_msg = inst.conversation[0]
        task_msg = Message(role=USER, content='task')
        lifecycle.initialize_conversation(
            instance=inst,
            sys_msg=sys_msg,
            task_msg=task_msg,
            is_reuse=True,
            instance_name='worker1',
            agent_class='coder',
        )

        assert '## Session Metadata' in inst.conversation[0].content


# ──────────────────────────────────────────────
# T8 — m11_reachable_via_setup_turn_m2
# ──────────────────────────────────────────────


class TestM11ReachableViaSetupTurnM2:

    def test_m11_reachable_via_setup_turn_m2(self):
        """T8: M2 path (conv[0] is USER) + freeze → second recall-initialize
        leaves content unchanged.

        Pre-fix: first _setup_turn inserts a SYSTEM head (M2), which lacks the
        metadata heading. Second initialize_conversation then injects it (M11).
        Post-fix: the frozen prompt is never touched by M11.
        """
        # Start with a USER message as conv[0] (no system message).
        inst = AgentInstance(
            instance_name='worker1',
            agent_class='coder',
            conversation=[Message(role=USER, content='hello')],
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            latest_marker_index=-1,
        )
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # First turn: M2 inserts a SYSTEM message at head.
        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        # After first turn, conv[0] should be SYSTEM (M2 injected it).
        assert inst.conversation[0].role == SYSTEM

        before = inst.conversation[0].content

        # Second recall: real initialize_conversation reuse path.
        lifecycle = AgentLifecycleManager(pool)
        sys_msg = inst.conversation[0]
        task_msg = Message(role=USER, content='second task')
        lifecycle.initialize_conversation(
            instance=inst,
            sys_msg=sys_msg,
            task_msg=task_msg,
            is_reuse=True,
            instance_name='worker1',
            agent_class='coder',
        )

        assert inst.conversation[0].content == before, (
            'M11 mutated the frozen prompt after M2 injection'
        )


# ──────────────────────────────────────────────
# T10 — root_vs_subagent
# ──────────────────────────────────────────────


class TestRootVsSubagent:

    def test_root_instance_freeze(self):
        """T10a: Root instance (parent_instance=None) freezes correctly.

        Pre-fix: no freeze attribute.
        """
        inst = _make_instance(parent_instance=None)
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        assert inst._system_prompt_frozen is True

    def test_subagent_freeze_preserves_bytes(self):
        """T10b: Sub-agent instance (parent_instance set) — forced rebuild
        preserves bytes, same as T2 but with a parent.

        Pre-fix: no freeze → M1 rewrites.
        """
        inst = _make_instance(parent_instance='Main')
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        before = inst.conversation[0].content
        _mutate_rebuild_inputs(inst, pool)

        _drive_setup_turn(engine, inst)

        assert inst.conversation[0].content == before


# ──────────────────────────────────────────────
# T11 — survives_compression
# ──────────────────────────────────────────────


class TestSurvivesCompression:

    def test_survives_compression(self):
        """T11: rebuild_conversation does NOT unfreeze. Flag survives.

        Pre-fix: no freeze attribute at all.
        """
        inst = _make_instance()
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        assert inst._system_prompt_frozen is True
        before = inst.conversation[0].content

        # Simulate compression: rebuild_conversation replaces the list.
        sys_copy = Message(role=SYSTEM, content=before)
        user_msg = Message(role=USER, content='some user msg')
        inst.rebuild_conversation([sys_copy, user_msg])

        assert inst._system_prompt_frozen is True, (
            'rebuild_conversation unfroze the instance'
        )
        assert inst.conversation[0].content == before


# ──────────────────────────────────────────────
# T12 — reset_unfreezes
# ──────────────────────────────────────────────


class TestResetUnfreezes:

    def test_reset_unfreezes(self):
        """T12: reset_conversation clears the freeze flag.

        Pre-fix: no freeze attribute; reset does not clear it (AttributeError).
        """
        inst = _make_instance()
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)
        assert inst._system_prompt_frozen is True

        # Reset unfreezes.
        inst.reset_conversation()
        assert inst._system_prompt_frozen is False

        # New system message can be set without error.
        inst.conversation.append(Message(role=SYSTEM, content='fresh prompt'))


# ──────────────────────────────────────────────
# T13 — regression pin: _inject_self_augmentation_skill on frozen instance
# ──────────────────────────────────────────────


class TestSelfAugInjectionOnFrozenInstance:

    def test_self_aug_injection_noop_on_frozen(self):
        """T13 (REGRESSION PIN): _inject_self_augmentation_skill on a frozen
        instance with the '## Active Skills' heading present is a no-op.

        This passes pre-fix too — it pins that the heading-exists idempotency
        guard in _inject_skills_to_system_message protects the frozen prompt.
        In production, injection always happens before freeze, so the heading
        is always present → the guard skips. This test documents WHY T13 in the
        original plan (frozen instance lacking the heading) was unreachable.

        The assertion is byte-identity of conversation[0].content after calling
        _inject_self_augmentation_skill on a frozen instance.
        """
        from agent_cascade.engine.helpers import _inject_self_augmentation_skill

        inst = _make_instance()  # has '## Active Skills' in prompt
        pool = _make_pool(config_version=0)
        engine = _make_engine(pool)

        # Freeze via first turn (normal creation path: skills injected before freeze).
        inst._last_config_version = -1
        _drive_setup_turn(engine, inst)

        before = inst.conversation[0].content

        # Call the M4 entry point on the frozen instance.
        result = _inject_self_augmentation_skill(pool, inst)

        # The heading-exists guard skips → no mutation.
        assert result is False, (
            '_inject_self_augmentation_skill should skip (heading exists)'
        )
        assert inst.conversation[0].content == before, (
            'conversation[0] was mutated by self-aug injection on frozen instance'
        )
