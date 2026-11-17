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
from agent_cascade.skills import selector as sel  # noqa: E402
from agent_cascade.skills.manager import SkillManager  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_selector_throttle():
    """Clear the module-global failure-log throttle state before and after each test.

    Throttle state lives in module globals keyed by (subsystem, condition). Without this
    reset, a test that triggers a failure WARNING would suppress the NEXT test's WARNING
    for the same key across the whole 60s cooldown, making the log assertions an
    order-dependent false-failure. Clearing before the test guarantees a clean slate.
    """
    with sel._fail_log_lock:
        sel._fail_log_last.clear()
        sel._fail_log_suppressed.clear()
    yield
    with sel._fail_log_lock:
        sel._fail_log_last.clear()
        sel._fail_log_suppressed.clear()


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


# ── 6. BUG_0342 observability: degradation paths are visible, not hidden ──────
#
# Strategy (three-layer-proof / wrong-shape-double-false-pass): patch the transport
# (requests.post) and the module logger; drive the REAL select()/ask_decision_model.
# Every assertion pins the diagnostic text (a WARNING can only exist if the real branch
# ran) AND an execution witness (post called/not-called), never the shared `None`
# fallback return that several code paths share.


def _log_messages(logger_mock, level: str) -> str:
    """Join the FORMATTED messages of every ``logger_mock.<level>()`` call.

    A ``MagicMock`` logger records the raw %-args, NOT a rendered string, so the text a
    user actually sees only exists after ``fmt % args``. Formatting here mirrors what a
    real logging handler would emit, so the assertions below pin user-visible text.
    """
    out = []
    for c in getattr(logger_mock, level).call_args_list:
        args = c.args
        if args and isinstance(args[0], str):
            if len(args) > 1:
                try:
                    out.append(args[0] % args[1:])
                    continue
                except (TypeError, ValueError):
                    pass  # arg/format mismatch → fall back to the raw format string
            out.append(args[0])
        if c.kwargs:
            out.append(str(c.kwargs))
    return ' '.join(out)


def _mgr_with_endpoint(ep_pairs=(('ep1', 'Codiv', 'https://api.codiv.ai/v1/systemone', 'm'),)):
    router = _make_router(list(ep_pairs), priorities=[eid for eid, *_ in ep_pairs])
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    return mgr


def test_endpoint_failure_warns_not_debug():
    """(a) A per-endpoint failure now logs WARNING naming prefix+endpoint+exception,
    NOT the old DEBUG-only line. (The DEBUG-level line is what hid the 7-day 429.)"""
    mgr = _mgr_with_endpoint()
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}), \
         patch('agent_cascade.skills.selector.requests.post',
               side_effect=OSError('boom')) as mock_post, \
         patch.object(sel, 'logger') as mock_logger:
        result = mgr.match_skills('some query')
    assert result == _keyword_results()  # degradation contract unchanged
    mock_post.assert_called_once()       # execution witness: the real except path ran
    warn = _log_messages(mock_logger, 'warning')
    assert 'SKILL-SELECTOR' in warn and 'ep1' in warn and 'boom' in warn
    debug = _log_messages(mock_logger, 'debug')
    assert 'boom' not in debug           # the failure must NOT be debug-only anymore


def test_no_usable_endpoints_warns():
    """(b) api mode active but no usable skill_selector endpoint → WARNING + keyword.
    This is the exact line that would have caught BUG_0342 immediately."""
    router = APIRouter(default_llm_cfg={})  # no skill_selector priority → no endpoints
    mgr = _make_manager(mode='api', router=router)
    _wire_keyword(mgr)
    with patch('agent_cascade.skills.selector.requests.post') as mock_post, \
         patch.object(sel, 'logger') as mock_logger:
        result = mgr.match_skills('some query')
    assert result == _keyword_results()
    mock_post.assert_not_called()        # execution witness: the no-endpoints guard ran
    warn = _log_messages(mock_logger, 'warning')
    assert 'SKILL-SELECTOR' in warn and 'no usable skill_selector endpoint' in warn


