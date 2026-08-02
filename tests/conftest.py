"""Pytest configuration for AgentCascade test suite.

Provides test isolation fixtures so tests never touch production config files.
"""

import os
from typing import Any, Dict, List, Optional
import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.llm.schema import SYSTEM, USER


class MockInstance:
    """Minimal mock of AgentInstance for compression tests."""

    def __init__(self, conversation: List[Any]):
        self.conversation = list(conversation)


class _MockInstanceConversationMapping(Dict[str, List[Any]]):
    """Dict subclass that syncs writes to instances[name].conversation immediately.

    Mirrors production AgentPool.instance_conversations behavior: the mapping is
    a view onto instance.conversation lists, not separate storage.
    """

    def __init__(self, pool: "MockAgentPool"):
        super().__init__()
        self._pool = pool

    def __getitem__(self, key: str) -> List[Any]:
        inst = self._pool.instances.get(key)
        if inst is None:
            return []
        return list(inst.conversation)

    def __setitem__(self, key: str, value: List[Any]) -> None:
        inst = self._pool.instances.get(key)
        if inst is not None:
            inst.conversation[:] = list(value)

    def __contains__(self, key: object) -> bool:
        return key in self._pool.instances


class MockAgentPool:
    """Mock AgentPool for compression tests.

    Implements the subset of AgentPool used by compress_context and helpers.
    Matches production logic from agent_pool.py for target-set calculation
    and marker detection.
    """

    def __init__(self, history: Optional[List[Any]] = None):
        self.instance_name: str = "TestAgent"
        self.instances: Dict[str, MockInstance] = {}
        self.instance_conversations = _MockInstanceConversationMapping(self)

        # Create TestAgent instance with the provided history
        if history is not None:
            self.instances["TestAgent"] = MockInstance(history)

    @staticmethod
    def _msg_field(msg: Any, field: str, default: Any = "") -> Any:
        """Extract a field from a message (dict or Message object)."""
        return msg.get(field, default) if isinstance(msg, dict) else getattr(msg, field, default)

    def get_conversation(self, agent_name: str) -> List[Any]:
        """Get the conversation list for an agent. Returns empty list if not found."""
        inst = self.instances.get(agent_name)
        if inst is None:
            return []
        return list(inst.conversation)

    def get_compression_target_set(self, instance_name: str):
        """Returns (active_start_idx, active_set, latest_summary_idx) for compression.

        Delegates to get_compression_target_set_from_conversation after fetching conv.
        Matches production logic from agent_pool.py:2093-2123.
        """
        conv = self.get_conversation(instance_name)
        if not conv:
            return 0, [], -1

        return self.get_compression_target_set_from_conversation(instance_name, conv)

    def get_compression_target_set_from_conversation(
        self, instance_name: str, conv: List[Any]
    ):
        """Like get_compression_target_set but accepts a pre-fetched conversation snapshot.

        Matches production logic from agent_pool.py:2125-2142:
        - Skip SYSTEM+U0 unless a compression marker exists.
        - When marker exists, active starts right after it.
        """
        if not conv:
            return 0, [], -1

        latest_marker = self.find_last_marker(conv)

        if latest_marker >= 0:
            active_start_idx = latest_marker + 1
        else:
            # Skip system message at index 0 AND first user message (U0)
            first_role = self._msg_field(conv[0], "role")
            active_start_idx = 2 if first_role == SYSTEM else 1

        active_set = conv[active_start_idx:]
        return active_start_idx, active_set, latest_marker

    @staticmethod
    def find_last_marker(history: List[Any]) -> int:
        """Find the index of the last COMPRESSION_MARKER message in a conversation.

        Only considers messages with role=USER (compression markers are user messages).
        Returns -1 if no marker is found.
        Matches production logic from agent_pool.py:2514-2527.
        """
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = MockAgentPool._msg_field(msg, "role")
            content = MockAgentPool._msg_field(msg, "content")
            # Only consider USER messages (compression markers are always user role)
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def get_agent(self, name: str):
        """Return None — tests mock invoke_compression_agent directly.

        Returning a MagicMock for Compressor causes issues because core.py
        inspects comp_agent.llm.generate_cfg.get('max_input_tokens'), and
        MagicMock returns another MagicMock which breaks numeric comparisons.
        """
        return None

    def load_agent(self, name: str) -> None:
        """No-op — tests mock invoke_compression_agent directly."""
        pass


@pytest.fixture(scope="session", autouse=True)
def isolated_config_dir(tmp_path_factory):
    """Set AGENT_CASCADE_TEST_CONFIG_DIR to a temp directory for the entire test session.

    This ensures all APIRouter instances created during tests use an isolated config
    directory instead of the production project-root/config directory.
    """
    test_config = tmp_path_factory.mktemp("test_config")
    os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(test_config)
    yield test_config
    # Cleanup not strictly necessary (tmp_path handles it), but explicit is clear
    os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)