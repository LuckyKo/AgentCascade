"""BUG_0180 follow-up: Security-approval cache (verdicts per tool-call signature).

Repeated identical guard reviews collapse into one LLM turn: a fresh (within-TTL)
cached [YES]/[NO] verdict is returned by `SecurityAdvisorHandler._execute_check`
WITHOUT running the LLM guard. These tests drive the REAL `_execute_check` with a
fake pool + fake engine so the observable is the guard invocation count and the
approval/reject delivered to the operation manager.
"""

import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.runtime_state import SecurityApprovalCache, state as _runtime_state
from agent_cascade.security_handler import (
    _get_security_approval_cache,
    _security_approval_signature,
    SecurityAdvisorHandler,
)

PROMPT_TEMPLATE = ('PROMPT tool={tool_name} desc={description} args={arguments} '
                   'os={os_info} ws={workspace_info}')


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeOperationManager:
    """Records the verdicts the guard delivers (the observable effect)."""

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
        self.active_stack_removed = []

    def get_instance(self, name):
        return None

    def get_template(self, name):
        return None

    def active_stack_remove(self, name):
        self.active_stack_removed.append(name)


def _make_ap(rid, command):
    return {
        'request_id': rid,
        'tool_name': 'shell_cmd',
        'description': 'probe endpoint',
        'tool_args': {'command': command},
    }


