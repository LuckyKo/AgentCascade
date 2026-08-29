"""Regression tests for the call_agent unknown-agent-class guard.

Covers the fix in ToolDispatcher.handle_call_agent: an agent_class that has no
registered template is REJECTED early with a helpful error that lists the
available classes — instead of sailing through every guard, creating a new
instance, and crashing deep in lifecycle_manager.build_system_message() with a
bare "No template for agent class <wrong_class>" string.

Key behaviors:
  1. Unknown class on a fresh instance name is rejected (routing never reached).
  2. Unknown class is rejected even when an IDLE instance exists under that name
     (no recall special-case — the simplest correct behavior).
  3. A valid class on a fresh name routes through (async path).
  4. Case-insensitive template match passes (get_template fallback finds it).
  5. The error message lists ALL available classes, sorted.
  6. Empty templates → generic "No agent classes are registered" fallback.

All tests are self-contained — no LLM or API server required. Uses a lightweight
fake pool that extends the one in test_call_agent_self_and_resurrection_guard.py
with template support (get_template / list_agents).
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
    """Lightweight fake AgentPool exposing only what handle_call_agent needs.

    Extends the base fake with template support (get_template / list_agents) so
    the unknown-class guard can be exercised in isolation.
    """

    def __init__(self, instances=None, active_stack=None, templates=None):
        self.instances = dict(instances or {})
        self.instance_classes = {n: i.agent_class for n, i in self.instances.items()}
        self._execution = MagicMock()
        self._execution._state_lock = threading.RLock()
        self._execution.active_stack = list(active_stack or [])
        self.api_router = None  # → child needs no slot → ASYNC path
        self.settings = MagicMock()
        self.settings.max_nesting_depth = 10
        # Template registry: class name (as registered) -> Assistant template.
        self.templates = dict(templates if templates is not None else
                              {"coder": MagicMock(), "orchestrator": MagicMock(),
                               "reviewer": MagicMock()})

    def get_template(self, name: str):
        """Case-insensitive fallback mirroring pool/config_persist.py: exact → lowercase → titlecase."""
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
# 1. Unknown class on a fresh name is rejected
# ──────────────────────────────────────────────

class TestUnknownClassRejected:
    """An agent_class with no registered template is rejected early."""

    def test_unknown_class_fresh_name_rejected(self):
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "nonexistent")

        assert result.startswith("Error:")
        assert "nonexistent" in result  # names the requested class
        assert "coder" in result  # lists at least one available class
        # Rejection happens before routing — no child is spawned.
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()

    def test_unknown_class_rejected_even_with_idle_instance(self):
        """Existing IDLE 'worker1' (coder), request agent_class='bogus' → still rejected.

        Confirms we do NOT special-case the recall path: a genuinely unknown class
        is rejected regardless of whether an instance exists under that name.
        """
        existing = _make_mock_instance("worker1", "coder")  # IDLE
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"worker1": existing, "Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "bogus")

        assert result.startswith("Error:")
        assert "bogus" in result
        assert "coder" in result  # available-classes list present
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()


# ──────────────────────────────────────────────
# 2. Valid / case-insensitive classes route through
# ──────────────────────────────────────────────

class TestValidClassRoutesThrough:
    """A registered class (exact or via case fallback) passes the guard and routes."""

    def test_valid_class_fresh_name_routes(self):
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Maine": caller})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "coder")

        assert not result.startswith("Error:")
        # Routing reached — async path taken (no slot info → needs no slot).
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()

    def test_case_insensitive_template_match_routes(self):
        """Template registered as 'Coder' (capital C); request 'coder' (lowercase).
        get_template fallback finds it → routes through, NOT rejected."""
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(
            instances={"Maine": caller},
            templates={"Coder": MagicMock(), "Orchestrator": MagicMock()},
        )
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "coder")

        assert not result.startswith("Error:")
        dispatcher._run_child_async.assert_called_once()
        dispatcher._run_child_sync.assert_not_called()


# ──────────────────────────────────────────────
# 3. Error message lists ALL available classes
# ──────────────────────────────────────────────

class TestErrorMessageListsAllClasses:
    """The rejection error enumerates every registered class, sorted."""

    def test_error_lists_all_available_classes(self):
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(
            instances={"Maine": caller},
            templates={
                "coder": MagicMock(),
                "researcher": MagicMock(),
                "reviewer": MagicMock(),
                "writer": MagicMock(),
            },
        )
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "nonexistent")

        assert result.startswith("Error:")
        for cls in ("coder", "researcher", "reviewer", "writer"):
            assert cls in result, f"expected '{cls}' in error: {result}"


# ──────────────────────────────────────────────
# 4. Empty templates edge case
# ──────────────────────────────────────────────

class TestEmptyTemplatesEdgeCase:
    """No registered templates → generic fallback message (no class list)."""

    def test_empty_templates_fallback_message(self):
        caller = _make_mock_instance("Maine", "orchestrator")
        pool = FakePool(instances={"Maine": caller}, templates={})
        dispatcher = _make_dispatcher(pool)

        result = _run(dispatcher, caller, "worker1", "anything")

        assert result.startswith("Error:")
        assert "anything" in result  # names the requested class
        assert "No agent classes are registered" in result
        dispatcher._run_child_sync.assert_not_called()
        dispatcher._run_child_async.assert_not_called()
