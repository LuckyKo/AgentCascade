"""Unit tests for the completion classifier (BUG_0048).

The classifier is a thin decision-model wrapper on the EXISTING malformed-turn detector
path. These tests pin the four properties that matter:

1. Happy path, both verdicts — 'incomplete' reaches the generic auto-continue retry path
   (returns True), 'completed' does not (returns False).
2. Fail-open, parametrized — post raises / 'none' choice / no router → ``None``, no raise,
   so the turn loop is never blocked or altered by an API problem.
3. Cost guard — only the structurally-AMBIGUOUS turn is classified; empty-output and
   reasoning-only turns never fire HTTP (the structural detector wins first).
4. Toggle gate — ``completion_classifier_enabled=False`` means zero HTTP and byte-identical
   pre-fix behavior; ``auto_continue=False`` likewise. Plus the setting round-trips through
   config_handlers + PoolSettings + state_builder.

HTTP is mocked at ``agent_cascade.skills.selector.requests.post``; no real network.

Run: pytest tests/test_completion_classifier.py -v
"""

import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.agent_instance import AgentInstance  # noqa: E402
from agent_cascade.api_router_pkg.endpoints import APIEndpoint  # noqa: E402
from agent_cascade.api_router_pkg.router import APIRouter  # noqa: E402
from agent_cascade.engine.core import ExecutionEngine, _last_assistant_text  # noqa: E402
from agent_cascade.llm.schema import ASSISTANT, Message  # noqa: E402
from agent_cascade.skills.selector import classify_completion  # noqa: E402

_POST = 'agent_cascade.skills.selector.requests.post'


# ── Test doubles (mirror tests/test_skill_selector.py) ────────────────────────


def _make_router(pairs, priorities=None):
    """Real APIRouter populated with endpoints by (id, name, base, model)."""
    router = APIRouter(default_llm_cfg={})
    for eid, name, base, model in pairs:
        router.add_endpoint(APIEndpoint(id=eid, name=name, api_base=base, api_key='k', model=model))
    if priorities is not None:
        router.agent_priorities['skill_selector'] = priorities
    return router


