"""Unit tests for the API decision-model skill selector (backend half).

Covers:
- mode=keyword (default) → keyword results, no HTTP attempted.
- mode=api with a mocked endpoint returning a valid choice → chosen skill first.
- mode=api failure modes (raise / timeout / 'none' / unknown model) → fall back to keyword, never raise.
- get_endpoint_by_name: correct match, duplicate-name warning + first, None when absent.
- Fallback list order: primary fails, secondary succeeds → uses secondary.

The HTTP layer is mocked at ``agent_cascade.skills.selector.requests.post``; no real network.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as _requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.api_router_pkg.endpoints import APIEndpoint  # noqa: E402
from agent_cascade.api_router_pkg.router import APIRouter  # noqa: E402
from agent_cascade.skills.manager import SkillManager  # noqa: E402


# ── Test doubles ──────────────────────────────────────────────────────────────


class _FakePool:
    """Minimal stand-in for the pool back-reference set on SkillManager."""

    def __init__(self, mode='keyword', router=None):
        self.settings = MagicMock()
        self.settings.skill_selector_mode = mode
        self.api_router = router


def _make_manager(mode='keyword', router=None):
    """A real SkillManager with a fake pool wired in (no production I/O)."""
    import threading
    mgr = SkillManager.__new__(SkillManager)  # bypass __init__ — we only exercise match_skills
    mgr._skills_registry = {}
    mgr._matcher = MagicMock()
    mgr._matcher._field_index = {'x': 1}  # non-empty → skip lazy rebuild
    mgr._disabled_names = set()
    mgr._write_lock = threading.RLock()
    mgr.pool = _FakePool(mode=mode, router=router)
    return mgr


def _keyword_results():
    """The keyword matcher's ranked output (name, score)."""
    return [('alpha', 0.9), ('beta', 0.5), ('gamma', 0.2)]


def _wire_keyword(mgr):
    """Make the manager's internal matcher return fixed keyword results."""
    mgr._matcher.match.return_value = _keyword_results()


def _make_router(pairs, priorities=None):
    """Build a real APIRouter populated with endpoints by (id, name, base, model)."""
    router = APIRouter(default_llm_cfg={})
    for eid, name, base, model in pairs:
        ep = APIEndpoint(id=eid, name=name, api_base=base, api_key='k', model=model)
        router.add_endpoint(ep)
    if priorities is not None:
        router.agent_priorities['skill_selector'] = priorities
    return router


