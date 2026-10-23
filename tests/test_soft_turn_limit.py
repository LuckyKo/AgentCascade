"""Tests for the Soft Turn Limit toggle (todo.md:107 / BUG_0141).

Hard mode (default) is covered by the existing turn-budget suites. This file pins the
new behavior:

- OFF: unchanged hard stop at ``max_turns`` + the normal turn-limit notice.
- ON: the run continues past ``max_turns``, the final hard-budget turn keeps its tools,
  a wrap-up warning is injected every 5 over-limit turns, and a natural finish does not
  append the "Turn limit reached" notice.
- ON still hard-stops at the absolute safety ceiling from settings.py.
- The setting is wired through PoolSettings, config handlers, state serialization, and
  the Web UI.

Harness: imports the real-run() fake pool/LLM helpers from
``test_turn_limit_warning_user_messages.py`` (same family as
``test_auto_continue_turn_budget.py``).
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent_cascade.agent_instance import PoolSettings
from agent_cascade.llm.schema import ASSISTANT, Message
from tests.test_turn_limit_warning_user_messages import (_FakePool, _ScriptedLLM, _build_engine,
                                                         _count_in_conversation, _find_warning_in_call,
                                                         _make_instance, _tool_call_msg)

KEY = 'soft_turn_limit'
ABSOLUTE_CAP_ENV = 'AGENT_CASCADE_SOFT_TURN_LIMIT_ABSOLUTE_MAX'


class _RecordingLLM(_ScriptedLLM):
    """Scripted LLM that also snapshots the per-call disabled-tools override.

    ``disabled_tools_by_call[i]`` is the list the engine set on
    ``instance._generate_cfg_override`` just before LLM call *i* (empty when tools are
    enabled). This is how the tests prove soft mode does not disable tools on the last
    hard-budget turn.
    """

    def __init__(self, script):
        super().__init__(script)
        self.disabled_tools_by_call = []

    def __call__(self, instance, llm_messages):
        override = getattr(instance, '_generate_cfg_override', None) or {}
        self.disabled_tools_by_call.append(list(override.get('disabled_tools') or []))
        yield from super().__call__(instance, llm_messages)


def _run_soft(max_turns, script, soft):
    """Drive the real run() with soft mode ON/OFF and return the recording LLM + instance."""
    # The fake template must know every per-turn noop name, including over-limit turns.
    pool = _FakePool(len(script) + 1)
    if soft:
        pool.settings.soft_turn_limit = True
    pool.settings.completion_classifier_enabled = False  # keep natural-end turns offline
    llm = _RecordingLLM(script)
    instance = _make_instance(max_turns)
    engine = _build_engine(pool, instance, llm)
    list(engine.run(instance))
    return llm, instance


def _clean_msg(text='Task complete. Final answer.'):
    return Message(role=ASSISTANT, content=text)


# ── Core behavior ───────────────────────────────────────────────────────────


class TestHardModeUnchanged:
    def test_default_off_stops_at_max_turns_with_notice(self):
        # max_turns=2, both turns are tool calls, soft mode defaults OFF.
        llm, instance = _run_soft(2, [_tool_call_msg(0), _tool_call_msg(1)], soft=False)

        assert len(llm.calls) == 2
        assert _count_in_conversation(instance, 'Turn limit reached') == 1
        # The final hard-budget turn still disables tools (legacy behavior).
        assert llm.disabled_tools_by_call[1], 'hard mode must disable tools on the final turn'


class TestSoftModeContinues:
    def test_continues_past_budget_warns_every_five_and_ends_naturally(self):
        # max_turns=2. Turns 1-11 are tool calls; the run is over budget from turn 3.
        # Turns 7 and 12 are the 5th and 10th over-limit turns -> two wrap-up warnings.
        # Turn 12 is a clean answer and ends the run naturally.
        script = [_tool_call_msg(i) for i in range(11)] + [_clean_msg()]
        llm, instance = _run_soft(2, script, soft=True)

        assert len(llm.calls) == 12, 'soft mode must keep running past max_turns'
        assert _count_in_conversation(instance, 'You are over the turn limit') == 2
        for call_index in (6, 11):
            warn = _find_warning_in_call(llm.calls[call_index], 'You are over the turn limit')
            assert warn is not None, f'over-limit warning missing on call {call_index} (5th/10th past max)'
            assert 'please wrap up' in warn.content
        # Natural finish, not a budget stop: no "results may be incomplete" notice.
        assert _count_in_conversation(instance, 'Turn limit reached') == 0

    def test_final_hard_budget_turn_keeps_tools_enabled(self):
        script = [_tool_call_msg(0), _tool_call_msg(1), _clean_msg()]
        llm, instance = _run_soft(2, script, soft=True)

        assert len(llm.calls) == 3
        assert not any(llm.disabled_tools_by_call), \
            'soft mode must never disable tools (that would force the hard stop)'
        assert _find_warning_in_call(llm.calls[1], 'Final turn') is not None
        # The soft-mode wording must not claim tools were disabled.
        final_warn = _find_warning_in_call(llm.calls[1], 'Final turn')
        assert 'tools stay enabled' in final_warn.content
        assert 'tools have been disabled' not in final_warn.content


class TestAbsoluteSafetyCeiling:
    def test_soft_mode_hard_stops_at_absolute_cap(self, monkeypatch):
        # Lower the ceiling for the test: 4 effectuated turns total (2 hard + 2 over).
        monkeypatch.setattr('agent_cascade.engine.core.SOFT_TURN_LIMIT_ABSOLUTE_MAX', 4)
        script = [_tool_call_msg(i) for i in range(4)]
        llm, instance = _run_soft(2, script, soft=True)

        assert len(llm.calls) == 4, 'absolute ceiling must stop an endless tool-call loop'
        assert _count_in_conversation(instance, 'Turn limit reached') == 1, \
            'hitting the safety ceiling must still tell the caller why the run ended'


# ── Settings / UI wiring ────────────────────────────────────────────────────


class TestSoftTurnLimitWiring:
    def test_pool_settings_default_is_false(self):
        assert PoolSettings().soft_turn_limit is False
        # from_dict is the pool_settings.json load path; unknown/missing keys are ignored.
        assert PoolSettings.from_dict({'soft_turn_limit': True}).soft_turn_limit is True
        assert PoolSettings.from_dict({}).soft_turn_limit is False

    def test_in_pool_settings_keys(self):
        from agent_cascade.config_handlers import POOL_SETTINGS_KEYS

        assert KEY in POOL_SETTINGS_KEYS, f'{KEY} missing from POOL_SETTINGS_KEYS (save trigger)'

    def test_in_non_llm_keys(self):
        from agent_cascade.constants import NON_LLM_KEYS

        assert KEY in NON_LLM_KEYS, f'{KEY} must not be forwarded as an LLM API parameter'

    def test_handler_registered_and_live_mutation(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        assert KEY in CONFIG_HANDLERS, f'missing handler for {KEY}'
        fake_pool = SimpleNamespace(settings=SimpleNamespace(soft_turn_limit=False))
        CONFIG_HANDLERS[KEY]({KEY: True}, fake_pool, [])
        assert fake_pool.settings.soft_turn_limit is True

    def test_router_apply_live_mutation(self):
        from agent_cascade.config_handlers import ConfigUpdateRouter

        fake_pool = SimpleNamespace(settings=SimpleNamespace(soft_turn_limit=False))
        router = ConfigUpdateRouter(fake_pool, [])
        asyncio.new_event_loop().run_until_complete(router.apply({KEY: True}))
        assert fake_pool.settings.soft_turn_limit is True

    def test_in_both_state_builder_blocks(self):
        src = Path('agent_cascade/api_integration_pkg/state_builder.py').read_text(encoding='utf-8')
        assert src.count(f"'{KEY}'") >= 2, f'{KEY}: both pool_settings serialization blocks must carry it'

    def test_frontend_seams(self):
        app_js = Path('web_ui/app.js').read_text(encoding='utf-8')
        index_html = Path('web_ui/index.html').read_text(encoding='utf-8')

        assert app_js.count(f"'{KEY}'") >= 4, f'{KEY}: app.js needs map + save + restore + getGenerateCfg'
        assert "id: '#setting-soft-turn-limit'" in app_js
        assert 'id="setting-soft-turn-limit"' in index_html


def test_absolute_cap_constant_is_env_overridable():
    """The safety ceiling is a settings.py constant (env-overridable), not a magic number."""
    src = Path('agent_cascade/settings.py').read_text(encoding='utf-8')
    assert 'SOFT_TURN_LIMIT_ABSOLUTE_MAX' in src
    assert ABSOLUTE_CAP_ENV in src