def _post_verdict(choice):
    """A successful requests.post mock answering the classifier's 'verdict' question."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {'answers': {'verdict': {'choice': choice}}}
    return resp


def _pool_with_router(router=None):
    """A plain fake pool — NOT a MagicMock (see the toggle-gate note below)."""
    pool = MagicMock()
    pool.api_router = router
    return pool


def _make_instance() -> AgentInstance:
    now = time.monotonic()
    return AgentInstance(
        instance_name='TestAgent',
        agent_class='coder',
        conversation=[],
        created_at=now,
        last_activity=now,
        latest_marker_index=-1,
    )


def _text_only_msg(text='Now I will search the repo for the failing test.') -> Message:
    """The BUG_0048 shape: text, NO tool call → structurally ambiguous."""
    return Message(role=ASSISTANT, content=text)


def _reasoning_only_msg() -> Message:
    return Message(role=ASSISTANT, content='', reasoning_content='let me think about this carefully')


def _empty_output_msg() -> Message:
    return Message(role=ASSISTANT, content='')


class _FakePool:
    """Fake pool exposing only what the engine method under test touches.

    Deliberately NOT a MagicMock: auto-created attributes are truthy, which would make
    ``_is_terminal_stop`` (a bool-OR of three attrs) always True and defeat the gate tests.
    """

    def __init__(self, *, auto_continue=True, classifier_enabled=True, router=None):
        settings_attrs = {
            'auto_continue': auto_continue,
            'SOFT_CONTINUE_NUDGE_ENABLED': False,
            'tail_sync_check_enabled': False,
        }
        if classifier_enabled is not None:
            settings_attrs['completion_classifier_enabled'] = classifier_enabled
        self.settings = type('Settings', (), settings_attrs)()
        self.api_router = router
        self.stopped = False
        self._run_generation = 0
        self.telemetry = None
        self._rollback_calls = []

    def _rollback_instance(self, inst_name, pop_count):
        self._rollback_calls.append(pop_count)

    def _mark_activity(self, inst_name):
        pass

    def is_instance_terminated(self, inst_name):
        return False

    def get_logger(self, inst_name, agent_class):
        return MagicMock()


class _Engine:
    """Bind the real engine method under test to a fake pool (no full construction)."""

    def __init__(self, pool):
        self.pool = pool
        self._my_generation = 0
        self._check_and_handle_truncation = ExecutionEngine._check_and_handle_truncation.__get__(self, _Engine)
        self._is_terminal_stop = ExecutionEngine._is_terminal_stop.__get__(self, _Engine)
        self._telemetry = lambda: None
        self._rebuild_working_set = lambda messages, llm_messages, inst_name: (
            messages.clear(), llm_messages.clear())


def _engine(pool):
    return _Engine(pool)


class _StatePool:
    """Pool double rich enough for the REAL state_builder serializers.

    ``settings`` is a genuine ``PoolSettings`` so the serialization is exercised with
    real attribute values rather than auto-mocks (a MagicMock would make every
    ``getattr(ps, ..., default)`` return a truthy mock and prove nothing).
    """

    def __init__(self, settings):
        self.settings = settings
        self.instances = {}
        self.templates = {}
        self.stopped = False
        self.api_router = None
        self.telemetry = None
        self.operation_manager = None
        self._enable_async_shell_console_window = False

    def get_instance(self, name):
        return self.instances.get(name)

    def get_conversation(self, name):
        inst = self.instances.get(name)
        return list(inst.conversation) if inst else []

    def get_queue_messages(self, name):
        return []

    def is_paused(self):
        return False

    def list_pending_approvals(self):
        return []

    def is_instance_terminated(self, name):
        return False

    def get_template(self, agent_class):
        return None

    def has_messages(self, name):
        return False

    def is_instance_halted(self, name):
        return False

    def slice_history_for_llm(self, messages):
        return messages


def _run(engine, instance, turn_output, is_truncated=False):
    messages, llm_messages, response = [], [], list(turn_output)
    return engine._check_and_handle_truncation(
        is_truncated, turn_output, instance, instance.instance_name,
        messages, llm_messages, response)


# ── 1. Happy path, both verdicts ─────────────────────────────────────────────


class TestHappyPath:

    def test_incomplete_verdict_triggers_generic_retry(self):
        """'incomplete' → the opaque tag lands in the EXISTING generic retry path."""
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('incomplete')) as mock_post:
            result = _run(engine, instance, [_text_only_msg()])

        assert result is True
        mock_post.assert_called_once()
        # generic path: rollback of the turn + rebuild (no reasoning-only soft continue)
        assert engine.pool._rollback_calls == [1]
        assert instance._reasoning_only_soft_attempts == 0

    def test_completed_verdict_is_current_behavior(self):
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('completed')) as mock_post:
            result = _run(engine, instance, [_text_only_msg()])

        assert result is False  # current behavior: normal completion
        mock_post.assert_called_once()
        assert engine.pool._rollback_calls == []

    def test_classifier_asks_verdict_question_with_3s_timeout(self):
        """The request envelope is the shared one, with the classifier's own question key."""
        import json

        from agent_cascade.skills import selector as sel
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', '')],
                              priorities=['ep1'])
        pool = _pool_with_router(router)
        with patch(_POST, return_value=_post_verdict('incomplete')) as mock_post:
            assert classify_completion(pool, 'hello') == 'incomplete'

        payload = json.loads(mock_post.call_args.kwargs['data'].decode('utf-8'))
        assert set(payload['questions']) == {'verdict'}
        assert set(payload['questions']['verdict']['criteria']) == {'completed', 'incomplete'}
        assert mock_post.call_args[1]['timeout'] == sel._COMPLETION_CLASSIFIER_TIMEOUT_SECONDS
        assert sel._COMPLETION_CLASSIFIER_TIMEOUT_SECONDS == 3.0
        # Empty endpoint model falls back to the shared module default.
        assert payload['model'] == sel.DEFAULT_SKILL_SELECTOR_MODEL


# ── 2. Fail-open, parametrized ───────────────────────────────────────────────