def test_all_endpoints_failed_warns():
    """(c) Every endpoint fails/abstains → WARNING naming the endpoint count + keyword."""
    mgr = _mgr_with_endpoint(
        [('ep1', 'P', 'https://a/v1/systemone', 'm'),
         ('ep2', 'S', 'https://b/v1/systemone', 'm')])
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}), \
         patch('agent_cascade.skills.selector.requests.post',
               side_effect=OSError('down')) as mock_post, \
         patch.object(sel, 'logger') as mock_logger:
        result = mgr.match_skills('some query')
    assert result == _keyword_results()
    assert mock_post.call_count == 2     # execution witness: both endpoints attempted
    warn = _log_messages(mock_logger, 'warning')
    assert 'all 2 endpoint(s) failed or abstained' in warn


def test_success_logs_debug_pick_with_latency():
    """(e) A valid pick is confirmable: DEBUG naming endpoint + skill + latency (ms),
    and the selection result is unchanged."""
    mgr = _mgr_with_endpoint()
    with patch.object(SkillManager, 'get_skill_metadata',
                      side_effect=lambda n: {'name': n, 'description': '', 'triggers': []}), \
         patch('agent_cascade.skills.selector.requests.post',
               return_value=_post_ok('beta')), \
         patch.object(sel, 'logger') as mock_logger:
        result = mgr.match_skills('some query')
    # unchanged return: chosen skill first, remainder in keyword order
    assert [n for n, _ in result] == ['beta', 'alpha', 'gamma']
    debug = _log_messages(mock_logger, 'debug')
    assert 'SKILL-SELECTOR' in debug and 'ep1' in debug and 'beta' in debug and 'ms' in debug


def test_throttle_suppresses_within_cooldown_and_re_emits():
    """(d) The _should_log gate emits once, suppresses repeats inside the cooldown,
    then re-emits after it carrying the number of occurrences suppressed in between."""
    key = 'TEST:throttle'
    times = [1000.0]
    with patch.object(sel, 'time') as mock_time, \
         patch.object(sel, 'SKILL_SELECTOR_FAIL_LOG_COOLDOWN', 60.0):
        mock_time.monotonic.side_effect = lambda: times[0]
        assert sel._should_log(key) == 0          # first emit → 0 suppressed
        times[0] = 1010.0
        assert sel._should_log(key) is None       # within cooldown → suppressed
        times[0] = 1020.0
        assert sel._should_log(key) is None       # still suppressed
        times[0] = 1061.0                         # 61s > 60s cooldown → re-emit
        assert sel._should_log(key) == 2          # carries the two suppressed in between
        times[0] = 1062.0                         # counter reset → suppressed again
        assert sel._should_log(key) is None


def test_throttle_reemit_warning_names_suppressed_count():
    """The end-to-end WARNING a throttled persistent failure produces: first emit has
    no note, the re-emit after cooldown says 'suppressed N in between'."""
    ep = APIEndpoint(id='ep1', name='E', api_base='https://x/v1/systemone',
                     api_key='k', model='m')
    times = [0.0]
    with patch.object(sel, 'time') as mock_time, \
         patch.object(sel, 'SKILL_SELECTOR_FAIL_LOG_COOLDOWN', 60.0), \
         patch.object(sel, 'logger') as mock_logger, \
         patch('agent_cascade.skills.selector.requests.post',
               side_effect=OSError('boom')) as mock_post:
        mock_time.monotonic.side_effect = lambda: times[0]
        assert sel.ask_decision_model(ep, 'm', 'st', 'pick', 'instr', {'none': ''}) is None
        times[0] = 5.0
        assert sel.ask_decision_model(ep, 'm', 'st', 'pick', 'instr', {'none': ''}) is None
        times[0] = 70.0
        assert sel.ask_decision_model(ep, 'm', 'st', 'pick', 'instr', {'none': ''}) is None
    assert mock_post.call_count == 3           # all three hits reached the real except
    warns = mock_logger.warning.call_args_list
    assert len(warns) == 2                         # 1st + 3rd emitted, 2nd suppressed
    rendered = [c.args[0] % c.args[1:] for c in warns]
    assert 'suppressed' not in rendered[0]         # first emit: no suppressed note
    assert 'suppressed 1 in between' in rendered[1]
