"""Tests for wiring ``AUTO_SKILL_MIN_TURNS`` to the UI settings screen (todo 148).

Covers three seams of the feature:

1. The config handler (``config_handlers._handle_auto_skill_min_turns``) — happy path
   plus clamp-low / clamp-high validation (clamp range [1, 500]).
2. The ``PoolSettings.auto_skill_min_turns`` dataclass field — default + persistence
   round-trip (auto-persists via asdict/from_dict; no explicit pop needed).
3. The runtime gate at ``engine/core.py::_try_auto_skill_extension`` — proves the turn
   threshold is read LIVE from ``pool.settings`` (not the import-time constant), which is
   what makes the UI edit a live update rather than restart-required.

Related: plans/todo148_auto_skill_min_turns_ui_PLAN.md,
reports/todo148_auto_skill_min_turns_ui_wiring.md.
"""

from unittest.mock import MagicMock

from agent_cascade.agent_instance import PoolSettings


class TestHandlerAutoSkillMinTurns:
    """Validation of the ``auto_skill_min_turns`` config handler (clamp [1, 500])."""

    def _run_handler(self, value):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        pool = MagicMock()
        pool.settings = PoolSettings()
        handler = CONFIG_HANDLERS['auto_skill_min_turns']
        handler({'auto_skill_min_turns': value}, pool, [])
        return pool.settings.auto_skill_min_turns

    def test_handler_valid_value(self):
        assert self._run_handler(30) == 30

    def test_handler_clamps_low_to_one(self):
        assert self._run_handler(0) == 1
        assert self._run_handler(-5) == 1

    def test_handler_clamps_high_to_500(self):
        assert self._run_handler(9999) == 500


class TestPoolSettingsField:
    """The ``auto_skill_min_turns`` dataclass field default + persistence round-trip."""

    def test_default_matches_constant(self):
        from agent_cascade.settings import AUTO_SKILL_MIN_TURNS

        assert PoolSettings().auto_skill_min_turns == AUTO_SKILL_MIN_TURNS

    def test_from_dict_round_trip(self):
        ps = PoolSettings(auto_skill_min_turns=35)
        d = ps.to_dict()
        assert d['auto_skill_min_turns'] == 35
        restored = PoolSettings.from_dict(d)
        assert restored.auto_skill_min_turns == 35

    def test_from_dict_missing_key_defaults_to_constant(self):
        # Old pool_settings.json files lack the key → from_dict fills it with the default.
        from agent_cascade.settings import AUTO_SKILL_MIN_TURNS

        d = PoolSettings().to_dict()
        d.pop('auto_skill_min_turns', None)
        ps = PoolSettings.from_dict(d)
        assert ps.auto_skill_min_turns == AUTO_SKILL_MIN_TURNS


