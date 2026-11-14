"""Focused tests for the tokens-mode budget unit (BUG_0315).

``call_agent(token_budget=N)`` documents ``N`` as a **generated (completion-only)**
token budget, so the run-loop warnings must be driven by the ``generated_tokens``
accumulator, never by ``total_tokens`` (prompt + completion). Prompt re-reads
happen on every turn, so a total-based budget is spent before the agent produces
anything meaningful.

The regression test is ``test_prompt_heavy_run_never_warns``: it fails against the
old total-token read (total 200k vs budget 10k fires 50%/90%/final immediately) and
passes once the read is generated-only.

Fakes are explicit (no MagicMock) and the telemetry fake keeps ``total`` and
``generated`` as separate dicts, so "read the wrong view" cannot pass silently.
"""

from agent_cascade.budget_words import budget_message
from agent_cascade.engine.run_loop import RunLoopMixin
from agent_cascade.llm.schema import USER, Message
from agent_cascade.settings import SOFT_TURN_LIMIT_WARNING_INTERVAL

BUDGET = 10_000


class _FakeTelemetry:
    """Per-instance accumulator reads; total and generated are independent."""

    def __init__(self, total=None, generated=None, cost=None):
        self._total = total or {}
        self._generated = generated or {}
        self._cost = cost

    @staticmethod
    def _key(inst):
        return inst if isinstance(inst, str) else getattr(inst, 'instance_name', '')

    def get_instance_total_tokens(self, inst):
        return self._total.get(self._key(inst), 0)

    def get_instance_generated_tokens(self, inst):
        return self._generated.get(self._key(inst), 0)

    def get_instance_cost(self, inst):
        if self._cost is None:
            return None
        return self._cost.get(self._key(inst))


class _FakeTemplate:
    def __init__(self, tools):
        self.function_map = {name: None for name in tools}


class _FakePool:
    """Only the hard-cut tool-disable path reads the pool (get_template)."""

    def __init__(self, tools=None):
        self._tools = tools or []

    def get_template(self, agent_class):
        return _FakeTemplate(self._tools)


class _FakeInst:
    """Instance fields the tokens-mode warnings read/write."""

    def __init__(self, name='worker', budget=BUDGET, baseline=0, agent_class='coder'):
        self.instance_name = name
        self._token_budget = budget
        self._token_baseline = baseline
        self._token_warned_50 = False
        self._token_warned_90 = False
        self._token_final_fired = False
        self._token_last_warn_past = 0
        self.agent_class = agent_class


class _Engine(RunLoopMixin):
    """Duck-typed engine: the mixin's methods only need these three seams."""

    def __init__(self, telemetry, tools=None):
        self.telemetry = telemetry
        self.pool = _FakePool(tools)
        self.appended = []

    def _telemetry(self):
        return self.telemetry

    def _append_and_log_to_llm(self, instance, msg, llm_messages, *, lock_held=False):
        self.appended.append(msg.content)
        llm_messages.append(msg)

    @staticmethod
    def _make_user_message(text):
        return Message(role=USER, content=text)


def _run(engine, inst, soft_turn_limit=False):
    """Invoke the real tokens-mode warning routine; return (disabled, past, texts)."""
    llm_messages = []
    disabled, last_past = RunLoopMixin._run_token_budget_warnings(
        engine, inst, llm_messages, soft_turn_limit)
    return disabled, last_past, [m.content for m in llm_messages]


# ── Regression: prompt tokens must not spend the budget ─────────────────────


def test_prompt_heavy_run_never_warns():
    # A realistic shape: ~200k prompt tokens re-read, 2k generated, 10k budget.
    # Total-based accounting would fire 50%/90%/final on the very first check.
    telem = _FakeTelemetry(total={'worker': 200_000}, generated={'worker': 2_000})
    engine, inst = _Engine(telem), _FakeInst()

    disabled, _, messages = _run(engine, inst)

    assert messages == []
    assert disabled is False
    assert inst._token_warned_50 is False and inst._token_warned_90 is False
    assert inst._token_final_fired is False


def test_baseline_is_subtracted_from_generated():
    # 10k generated before this run started, 2k during it, 5k budget -> 3k left.
    telem = _FakeTelemetry(total={'worker': 900_000}, generated={'worker': 12_000})
    engine, inst = _Engine(telem), _FakeInst(budget=5_000, baseline=10_000)

    disabled, _, messages = _run(engine, inst)

    assert messages == []
    assert disabled is False