def _make_handler(pool):
    app = type('App', (), {})()
    return SecurityAdvisorHandler(pool, {'session_name': 'Maine'}, app,
                                  MagicMock(), lambda *a, **k: None)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Each test starts from an empty verdict cache (process singleton)."""
    cache = _get_security_approval_cache(None)
    cache.clear()
    yield cache
    cache.clear()


@pytest.fixture
def guard(monkeypatch):
    """Install the fake engine + stub the LLM-adjacent helpers.

    Returns a counters dict: 'run' counts guard (LLM) invocations,
    'engine_constructed' counts engine constructions (the cache fast path
    never reaches the `try:` block, so it is 0 on a cache hit).
    """
    counters = {'run': 0, 'engine_constructed': 0}
    verdict_text = '[YES] safe [END]'

    class _FakeEngine:
        def __init__(self, pool):
            counters['engine_constructed'] += 1

        def run(self, instance):
            counters['run'] += 1

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
    # No slot to yield in the fake pool → the fresh path skips the yield/reacquire.
    monkeypatch.setattr('agent_cascade.security_handler.yield_caller_slot',
                        lambda *a, **k: False)
    # Warm-instance acquire: return a plain fake instance (no engine internals needed).
    monkeypatch.setattr(
        'agent_cascade.security_handler.security_reuse.acquire_security_agent',
        lambda engine, **kw: (types.SimpleNamespace(conversation=[]),
                              False, kw.get('fallback_name', 'Security_x')))
    # Verdict text extraction + streaming broadcast: pure plumbing, not under test.
    monkeypatch.setattr('agent_cascade.compression.helpers.extract_instance_output',
                        lambda conv, key: verdict_text)
    monkeypatch.setattr('agent_cascade.api_integration.broadcast_stream_update',
                        lambda **kw: (0.0, 0))
    return counters


def _run(pool, guard, rid, ap, auto_apply=True):
    handler = _make_handler(pool)
    handler._execute_check(
        ap, None, rid, auto_apply, 'Maine', 'coder',
        PROMPT_TEMPLATE, 60.0, 3600.0,
    )
    return handler


# ── Fast-path tests (the effect, not the plumbing) ──────────────────────────


def test_second_identical_call_served_from_cache(_fresh_cache, guard):
    """A repeated identical tool call must NOT trigger a second LLM guard turn."""
    pool = _FakePool()
    ap = _make_ap('op_cache_yes', 'curl -s http://127.0.0.1:8080/health')

    _run(pool, guard, 'op_cache_yes', ap)
    assert pool.operation_manager.approvals == [('op_cache_yes', 'safe')]
    assert guard['run'] == 1

    # Second identical call — same signature → cache hit, no new LLM turn.
    _run(pool, guard, 'op_cache_yes_2', ap)
    assert guard['run'] == 1
    assert guard['engine_constructed'] == 1  # fast path never builds an engine
    assert pool.operation_manager.approvals == [
        ('op_cache_yes', 'safe'), ('op_cache_yes_2', 'safe')]


def test_denied_verdict_is_cached(_fresh_cache, guard, monkeypatch):
    """A repeated [NO] must be served from the cache too (no re-review)."""
    pool = _FakePool()
    ap = _make_ap('op_cache_no', 'rm -rf /var/data')
    verdict = '[NO] unsafe: deletes outside workspace [END]'
    # Verdict extraction is plumbing — point it at the [NO] text.
    monkeypatch.setattr('agent_cascade.compression.helpers.extract_instance_output',
                        lambda conv, key: verdict)
    # Re-install the engine so its generator yields the [NO] verdict.
    class _NoEngine:
        def __init__(self, pool):
            guard['engine_constructed'] += 1

        def run(self, instance):
            guard['run'] += 1

            def _gen():
                yield ({'role': 'assistant', 'content': verdict}, False)
            return _gen()

        def _telemetry(self):
            return None

        def save_before_slot_yield(self, *args, **kwargs):
            pass

        @staticmethod
        def _discard_stale_permit(*args, **kwargs):
            pass

    monkeypatch.setattr('agent_cascade.execution_engine.ExecutionEngine', _NoEngine)

    _run(pool, guard, 'op_cache_no', ap)
    assert pool.operation_manager.rejections == [
        ('op_cache_no', 'unsafe: deletes outside workspace')]
    assert guard['run'] == 1

    _run(pool, guard, 'op_cache_no_2', ap)
    assert guard['run'] == 1
    assert pool.operation_manager.rejections == [
        ('op_cache_no', 'unsafe: deletes outside workspace'),
        ('op_cache_no_2', 'unsafe: deletes outside workspace')]


def test_ttl_expiry_forces_recheck(_fresh_cache, guard, monkeypatch):
    """An expired entry must fall through to a fresh guard review."""
    # Controllable clock: the cache compares time.monotonic() deltas, and on
    # Windows the real clock's granularity can make a ttl=0 delta read as 0
    # (an exactly-ttl-old entry is deliberately "still fresh").
    import agent_cascade.runtime_state as _rs
    fake = {'t': 1000.0}
    monkeypatch.setattr(_rs.time, 'monotonic', lambda: fake['t'])
    monkeypatch.setattr('agent_cascade.settings.SECURITY_APPROVAL_CACHE_TTL_SECONDS', 0.05)
    pool = _FakePool()
    ap = _make_ap('op_cache_ttl', 'curl -s http://127.0.0.1:8080/health')

    _run(pool, guard, 'op_cache_ttl', ap)
    assert guard['run'] == 1

    # Still within TTL → cache hit.
    fake['t'] += 0.04
    _run(pool, guard, 'op_cache_ttl_hit', ap)
    assert guard['run'] == 1

    # Past TTL → entry expired on access → re-reviewed.
    fake['t'] += 0.05
    _run(pool, guard, 'op_cache_ttl_2', ap)
    assert guard['run'] == 2
    assert len(pool.operation_manager.approvals) == 3


def test_different_args_get_separate_reviews(_fresh_cache, guard):
    """Near-identical-but-different calls are distinct signatures → one review each."""
    pool = _FakePool()
    ap_a = _make_ap('op_cache_args_a', 'curl -s http://127.0.0.1:8080/a')
    ap_b = _make_ap('op_cache_args_b', 'curl -s http://127.0.0.1:8080/b')

    _run(pool, guard, 'op_cache_args_a', ap_a)
    _run(pool, guard, 'op_cache_args_b', ap_b)
    assert guard['run'] == 2
    assert len(pool.operation_manager.approvals) == 2
    # Each signature IS cached independently now.
    _run(pool, guard, 'op_cache_args_a2', ap_a)
    _run(pool, guard, 'op_cache_args_b2', ap_b)
    assert guard['run'] == 2


def test_cache_disabled_runs_guard_every_time(_fresh_cache, guard, monkeypatch):
    """With the kill-switch off, behavior is byte-for-byte today's (no cache)."""
    monkeypatch.setattr('agent_cascade.settings.SECURITY_APPROVAL_CACHE_ENABLED', False)
    pool = _FakePool()
    ap = _make_ap('op_cache_off', 'curl -s http://127.0.0.1:8080/health')

    _run(pool, guard, 'op_cache_off', ap)
    _run(pool, guard, 'op_cache_off_2', ap)
    assert guard['run'] == 2
    assert len(_fresh_cache._entries) == 0  # nothing populated