class TestFailOpen:

    @pytest.mark.parametrize('mode', ['post_raises', 'none_choice', 'unknown_choice', 'no_router'])
    def test_failures_return_none_without_raising(self, mode):
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1']) if mode != 'no_router' else None
        pool = _pool_with_router(router)
        with patch(_POST) as mock_post:
            if mode == 'post_raises':
                mock_post.side_effect = RuntimeError('boom')
            elif mode == 'none_choice':
                mock_post.return_value = _post_verdict('none')
            elif mode == 'unknown_choice':
                mock_post.return_value = _post_verdict('banana')
            result = classify_completion(pool, 'some text')

        assert result is None

    def test_fail_open_leaves_turn_behavior_unchanged(self):
        """A classifier outage must not block or alter the turn: returns False, no rollback."""
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(router=router))
        instance = _make_instance()
        with patch(_POST, side_effect=RuntimeError('endpoint down')):
            result = _run(engine, instance, [_text_only_msg()])

        assert result is False
        assert engine.pool._rollback_calls == []


# ── Log-prefix ownership (POLISH finding #1) ─────────────────────────────────


class TestLogPrefixOwnership:
    """A shared helper must never attribute ITS caller's failure to the wrong subsystem.

    ``resolve_endpoints`` / ``ask_decision_model`` are used by BOTH the skill selector
    and the completion classifier. Without a threaded prefix, a classifier-side HTTP
    outage logs a ``[SKILL-SELECTOR]`` line and sends a maintainer to the wrong place.
    """

    def test_classifier_failure_logs_under_completion_classifier(self):
        from agent_cascade.skills import selector as sel
        router = _make_router([('ep1', 'A', 'https://a.example/v1/systemone', 'm')],
                              priorities=['ep1'])
        with patch(_POST, side_effect=RuntimeError('down')), \
                patch.object(sel, 'logger') as mock_logger:
            assert classify_completion(_pool_with_router(router), 'hello') is None

        logged = ' '.join(str(c) for c in mock_logger.debug.call_args_list)
        assert 'COMPLETION-CLASSIFIER' in logged
        assert 'SKILL-SELECTOR' not in logged, 'classifier failure must not be attributed to skill selection'

    def test_skill_selector_failure_keeps_its_own_prefix(self):
        """Behavior-identical: the existing ApiSelector path still logs [SKILL-SELECTOR]."""
        from agent_cascade.skills import selector as sel
        manager = MagicMock()
        manager._keyword_match.return_value = [('alpha', 1.0)]
        # Real dicts: a MagicMock metadata value would break json.dumps inside the
        # envelope and short-circuit before the endpoint call this test targets.
        manager.get_skill_metadata.return_value = {'description': 'a skill', 'triggers': []}
        # manager.pool must be the POOL (it owns .api_router), not the router itself —
        # ApiSelector._resolve_endpoints passes manager.pool straight to resolve_endpoints.
        manager.pool = _pool_with_router(
            _make_router([('ep1', 'A', 'https://a.example/v1/systemone', 'm')], priorities=['ep1']))
        with patch(_POST, side_effect=RuntimeError('down')), \
                patch.object(sel, 'logger') as mock_logger:
            assert sel.ApiSelector(manager).select('q', False, None) is None

        logged = ' '.join(str(c) for c in mock_logger.debug.call_args_list)
        assert 'SKILL-SELECTOR' in logged
        assert 'COMPLETION-CLASSIFIER' not in logged

    def test_endpoint_without_api_base_logs_under_caller_prefix(self):
        """The empty-api_base skip message is emitted from the shared resolver."""
        from agent_cascade.skills import selector as sel
        router = _make_router([('ep1', 'A', '', 'm')], priorities=['ep1'])
        with patch.object(sel, 'logger') as mock_logger:
            assert sel.resolve_endpoints(_pool_with_router(router),
                                         log_prefix='COMPLETION-CLASSIFIER') == []
        logged = ' '.join(str(c) for c in mock_logger.debug.call_args_list)
        assert 'COMPLETION-CLASSIFIER' in logged


# ── 3. Cost guard — only the ambiguous shape pays for a classifier call ──────


class TestCostGuard:

    @pytest.mark.parametrize('msg_factory', [_empty_output_msg, _reasoning_only_msg],
                             ids=['empty-output', 'reasoning-only'])
    def test_structural_turns_never_reach_the_classifier(self, msg_factory):
        """The structural detector wins first — those turns cost no HTTP."""
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('completed')) as mock_post:
            _run(engine, instance, [msg_factory()])
        mock_post.assert_not_called()

    def test_ambiguous_turn_does_reach_the_classifier(self):
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('completed')) as mock_post:
            _run(engine, instance, [_text_only_msg()])
        mock_post.assert_called_once()

    def test_single_endpoint_attempt_only(self):
        """Latency ceiling: only the first endpoint is tried, never the whole chain."""
        router = _make_router(
            [('ep1', 'A', 'https://a.example/v1/systemone', 'm'),
             ('ep2', 'B', 'https://b.example/v1/systemone', 'm')],
            priorities=['ep1', 'ep2'])
        pool = _pool_with_router(router)
        with patch(_POST, side_effect=RuntimeError('down')) as mock_post:
            assert classify_completion(pool, 'hello') is None
        assert mock_post.call_count == 1


