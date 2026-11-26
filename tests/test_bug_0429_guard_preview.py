"""BUG_0429: feed the BUG_0425 dry-run preview into the Security guard's prompt.

The human approval banner shows the dry-run preview (unified diff / affected-files /
command-echo) that BUG_0425 attaches to ``PendingApproval.preview``. This deployment lets
the LLM guard do most of the approving, so it must judge the same concrete effect — not
just the declared args.

These tests drive the REAL ``SecurityAdvisorHandler._execute_check`` with a fake pool +
fake engine (mirroring ``tests/test_builtin_tool_hint.py``) and capture the exact prompt
string handed to ``security_reuse.acquire_security_agent(task=prompt, ...)`` — the faithful
observable of the guard's prompt, including the preview append.

The whole feature is config-gated by nature: ``ap['preview']`` is only non-empty when the
preview-attach flag is ON, so the "no preview" assertions prove the prompt is byte-identical
(never a block appended) when the feature is off.
"""

import threading
import types
from unittest.mock import MagicMock

import pytest

from agent_cascade.security_handler import (
    SECURITY_PREVIEW_MAX_CHARS,
    SecurityAdvisorHandler,
)

# Minimal template carrying every placeholder the handler formats (the preview append
# happens after .format(), so the template content is not under test).
PROMPT_TEMPLATE = ('PROMPT tool={tool_name} desc={description} args={arguments} '
                   'os={os_info} ws={workspace_info}')

PREVIEW_LABEL = '[CONCRETE EFFECT PREVIEW (dry-run; NOT yet committed)]'


# ── Fakes (mirror tests/test_builtin_tool_hint.py) ──────────────────────────


class _FakeOperationManager:
    def __init__(self):
        self.base_dir = 'N:\\work\\WD\\AgentWorkspace'
        self.extra_work_folders_ro = []
        self.extra_work_folders_rw = []
        self.approvals = []
        self.rejections = []

    def user_approve(self, rid, reason=''):
        self.approvals.append((rid, reason))

    def user_reject(self, rid, message=''):
        self.rejections.append((rid, message))


class _FakePool:
    def __init__(self):
        self.operation_manager = _FakeOperationManager()
        self.instance_state = {}
        self._execution = MagicMock()
        self._execution._state_lock = threading.Lock()
        self.stopped = False

    def get_instance(self, name):
        return None

    def get_template(self, name):
        return None

    def active_stack_remove(self, name):
        pass


def _make_ap(rid, tool_name='edit_file', **extra):
    # 'edit_file' is never hint-eligible, so the prompt-build path always runs (the
    # shell_cmd hint fast path would return before the prompt is built).
    ap = {
        'request_id': rid,
        'tool_name': tool_name,
        'description': 'probe',
        'tool_args': {'path': 'a.txt'},
    }
    ap.update(extra)
    return ap


