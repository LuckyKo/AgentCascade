"""Feedback-path tests for the slot-queue dead-man's switch (plan todo163, §5).

Covers the B (real-reason feedback) and C (terminal-state surfacing) fixes, plus
the LLM deadline raise:

- B-fix-1: llm_call.py's terminal branch is now reachable (``>=`` not ``>``).
- B-fix-2: slot timeouts are classified ``fatal`` by retry_policy (no futile
  re-queue retries).
- B-fix-3 / C-fix-3: format_crash walks the __cause__ chain so the dead-man's-switch
  reason (holder name + quiet duration) survives the scheduler's TimeoutError
  re-wrap and reaches the caller-facing string.
- LLM deadline: LLM_CALL_DEADLINE_SECONDS raised 900 → 2400.
"""

import inspect
import unittest

from agent_cascade.retry_policy import classify_error
from agent_cascade.slot_queue import SlotQueueTimeout, QueueTicket


def _slot_timeout(reason: str = "holder 'H' on 'shared' made no progress for 300s") -> SlotQueueTimeout:
    t = QueueTicket(seq=0, agent_name='w', instance_name='w',
                    agent_class='coder', slot_key='shared',
                    created_at=0.0, deadline=0.0)
    return SlotQueueTimeout(t, reason=reason)


class TestSlotTimeoutIsFatal(unittest.TestCase):
    def test_scheduler_wrap_message_is_fatal(self):
        """B-fix-2: the scheduler's wrap message ('...waiting for endpoint slot...')
        must classify as fatal, not transient — the old 'timeout'/'timed out'
        retryable patterns would have caused futile re-queue retries."""
        msg = ('Timed out after 300s waiting for endpoint slot on http://127.0.0.1:1/v1. '
               'Current active count: 1, max allowed: 1. Currently held by: H (coder)')
        self.assertEqual(classify_error(TimeoutError(msg)), 'fatal')

    def test_raw_slot_queue_timeout_is_fatal(self):
        """B-fix-2: a raw SlotQueueTimeout (TimeoutError subclass) is fatal."""
        self.assertEqual(classify_error(_slot_timeout()), 'fatal')

    def test_generic_timeout_still_retryable(self):
        """Sanity: a plain network timeout (not a slot timeout) is NOT fatal — we only
        widened the slot-specific case."""
        self.assertNotEqual(classify_error(TimeoutError('connect timed out')), 'fatal')


class TestCrashFormatSurfacesReason(unittest.TestCase):
    def test_format_crash_walks_cause_chain(self):
        """C-fix-3 / B-fix-3: when the scheduler re-wraps a SlotQueueTimeout into a
        TimeoutError, format_crash must reach the root cause and surface the dead-man's
        switch reason (holder name + quiet duration) in the caller-facing string."""
        from agent_cascade.error_reporting import format_crash

        inner = _slot_timeout()
        # Mirror scheduler.py's `raise TimeoutError(...) from e`.
        outer = TimeoutError(
            'Timed out after 300s waiting for endpoint slot on http://x/v1. '
            'Current active count: 1, max allowed: 1')
        outer.__cause__ = inner

        text = format_crash(outer)
        self.assertIn('H', text, f"holder name lost in re-wrap: {text}")
        self.assertIn('no progress', text, f"reason lost in re-wrap: {text}")

    def test_cfix3_tool_dispatcher_uses_format_crash(self):
        """C-fix-3: the sync-child failure path in tool_dispatcher must route through
        format_crash (not bare str(e)), so the reason survives to the parent."""
        import agent_cascade.tool_dispatcher as td
        src = inspect.getsource(td)
        self.assertIn('format_crash(e)', src,
                      'tool_dispatcher no longer threads format_crash into the failure string')

    def test_cfix3_async_tools_uses_format_crash(self):
        """C-fix-3: the async-tool failure path must route through format_crash too."""
        import agent_cascade.async_tools as at
        src = inspect.getsource(at)
        self.assertIn('format_crash(e)', src,
                      'async_tools no longer threads format_crash into entry.error')


class TestLlmTerminalBranchReachable(unittest.TestCase):
    def test_terminal_branch_uses_gte(self):
        """B-fix-1: llm_call.py's informative terminal branch must be reachable — the
        guard is ``retry_count >= _max_attempts``, not ``>`` (which was unreachable and
        silently dropped to the 'Empty LLM response' branch)."""
        import agent_cascade.engine.llm_call as lc
        src = inspect.getsource(lc)
        self.assertIn('if retry_count >= _max_attempts:', src,
                      "llm_call terminal branch guard regressed to '>'")
        # And the dead guard is gone.
        self.assertNotIn('if retry_count > _max_attempts:', src)


class TestLlmDeadlineRaised(unittest.TestCase):
    def test_llm_call_deadline_is_2400(self):
        """The LLM call deadline was raised 900 → 2400 to accommodate long slot waits."""
        from agent_cascade.settings import LLM_CALL_DEADLINE_SECONDS
        self.assertEqual(LLM_CALL_DEADLINE_SECONDS, 2400)

    def test_post_yield_reacquire_timeout(self):
        """The post-yield tail re-queue uses a literal 120s bound (plan §3 A4)."""
        from agent_cascade.settings import POST_YIELD_REACQUIRE_TIMEOUT
        self.assertEqual(POST_YIELD_REACQUIRE_TIMEOUT, 120.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
