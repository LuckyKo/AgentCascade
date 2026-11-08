"""Focused tests for the cost/token-budget feature — Slices 3 & 4.

Slice 3: the ``call_agent`` completion message is enriched with elapsed time and the
child's total tokens INCLUDING all its sub-agents (walked via ``_child_instances``).

Slice 4: ``system_info`` surfaces a compact self-scoped Budget/Cost block — present only
when this instance carries a ``token_budget`` or its endpoint is priced, absent otherwise.

Explicit fakes (no MagicMock auto-attr) so a block that is wrongly absent can never
double-false-pass: the present-test asserts the exact rendered lines, the absent-test
asserts the block header is gone while a sibling line still renders.
"""

import re

from agent_cascade import child_runner as child_runner_mod
from agent_cascade.child_runner import _format_elapsed, _format_result, _sum_subtree_tokens, run_child_core
from agent_cascade.tools.custom.system_info import SystemInfo


# ── Shared explicit fakes ───────────────────────────────────────────────────


class _FakeInst:
    """Stands in for AgentInstance: only the fields the token-walk reads."""

    def __init__(self, name, children=None):
        self.instance_name = name
        self._child_instances = list(children or [])
        self.is_terminated = False


class _FakeTelemetry:
    """Stands in for the telemetry accumulator reads (Slice 2 API).

    ``totals``: name -> total tokens. ``cost``: None (all unpriced) or name -> float
    (missing names are unpriced -> None), mirroring get_instance_cost's None semantics.
    """

    def __init__(self, totals=None, cost=None):
        self._totals = totals or {}
        self._cost = cost

    @staticmethod
    def _key(inst):
        return inst if isinstance(inst, str) else getattr(inst, 'instance_name', '')

    def get_instance_total_tokens(self, inst):
        return self._totals.get(self._key(inst), 0)

    def get_instance_cost(self, inst):
        if self._cost is None:
            return None
        return self._cost.get(self._key(inst))


class _FakePool:
    """Minimal pool for the Slice-3 token walk + status check."""

    def __init__(self, by_name, telemetry):
        self._by_name = by_name
        self.telemetry = telemetry
        self.stopped = False
        self.terminated_instances = set()
        self._halted_instances = set()
        self._compression_halted = set()

    def get_instance(self, name):
        return self._by_name.get(name)


class _FakeEngine:
    def __init__(self, inst, conv):
        self._inst = inst
        self._conv = conv

    def _create_and_run_agent(self, *args, **kwargs):
        return self._inst, self._conv


# ── Slice 3 — elapsed formatting ────────────────────────────────────────────


def test_format_elapsed_under_minute():
    assert _format_elapsed(0.4) == '0s'
    assert _format_elapsed(11.6) == '12s'
    assert _format_elapsed(59.4) == '59s'


def test_format_elapsed_minutes():
    assert _format_elapsed(60) == '1m 0s'
    assert _format_elapsed(192) == '3m 12s'
    assert _format_elapsed(3600) == '60m 0s'


# ── Slice 3 — completion message shape ──────────────────────────────────────


def test_format_result_completed_enriched():
    out = _format_result('code_worker', 'done', elapsed_s=192.3, total_tokens=42190)
    assert out == "[Agent 'code_worker' Completed in 3m 12s, tokens: 42,190]:\ndone"


def test_format_result_legacy_shape_without_metrics():
    # Omitting elapsed/tokens keeps the exact legacy Completed] suffix (no callers break).
    out = _format_result('code_worker', 'done')
    assert out == "[Agent 'code_worker' Completed]:\ndone"


# ── Slice 3 — sub-tree token summation (incl. descendants) ──────────────────


def test_sum_subtree_tokens_includes_descendants():
    # root -> (a -> c), b ; every node counted exactly once.
    root = _FakeInst('root', children=['a', 'b'])
    a = _FakeInst('a', children=['c'])
    b = _FakeInst('b', children=[])
    c = _FakeInst('c', children=[])
    pool = _FakePool(
        {'root': root, 'a': a, 'b': b, 'c': c},
        _FakeTelemetry(totals={'root': 10, 'a': 20, 'b': 30, 'c': 40}),
    )
    assert _sum_subtree_tokens(pool, root) == 100


