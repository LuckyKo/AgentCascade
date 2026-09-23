"""E2E loop test for the memory-hint feature (plan §8).

Modelled on ``TestInLoopTrigger`` in ``tests/test_skill_generation.py:942``: it drives the
REAL ``ExecutionEngine.run()`` with a stubbed LLM and verifies that, when the feature is
enabled, a turn's assistant text produces a hint that rides the **tool-warning** queue (NO
user-message injection — plan §0 / R1), that the run completes normally, and that the
trigger never mutates the turn budget.

The stub LLM is a *generator* with ``.close()`` (the engine's Phase-3 ``finally`` calls it),
replies are numbered by a per-call counter (NOT ``len(msgs)``), and every injected message
is counted in conversation-length assertions.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.llm.schema import ASSISTANT, USER, Message


def _write_lesson(vault: Path, rel_path: str, name: str, description: str, body: str) -> None:
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntags: [test]\n---\n{body}\n",
        encoding='utf-8',
    )


class TestMemoryHintE2E:
    """Drive the real run loop and assert the hint flows via the tool-warning queue."""

    @staticmethod
    def _make_inst(max_turns):
        from agent_cascade.agent_instance import AgentInstance
        inst = AgentInstance.__new__(AgentInstance)
        inst.instance_name = 'w'
        inst.agent_class = 'test_agent'
        inst.conversation = [Message(role=USER, content='task')]
        inst._cached_messages = list(inst.conversation)
        inst._cached_llm_messages = list(inst.conversation)
        inst.max_turns = max_turns
        inst.state = AgentState.IDLE  # run() transitions IDLE→RUNNING itself
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
        inst._streaming_responses = []
        # Tool-warning queue (dataclass field, agent_instance.py:373) — the ONLY hint
        # delivery path. Must exist on a __new__ instance or _queue_tool_warning no-ops.
        inst._tool_warnings = []
        # Memory-hint state (normally set by __init__ field defaults).
        inst._memories_read = set()
        inst._recently_hinted = {}
        inst._last_memory_hint_turn = -1
        # Skill-hint state (feature: skills-in-memory-hints).
        inst._recently_skill_hinted = {}
        inst._last_skill_hint_turn = -1
        return inst

    @staticmethod
    def _conv_contents(conv):
        return [m.content if isinstance(m, Message) else m.get('content', '') for m in conv]

    @staticmethod
    def _wait_for_hint(inst, timeout=3.0):
        """Block until the daemon worker delivers a hint (or timeout).

        The hint rides _tool_warnings and is only *drained* when a tool result follows;
        since this harness runs no tools, we wait for the queue entry directly. This keeps
        the test deterministic without depending on wall-clock scheduling.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if inst._tool_warnings:
                return True
            time.sleep(0.02)
        return bool(inst._tool_warnings)

    @staticmethod
    def _make_pool(tmp_path, max_turns=2, enabled=True, natural_end_at=None, skill_manager=None):
        """Build a real ExecutionEngine + instance wired to a stubbed LLM.

        Returns (engine, inst, pool, run) where ``run()`` drives the generator to
        completion and returns it. The memory-hint manager is REAL (started as a daemon
        thread); only the LLM and turn-machinery guards are stubbed. ``skill_manager``
        injects a skill matcher for the skills-in-memory-hints sub-pipeline (None = off).
        """
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.memory_hint.manager import MemoryHintManager

        # A real vault with one strongly-relevant lesson for the query text below.
        v = tmp_path / 'proj' / '.agent_lessons'
        v.mkdir(parents=True)
        # Body deliberately overlaps the fake-LLM reply text ("debugging a compression
        # hang in the engine loop") so it is a CLEAR winner under the self-calibrating
        # gate (top1 above the adaptive floor; as the only doc, top2 = 0 → gap OK).
        _write_lesson(v, 'compression-debug.md', 'Compression Debug',
                      'How to debug compression hangs in the engine loop daemon thread lock ordering',
                      'Debugging a compression hang in the engine loop: check the daemon '
                      'thread and lock ordering first.')

        template = MagicMock()
        template.function_map = {'tool_a': None}

        pool = MagicMock()
        pool.settings.auto_skill_enabled = False
        pool.settings.default_load_skill_mode = 'NONE'
        pool.settings.tail_sync_check_enabled = False
        pool.get_template.return_value = template
        pool.is_instance_terminated.return_value = False
        pool.has_pending.return_value = False
        pool.has_messages.return_value = False
        pool.drain_queue.return_value = []

        # Real logger keeps the JSONL delta-sync honest (a MagicMock stays truthy).
        from agent_cascade.logger import AgentInstanceLogger
        log_inst = AgentInstanceLogger('test_agent', 'w', str(tmp_path), log_path=str(tmp_path / 'w.jsonl'))
        pool.get_logger.return_value = log_inst

        # Settings the manager reads live each cycle. No memory_hint_threshold key:
        # 0/absent = pure adaptive gate (EWMA floor + specificity gap).
        pool.llm_cfg = {
            'memory_hint_enabled': enabled,
            'memory_hint_max_entries': 3,
            'memory_hint_cooldown_seconds': 600,
            'memory_hint_query_chars': 1000,
        }

        # REAL manager: daemon worker + real vault index. get_instance resolves the fake.
        om = SimpleNamespace(base_dir=str(v.parent), extra_work_folders_ro=[], extra_work_folders_rw=[])
        pool.operation_manager = om
        # Skill sub-pipeline (feature: skills-in-memory-hints): None by default.
        pool.skill_manager = skill_manager
        inst = TestMemoryHintE2E._make_inst(max_turns)
        pool.get_instance.return_value = inst

        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()  # build the index synchronously before the run starts
        mgr.start()          # daemon worker (never blocks the main loop)
        pool.memory_hint_manager = mgr

        engine = ExecutionEngine(pool)

        # Stub turn machinery: one assistant message per LLM call, no tools.
        def fake_setup_turn(instance):
            return list(instance.conversation), [Message(role=USER, content='task')], []

        engine._setup_turn = MagicMock(side_effect=fake_setup_turn)
        engine._pre_llm_checks = MagicMock(return_value=False)
        engine._check_stop_conditions = MagicMock(return_value=False)
        engine._is_suspended_by_compression = MagicMock(return_value=False)
        engine._is_terminal_stop = MagicMock(return_value=False)
        engine._acquire_slot_with_logging = MagicMock(return_value=None)
        engine._check_stream_termination = MagicMock(return_value=None)

        # Stub LLM: a GENERATOR (has .close()) yielding None (streaming tick) then the
        # turn's assistant message. Replies numbered by a per-call counter, NOT len(msgs).
        _llm_call_count = {'n': 0}

        def fake_llm(inst_, msgs):
            _llm_call_count['n'] += 1
            yield None
            # Reply text is deliberately domain-specific so it strongly matches the
            # "Compression Debug" lesson (clear winner under the adaptive gate).
            # 'reply N' keeps replies distinguishable for layout assertions.
            yield Message(role=ASSISTANT,
                          content=f"reply {_llm_call_count['n']} debugging a compression hang in the engine loop")

        engine._call_llm_with_injection = MagicMock(side_effect=lambda inst_, msgs: fake_llm(inst_, msgs))
        # No tools executed → each turn is a no-tool answer (loop exits at Phase 5).
        engine._execute_detected_tools = MagicMock(return_value=False)

        # Natural-end driver: False on check N (genuine completion), True elsewhere.
        _nend = max_turns if natural_end_at is None else natural_end_at
        _ptc_calls = {'n': 0}

        def _post_turn_checks_driver(*a, **k):
            _ptc_calls['n'] += 1
            return _ptc_calls['n'] != _nend

        engine._post_turn_checks = MagicMock(side_effect=_post_turn_checks_driver)

        def run():
            gen = engine.run(inst)
            for _ in gen:
                pass
            return gen

        yield engine, inst, pool, run, mgr

    # ------------------------------------------------------------------ #
    # 1. Enabled → hint delivered via tool-warning queue (NO user injection)
    # ------------------------------------------------------------------ #

    def test_hint_delivered_via_tool_warning_not_user(self, tmp_path):
        """When enabled, the turn's text produces a [MEMORY HINT] on _tool_warnings, and
        NO [MEMORY HINT] message is ever injected into the conversation as a user message."""
        engine, inst, pool, run, mgr = self._make_pool(tmp_path, max_turns=2).__next__()
        run()

        # Wait for the daemon worker to deliver (no tool result follows to drain it).
        assert self._wait_for_hint(inst), 'expected a memory hint on _tool_warnings'
        # The hint landed on the tool-warning queue (the ONLY delivery path — plan §0/R1).
        hint = inst._tool_warnings[0]
        assert '[MEMORY HINT]' in hint
        assert 'compression-debug.md' in hint

        # NO user-message injection: no [MEMORY HINT] appears anywhere in the conversation.
        for content in self._conv_contents(inst.conversation):
            assert '[MEMORY HINT]' not in str(content), \
                f'memory hint must never be injected as a message, found in: {content!r}'

    # ------------------------------------------------------------------ #
    # 2. Disabled → no hint (feature OFF by default)
    # ------------------------------------------------------------------ #

    def test_disabled_no_hint(self, tmp_path):
        """memory_hint_enabled=False → the manager never hints even on a strong match."""
        engine, inst, pool, run, mgr = self._make_pool(tmp_path, max_turns=2, enabled=False).__next__()
        run()
        time.sleep(0.3)  # settle: give the worker a chance (it must NOT hint when disabled)
        assert inst._tool_warnings == [], 'disabled feature must not queue any hint'

    # ------------------------------------------------------------------ #
    # 3. Normal completion: the run finishes and the budget is NOT mutated
    # ------------------------------------------------------------------ #

    def test_normal_completion_budget_not_mutated(self, tmp_path):
        """The trigger is best-effort and never touches the turn budget: max_turns stays
        unchanged and the agent still completes naturally (no auto-skill extension)."""
        engine, inst, pool, run, mgr = self._make_pool(tmp_path, max_turns=2).__next__()
        before = inst.max_turns
        run()

        assert inst.max_turns == before, 'memory-hint trigger must not mutate the turn budget'
        # No auto-skill extension was triggered (that path is independent of hints).
        assert inst._auto_skill_proposed is False
        # The final reply is present in the conversation → normal completion.
        contents = self._conv_contents(inst.conversation)
        assert any('reply 1' in str(c) for c in contents)

    # ------------------------------------------------------------------ #
    # 4. Conversation-length accounting: every injected message is counted
    # ------------------------------------------------------------------ #

    def test_conversation_length_counts_all_messages(self, tmp_path):
        """Exact conversation layout for max_turns=2 (natural end at turn 2).

        Layout (verified against the run loop):
          [0] task                 (initial)
          [1] reply 1              (turn 1 assistant)
          [2] halfway warning      (turns_50pct==2 fires at start of turn 2)
          [3] final-turn warning   (turns_available==1, max_turns!=1)
          [4] reply 2              (turn 2 assistant — natural completion)
        = 5 messages. The memory hint is NOT one of these (it rides _tool_warnings).
        """
        engine, inst, pool, run, mgr = self._make_pool(tmp_path, max_turns=2).__next__()
        run()
        conv = inst.conversation
        assert len(conv) == 5, f'expected exactly 5 messages, got {len(conv)}: {self._conv_contents(conv)}'
        # The hint must NOT have inflated the conversation.
        assert sum(1 for c in self._conv_contents(conv) if '[MEMORY HINT]' in str(c)) == 0

    # ------------------------------------------------------------------ #
    # 5. Dedup across turns: a read memory is not re-hinted (cooldown/read-set)
    # ------------------------------------------------------------------ #

    def test_read_memory_not_rehinted(self, tmp_path):
        """If the agent already read the matched lesson, it is excluded from hints."""
        engine, inst, pool, run, mgr = self._make_pool(tmp_path, max_turns=2).__next__()
        with inst._compression_lock:
            inst._memories_read.add('compression-debug.md')
        run()
        time.sleep(0.3)  # settle: give the worker a chance (it must NOT hint an already-read memory)
        assert inst._tool_warnings == [], 'an already-read memory must not be re-hinted'

    # ------------------------------------------------------------------ #
    # 6. Skill suggestion rides the same tool-warning queue (skills-in-memory-hints)
    # ------------------------------------------------------------------ #

    def test_skill_suggestion_rides_tool_warning_queue(self, tmp_path):
        """A turn whose text matches a registered skill delivers the skill line on
        _tool_warnings via the SAME single-message path as memory hints."""
        from unittest.mock import MagicMock
        sm = MagicMock()
        # Score comfortably above SKILL_HINT_MIN_SCORE (0.15).
        sm.match_skills.return_value = [('docker-best-practices', 0.30)]
        engine, inst, pool, run, mgr = self._make_pool(
            tmp_path, max_turns=2, skill_manager=sm).__next__()
        run()

        # Wait for the daemon worker to deliver (no tool result follows to drain it).
        assert self._wait_for_hint(inst), 'expected a hint on _tool_warnings'
        hint = inst._tool_warnings[0]
        assert '[MEMORY HINT]' in hint
        assert 'Skills you may want to load' in hint
        assert 'docker-best-practices' in hint
        # The memory lesson still rides the SAME single message (combined hint).
        assert 'compression-debug.md' in hint