# ── 4. Toggle gate ───────────────────────────────────────────────────────────
#
# NOTE: the pool doubles above are PLAIN objects (never MagicMock) on purpose. A
# MagicMock pool auto-creates a truthy attribute for ANY settings name, so the gate
# would pass for the wrong reason (the classifier would appear to run when disabled).


class TestToggleGate:

    def test_disabled_toggle_never_calls_the_classifier(self):
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(classifier_enabled=False, router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('incomplete')) as mock_post:
            result = _run(engine, instance, [_text_only_msg()])

        assert result is False          # byte-identical pre-fix behavior
        mock_post.assert_not_called()   # zero HTTP
        assert engine.pool._rollback_calls == []

    def test_auto_continue_off_never_calls_the_classifier(self):
        """Both toggles gate the call — auto_continue=False is enough."""
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(auto_continue=False, router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('incomplete')) as mock_post:
            result = _run(engine, instance, [_text_only_msg()])

        assert result is False
        mock_post.assert_not_called()

    def test_enabled_toggle_calls_the_classifier(self):
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(classifier_enabled=True, router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('incomplete')) as mock_post:
            result = _run(engine, instance, [_text_only_msg()])

        assert result is True
        mock_post.assert_called_once()

    def test_missing_setting_defaults_to_enabled(self):
        """getattr(..., True): a pool without the field behaves as if ON."""
        router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                              priorities=['ep1'])
        engine = _engine(_FakePool(classifier_enabled=None, router=router))
        instance = _make_instance()
        with patch(_POST, return_value=_post_verdict('incomplete')) as mock_post:
            result = _run(engine, instance, [_text_only_msg()])
        mock_post.assert_called_once()   # missing field → defaults ON
        assert result is True


