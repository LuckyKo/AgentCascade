"""Shared mock-pool / mock-instance helpers for call_agent slot tests.

Extracted from test_call_agent_ancestor_collision.py and
test_call_agent_sync_async_selection.py, which previously each carried a ~30-line
copy of the same pool-construction logic (polish review finding #1).

The factories are parameterized so both suites share ONE implementation:
  - make_mock_instance accepts optional slot_key / parent_instance / state / nest_depth.
  - make_mock_pool builds a mock AgentPool from an explicit instance registry (the
    caller is just the first entry) with a real case-insensitive resolver.

No LLM or network. Pure test doubles.
"""

import threading
from typing import List, Optional
from unittest.mock import MagicMock

from agent_cascade.tool_dispatcher import ToolDispatcher


def make_mock_instance(
    instance_name: str = 'caller1',
    agent_class: str = 'coder',
    slot_release: Optional[callable] = None,
    state: str = 'RUNNING',
    nest_depth: int = 0,
    slot_key: Optional[str] = None,
    parent_instance: Optional[str] = None,
):
    """Mock AgentInstance with EXPLICIT slot state (plan §5.2).

    `slot_key` and `parent_instance` are set explicitly (defaulting to None): a bare
    MagicMock would auto-create them as truthy stubs, which would poison the sync/async
    pool-intersection check in the dispatcher (it reads the direct caller's `_slot_key`)
    and mislead any parent-chain traversal.
    """
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = MagicMock(name=f"{instance_name}_state")
    inst.state.name = state
    inst._slot_release = slot_release
    inst._nest_depth = nest_depth
    inst._slot_key = slot_key
    inst.parent_instance = parent_instance
    return inst


def make_mock_pool(router, instances: List, max_nesting_depth: int = 10):
    """Mock AgentPool with a real router and an explicit instance registry.

    The caller is simply the first entry in `instances`; every name resolves through
    a case-insensitive resolver (mirrors pool/lifecycle.py) so the self-call /
    resurrection identity guards in handle_call_agent work correctly.
    """
    pool = MagicMock()
    pool.api_router = router
    pool.settings = MagicMock()
    pool.settings.max_nesting_depth = max_nesting_depth

    pool.instances = {inst.instance_name: inst for inst in instances}
    pool.get_instance.side_effect = lambda name: pool.instances.get(name)

    # Track which path was taken
    pool.register_async_call = MagicMock()

    pool.instance_conversations = {}
    pool.instance_classes = {}

    def _resolve(name, exclude=None):
        name = name.strip()
        for n in pool.instances:
            if n != exclude and n.lower() == name.lower():
                return n
        return name

    pool._resolve_instance_name.side_effect = _resolve
    return pool


def create_dispatcher(pool, mock_engine=None):
    """ToolDispatcher with a controllable mocked engine. Returns (dispatcher, engine)."""
    if mock_engine is None:
        mock_engine = MagicMock()
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(mock_engine)
    return dispatcher, mock_engine