def test_manual_mode_cache_hit_sends_verdict_to_ui(_fresh_cache, guard):
    """auto_apply=False: a cache hit must route through the SAME _handle_verdict
    (UI confirmation) with the cached verdict/reason — the UI card's response
    body is the cached justification (the raw LLM text is not reproduced)."""
    pool = _FakePool()
    ap = _make_ap('op_cache_manual', 'curl -s http://127.0.0.1:8080/health')
    _run(pool, guard, 'op_cache_manual', ap)  # seed the cache (auto-apply)
    assert guard['run'] == 1

    handler = _make_handler(pool)
    with patch.object(handler, '_handle_verdict') as spy:
        handler._execute_check(
            ap, None, 'op_cache_manual_2', False, 'Maine', 'coder',
            PROMPT_TEMPLATE, 60.0, 3600.0,
        )
        # (rid, auto_apply, is_yes, is_no, justification, parsing_response, loop)
        spy.assert_called_once_with(
            'op_cache_manual_2', False, True, False, 'safe', 'safe', None)
    assert guard['run'] == 1


# ── Signature + cache-class unit tests ──────────────────────────────────────


def test_signature_determinism_and_separation():
    a1 = _security_approval_signature(
        {'tool_name': 'shell_cmd', 'tool_args': {'command': 'curl -s x', 'timeout': 5}})
    a2 = _security_approval_signature(
        {'tool_name': 'shell_cmd', 'tool_args': {'timeout': 5, 'command': 'curl -s x'}})
    assert a1 == a2  # key order must not matter (canonical JSON)

    b = _security_approval_signature(
        {'tool_name': 'shell_cmd', 'tool_args': {'command': 'curl -s y'}})
    assert a1 != b

    c = _security_approval_signature(
        {'tool_name': 'edit_file', 'tool_args': {'command': 'curl -s x', 'timeout': 5}})
    assert a1 != c  # different tool_name → different signature

    # Non-JSON-serializable args must not crash the digest.
    d = _security_approval_signature(
        {'tool_name': 't', 'tool_args': {'obj': object()}})
    assert d.startswith('t:')


def test_cache_class_ttl_and_lru(monkeypatch):
    import agent_cascade.runtime_state as _rs
    fake = {'t': 1000.0}
    monkeypatch.setattr(_rs.time, 'monotonic', lambda: fake['t'])

    cache = SecurityApprovalCache(max_entries=2)
    cache.put('a', True, 'ok')
    assert cache.get('a', ttl_seconds=300) == (True, 'ok')
    # Advance past the TTL → expired on access, evicted.
    fake['t'] += 301
    assert cache.get('a', ttl_seconds=300) is None

    fake['t'] = 2000.0
    cache.put('a', True, 'ok')
    cache.put('b', False, 'no')
    cache.put('c', True, 'ok')  # evicts the LRU entry ('a')
    assert cache.get('a', ttl_seconds=300) is None
    assert cache.get('b', ttl_seconds=300) == (False, 'no')
    assert cache.get('c', ttl_seconds=300) == (True, 'ok')
    assert len(cache._entries) == 2


def test_cache_class_thread_safety_smoke():
    cache = SecurityApprovalCache(max_entries=64)
    errors = []

    def _worker(n):
        try:
            for i in range(500):
                sig = f'sig_{(n * 7 + i) % 128}'
                cache.put(sig, i % 2 == 0, f'reason-{i}')
                cache.get(sig, ttl_seconds=300)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache._entries) <= 64


def test_concurrent_same_signature_populate_last_writer_wins():
    """N threads populate ONE signature concurrently: the stored entry must be
    exactly one of the values written (last-writer-wins under the lock) — no
    torn tuple, no lost/corrupted state."""
    cache = SecurityApprovalCache(max_entries=8)
    n = 8

    def _writer(n):
        for i in range(200):
            cache.put('same_sig', n % 2 == 0, f'writer-{n}-iter-{i}')

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entry = cache.get('same_sig', ttl_seconds=300)
    assert entry is not None
    approved, reason = entry
    assert approved in (True, False)
    assert reason.startswith('writer-')
    assert len(cache._entries) == 1


def test_singleton_cache_is_shared(_fresh_cache):
    """The handler's fast path and tests must see the SAME process-level cache."""
    assert _get_security_approval_cache(None) is _runtime_state.security_approval_cache
    assert _get_security_approval_cache(object()) is _runtime_state.security_approval_cache