class TestSettingPlumbing:

    def test_pool_settings_default_true(self):
        from agent_cascade.agent_instance import PoolSettings
        assert PoolSettings().completion_classifier_enabled is True

    def test_setting_round_trips_through_from_dict_to_dict(self):
        from agent_cascade.agent_instance import PoolSettings
        restored = PoolSettings.from_dict({'completion_classifier_enabled': False})
        assert restored.completion_classifier_enabled is False
        assert PoolSettings.from_dict({}).completion_classifier_enabled is True
        assert restored.to_dict()['completion_classifier_enabled'] is False

    @pytest.mark.parametrize('value', [True, False])
    def test_config_handler_sets_the_setting(self, value):
        from agent_cascade.config_handlers import CONFIG_HANDLERS, POOL_SETTINGS_KEYS

        assert 'completion_classifier_enabled' in POOL_SETTINGS_KEYS
        handler = CONFIG_HANDLERS['completion_classifier_enabled']
        pool = MagicMock()
        handler({'completion_classifier_enabled': value}, pool, [])
        assert pool.settings.completion_classifier_enabled is value

    def test_config_handler_defaults_to_true_when_absent(self):
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        pool = MagicMock()
        pool.settings.completion_classifier_enabled = False
        CONFIG_HANDLERS['completion_classifier_enabled']({}, pool, [])
        assert pool.settings.completion_classifier_enabled is True

    def test_config_handler_no_ops_without_pool(self):
        """No pool → no raise, returns None, and mutates nothing.

        ``pool=None`` is the "pool not built yet" contract. The observable is the
        absence of any side effect, so the test proves it by CONTRAST: the very same
        handler on the very same input DOES write through to a pool when one is
        supplied, and with no pool it returns None without raising.
        """
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        handler = CONFIG_HANDLERS['completion_classifier_enabled']
        payload = {'completion_classifier_enabled': False}

        # A MagicMock records any attribute write; with a pool this MUST happen.
        with_pool = MagicMock()
        handler(payload, with_pool, [])
        assert with_pool.settings.completion_classifier_enabled is False

        # Without a pool the handler must be a silent no-op, not a crash.
        assert handler(payload, None, []) is None

    @pytest.mark.parametrize('value', [True, False])
    def test_state_builder_emits_the_setting_in_pool_settings(self, value):
        """BEHAVIOR: both state paths must put the live value in the emitted dict.

        Exercises the real ``build_state_from_pool`` / ``build_stream_update_from_pool``
        against a pool double whose ``settings`` is a real ``PoolSettings``. A key
        missing from EITHER block is invisible until a restart, so both are pinned
        here by running them rather than by grepping the source.
        """
        from agent_cascade.agent_instance import PoolSettings
        from agent_cascade.api_integration_pkg.state_builder import (
            build_state_from_pool, build_stream_update_from_pool)

        pool = _StatePool(settings=PoolSettings(completion_classifier_enabled=value))
        pool.instances['Maine'] = _make_instance()
        state = build_state_from_pool(pool, 'Maine')
        stream = build_stream_update_from_pool(pool, 'Maine')
        assert state is not None and stream is not None
        assert state['pool_settings']['completion_classifier_enabled'] is value
        assert stream['pool_settings']['completion_classifier_enabled'] is value

    def test_setting_is_not_forwarded_to_the_llm(self):
        from agent_cascade.constants import NON_LLM_KEYS
        assert 'completion_classifier_enabled' in NON_LLM_KEYS

    def test_ui_dom_id_exists_and_is_checked_by_default(self):
        """BEHAVIOR for markup: the checkbox the JS wires to must actually exist.

        app.js looks the control up by this id in four places (POOL_SETTINGS_MAP,
        saveSettings, loadSettings, getGenerateCfg). A missing id degrades silently to
        "setting never saved", so pin the id + its checked default from the live markup
        rather than counting JS occurrences. (Regex, not BeautifulSoup — keeps the test
        suite free of an HTML-parsing dependency.)
        """
        index_html = (_PROJECT_ROOT / 'web_ui' / 'index.html').read_text(encoding='utf-8')
        tags = re.findall(r'<input\b[^>]*>', index_html)
        matches = [t for t in tags if 'id="setting-completion-classifier"' in t]
        assert len(matches) == 1, 'exactly one control with that id (ids must be unique)'
        tag = matches[0]
        assert 'type="checkbox"' in tag
        assert 'checked' in tag, 'must default to ON (fail-open to enabled)'

    def test_ui_js_wires_the_same_dom_id_and_key(self):
        """Every app.js seam must reference the SAME id and the SAME config key.

        The four seams are individually unobservable from Python, so this is a
        source check — kept honest by asserting on the exact id/key strings (not on
        counts) and by naming what a mismatch would break.
        """
        app_js = (_PROJECT_ROOT / 'web_ui' / 'app.js').read_text(encoding='utf-8')

        # Registry entry must map this DOM id → this config key (used for load+save).
        assert ("id: '#setting-completion-classifier'" in app_js
                and "key: 'completion_classifier_enabled'" in app_js)
        # saveSettings / loadSettings share the POOL_SETTINGS_MAP localKey convention.
        assert "s['completion-classifier'] = $('#setting-completion-classifier').checked" in app_js
        # getGenerateCfg must send the server-side key name (not the local one).
        assert "cfg.completion_classifier_enabled = $('#setting-completion-classifier').checked" in app_js
        # loadSettings must guard on presence, mirroring auto_continue.
        assert "_present(s['completion-classifier'])" in app_js


# ── Helper unit tests ────────────────────────────────────────────────────────


class TestLastAssistantText:

    def test_extracts_last_assistant_text(self):
        assert _last_assistant_text([_text_only_msg('first'), _text_only_msg('second')]) == 'second'

    def test_skips_trailing_non_assistant_messages(self):
        assert _last_assistant_text([_text_only_msg('answer'), Message(role='user', content='q')]) == 'answer'

    def test_returns_empty_when_no_assistant_message(self):
        assert _last_assistant_text([Message(role='user', content='q')]) == ''

    def test_handles_content_block_list(self):
        msg = {'role': ASSISTANT, 'content': [{'type': 'text', 'text': 'a'}, {'type': 'image', 'url': 'x'}]}
        assert _last_assistant_text([msg]) == 'a'