class TestMemoryHintPoolStopResume:
    """E2E regression (todo162): the memory-hint worker must SURVIVE a real pool
    stop→resume cycle. This drives a REAL ``AgentPool`` through the exact resume
    sequence ws_handlers.py performs (``stopped = True`` → ``stopped = False``) and
    asserts hints keep flowing afterwards.

    Pre-fix: the value=True branch called ``memory_hint_manager.stop()`` (worker dies),
    and the value=False branch never re-called ``start()`` — so the second job was never
    processed and this test FAILED. Post-fix: resume revives the worker → PASSES.
    """

    @staticmethod
    def _make_pool_instance():
        """A minimal AgentInstance (via __new__) exposing only what the hint path needs."""
        inst = AgentInstance.__new__(AgentInstance)
        inst.instance_name = 'w'
        inst.agent_class = 'test_agent'
        inst.state = AgentState.RUNNING  # not SLEEPING → hint is allowed to fire
        inst._compression_lock = threading.RLock()
        inst._tool_warnings = []
        inst._memories_read = set()
        inst._recently_hinted = {}
        inst._last_memory_hint_turn = -1
        inst._recently_skill_hinted = {}
        inst._last_skill_hint_turn = -1
        # parent_instance: the idle checker reads it for every registered instance; a
        # __new__ instance lacks dataclass defaults, so set it to keep the (unrelated)
        # idle-checker thread from logging AttributeError noise during the test.
        inst.parent_instance = None
        return inst

    @staticmethod
    def _wait_for_delivery(inst, timeout=4.0):
        """Bounded polling for a tool-warning delivery (no fixed sleeps → no flakes)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if inst._tool_warnings:
                return True
            time.sleep(0.02)
        return bool(inst._tool_warnings)

    def test_memory_hint_survives_pool_stop_resume_cycle(self, tmp_path):
        """Submit a job (delivered), drive pool.stopped=True then =False (the exact resume
        sequence), submit another job → it must STILL be delivered. FAILS pre-fix."""
        from agent_cascade.agent_pool import AgentPool

        # A real vault with two DISJOINT-topic lessons. Job 1 matches compression-debug;
        # job 2 (post stop→resume) matches skill-creator-notes — a lesson NOT hinted by
        # job 1, so the per-memory cooldown can't suppress it and delivery is purely a
        # function of worker liveness (the regression under test).
        v = tmp_path / 'proj' / '.agent_lessons'
        v.mkdir(parents=True)
        _write_lesson(v, 'compression-debug.md', 'Compression Debug',
                      'How to debug compression hangs in the engine loop daemon thread lock ordering',
                      'Debugging a compression hang in the engine loop: check the daemon '
                      'thread and lock ordering first.')
        _write_lesson(v, 'skill-creator-notes.md', 'Skill Creator Notes',
                      'Notes on writing reusable skills with frontmatter tags body section',
                      'Skills need a name description tags and a clear body section.')

        llm_cfg = {
            'model': 'mock',
            'api_base': 'http://127.0.0.1:9/v1',
            'model_server': 'http://127.0.0.1:9/v1',
            'api_key': 'EMPTY',
            'memory_hint_enabled': True,
            'memory_hint_max_entries': 3,
            'memory_hint_cooldown_seconds': 600,
            'memory_hint_query_chars': 1000,
        }
        # Empty agents dir → _discover_agents is a no-op; no real LLM/agents involved.
        pool = AgentPool(llm_cfg, agents_dir=str(tmp_path / 'agents'))

        # Point vault discovery at our temp vault (base_dir + extra RW folders).
        om = SimpleNamespace(base_dir=str(v.parent), extra_work_folders_ro=[], extra_work_folders_rw=[])
        pool.operation_manager = om

        # A real (hermetic) SkillManager so the skill sub-pipeline runs on real code.
        from tests.conftest import make_hermetic_skill_manager
        pool.skill_manager = make_hermetic_skill_manager(tmp_path)

        # Stop the idle checker BEFORE registering our minimal instance so its loop
        # never touches it (keeps the test focused on the hint worker + clean logs).
        try:
            pool._idle.stop()
        except Exception:  # noqa: BLE001 — best-effort; irrelevant to the assertion
            pass

        # Register a live instance the manager can resolve via pool.get_instance().
        inst = self._make_pool_instance()
        with pool._pool_lock:
            pool.instances[inst.instance_name] = inst

        mgr = pool.memory_hint_manager
        assert mgr is not None, 'memory_hint_enabled=True must create the manager in __init__'
        # Ensure the worker + index are up (mirrors __init__ when enabled).
        mgr.rescan_vaults()
        mgr.start()

        query1 = 'debugging a compression hang in the engine loop daemon thread lock ordering'

        # ── Job 1: before any stop/resume — must be delivered. ────────────────
        mgr.submit(inst.instance_name, query1, inst.agent_class, turn=1)
        assert self._wait_for_delivery(inst), 'job 1 (pre-cycle) must be delivered'
        assert '[MEMORY HINT]' in inst._tool_warnings[0]

        # ── The EXACT resume sequence ws_handlers.py:441/449 performs. ─────────
        pool.stopped = True    # → memory_hint_manager.stop() (worker dies)
        pool.stopped = False   # → resume: must re-start the worker (the fix)

        # Give the stopped worker a moment to observe its sentinel and exit, so the
        # respawn assertion below is meaningful. Bounded — no fixed long sleep.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and mgr._worker is not None and mgr._worker.is_alive():
            time.sleep(0.01)

        # ── Job 2: after the cycle — must STILL be delivered (the regression). ─
        # A DIFFERENT query matching a lesson job 1 never hinted, so cooldown can't mask
        # worker liveness. Pre-fix the dead worker delivers nothing → this assertion fails.
        inst._tool_warnings.clear()
        query2 = 'writing reusable skills with frontmatter tags body section'
        mgr.submit(inst.instance_name, query2, inst.agent_class, turn=2)
        assert self._wait_for_delivery(inst), (
            'job 2 (post stop→resume) was never delivered — the memory-hint worker died '
            'on resume and was not restarted (pre-fix signature: start() after stop() no-op)'
        )
        assert '[MEMORY HINT]' in inst._tool_warnings[0]

        # Teardown: stop background threads so they don't linger across tests.
        try:
            pool.stopped = True
            mgr.stop()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