def test_consumed_helper_is_generated_delta():
    telem = _FakeTelemetry(total={'worker': 500_000}, generated={'worker': 7_500})
    engine, inst = _Engine(telem), _FakeInst(baseline=2_500)

    assert RunLoopMixin._tokens_consumed_this_run(engine, inst) == 5_000


# ── Threshold progression: each warning fires exactly once ──────────────────


def test_halfway_warns_once_between_fifty_and_ninety():
    # 60% of the budget generated: past the 50% mark, short of the 90% mark.
    telem = _FakeTelemetry(total={'worker': 300_000}, generated={'worker': int(BUDGET * 0.6)})
    engine, inst = _Engine(telem), _FakeInst()

    _, _, messages = _run(engine, inst)
    assert messages == [budget_message('halfway', 'tokens', remaining=BUDGET - int(BUDGET * 0.6), total=BUDGET)]

    # Second turn at the same generated count: no duplicate warning.
    _, _, messages = _run(engine, inst)
    assert messages == []


def test_ninety_percent_warns_once_after_halfway():
    telem = _FakeTelemetry(total={'worker': 300_000}, generated={'worker': int(BUDGET * 0.95)})
    engine, inst = _Engine(telem), _FakeInst()

    _, _, messages = _run(engine, inst)

    remaining = BUDGET - int(BUDGET * 0.95)
    assert messages == [
        budget_message('halfway', 'tokens', remaining=remaining, total=BUDGET),
        budget_message('approaching', 'tokens', remaining=remaining, total=BUDGET),
    ]
    # Both flags are latched: a second check adds nothing.
    _, _, messages = _run(engine, inst)
    assert messages == []


def test_jump_past_both_thresholds_fires_both_in_one_check():
    telem = _FakeTelemetry(total={'worker': 300_000}, generated={'worker': BUDGET})
    engine, inst = _Engine(telem), _FakeInst()

    _, _, messages = _run(engine, inst)

    assert messages[:2] == [
        budget_message('halfway', 'tokens', remaining=0, total=BUDGET),
        budget_message('approaching', 'tokens', remaining=0, total=BUDGET),
    ]
    assert budget_message('final_hard', 'tokens') in messages


# ── Hard cut: exhausted generated budget disables every tool ────────────────


def test_hard_cut_disables_all_tools_at_generated_budget():
    tools = ['run_command', 'write_file']
    telem = _FakeTelemetry(total={'worker': 900_000}, generated={'worker': BUDGET})
    engine, inst = _Engine(telem, tools), _FakeInst()

    disabled, _, messages = _run(engine, inst)

    assert disabled is True
    assert messages[-1] == budget_message('final_hard', 'tokens')
    assert inst._generate_cfg_override['disabled_tools'] == tools
    # One-shot: a later turn does not re-fire the final warning nor re-disable.
    inst2 = inst
    disabled_again, _, messages = _run(engine, inst2)
    assert disabled_again is False
    assert messages == []


def test_soft_mode_keeps_tools_and_nudges_over_limit():
    # Soft mode never disables tools; past the budget it nudges per interval.
    past = SOFT_TURN_LIMIT_WARNING_INTERVAL
    telem = _FakeTelemetry(total={'worker': 900_000}, generated={'worker': BUDGET + past})
    engine, inst = _Engine(telem, ['run_command']), _FakeInst()

    disabled, last_past, messages = _run(engine, inst, soft_turn_limit=True)

    assert disabled is False
    assert not hasattr(inst, '_generate_cfg_override')
    assert budget_message('final_soft', 'tokens') in messages
    assert budget_message('over_limit', 'tokens', past=past, total=BUDGET) in messages
    assert last_past == past


# ── Degraded telemetry ──────────────────────────────────────────────────────


def test_telemetry_unavailable_is_a_no_op():
    engine, inst = _Engine(None), _FakeInst()

    disabled, last_past, messages = _run(engine, inst)

    assert messages == []
    assert disabled is False
    assert last_past == 0


def test_zero_budget_short_circuits():
    # Defensive: the routine is only called in tokens mode, but must be inert if
    # the instance field is missing/0 rather than treating it as "exhausted".
    telem = _FakeTelemetry(total={'worker': 10}, generated={'worker': 10})
    engine, inst = _Engine(telem), _FakeInst(budget=0)

    disabled, _, messages = _run(engine, inst)

    assert messages == []
    assert disabled is False