def _make_handler(pool):
    app = type('App', (), {})()
    return SecurityAdvisorHandler(pool, {'session_name': 'Maine'}, app,
                                  MagicMock(), lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Start every test from an empty verdict cache so the full prompt path runs."""
    from agent_cascade.security_handler import _get_security_approval_cache
    # The BUG_0180 approval cache short-circuits repeated identical tool_name+tool_args
    # runs BEFORE the prompt is built. The prompt-build path (not the cache) is what's
    # under test here, so disable it to force a fresh prompt per check.
    monkeypatch.setattr('agent_cascade.settings.SECURITY_APPROVAL_CACHE_ENABLED', False)
    cache = _get_security_approval_cache(None)
    cache.clear()
    yield cache
    cache.clear()


@pytest.fixture
def prompt_capture(monkeypatch):
    """Stub the LLM-adjacent plumbing and capture the exact prompt sent to the guard."""
    captured = {'prompts': []}
    verdict_text = '[YES] safe [END]'

    class _FakeEngine:
        def __init__(self, pool):
            pass

        def run(self, instance):
            def _gen():
                yield ({'role': 'assistant', 'content': verdict_text}, False)
            return _gen()

        def _telemetry(self):
            return None

        def save_before_slot_yield(self, *args, **kwargs):
            pass

        @staticmethod
        def _discard_stale_permit(*args, **kwargs):
            pass

    monkeypatch.setattr('agent_cascade.execution_engine.ExecutionEngine', _FakeEngine)
    monkeypatch.setattr('agent_cascade.security_handler.yield_caller_slot',
                        lambda *a, **k: False)

    def _acquire(engine, **kw):
        captured['prompts'].append(kw.get('task'))
        return (types.SimpleNamespace(conversation=[]),
                False, kw.get('fallback_name', 'Security_x'))

    monkeypatch.setattr(
        'agent_cascade.security_handler.security_reuse.acquire_security_agent', _acquire)
    monkeypatch.setattr('agent_cascade.compression.helpers.extract_instance_output',
                        lambda conv, key: verdict_text)
    monkeypatch.setattr('agent_cascade.api_integration.broadcast_stream_update',
                        lambda **kw: (0.0, 0))
    return captured


# ── Tests ───────────────────────────────────────────────────────────────────


def test_preview_appended_when_present(prompt_capture):
    pool = _FakePool()
    preview = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n+hello\n'
    handler = _make_handler(pool)
    handler._execute_check(
        _make_ap('op_prev', preview=preview), None, 'op_prev', True, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    prompt = prompt_capture['prompts'][-1]
    # The labeled block is present AND carries the concrete preview text.
    assert PREVIEW_LABEL in prompt
    assert '+hello' in prompt
    # preview is .strip()ed before appending, so the block ends at the stripped preview.
    assert prompt.endswith(preview.strip())


def test_no_preview_key_is_byte_identical_to_empty(prompt_capture):
    """Missing key, empty string, and whitespace-only must all yield the same prompt
    with NO preview block — i.e. the feature-off prompt is unchanged."""
    pool = _FakePool()

    # (a) ap with NO 'preview' key at all (old callers / feature off).
    handler = _make_handler(pool)
    handler._execute_check(
        _make_ap('op_nokey'), None, 'op_nokey', True, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    p_nokey = prompt_capture['prompts'][-1]

    # (b) ap with 'preview': '' (the PendingApproval default).
    handler = _make_handler(pool)
    handler._execute_check(
        _make_ap('op_empty', preview=''), None, 'op_empty', True, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    p_empty = prompt_capture['prompts'][-1]

    # (c) whitespace-only preview (strips to '').
    handler = _make_handler(pool)
    handler._execute_check(
        _make_ap('op_ws', preview='   \n\t  '), None, 'op_ws', True, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    p_ws = prompt_capture['prompts'][-1]

    assert p_nokey == p_empty == p_ws
    assert PREVIEW_LABEL not in p_nokey
    assert p_nokey.endswith('AgentWorkspace\n')  # ends at base template (no preview block)


def _run(pool, ap, rid):
    """Drive one guard check; the prompt is captured via the prompt_capture fixture."""
    _make_handler(pool)._execute_check(
        ap, None, rid, True, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )


def test_prompt_with_preview_equals_base_plus_labelled_block(prompt_capture):
    """The with-preview prompt must be EXACTLY the no-preview prompt plus the labelled
    block — proving the append is purely additive, not a rewrite of the base."""
    pool = _FakePool()
    _run(pool, _make_ap('op_base'), 'op_base')          # no preview
    preview = 'AFFECTED: a.txt (12 bytes)'
    _run(pool, _make_ap('op_prev2', preview=preview), 'op_prev2')  # with preview
    base = prompt_capture['prompts'][0]
    with_prev = prompt_capture['prompts'][1]
    assert with_prev == base + '\n\n' + PREVIEW_LABEL + '\n' + preview


def test_long_preview_is_bounded(prompt_capture):
    pool = _FakePool()
    # Preview longer than the cap → truncated to cap chars + the truncation marker.
    body = 'x' * (SECURITY_PREVIEW_MAX_CHARS + 500)
    handler = _make_handler(pool)
    handler._execute_check(
        _make_ap('op_big', preview=body), None, 'op_big', True, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    prompt = prompt_capture['prompts'][-1]
    assert PREVIEW_LABEL in prompt
    # Exactly SECURITY_PREVIEW_MAX_CHARS of the body survive, then the marker.
    assert body[:SECURITY_PREVIEW_MAX_CHARS] in prompt
    assert body[:SECURITY_PREVIEW_MAX_CHARS + 1] not in prompt  # one char past the cap gone
    assert prompt.endswith('\n…(truncated)')
