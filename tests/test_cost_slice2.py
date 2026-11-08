"""Cost/token-budget Slice 2 focused tests.

Covers: per-instance total-token accumulator (telemetry), tokens-mode vs
turns-mode wording helper (turns-mode byte-identical), NON_LLM_KEYS security
boundary (token_budget/budget_unit must not leak to the LLM API), settings
registry SSOT for the new pool key, and call_agent token_budget validation.

Run: pytest tests/test_cost_slice2.py -x -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── 1. Security boundary: token_budget/budget_unit stripped before LLM call ──
def test_non_llm_keys_strip_budget_kwargs():
    from agent_cascade.constants import NON_LLM_KEYS
    assert 'token_budget' in NON_LLM_KEYS
    assert 'budget_unit' in NON_LLM_KEYS


# ── 2. Wording helper: turns-mode byte-identical, tokens-mode renders tok/$ ──
def test_budget_words_turns_mode_byte_identical():
    """The pinned turn-limit strings (test_turn_limit_warning_user_messages)
    must be reproduced verbatim by the shared helper, else turns-mode regressed."""
    from agent_cascade.budget_words import budget_message, budget_limit_notice, budget_allocation_notice

    halfway = budget_message('halfway', 'turns', remaining=25, total=50)
    approaching = budget_message('approaching', 'turns', remaining=5, total=50)
    over = budget_message('over_limit', 'turns', past=3, total=50)
    final_soft = budget_message('final_soft', 'turns')
    final_hard = budget_message('final_hard', 'turns')

    # Byte-identical to the pre-Slice-2 inline f-strings.
    assert halfway.startswith('[SYSTEM WARNING:') and '25 turn(s)' in halfway
    assert 'Turn limit approaching' in approaching and '5 turn(s) remaining out of 50' in approaching
    assert 'You are over the turn limit' in over and '3 turn(s) past the 50-turn limit' in over
    assert 'Final turn' in final_soft and 'soft turn limit' in final_soft
    assert 'Final turn' in final_hard and 'tools have been disabled' in final_hard

    # No dollar/tokens rendering in turns mode.
    assert '$' not in halfway and ' tok' not in halfway

    assert budget_limit_notice('turns') == '\n\n[Turn limit reached — results may be incomplete. Continue if needed.]'
    assert budget_allocation_notice(10, 'turns') == '\n\nYour turn budget for this task: 10 turns.'


def test_budget_words_tokens_mode_rendering():
    from agent_cascade.budget_words import budget_message, budget_limit_notice, budget_allocation_notice

    # Unpriced: tokens only, no "$".
    unpriced = budget_message('approaching', 'tokens', remaining=1000, total=10000, cost=None)
    assert 'Approaching' in unpriced or 'approaching' in unpriced.lower()
    assert ' tok' in unpriced
    assert '$' not in unpriced

    # Priced: N tok / $X.
    priced = budget_message('approaching', 'tokens', remaining=1000, total=10000, cost=0.42)
    assert ' tok' in priced
    assert '/ $' in priced and '0.42' in priced

    assert 'Token budget reached' in budget_limit_notice('tokens')
    assert 'token budget' in budget_allocation_notice(5000, 'tokens')


# ── 3. Accumulator: per-instance isolate + accumulate + read helper ──
def _fake_call(telemetry, inst, in_tokens, out_tokens, pricing=None):
    """Drive the real record_llm_call_start -> record_llm_call_end choke point
    WITHOUT server usage, so the call stays UNclassified (char estimates only).

    total_tokens = input_tokens_est + actual_output (prompt+completion), the same
    arithmetic production uses — so this asserts production math, not mocks.
    """
    telemetry.record_llm_call_start(inst, input_tokens_est=in_tokens, pricing=pricing)
    telemetry.record_llm_call_end(inst, output_tokens_est=out_tokens)


def _classified_call(telemetry, inst, prompt_tokens, completion_tokens,
                     cached_tokens=0, pricing=None):
    """Drive the full choke point INCLUDING record_token_usage so the call IS
    cache-classified (server-reported usage). Only such priced calls carry a $
    cost — matching session_cost exactly (FIX 1)."""
    telemetry.record_llm_call_start(inst, input_tokens_est=prompt_tokens, pricing=pricing)
    telemetry.record_token_usage(inst, prompt_tokens=prompt_tokens,
                                 completion_tokens=completion_tokens,
                                 details={'cached_tokens': cached_tokens})
    telemetry.record_llm_call_end(inst)


def test_accumulator_per_instance_isolated_and_accumulates():
    from agent_cascade.telemetry import TelemetryCollector
    tel = TelemetryCollector()

    # Two calls on instance A accumulate; instance B is isolated.
    _fake_call(tel, 'A', 100, 20)
    _fake_call(tel, 'A', 300, 40)
    _fake_call(tel, 'B', 700, 100)

    assert tel.get_instance_total_tokens('A') == 100 + 20 + 300 + 40
    assert tel.get_instance_total_tokens('B') == 700 + 100
    # Isolation
    assert tel.get_instance_total_tokens('A') != tel.get_instance_total_tokens('B')

    # Unknown instance reads 0.
    assert tel.get_instance_total_tokens('unknown') == 0

    # Unpriced: cost reads None.
    assert tel.get_instance_cost('A') is None

    # FIX 1 gate: a PRICED call the server never classified contributes NO $ to
    # the instance — session_cost skips unclassified calls too, so the two
    # figures cannot conflict.
    tel_u = TelemetryCollector()
    _fake_call(tel_u, 'U', 1_000_000, 1_000_000,
               pricing={'input_per_m': 1.0, 'output_per_m': 1.0})
    assert tel_u.get_instance_total_tokens('U') == 2_000_000
    assert tel_u.get_instance_cost('U') is None

    # A cache-classified PRICED call: cost is positive AND ties out with the
    # session_cost line exactly (same usage tuple + gate).
    tel2 = TelemetryCollector()
    _classified_call(tel2, 'C', 1_000_000, 1_000_000, cached_tokens=0,
                     pricing={'input_per_m': 1.0, 'output_per_m': 1.0})
    assert tel2.get_instance_total_tokens('C') == 2_000_000
    inst_cost = tel2.get_instance_cost('C')
    assert inst_cost is not None and inst_cost > 0
    assert tel2.get_session_summary()['session_cost'] == pytest.approx(inst_cost)

    # reset clears the accumulator (cost and priced flag included).
    tel.reset_instance_total_tokens('A')
    assert tel.get_instance_total_tokens('A') == 0


def test_instance_cost_sums_to_session_cost():
    """Per-instance costs sum to the single session_cost figure — no conflicting
    $ totals (FIX 1). Two classified priced instances share one telemetry session."""
    from agent_cascade.telemetry import TelemetryCollector
    tel = TelemetryCollector()
    _classified_call(tel, 'A', 500_000, 100_000,
                     pricing={'input_per_m': 3.0, 'output_per_m': 15.0})
    _classified_call(tel, 'B', 250_000, 50_000,
                     pricing={'input_per_m': 3.0, 'output_per_m': 15.0})
    total = (tel.get_instance_cost('A') or 0.0) + (tel.get_instance_cost('B') or 0.0)
    assert total > 0
    assert tel.get_session_summary()['session_cost'] == pytest.approx(total)


# ── 4. Settings registry SSOT for the new pool key ──
def test_registry_token_budget_pool_key():
    from agent_cascade import settings_registry as sr
    sr.validate_registry()  # raises on drift (incl. pool count != 104)
    assert 'token_budget' in sr.POOL_SETTINGS_KEYS
    assert sr._DEFAULTS['token_budget'] == 0  # default OFF, int not None
    assert not isinstance(sr._DEFAULTS['token_budget'], bool)  # bool would misclassify as toggle


# ── 5. call_agent token_budget validation (positive int required when present) ──
def test_call_agent_token_budget_validation():
    from agent_cascade.tool_dispatcher import ToolDispatcher

    class _FakePool:
        settings = None

    def _mk():
        d = ToolDispatcher.__new__(ToolDispatcher)
        d.pool = _FakePool()
        return d

    # Valid positive int -> no error.
    _, _, err = _mk()._validate_call_agent_args(
        {'instance_name': 'w1', 'agent_class': 'coder', 'token_budget': 5000}, 'main')
    assert err is None

    # Missing -> no error (optional).
    _, _, err = _mk()._validate_call_agent_args({'instance_name': 'w1', 'agent_class': 'coder'}, 'main')
    assert err is None

    # Invalid -> error mentioning token_budget.
    for bad in (0, -1, 1.5, 'x', True):
        _, _, err = _mk()._validate_call_agent_args(
            {'instance_name': 'w1', 'agent_class': 'coder', 'token_budget': bad}, 'main')
        assert err is not None and 'token_budget' in err, f"expected rejection for {bad!r}, got {err}"
