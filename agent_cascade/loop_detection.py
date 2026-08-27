"""⚠️ DEPRECATED — DO NOT USE for new code.

This module is a backward-compatibility stub. Loop detection lives elsewhere:

- Tier 1 (exact, rollback):   :mod:`agent_cascade.exact_loop_detect`
- Tier 2 (fuzzy, warning-first): :mod:`agent_cascade.tool_loop_detect`

See plans/loop_detector_exact_redesign_PLAN.md.

REPLACED (2026-08 two-tier redesign): the exact contiguous matcher
``detect_loop`` that lived here was REMOVED and replaced by
:mod:`agent_cascade.exact_loop_detect` (Tier 1, window 60, max period 12,
wrapper-stripped FUNCTION features). The fuzzy tool-call detector lives in
:mod:`agent_cascade.tool_loop_detect` (Tier 2, warning-first with optional
escalation).

Only :class:`LoopDetectedError` remains in this module. It is no longer raised
by production code; it is kept ONLY because ``api_integration_pkg/runner.py``
and existing tests import it. Do not add new imports of this module.
"""


class LoopDetectedError(Exception):
    """DEPRECATED (2026-08): No longer raised by the main codebase.

    Raised when a repetitive loop is detected in agent turns. Kept for backward
    compatibility with existing tests and external consumers. Loop detection is
    now handled inline inside ExecutionEngine._pre_llm_checks().
    """
    def __init__(self, reason, agent_name=None, pop_count=None, turn_pop_count=0, resp_snapshot=None):
        self.reason = reason
        self.agent_name = agent_name
        self.pop_count = pop_count
        self.turn_pop_count = turn_pop_count
        self.resp_snapshot = resp_snapshot or []
        super().__init__(f"Loop detected for {agent_name or 'agent'}: {reason}")


# detect_loop (the exact contiguous matcher, window 40 / max period 20) was
# REMOVED here in the 2026-08 two-tier redesign and replaced by
# agent_cascade.exact_loop_detect.detect_exact_loop (window 60 / max period 12,
# wrapper-stripped FUNCTION features). Its 67 pinned tests migrated to
# tests/test_exact_loop_detect.py + tests/test_loop_detection.py.
