"""BUG B regression tests — stop-cascade + L1 race guard (2026-08-21 incident).

Covers three verified defects from reports/fallback-compression-misclass-and-stop-cascade.md:

B1. No stop-check in the fallback-compression round loop (llm_call.py) → Compressor_2..5
    spawned AFTER the user pressed Stop; exhaustion raised a spurious ContextWindowExceeded.
    Fix: terminal-stop check at the top of each round raises AgentTerminatedError, which the
    outer retry loop treats as a clean abort signal.

B2. Generator abandonment on stop-break:
    - compression/agent_invoker.py consumed engine.run() with `break` and no gen.close(),
      leaving the compressor instance RUNNING; the empty-summary retry loop then re-entered
      run() and tripped the L1 race guard ([BUG] entered engine.run() in state RUNNING).
      Fix: bind generator + close in finally; raise AgentTerminatedError instead of break.
    - engine/core.py _create_and_run_agent() consumer had the same break-without-close pattern.
      Fix: bind generator + close in finally.

B3. Pre-try early returns in engine.run() (core.py) executed after the RUNNING transition but
    before the try block, skipping the exit finally's RUNNING→IDLE transition → instance wedged
    in RUNNING. Fix: guards moved inside the try block.

No LLM or network connections required.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.exceptions import AgentTerminatedError, FallbackCompressionRequired
from agent_cascade.llm.schema import Message


# ============================================================================
# Shared helpers
# ============================================================================

def _make_pool_and_engine():
    """Mocked pool + real ExecutionEngine, mirroring TestExecutionEngineIterativeCompression."""
    from agent_cascade.execution_engine import ExecutionEngine

    pool = MagicMock()
    instance = MagicMock()
    compression_lock = MagicMock()
    compression_lock.__enter__ = MagicMock()
    compression_lock.__exit__ = MagicMock()
    instance._compression_lock = compression_lock
    instance._streaming_responses = []
    instance.instance_name = "test-agent"
    instance._force_compress_count = 0
    instance.compression_summary = None
    instance.latest_marker_index = -1

    # _is_terminal_stop() reads pool.stopped / pool._run_generation /
    # pool.is_instance_terminated(). Configure so it returns False by default.
    pool.stopped = False
    pool.is_instance_terminated.return_value = False
    pool._run_generation = 1

    pool.get_instance.return_value = instance
    history = [Message(role="system", content="sys")] + [
        Message(role="user", content=f"msg{i}") for i in range(20)
    ]
    pool.get_conversation.return_value = history
    pool.get_compression_target_set_from_conversation.return_value = (2, history[2:], -1)
    pool.slice_history_for_llm.return_value = history[2:]

    comp_chain = [{"max_input_tokens": 32768}]
    pool.api_router = MagicMock()
    pool.api_router.get_endpoint_chain.side_effect = (
        lambda agent_type, **kw: comp_chain if agent_type == "Compressor" else [{"max_input_tokens": 10000}]
    )

    comp_agent = MagicMock()
    comp_agent.system_message = "You are a compressor."
    pool.get_agent.return_value = comp_agent

    class Settings:
        retry_max_attempts = 2
        retry_base_delay = 0.1
        retry_max_delay = 1.0
        loop_min_chars = 4000
        loop_max_chars = 40960
        loop_char_run_enabled = True
        loop_char_run_limit = 129
        loop_max_chars_enabled = True
        loop_two_phase_enabled = False
        loop_suspicion_threshold = 7
        loop_confirm_required = 3
        loop_cooldown_feeds = 50

    pool.settings = Settings()

    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    return engine, pool, instance, history


def _make_template():
    template = MagicMock()
    template.llm_cfg = {"model": "test"}
    template.function_map = {}
    template.llm = MagicMock()
    template.llm.generate_cfg = {}
    return template


# ============================================================================
# B1 — Stop-check in the fallback-compression round loop
# ============================================================================

class TestFallbackCompressionStopCheck:
    """A user Stop during fallback compression must abort cleanly — no further
    compressor spawns, no spurious ContextWindowExceeded."""

    def test_stop_mid_compression_aborts_without_context_window_exceeded(self):
        """Stop fires while compress_context is running → next round check raises
        AgentTerminatedError (clean abort), NOT ContextWindowExceeded."""
        engine, pool, instance, history = _make_pool_and_engine()

        # Stop is pressed DURING round 1's compression call.
        def stopped_during_round(*args, **kwargs):
            pool.stopped = True
            return MagicMock(success=False, error="simulated failure")

        with patch.object(engine, "_execute_llm_call",
                          side_effect=FallbackCompressionRequired("test-agent", "Coder", "small-model")):
            with patch("agent_cascade.compression.core.compress_context", side_effect=stopped_during_round):
                gen = engine._execute_llm_call_with_retry(
                    instance, [Message(role="user", content="test")], _make_template(), []
                )
                with pytest.raises(AgentTerminatedError):
                    list(gen)

    def test_no_further_compress_after_stop(self):
        """compress_context must NOT be invoked again once the stop is detected."""
        engine, pool, instance, history = _make_pool_and_engine()

        compress_calls = []

        def stopped_during_round(*args, **kwargs):
            compress_calls.append(1)
            pool.stopped = True  # Stop lands mid-round-1
            return MagicMock(success=False, error="simulated failure")

        with patch.object(engine, "_execute_llm_call",
                          side_effect=FallbackCompressionRequired("test-agent", "Coder", "small-model")):
            with patch("agent_cascade.compression.core.compress_context", side_effect=stopped_during_round):
                gen = engine._execute_llm_call_with_retry(
                    instance, [Message(role="user", content="test")], _make_template(), []
                )
                with pytest.raises(AgentTerminatedError):
                    list(gen)

        assert len(compress_calls) == 1, (
            f"Expected exactly 1 compressor spawn before stop detection, got {len(compress_calls)}"
        )

    def test_stop_during_compress_context_propagates_immediately(self):
        """Stop lands DURING _compress_local (the invoker raises AgentTerminatedError
        on pool.stopped) → must propagate out of the round loop immediately: no
        additional rounds, no ContextWindowExceeded, no error-log swallow."""
        engine, pool, instance, history = _make_pool_and_engine()

        compress_calls = []

        def stop_mid_compression(*args, **kwargs):
            compress_calls.append(1)
            pool.stopped = True  # Stop lands while compress_context is executing
            raise AgentTerminatedError("Compressor_1")

        with patch.object(engine, "_execute_llm_call",
                          side_effect=FallbackCompressionRequired("test-agent", "Coder", "small-model")):
            with patch("agent_cascade.compression.core.compress_context", side_effect=stop_mid_compression):
                gen = engine._execute_llm_call_with_retry(
                    instance, [Message(role="user", content="test")], _make_template(), []
                )
                with pytest.raises(AgentTerminatedError):
                    list(gen)

        assert len(compress_calls) == 1, (
            f"AgentTerminatedError from inside compress_context must abort the round loop "
            f"immediately; expected 1 spawn, got {len(compress_calls)}"
        )

    def test_pre_existing_stop_never_spawns_compressor(self):
        """If the pool is already stopped when the FCR handler starts, zero compressor
        spawns occur (round-1 top-of-loop check)."""
        engine, pool, instance, history = _make_pool_and_engine()
        pool.stopped = True

        compress_calls = []

        def should_not_run(*args, **kwargs):
            compress_calls.append(1)
            return MagicMock(success=False, error="should never be reached")

        with patch.object(engine, "_execute_llm_call",
                          side_effect=FallbackCompressionRequired("test-agent", "Coder", "small-model")):
            with patch("agent_cascade.compression.core.compress_context", side_effect=should_not_run):
                gen = engine._execute_llm_call_with_retry(
                    instance, [Message(role="user", content="test")], _make_template(), []
                )
                with pytest.raises(AgentTerminatedError):
                    list(gen)

        assert len(compress_calls) == 0


# ============================================================================
# B2.1 — Compressor invoker closes its generator on stop-break
# ============================================================================

class _FakeCompressorInstance:
    """Minimal real AgentInstance so state transitions actually execute."""

    def __init__(self, name):
        self.instance = AgentInstance(
            instance_name=name,
            agent_class="Compressor",
            conversation=[],
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            latest_marker_index=-1,
        )
        self.name = name

    @property
    def state(self):
        return self.instance.state


class TestInvokerGeneratorCloseOnStop:
    """The compressor invoker must close engine.run()'s generator deterministically
    so the instance transitions RUNNING→IDLE even when the pool stops mid-run."""

    def _make_mock_pool(self, stopped):
        pool = MagicMock()
        pool.stopped = stopped
        pool.session_name = "TestCaller"
        # _ensure_compressor_loaded needs get_agent to return truthy
        comp_agent = MagicMock()
        comp_agent.llm.generate_cfg = {}
        pool.get_agent.return_value = comp_agent
        # _configure_compressor_instance is patched out, but keep these sane anyway
        template = MagicMock()
        template.llm.generate_cfg = {}
        pool.get_template.return_value = template
        pool.get_instance.return_value = None
        # finally block: agent_pool._execution._state_lock, instance_state, active_stack_remove
        pool._execution = MagicMock()
        pool.instance_state = {}
        pool.active_stack_remove = MagicMock()
        return pool

    def _invoke(self, pool, fake_instance, run_side_effect):
        """Run invoke_compression_agent with a mocked ExecutionEngine whose run()
        behaves like run_side_effect (a generator function)."""
        from agent_cascade.compression import agent_invoker

        engine = MagicMock()
        engine._create_system_agent.return_value = fake_instance.instance
        engine.run.side_effect = run_side_effect
        engine._telemetry.return_value = None

        # invoke_compression_agent lazily imports ExecutionEngine inside its body,
        # so patch at the source module (same pattern as TestCompressionRetryReuse).
        with patch("agent_cascade.execution_engine.ExecutionEngine") as mock_engine_cls:
            mock_engine_cls.return_value = engine
            with patch.object(agent_invoker, "_configure_compressor_instance"):
                return agent_invoker.invoke_compression_agent(
                    agent_pool=pool,
                    target_messages=[{"role": "user", "content": "hello"}],
                    caller_name="TestCaller",
                )

    def test_gen_closed_on_stop_break_and_instance_ends_idle(self):
        """Stop lands mid-run → invoker aborts with AgentTerminatedError AND the
        suspended run() generator is closed, driving RUNNING→IDLE via its finally.

        Note: an external reference is held to the generator so CPython refcount
        finalization cannot accidentally clean it up — only an explicit close()
        guarantees the exit finally runs (mirrors production where tracebacks and
        cross-thread transitions defer generator finalization)."""
        pool = self._make_mock_pool(stopped=False)
        fake_inst = _FakeCompressorInstance("Compressor_9001")
        close_calls = []
        held_gens = []  # Keep generators alive → refcount cleanup cannot fire

        def fake_run(comp_instance):
            def _g():
                # Mimic ExecutionEngine.run(): transition to RUNNING on start,
                # back to IDLE in an exit finally (which only runs if close() is called).
                comp_instance._transition(AgentState.RUNNING)
                try:
                    yield {"role": "assistant", "content": "tick"}
                    # User presses Stop while the compressor is generating.
                    pool.stopped = True
                    yield {"role": "assistant", "content": "tick2"}
                finally:
                    close_calls.append("exit-finally")
                    if comp_instance.state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
                        comp_instance._transition(AgentState.IDLE)

            g = _g()
            held_gens.append(g)
            return g

        with pytest.raises(AgentTerminatedError):
            self._invoke(pool, fake_inst, fake_run)

        # The exit finally inside the generator MUST have run (via gen.close()).
        assert close_calls == ["exit-finally"]
        # Instance must NOT be wedged in RUNNING.
        assert fake_inst.state is AgentState.IDLE

    def test_reentry_after_stopped_compressor_does_not_trip_l1_guard(self):
        """After a stopped compressor run, re-entering engine.run() must find the
        instance IDLE — i.e., no [BUG] L1 race guard RuntimeError."""
        pool = self._make_mock_pool(stopped=False)
        fake_inst = _FakeCompressorInstance("Compressor_9002")
        held_gens = []

        def fake_run(comp_instance):
            def _g():
                comp_instance._transition(AgentState.RUNNING)
                try:
                    yield {"role": "assistant", "content": "tick"}
                    pool.stopped = True  # Stop lands mid-generation
                    yield {"role": "assistant", "content": "tick2"}
                finally:
                    if comp_instance.state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
                        comp_instance._transition(AgentState.IDLE)

            g = _g()
            held_gens.append(g)
            return g

        with pytest.raises(AgentTerminatedError):
            self._invoke(pool, fake_inst, fake_run)

        # Simulate the incident's re-entry: engine.run() entry guard requires IDLE.
        # Before the fix this found RUNNING and raised the L1 race-guard error.
        assert fake_inst.state is AgentState.IDLE
        # Mirrors core.py's entry guard — must NOT raise.
        with fake_inst.instance._state_lock:
            if fake_inst.instance.state != AgentState.IDLE:
                raise RuntimeError(
                    f"[BUG] {fake_inst.name} entered engine.run() in state "
                    f"{fake_inst.instance.state.name} — should be IDLE. L1 race guard failed!"
                )

    def test_pre_existing_stop_never_invokes_compressor(self):
        """If the pool is already stopped when the invoker starts, the retry-loop
        stop-check aborts BEFORE any compressor invocation (B1 defense)."""
        pool = self._make_mock_pool(stopped=True)
        fake_inst = _FakeCompressorInstance("Compressor_9004")

        def should_not_run(comp_instance):
            raise AssertionError("engine.run() must not be invoked after a stop")
            yield  # pragma: no cover — makes this a generator

        with pytest.raises(AgentTerminatedError):
            self._invoke(pool, fake_inst, should_not_run)

    def test_normal_completion_leaves_generator_exhausted_not_broken(self):
        """Without a stop, the generator runs to completion and close() is a no-op;
        summary extraction still works end-to-end."""
        pool = self._make_mock_pool(stopped=False)
        fake_inst = _FakeCompressorInstance("Compressor_9003")
        marker = "--- END SUMMARY ---"

        def fake_run(comp_instance):
            comp_instance._transition(AgentState.RUNNING)
            try:
                msg = Message(role="assistant", content=f"Compressed context notes.\n{marker}")
                # Mimic _process_response(): final message lands in conversation.
                comp_instance.append_message(msg)
                yield ([msg], False)
            finally:
                if comp_instance.state in (AgentState.COMPLETING, AgentState.SLEEPING, AgentState.RUNNING):
                    comp_instance._transition(AgentState.IDLE)

        summary = self._invoke(pool, fake_inst, fake_run)

        assert summary.strip() == "Compressed context notes."
        assert fake_inst.state is AgentState.IDLE


# ============================================================================
# B3 — Pre-try early returns leave the instance IDLE
# ============================================================================

class TestPreTryEarlyReturnStateWedge:
    """engine.run()'s pre-try terminal-stop returns must pass through the exit
    finally so the instance ends IDLE, not wedged in RUNNING."""

    def _make_real_instance(self, name="wedge-test"):
        return AgentInstance(
            instance_name=name,
            agent_class="coder",
            conversation=[Message(role="system", content="sys")],
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            latest_marker_index=-1,
        )

    def _make_engine_for_run(self, pool):
        from agent_cascade.execution_engine import ExecutionEngine
        return ExecutionEngine(pool)

    def test_terminal_stop_before_slot_acquire_ends_idle(self):
        """Terminal stop at the first pre-slot guard → run() exits cleanly through
        the exit finally → instance is IDLE afterwards."""
        pool = MagicMock()
        pool.stopped = True  # Terminal stop active from the start
        pool._run_generation = 1
        pool.is_instance_terminated.return_value = False
        pool.telemetry = None
        pool.drain_queue = MagicMock()
        inst = self._make_real_instance()

        engine = self._make_engine_for_run(pool)
        engine._my_generation = 1

        # Should not raise, not yield anything, and leave the instance IDLE.
        yielded = list(engine.run(inst))
        assert yielded == []
        assert inst.state is AgentState.IDLE

    def test_terminal_stop_after_slot_acquire_ends_idle(self):
        """Terminal stop at the post-acquire guard → slot released, exit finally
        runs → instance is IDLE afterwards."""
        pool = MagicMock()
        pool.stopped = False
        pool._run_generation = 1
        pool.is_instance_terminated.return_value = False
        pool.telemetry = None
        pool.drain_queue = MagicMock()

        inst = self._make_real_instance()

        engine = self._make_engine_for_run(pool)
        engine._my_generation = 1

        acquire_calls = []

        def flip_stop_after_acquire(instance, context):
            acquire_calls.append(context)
            pool.stopped = True  # Stop lands between acquire and the second guard

        engine._acquire_slot_with_logging = flip_stop_after_acquire

        released = []
        engine._release_slot = lambda *a, **k: released.append(1)

        yielded = list(engine.run(inst))
        assert yielded == []
        assert acquire_calls == ["initial"]
        assert released, "Slot must be explicitly released on the post-acquire stop path"
        assert inst.state is AgentState.IDLE

    def test_l1_guard_does_not_fire_on_reentry_after_wedge_path(self):
        """Re-entering run() after a wedge-path exit must find IDLE — no L1 guard
        RuntimeError. This is the exact incident symptom."""
        pool = MagicMock()
        pool.stopped = True
        pool._run_generation = 1
        pool.is_instance_terminated.return_value = False
        pool.telemetry = None
        pool.drain_queue = MagicMock()
        inst = self._make_real_instance()

        engine = self._make_engine_for_run(pool)
        engine._my_generation = 1

        list(engine.run(inst))  # First entry exits via pre-try guard path
        assert inst.state is AgentState.IDLE

        # Second entry must pass the L1 guard without raising.
        list(engine.run(inst))
        assert inst.state is AgentState.IDLE
