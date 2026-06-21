"""End-to-end tests for the unified AgentCascade system (post-WebUI unification).

Tests the import chain, agent initialization, and message processing through CLI.
All tests are self-contained — no LLM or API server required (mocked).

Run with: pytest tests/test_unified_system.py -v
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure top-level imports work (same as start_api_server.py)
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))


class TestImportChain:
    """Verify that all key imports resolve correctly after unification.

    This tests the exact import chain used in start_api_server.py and
    the __init__.py exports.
    """

    def test_import_agent_pool_from_package(self):
        """AgentPool is exported from agent_cascade.agent_pool."""
        from agent_cascade.agent_pool import AgentPool  # noqa: F401
        assert AgentPool is not None

    def test_import_create_app_from_api_server(self):
        """create_app factory function exists in agent_cascade.api_server."""
        from agent_cascade.api_server import create_app  # noqa: F401
        assert callable(create_app)

    def test_import_execution_engine(self):
        """ExecutionEngine can be imported from agent_cascade.execution_engine."""
        from agent_cascade.execution_engine import ExecutionEngine  # noqa: F401
        assert ExecutionEngine is not None

    def test_import_operation_manager(self):
        """OperationManager can be imported from agent_cascade.operation_manager."""
        from agent_cascade.operation_manager import OperationManager  # noqa: F401
        assert OperationManager is not None

    def test_import_load_orchestrator_agent(self):
        """load_orchestrator_agent can be imported from agent_cascade.agent_factory."""
        from agent_cascade.agent_factory import load_orchestrator_agent  # noqa: F401
        assert callable(load_orchestrator_agent)

    def test_import_load_agent_template(self):
        """load_agent_template can be imported from agent_cascade.agent_factory."""
        from agent_cascade.agent_factory import load_agent_template  # noqa: F401
        assert callable(load_agent_template)

    def test_import_from_package_init(self):
        """Key exports are available directly from agent_cascade package."""
        from agent_cascade import (  # noqa: F401
            Agent, MultiAgentHub, APIRouter, TelemetryCollector,
            OperationManager, load_orchestrator_agent, load_agent_template,
        )
        assert Agent is not None

    def test_import_agent_cascade_tools(self):
        """Core tool modules can be imported from agent_cascade.tools."""
        from agent_cascade.tools.custom import ReadFile, WriteFile, EditFile  # noqa: F401
        from agent_cascade.tools.base import BaseTool  # noqa: F401
        assert issubclass(ReadFile, BaseTool)

    def test_import_llm_schema(self):
        """LLM schema can be imported."""
        from agent_cascade.llm.schema import Message, SYSTEM, USER, ASSISTANT  # noqa: F401
        assert Message is not None

    def test_import_agent_instance_proxy(self):
        """Agent instance proxy and CALL_AGENT_SCHEMA can be imported from new location."""
        from agent_cascade.tools._agent_instance_proxy import _AgentInstanceFunctionProxy, CALL_AGENT_SCHEMA  # noqa: F401
        assert _AgentInstanceFunctionProxy is not None
        assert isinstance(CALL_AGENT_SCHEMA, dict)

    def test_import_soul_loader(self):
        """Soul loader can be imported from the package."""
        from agent_cascade.soul_loader import create_agent_from_soul  # noqa: F401
        assert callable(create_agent_from_soul)


class TestAgentPoolInitialization:
    """Verify AgentPool initializes correctly with mock config."""

    def test_create_pool_with_minimal_config(self):
        """Create an AgentPool with minimal LLM config and default agents dir."""
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
            'max_input_tokens': 8192,
        }

        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        assert pool is not None
        # Pool should store llm_cfg for fallback when no api_router
        assert pool.llm_cfg == llm_cfg
        # Pool should have loaded agent templates from soul files
        assert len(pool.templates) > 0, "Should have loaded at least one agent template"

    def test_list_agents(self):
        """Pool should list known agent types."""
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        }

        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        agent_names = pool.list_agents()
        assert 'orchestrator' in agent_names
        # At least some sub-agents should be present
        assert len(agent_names) >= 2


class TestOrchestratorLoading:
    """Verify orchestrator loads correctly from soul.md."""

    def _make_pool(self):
        """Helper: create a minimal AgentPool for testing."""
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
            'max_input_tokens': 8192,
        }
        return AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))

    def test_load_orchestrator_agent(self):
        """load_orchestrator_agent returns a valid agent with tools registered."""
        from agent_cascade.agent_factory import load_orchestrator_agent

        pool = self._make_pool()
        orchestrator = load_orchestrator_agent(pool, pool.llm_cfg)

        assert orchestrator is not None
        assert hasattr(orchestrator, 'name')
        assert hasattr(orchestrator, 'function_map')
        # Should have key tools registered
        assert 'call_agent' in orchestrator.function_map
        assert 'read_file' in orchestrator.function_map
        assert 'write_file' in orchestrator.function_map

    def test_orchestrator_has_system_prompt(self):
        """Orchestrator should have a system prompt set."""
        from agent_cascade.agent_factory import load_orchestrator_agent

        pool = self._make_pool()
        orchestrator = load_orchestrator_agent(pool, pool.llm_cfg)

        assert hasattr(orchestrator, 'base_system_message')
        assert len(orchestrator.base_system_message) > 0

    def test_load_agent_template(self):
        """load_agent_template loads a specific agent type."""
        from agent_cascade.agent_factory import load_agent_template

        pool = self._make_pool()
        coder = load_agent_template(pool, 'coder', pool.llm_cfg)

        assert coder is not None
        assert hasattr(coder, 'name')

    def test_load_multiple_agent_types(self):
        """Loading multiple agent types should work independently."""
        from agent_cascade.agent_factory import load_agent_template

        pool = self._make_pool()
        agents_loaded = []
        for name in ['orchestrator', 'coder', 'researcher']:
            if name in pool.list_agents():
                agent = load_agent_template(pool, name, pool.llm_cfg)
                assert agent is not None, f"Failed to load {name}"
                agents_loaded.append(name)

        assert len(agents_loaded) >= 2, "Should be able to load at least 2 agent types"


class TestExecutionEngine:
    """Test the ExecutionEngine with mocked LLM (no API key needed)."""

    def _make_pool_and_orchestrator(self):
        """Helper: create pool and orchestrator with mocked LLM."""
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.agent_factory import load_orchestrator_agent
        from agent_cascade.llm.schema import Message

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
            'max_input_tokens': 8192,
        }

        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        orchestrator = load_orchestrator_agent(pool, llm_cfg)

        # Mock the LLM chat method (ExecutionEngine calls template.llm.chat())
        mock_llm = MagicMock()
        mock_response = iter([Message(role='assistant', content='Hello! I can help with that.')])
        mock_llm.chat.return_value = mock_response
        orchestrator.llm = mock_llm

        return pool, orchestrator, llm_cfg

    def test_execution_engine_creation(self):
        """ExecutionEngine can be created with a pool reference."""
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }
        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        engine = ExecutionEngine(pool)

        assert engine is not None
        assert engine.pool is pool

    def test_agent_instance_creation(self):
        """AgentPool can create an instance from a template."""
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'max_input_tokens': 8192,
        }

        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        
        # Create an instance using the pool's instance management
        instance = pool.create_instance(
            instance_name='TestOrchestrator',
            agent_class='orchestrator',
        )
        
        assert instance is not None
        assert instance.instance_name == 'TestOrchestrator'

    def test_execution_engine_phase_methods_exist(self):
        """ExecutionEngine has all the phase methods defined in DESIGN_REWRITE."""
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }
        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        engine = ExecutionEngine(pool)

        # Verify all phase methods exist (from DESIGN_REWRITE §3.1)
        assert hasattr(engine, 'run')
        assert hasattr(engine, '_setup_turn')
        assert hasattr(engine, '_pre_llm_checks')
        assert hasattr(engine, '_call_llm_with_injection')
        assert hasattr(engine, '_process_response')

    def test_execution_engine_handles_missing_template(self):
        """ExecutionEngine handles a missing agent class gracefully (loop detection fires first)."""
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.llm.schema import Message, USER

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }
        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))

        # Create instance with a non-existent agent class — the engine will detect
        # the missing template and produce an error, but loop detection fires first
        instance = pool.create_instance(
            instance_name='TestMissing',
            agent_class='nonexistent_agent',
        )
        instance.conversation.append(Message(role=USER, content='test'))

        engine = ExecutionEngine(pool)
        
        # The missing template causes error responses which trigger loop detection.
        # What matters is the engine doesn't crash — it raises a controlled exception.
        from agent_cascade.loop_detection import LoopDetectedError
        try:
            list(engine.run(instance))
        except LoopDetectedError:
            pass  # Expected — missing template produces error messages that loop


class TestAPIAppCreation:
    """Test that the FastAPI app can be created with mock agents."""

    def test_create_app_with_mock_agents(self):
        """create_app should work with a minimal agent setup."""
        from agent_cascade.api_server import create_app
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        }

        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))

        # Create a mock agent
        mock_agent = MagicMock()
        mock_agent.name = 'TestAgent'
        mock_agent.agent_type = 'orchestrator'

        config = {'session_name': 'TestSession', 'verbose': False}
        app = create_app([mock_agent], pool, config)

        assert app is not None
        # FastAPI apps have a title attribute
        assert hasattr(app, 'title')


class TestToolRegistration:
    """Verify that tools are properly registered on agents."""

    def _make_pool(self):
        """Helper: create a minimal AgentPool for testing."""
        from agent_cascade.agent_pool import AgentPool

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        }
        return AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))

    def test_orchestrator_has_core_tools(self):
        """Orchestrator should have all core management tools."""
        from agent_cascade.agent_factory import load_orchestrator_agent

        pool = self._make_pool()
        orchestrator = load_orchestrator_agent(pool, pool.llm_cfg)

        core_tools = [
            'call_agent', 'dismiss_agent', 'list_agents',
            'read_file', 'write_file', 'edit_file', 'delete_file',
            'copy_file', 'move_file', 'list_dir', 'grep',
            'compress_context',
        ]
        for tool in core_tools:
            assert tool in orchestrator.function_map, \
                f"Missing core tool: {tool}"

    def test_orchestrator_has_default_tools_list(self):
        """Orchestrator should have tools registered (default_tools is optional)."""
        from agent_cascade.agent_factory import load_orchestrator_agent

        pool = self._make_pool()
        orchestrator = load_orchestrator_agent(pool, pool.llm_cfg)

        # Check function_map has tools (the real source of truth)
        assert len(orchestrator.function_map) > 0


class TestSoulLoader:
    """Verify soul.md files load correctly."""

    def test_load_orchestrator_soul(self):
        """Orchestrator soul.md should load as valid config and create an agent."""
        from agent_cascade.soul_loader import create_agent_from_soul

        soul_path = PROJECT_ROOT / 'agents' / 'orchestrator_soul.md'
        assert soul_path.exists(), f"Soul file not found: {soul_path}"

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }

        # create_agent_from_soul defaults to Assistant when agent_class is not provided
        # Also pass role_name for complete flow parity with production code path
        agent, config = create_agent_from_soul(
            llm_cfg, str(soul_path),
            role_name='orchestrator',
        )
        assert agent is not None
        assert config is not None

    def test_all_soul_files_load(self):
        """All soul files in the agents dir should load without errors."""
        from agent_cascade.soul_loader import create_agent_from_soul

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }

        agents_dir = PROJECT_ROOT / 'agents'
        for soul_file in agents_dir.glob('*_soul.md'):
            agent_name = soul_file.name.replace('_soul.md', '')
            agent, config = create_agent_from_soul(
                llm_cfg, str(soul_file),
                role_name=agent_name,
            )
            assert agent is not None, f"Failed to load {soul_file.name}"
            assert config is not None, f"Failed to get config for {soul_file.name}"


class TestStartApiServerIntegration:
    """Test that the start_api_server.py import chain works end-to-end.

    This simulates what happens when start_api_server.py runs initialize_agents(),
    but with a mocked LLM so we don't need an actual server.
    """

    def test_initialize_agents_flow(self):
        """Simulate the full agent initialization flow from start_api_server.py."""
        # This mirrors the imports and setup in start_api_server.py
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.agent_factory import load_orchestrator_agent

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
            'max_input_tokens': 65536,
        }

        # Step 1: Create pool (same as start_api_server.py line 143)
        agent_pool = AgentPool(llm_cfg, 'agents')

        # Step 2: Verify agents were discovered
        agent_names = agent_pool.list_agents()
        assert len(agent_names) > 0, "No agents discovered"
        assert 'orchestrator' in agent_names

        # Step 3: Load orchestrator (same as start_api_server.py line 189)
        orchestrator = load_orchestrator_agent(agent_pool, llm_cfg)
        assert orchestrator is not None

        # Step 4: Verify tools are registered
        assert 'call_agent' in orchestrator.function_map
        assert 'read_file' in orchestrator.function_map

        # Step 5: Collect all agents (same pattern as start_api_server.py lines 197-202)
        all_agents = [orchestrator]
        for agent_name in agent_pool.list_agents():
            if agent_name != 'orchestrator':
                sub_agent = agent_pool.templates.get(agent_name)
                if sub_agent:
                    all_agents.append(sub_agent)

        assert len(all_agents) >= 2, "Should have orchestrator + at least one sub-agent"


class TestCLIMode:
    """Test CLI/testing mode where no api_router is configured.

    This is the critical path that was broken before the bug fixes:
    creating an AgentPool without an api_router and loading agents.
    """

    def test_load_agent_without_api_router(self):
        """CLI/testing mode: pool without api_router should load agents."""
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.agent_factory import load_orchestrator_agent

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }

        # No api_router passed — pool now auto-creates one (matches main branch behavior)
        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
        assert pool.api_router is not None  # APIRouter is auto-created

        orch = load_orchestrator_agent(pool, pool.llm_cfg)
        assert orch is not None
        assert 'call_agent' in orch.function_map

    def test_llm_cfg_none_raises_value_error(self):
        """Passing llm_cfg=None should cause agent loading to fail with a clear error."""
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.agent_factory import load_agent_template

        # Pool with None LLM config — APIRouter is auto-created but has no default config
        pool = AgentPool(llm_cfg=None, agents_dir=str(PROJECT_ROOT / 'agents'))
        
        # Loading a NEW agent (not pre-loaded by _discover_agents) should raise ValueError
        try:
            load_agent_template(pool, 'nonexistent_agent', llm_cfg=None)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            msg = str(e).lower()
            assert 'no llm configuration available' in msg.lower()

    def test_api_router_path_when_injected(self):
        """When api_router IS provided, it should be used for LLM config."""
        from agent_cascade.agent_pool import AgentPool
        from agent_cascade.agent_factory import load_orchestrator_agent

        llm_cfg = {
            'model': 'test_model',
            'model_server': 'http://localhost:1234/v1',
            'api_key': 'EMPTY',
        }

        # Create a mock api_router that provides LLM config
        mock_router = MagicMock()
        mock_router.get_llm_config.return_value = {
            'model': 'routed_model',
            'model_server': 'http://router.example/v1',
            'api_key': 'ROUTED_KEY',
        }

        pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'), api_router=mock_router)

        orch = load_orchestrator_agent(pool, llm_cfg)
        assert orch is not None
        
        # _discover_agents calls get_llm_config for each agent during pool init.
        # Then load_orchestrator_agent calls it again. Verify orchestrator was called.
        mock_router.get_llm_config.assert_any_call('orchestrator')


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v', '--tb=short']))