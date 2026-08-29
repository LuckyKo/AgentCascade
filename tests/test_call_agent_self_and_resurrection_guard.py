"""Regression tests for the call_agent self-call and resurrection identity-mismatch guards.

Covers the fix in ToolDispatcher.handle_call_agent:
  1. Self-call (resolved target == caller's canonical name, case-insensitive) is REJECTED
     unconditionally — no more silent `_child{N}` cloning of the caller's own name.
  2. Resurrection identity mismatch (existing instance under the resolved name whose
     persisted identity differs on either dimension, case-insensitive) is REJECTED:
       - requested name is a case-only variant of the canonical name, OR
       - requested agent_class differs from the existing instance's class.

Everything else is untouched: legitimate re-calling of a DISTINCT idle child still routes
through; fresh names still route through.

All tests are self-contained — no LLM or API server required. Uses a lightweight fake pool.
"""

import threading
from unittest.mock import MagicMock

import pytest

from agent_cascade.tool_dispatcher import ToolDispatcher


# ──────────────────────────────────────────────
# Test Helpers — lightweight fakes
# ──────────────────────────────────────────────

def _make_mock_instance(instance_name: str, agent_class: str = "coder"):
    """Minimal mock AgentInstance with the attributes handle_call_agent touches."""
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = MagicMock(name=f"{instance_name}_state")
    inst.state.name = "IDLE"  # Not in ACTIVE_STATES → Active Instance Guard passes
    inst._slot_release = None
    inst._nest_depth = 0
    return inst


class FakePool:
    """Lightweight fake AgentPool exposing only what handle_call_agent needs."""

    def __init__(self, instances=None, active_stack=None):
        self.instances = dict(instances or {})
        self.instance_classes = {n: i.agent_class for n, i in self.instances.items()}
        self._execution = MagicMock()
        self._execution._state_lock = threading.RLock()
        self._execution.active_stack = list(active_stack or [])
        self.api_router = None  # → child needs no slot → ASYNC path
        self.settings = MagicMock()
        self.settings.max_nesting_depth = 10
        # Template registry — the unknown-class guard calls pool.get_template()/list_agents().
        # Default to a small set of valid classes so legitimate routing tests pass the guard.
        self.templates = {
            "coder": MagicMock(), "orchestrator": MagicMock(), "reviewer": MagicMock(),
        }

    def get_template(self, name: str):
        """Case-insensitive fallback mirroring pool/config_persist.py."""
        if name in self.templates:
            return self.templates[name]
        for key in self.templates:
            if key.lower() == name.lower():
                return self.templates[key]
        return None

    def list_agents(self):
        return list(self.templates.keys())

    def _resolve_instance_name(self, instance_name: str, exclude=None):
        """Case-insensitive resolution mirroring pool/lifecycle.py."""
        instance_name = instance_name.strip()
        for name in self.instances:
            if name != exclude and name.lower() == instance_name.lower():
                return name
        return instance_name

    def get_instance(self, instance_name: str):
        return self.instances.get(instance_name.strip())


def _make_dispatcher(pool: FakePool):
    """Create a dispatcher with routing methods stubbed so tests can assert on them."""
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(MagicMock())
    # Stub the routing endpoints: rejections must never reach these; legitimate
    # calls must. (pool.register_async_call is also stubbed as a backstop in case
    # _run_child_async runs for real.)
    dispatcher._run_child_sync = MagicMock(return_value="SYNC_ROUTED")
    dispatcher._run_child_async = MagicMock(return_value="ASYNC_ROUTED")
    pool.register_async_call = MagicMock()
    return dispatcher


def _run(dispatcher, caller, instance_name, agent_class):
    """Drive handle_call_agent and return the result string."""
    return dispatcher.handle_call_agent(
        args={"instance_name": instance_name, "agent_class": agent_class, "task": "test"},
        messages=[],
        instance=caller,
    )


# ──────────────────────────────────────────────
# 1. Self-call guard
# ──────────────────────────────────────────────

class TestSelfCallGuard:
    """An agent calling itself (case-insensitive) is rejected outright."""

    def test_self_call_same_class_rejected(self):
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "Maine", "orchestrator")

        assert result.startswith("Error:")
        assert "Maine" in result
        assert "different instance name" in result
        # Rejection happens before routing — no child is spawned.
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_self_call_case_variant_rejected(self):
        """Requesting 'maine' when the caller's canonical name is 'Maine' → rejected."""
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "maine", "orchestrator")

        assert result.startswith("Error:")
        assert "Maine" in result
        assert "different instance name" in result
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_self_call_while_stacked_rejected_no_clone(self):
        """Even if the caller is currently active/stacked, self-call is rejected —
        no `_child1` clone is created (old P2 behavior removed)."""
        caller = _make_mock_instance("Maine", "orchestrator")
        # Caller's own name is on the execution stack (active/stacked)
        pool = FakePool(
            instances={"Maine": caller},
            active_stack=[("Maine", 1)],
        )
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "Maine", "orchestrator")

        assert result.startswith("Error:")
        assert "different instance name" in result
        # No clone was spawned under any *_child* name.
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()
        assert not any(n.startswith("Maine_child") for n in pool.instances), \
            f"Self-clone created: {list(pool.instances)}"


# ──────────────────────────────────────────────
# 2. Resurrection identity-mismatch guard
# ──────────────────────────────────────────────

class TestResurrectionIdentityMismatchGuard:
    """Existing instance under the resolved name with a mismatched identity is rejected."""

    def test_case_only_name_variant_rejected(self):
        """Existing 'Worker', request 'worker' (same class) → rejected (name variant)."""
        existing = _make_mock_instance("Worker", "coder")
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Worker": existing, "Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker", "coder")

        assert result.startswith("Error:")
        assert "Worker" in result  # names the existing canonical identity
        assert "different instance name" in result
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_class_mismatch_rejected(self):
        """Existing coder, request reviewer under same name → rejected (old P5 regression)."""
        existing = _make_mock_instance("worker1", "coder")
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"worker1": existing, "Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "reviewer")

        assert result.startswith("Error:")
        assert "worker1" in result
        assert "coder" in result  # names the existing class
        assert "different instance name" in result
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()


# ──────────────────────────────────────────────
# 3. Legitimate cases — NOT rejected (routing reached)
# ──────────────────────────────────────────────

class TestLegitimateCallsNotRejected:
    """Distinct idle child re-calls and fresh names still route through."""

    def test_distinct_idle_child_recall_not_rejected(self):
        """Different name, same class as an existing IDLE instance → routes (async)."""
        existing = _make_mock_instance("worker1", "coder")  # IDLE
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"worker1": existing, "Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "coder")

        assert not result.startswith("Error:")
        # Routing reached — async path taken (no slot info → needs no slot).
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()

    def test_fresh_name_not_rejected(self):
        """Name not in the pool at all → routes through (async)."""
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "brandnew", "coder")

        assert not result.startswith("Error:")
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()

    def test_whitespace_padded_exact_name_not_rejected(self):
        """Request ' worker ' (padded) when existing canonical is 'worker' (same class).
        _resolve_instance_name strips it to the exact canonical name → not a variant,
        so it routes through as a legitimate recall."""
        existing = _make_mock_instance("worker", "coder")  # IDLE
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"worker": existing, "Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, " worker ", "coder")

        assert not result.startswith("Error:")
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()
