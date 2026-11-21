"""BUG_0373 / BUG_0374 — system-launched Compressor is fully tool-less.

BUG_0373: the system Compressor previously passed the caller's UI-disabled-tools config
through (or left ``disabled_tools`` unset), so it could keep ``load_skill`` / ``shell_cmd``
available and emit a malformed empty-name ``load_skill`` retry loop (BUG_0374). The fix forces
the Compressor to the same all-tools-disabled union the reviewer uses, and turns a degenerate
``load_skill`` call (from any agent) into a soft no-op rather than a counted "not found" failure.

All tests are self-contained — no LLM or API server required.
"""

from unittest.mock import MagicMock

import pytest

from agent_cascade.compression.agent_invoker import _configure_compressor_instance
from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS
from agent_cascade.tools.base import TOOL_REGISTRY
from agent_cascade.tools.custom.load_skill import LoadSkill


class TestCompressorIsToolless:
    """The system Compressor is forced tool-less — every known tool disabled (BUG_0373)."""

    def test_compressor_is_toolless(self):
        """`_configure_compressor_instance` disables every known tool, mirroring the reviewer.

        The assertion mirrors ``test_compaction_reviewer_gate``'s reviewer check so the single
        source of truth (``TOOL_REGISTRY | RUNTIME_REGISTERED_TOOLS + __all_mcp_tools__``) is
        proven to reach the Compressor regardless of the caller's UI config.
        """
        pool = MagicMock()  # retained for call-site compatibility; unused by the forced union
        comp_instance = MagicMock()

        _configure_compressor_instance(pool, comp_instance, 'Caller')

        disabled = set(comp_instance._generate_cfg_override['disabled_tools'])
        assert set(TOOL_REGISTRY) <= disabled
        assert set(RUNTIME_REGISTERED_TOOLS) <= disabled
        assert '__all_mcp_tools__' in disabled
        # Specifically the tools that produced the BUG_0374 retry loop must be off.
        for tool in ('load_skill', 'shell_cmd', 'calculate'):
            assert tool in disabled


class TestLoadSkillDegenerateNoop:
    """A degenerate ``load_skill`` call is a soft no-op, not a counted failure (BUG_0374)."""

    @pytest.mark.parametrize('skill_names', ['"', '', '   ', '\t', '!"'])
    def test_load_skill_degenerate_noop(self, skill_names):
        """Malformed skill_names short-circuit to a no-op success before any pool/instance work.

        A lone quote / empty / whitespace-only name never matches the skill-name regex, so it is
        dropped and the call returns a benign success — NOT the "not found" hard failure.
        """
        # agent_pool is irrelevant here: the degenerate filter returns before the pool check,
        # which is the whole point — a malformed call is answered without touching infra.
        result = LoadSkill(agent_pool=None).call({'skill_names': skill_names})

        assert 'nothing to load' in result.lower()
        assert 'not found' not in result.lower()
        assert 'Failed to load' not in result