def _post_ok(choice):
    """A successful requests.post mock returning a systemone response."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {'answers': {'pick': {'choice': choice}}}
    return resp


# ── 1. mode=keyword (default) → keyword results, no HTTP ─────────────────────


def test_keyword_mode_default_no_http():
    mgr = _make_manager(mode='keyword')
    _wire_keyword(mgr)
    with patch('agent_cascade.skills.selector.requests.post') as mock_post:
        result = mgr.match_skills('some query')
    assert result == _keyword_results()
    mock_post.assert_not_called()


def test_keyword_mode_when_no_pool():
    """No pool at all → keyword path (byte-identical), no HTTP."""
    mgr = _make_manager(mode='api')
    mgr.pool = None
    _wire_keyword(mgr)
    with patch('agent_cascade.skills.selector.requests.post') as mock_post:
        result = mgr.match_skills('some query')
    assert result == _keyword_results()
    mock_post.assert_not_called()


# ── 2. mode=api, valid choice → chosen skill first ───────────────────────────


def test_api_mode_valid_choice_first():
    router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                          priorities=['ep1'])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    # Metadata for the pool so criteria build cleanly.
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': f'desc {n}', 'triggers': ['t1']}):
        with patch('agent_cascade.skills.selector.requests.post', return_value=_post_ok('beta')) as mock_post:
            result = mgr.match_skills('some query')
    assert result[0][0] == 'beta'
    # Remaining candidates follow in keyword order.
    assert [n for n, _ in result] == ['beta', 'alpha', 'gamma']
    mock_post.assert_called_once()


def test_api_mode_url_appended_when_missing():
    """api_base not ending in /systemone → it is appended."""
    router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1', 'openjev-latest')],
                          priorities=['ep1'])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}):
        with patch('agent_cascade.skills.selector.requests.post', return_value=_post_ok('alpha')) as mock_post:
            mgr.match_skills('some query')
    url = mock_post.call_args[0][0]
    assert url == 'https://api.codiv.ai/v1/systemone'


def test_api_mode_empty_model_uses_default():
    """Endpoint with empty model → module default model is used, request still sent."""
    import json as _json
    from agent_cascade.skills.selector import DEFAULT_SKILL_SELECTOR_MODEL
    router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', '')],
                          priorities=['ep1'])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}):
        with patch('agent_cascade.skills.selector.requests.post',
                   return_value=_post_ok('beta')) as mock_post:
            result = mgr.match_skills('some query')
    # A valid choice was returned → API path taken (not keyword fallback).
    assert result[0][0] == 'beta'
    mock_post.assert_called_once()
    sent = mock_post.call_args.kwargs.get('data') or mock_post.call_args.args[1]
    assert _json.loads(sent)['model'] == DEFAULT_SKILL_SELECTOR_MODEL


def test_api_mode_respects_cap():
    """A cap passed to match_skills is honored on the API result too."""
    router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                          priorities=['ep1'])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}):
        with patch('agent_cascade.skills.selector.requests.post', return_value=_post_ok('alpha')):
            result = mgr.match_skills('some query', cap=2)
    assert len(result) == 2
    assert result[0][0] == 'alpha'


# ── 3. mode=api failure modes → fall back to keyword, never raise ────────────


@pytest.mark.parametrize(
    'post_kw',
    [
        pytest.param({'side_effect': OSError('boom')}, id='post-raises'),
        pytest.param({'side_effect': _requests.exceptions.Timeout('slow')}, id='timeout'),
        pytest.param({'return_value': _post_ok('none')}, id='choice-none'),
        pytest.param({'return_value': _post_ok('not-a-skill')}, id='choice-unknown'),
    ],
)
def test_api_mode_failure_falls_back(post_kw):
    """Any API failure / abstention → fall back to keyword results, never raise."""
    router = _make_router([('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'openjev-latest')],
                          priorities=['ep1'])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}):
        with patch('agent_cascade.skills.selector.requests.post', **post_kw):
            result = mgr.match_skills('some query')  # must not raise
    assert result == _keyword_results()


def test_api_mode_no_priority_list_falls_back():
    """No skill_selector priority list → no endpoints → keyword, no HTTP."""
    router = APIRouter(default_llm_cfg={})  # no priorities at all
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    with patch('agent_cascade.skills.selector.requests.post') as mock_post:
        result = mgr.match_skills('some query')
    assert result == _keyword_results()
    mock_post.assert_not_called()


# ── 4. get_endpoint_by_name ───────────────────────────────────────────────────


def test_get_endpoint_by_name_found():
    router = APIRouter(default_llm_cfg={})
    ep = APIEndpoint(id='id1', name='CodivAI-decission', api_base='https://x/v1', model='openjev-latest')
    router.add_endpoint(ep)
    assert router.get_endpoint_by_name('CodivAI-decission') is ep


def test_get_endpoint_by_name_absent():
    router = APIRouter(default_llm_cfg={})
    router.add_endpoint(APIEndpoint(id='id1', name='Other'))
    assert router.get_endpoint_by_name('Missing') is None


def test_get_endpoint_by_name_duplicate_warns_and_first(capsys):
    import logging
    router = APIRouter(default_llm_cfg={})
    first = APIEndpoint(id='first-id', name='Dup')
    second = APIEndpoint(id='second-id', name='Dup')
    router.add_endpoint(first)
    router.add_endpoint(second)
    with patch('agent_cascade.api_router_pkg.router.logger') as mock_logger:
        result = router.get_endpoint_by_name('Dup')
    assert result is first  # insertion-order first
    mock_logger.warning.assert_called()


# ── 5. Fallback list order: primary fails, secondary succeeds ────────────────


def test_fallback_secondary_used_when_primary_fails():
    router = _make_router(
        [('ep1', 'Primary', 'https://a/v1/systemone', 'm1'),
         ('ep2', 'Secondary', 'https://b/v1/systemone', 'm2')],
        priorities=['ep1', 'ep2'])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)

    def side_effect(url, *a, **k):
        if url.startswith('https://a/'):
            raise OSError('primary down')
        return _post_ok('gamma')

    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}):
        with patch('agent_cascade.skills.selector.requests.post', side_effect=side_effect) as mock_post:
            result = mgr.match_skills('some query')
    assert result[0][0] == 'gamma'  # chosen by secondary
    assert mock_post.call_count == 2  # tried primary then secondary