def test_sum_subtree_tokens_cycle_safe():
    # back-edges (root->root, a->root) must not double-count or loop.
    root = _FakeInst('root', children=['root', 'a'])
    a = _FakeInst('a', children=['root'])
    pool = _FakePool({'root': root, 'a': a}, _FakeTelemetry(totals={'root': 10, 'a': 20}))
    assert _sum_subtree_tokens(pool, root) == 30


def test_sum_subtree_tokens_none_when_no_telemetry():
    pool = _FakePool({'root': _FakeInst('root')}, telemetry=None)
    assert _sum_subtree_tokens(pool, _FakeInst('root')) == 0


def test_run_child_core_includes_subagent_tokens(monkeypatch):
    """The completion message's token total is the child + every descendant, and
    elapsed is present — both surfaced through the real run_child_core path."""
    child = _FakeInst('code_worker', children=['sub1'])
    sub1 = _FakeInst('sub1', children=['sub2'])
    sub2 = _FakeInst('sub2', children=[])
    pool = _FakePool(
        {'code_worker': child, 'sub1': sub1, 'sub2': sub2},
        _FakeTelemetry(totals={'code_worker': 1000, 'sub1': 2190, 'sub2': 0}),
    )
    engine = _FakeEngine(child, [{'role': 'assistant', 'content': 'done'}])
    monkeypatch.setattr(child_runner_mod, 'extract_instance_output', lambda *a, **k: 'done')

    out = run_child_core(engine, pool, 'coder', 'code_worker', {}, 'Main', 1)

    assert out.startswith("[Agent 'code_worker' Completed in ")
    # 1000 + 2190 + 0 (sub2) = 3190, thousands-separated.
    assert re.search(r'Completed in (\d+s|\d+m \d+s), tokens: 3,190\]:\ndone$', out), out


# ── Slice 4 — system_info Budget/Cost self-info ─────────────────────────────


class _SIInst:
    """Instance view for the system_info budget block."""

    def __init__(self, name, _current_turn, max_turns, token_budget, budget_unit='turns'):
        self.instance_name = name
        self._current_turn = _current_turn
        self.max_turns = max_turns
        self.token_budget = token_budget
        self.budget_unit = budget_unit


class _SIPool:
    """Pool mirror of test_system_info_server._FakePool, plus telemetry + instance."""

    def __init__(self, inst, telemetry):
        self.server_info = None
        self.instance_conversations = {}
        self.operation_manager = None
        self.telemetry = telemetry
        self._by_name = {inst.instance_name: inst}
        self._conv = []

    def get_instance(self, name):
        return self._by_name.get(name)

    def get_conversation(self, name):
        return list(self._conv)


def _slice4_call(inst, telemetry):
    tool = SystemInfo(agent_pool=_SIPool(inst, telemetry), agent_name=inst.instance_name)
    return tool.call('')


def test_system_info_budget_block_present_when_configured():
    inst = _SIInst('me', _current_turn=3, max_turns=50, token_budget=100000, budget_unit='tokens')
    telem = _FakeTelemetry(totals={'me': 42190}, cost={'me': 0.012345})
    out = _slice4_call(inst, telem)

    assert 'Budget / Cost:' in out
    assert 'consumed: 42,190 tokens' in out
    assert 'budget: 100,000 tokens (budget_unit=tokens)' in out
    assert 'remaining: 57,810 tokens' in out
    assert 'cost: $0.012345' in out


def test_system_info_budget_block_priced_only_no_remaining():
    inst = _SIInst('me', _current_turn=2, max_turns=50, token_budget=0)
    telem = _FakeTelemetry(totals={'me': 700}, cost={'me': 0.001})
    out = _slice4_call(inst, telem)

    assert 'Budget / Cost:' in out
    assert 'consumed: 700 tokens' in out
    assert 'cost: $0.001' in out
    # No token budget -> no budget/remaining lines.
    assert 'budget:' not in out
    assert 'remaining:' not in out


def test_system_info_budget_block_absent_when_unconfigured():
    # token_budget 0 + unpriced (cost None) -> whole block absent, but the tool still runs.
    inst = _SIInst('me', _current_turn=1, max_turns=50, token_budget=0, budget_unit='turns')
    telem = _FakeTelemetry(totals={'me': 500}, cost=None)
    out = _slice4_call(inst, telem)

    assert 'Budget / Cost:' not in out
    # Sibling line still present -> proves we're in the right region and the block is
    # gated, not that the run crashed into a different code path.
    assert 'Current Turn:' in out
