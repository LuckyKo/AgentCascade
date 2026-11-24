"""BUG_0405 regression: reusing a TERMINATED instance must clear its stale
termination signal, otherwise the reused child's run loop bails before the first
turn and the dispatch reports "agent creation returned no output".

Root cause (see .agent_lessons/bug0405-terminated-instance-child-reuse-no-output.md):
  - find_or_create_instance permits reusing an instance whose state is IDLE OR
    TERMINATED (the AFK-Admin direct-run dispatch relies on this for session
    continuity, force_fresh=False).
  - _prepare_instance_for_reuse (the shared reuse-cleanup helper) cleared per-run state but NOT
    is_terminated / pool.terminated_instances / state. The fix lives in this shared helper for
    consistency with its "both reuse paths cannot drift" contract; the other caller
    (ExecutionEngine._acquire_reusable_system_agent) gates reuse on state==IDLE and rejects
    terminated instances, so a TERMINATED instance cannot reach it that way — the reset there is a
    defensive no-op, not a second live repro.
  - pool.is_instance_terminated() ORs the name-scoped set and the instance flag
    (pool/slots.py). With either still set, _stream_child_run's very first line
    returns ([], None) -> empty conversation -> child_runner.py "agent creation
    returned no output" -> recurring failure-ping for the AFK cadence.

The fix clears exactly that signal at the reuse-cleanup point, mirroring
_reset_terminated_compressor_for_retry (compression/agent_invoker.py): under
_state_lock is_terminated=False + TERMINATED->IDLE, then under _pool_lock
terminated_instances.discard(name). Guarded on TERMINATED so a live/IDLE instance
is a strict no-op.

These tests exercise the REAL reuse path (no LLM, no thread). They fail on the
unfixed code: the reused instance's state stays TERMINATED, is_terminated stays
True, the name stays in pool.terminated_instances, and is_instance_terminated()
still reports True.
"""

import time

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.agent_pool import AgentPool
from agent_cascade.lifecycle_manager import AgentLifecycleManager

DUMMY_LLM_CFG = {
    'model': 'qwen/qwen3-4b',
    'model_server': 'http://127.0.0.1:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
}


def make_instance(name: str, state: AgentState = AgentState.IDLE) -> AgentInstance:
    """Minimal AgentInstance, state set directly (mirrors FLEET_RESTORE, which
    assigns inst.state=TERMINATED + inst.is_terminated=True without a normal
    transition — the precondition that triggers BUG_0405)."""
    return AgentInstance(
        instance_name=name,
        agent_class='Admin',
        conversation=[],
        state=state,
        max_turns=None,
        parent_instance=None,
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        compression_summary=None,
        latest_marker_index=-1,
    )


def _unique(name: str) -> str:
    return f"{name}_{int(time.monotonic() * 1e6) % 1_000_000}_{time.time_ns() % 1_000_000}"


class TestTerminatedReuseClearsStaleTerminated:

    def test_terminated_reuse_clears_all_terminated_markers(self):
        """A TERMINATED instance reused via the AFK-Admin-shaped dispatch
        (force_fresh=False, no log_file) must end up runnable: state IDLE,
        is_terminated False, name absent from pool.terminated_instances, and
        is_instance_terminated() False. Pre-fix, all four assertions fail."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        name = _unique('bug0405_afk')

        # Poison: a TERMINATED instance still flagged terminated in both surfaces,
        # exactly what a FLEET_RESTORE / prior terminate_instance leaves behind.
        inst = make_instance(name, state=AgentState.TERMINATED)
        inst.is_terminated = True
        pool.instances[name] = inst
        pool.terminated_instances.add(name)

        # Preconditions — the bug's starting state.
        assert pool.is_instance_terminated(name) is True, \
            'precondition: instance must report terminated'
        assert inst.state == AgentState.TERMINATED, \
            'precondition: instance state must be TERMINATED'

        # Dispatch shape: AFK-Admin (api_server._afk_admin_nudge -> run_child_core)
        # passes force_fresh=False and NO log_file. log_file omitted here.
        result_inst, is_reuse, session_was_loaded = manager.find_or_create_instance(
            agent_class='Admin', instance_name=name, caller=None,
            nest_depth=0, force_fresh=False)

        # Reuse semantics preserved (the fix must NOT forbid TERMINATED reuse).
        assert is_reuse is True, \
            'BUG_0405: TERMINATED instance must still be reused (fix must not forbid reuse)'
        assert result_inst is inst, 'reuse must return the same instance object'
        assert session_was_loaded is False, 'no log_file was supplied; nothing to load'

        # The fix: every stale termination marker must be cleared so the run loop proceeds.
        assert inst.state == AgentState.IDLE, \
            f'BUG_0405: reused TERMINATED instance must be IDLE to pass the engine L1 ' \
            f'race guard, got {inst.state!r}'
        assert inst.is_terminated is False, \
            'BUG_0405: reused instance is_terminated must be False'
        assert name not in pool.terminated_instances, \
            'BUG_0405: stale name entry must be discarded from pool.terminated_instances'
        # The exact predicate _stream_child_run checks on its first line. The set and
        # the flag are OR-ed, so clearing only one would still leave this True.
        assert pool.is_instance_terminated(name) is False, \
            'BUG_0405: is_instance_terminated() must be False — this is the bail trigger ' \
            'that produced "agent creation returned no output"'

    def test_running_instance_reuse_is_noop_on_terminated_clear(self):
        """Edge case: the reset must be a strict no-op for a non-TERMINATED
        (e.g. RUNNING) instance — it must never clobber a live instance's state or
        clear anything it did not terminate. Invoked directly because
        find_or_create_instance never routes an active instance to the reuse helper."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        name = _unique('bug0405_running')

        inst = make_instance(name, state=AgentState.RUNNING)
        pool.instances[name] = inst

        manager._prepare_instance_for_reuse(inst, caller=None, nest_depth=0)

        assert inst.state == AgentState.RUNNING, \
            'BUG_0405: a RUNNING instance must not be transitioned by the reuse cleanup'
        assert inst.is_terminated is False, \
            'BUG_0405: a RUNNING instance is_terminated flag must be left untouched'
        assert name not in pool.terminated_instances, \
            'RUNNING instance must not appear in terminated_instances (discard is a no-op here)'

    def test_idle_reuse_leaves_state_idle(self):
        """The common, healthy reuse path (already-IDLE instance) is unaffected:
        state stays IDLE and the run loop is not tripped."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        name = _unique('bug0405_idle')

        inst = make_instance(name, state=AgentState.IDLE)
        pool.instances[name] = inst

        result_inst, is_reuse, _ = manager.find_or_create_instance(
            agent_class='Admin', instance_name=name, caller=None,
            nest_depth=0, force_fresh=False)

        assert is_reuse is True
        assert result_inst is inst
        assert inst.state == AgentState.IDLE, \
            'BUG_0405: an already-IDLE instance must remain IDLE (no spurious transition)'
        assert pool.is_instance_terminated(name) is False