class TestLiveGate:
    """Prove core.py::_try_auto_skill_extension reads the LIVE pool.settings value.

    The turn gate lives inside the shared helper ``_auto_skill_gates_met`` (turn check at
    core.py:264) and must respect ``pool.settings.auto_skill_min_turns`` — not the
    import-time constant — so a UI edit takes effect without a restart. We detect whether
    execution PASSES the gate by checking that the compression lock is entered in
    ``_try_auto_skill_extension`` (core.py:315), which only happens after the gate succeeds.
    ``_auto_skill_proposed`` is kept False so the turn gate is the sole pass/fail decider.
    """

    def _make_engine(self, pool):
        from agent_cascade.execution_engine import ExecutionEngine

        engine = MagicMock(spec=ExecutionEngine)
        engine.pool = pool
        # Bind the REAL gate method so its live-read logic actually runs.
        engine._try_auto_skill_extension = ExecutionEngine._try_auto_skill_extension.__get__(engine, ExecutionEngine)
        # The turn gate moved into this helper; a bare spec-MagicMock would auto-mock it to truthy
        # and bypass the gate entirely (see .agent_lessons/magicmock-spec-hides-new-methods.md).
        engine._auto_skill_gates_met = ExecutionEngine._auto_skill_gates_met.__get__(engine, ExecutionEngine)
        return engine

    def _gate_passed(self, min_turns, current_turn):
        """Drive the gate with a LIVE ``auto_skill_min_turns``; report whether it passed."""
        pool = MagicMock()
        pool.skill_manager = MagicMock()          # not None → passes gate 1 (core.py:256, in _auto_skill_gates_met)
        ps = PoolSettings()
        ps.auto_skill_enabled = True             # passes gate 2 (core.py:259); enabled by default anyway
        ps.default_load_skill_mode = 'AUTO'      # passes gate 3 (core.py:261); not LOAD_SKILL_NONE ('NONE')
        ps.auto_skill_min_turns = min_turns      # LIVE value under test
        pool.settings = ps

        inst = MagicMock()
        inst._current_turn = current_turn
        # Flag check now lives inside _auto_skill_gates_met; keep it False so the turn gate is the
        # ONLY thing deciding pass/fail. (The one-shot flag is orthogonal to the live-read under test.)
        inst._auto_skill_proposed = False
        # inst._compression_lock is a MagicMock → `with` triggers __enter__ only if core.py:315 was reached.

        engine = self._make_engine(pool)
        engine._try_auto_skill_extension(inst, [], [], None)
        return inst._compression_lock.__enter__.called

    def test_live_gate_passes_when_turn_exceeds_live_value(self):
        # Live min_turns=5, current_turn=6 → 6 > 5 so the gate PASSES (reaches the lock).
        # If the import-time constant (20) were read instead, 6 <= 20 would FAIL the gate.
        assert self._gate_passed(5, 6) is True

    def test_live_gate_fails_when_turn_below_live_value(self):
        # Live min_turns=100, current_turn=6 → 6 <= 100 so the gate FAILS (never reaches the lock).
        assert self._gate_passed(100, 6) is False

    def test_live_gate_strictly_greater_boundary(self):
        # Strictly-greater semantics: current_turn == min_turns still fails (must EXCEED threshold).
        assert self._gate_passed(5, 5) is False


class TestManagerGateLiveRead:
    """Prove skills/manager.py::auto_skill_qualifies respects the LIVE ``min_turns`` value
    threaded from core.py, not the import-time constant.

    This is the SECOND runtime consumer of AUTO_SKILL_MIN_TURNS (the first is the gate at
    core.py::_try_auto_skill_extension). Both must agree on the live value — otherwise
    lowering the threshold via the UI would be a silent no-op (core passes, but manager still
    blocks on the constant 20). We isolate to JUST the turn gate by stubbing
    ``load_full_instructions`` (skill-creator always loadable) and the prompt builder.
    """

    def _qualifies(self, turns_effectuated, min_turns=None):
        import threading
        from unittest.mock import patch
        from agent_cascade.skills.manager import SkillManager

        mgr = SkillManager()
        # Isolate to the turn gate: skill-creator always "loadable" (sentinel creator).
        mgr.load_full_instructions = lambda name, count_load=False: 'CREATOR' if name == 'skill-creator' else None

        class _Inst:
            def __init__(self):
                self._compression_lock = threading.RLock()
                self._auto_skill_proposed = False

        with patch('agent_cascade.skills.manager._build_auto_skill_reflection_prompt', return_value='PROMPT'):
            return mgr.auto_skill_qualifies(_Inst(), turns_effectuated, min_turns=min_turns)

    def test_low_live_threshold_passes_below_constant(self):
        # Constant AUTO_SKILL_MIN_TURNS is 20. Live threshold 5 → turn 6 must PASS (6 > 5),
        # even though 6 <= 20 would fail against the constant.
        assert self._qualifies(6, min_turns=5) == 'PROMPT'

    def test_high_live_threshold_blocks_above_constant(self):
        # Live threshold 100 → turn 30 must FAIL (30 <= 100), even though 30 > 20 would pass
        # against the constant.
        assert self._qualifies(30, min_turns=100) is None

    def test_none_falls_back_to_constant(self):
        # min_turns=None → import-time constant (20). turn 21 passes, turn 20 fails.
        from agent_cascade.settings import AUTO_SKILL_MIN_TURNS
        assert self._qualifies(AUTO_SKILL_MIN_TURNS + 1) == 'PROMPT'
        assert self._qualifies(AUTO_SKILL_MIN_TURNS) is None
