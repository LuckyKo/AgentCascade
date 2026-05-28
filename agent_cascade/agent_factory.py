"""
Agent Factory — Unified tool registration and agent loading.

All agents are OrchestratorAgent instances (capable of spawning sub-agents).
The "main orchestrator" is just one more agent with its own soul.md, not a
special class. Tool availability is controlled via the disabled_tools policy,
not by which loader function was used.
"""

from agent_cascade.agents import Assistant
from agent_cascade.log import logger
from agent_cascade.tools.code_interpreter import CodeInterpreter
from agent_cascade.tools.custom import (
    ReadFile, ViewImage, WriteFile, EditFile, ListDir, Grep,
    DeleteFile, CopyFile, MoveFile, DismissAgent, ListAgents, ShellCmd, SystemInfo,
    ReadLogs, Calculate, CodeMap,
)
from agent_cascade.tools.custom.compression_tools import CompressContext
from agent_cascade.soul_loader import create_agent_from_soul
from agent_cascade.settings import DEFAULT_WORKSPACE


def register_standard_tools(agent, agent_pool, agent_name: str):
    """
    Register the standard tool suite on an agent instance.
    
    This is the SINGLE place where tool instances are created and wired
    to their agent_pool / agent_name.
    
    Args:
        agent: The agent to register tools on.
        agent_pool: The AgentPool instance (for file ops and approvals).
        agent_name: The role name (e.g. 'orchestrator', 'coder').
    """
    from agent_cascade.orchestrator_agent import _AgentInstanceFunctionProxy, CALL_AGENT_SCHEMA

    # ── Sub-agent management (intercepted in _run, not _call_tool) ──
    agent.function_map['call_agent'] = _AgentInstanceFunctionProxy(CALL_AGENT_SCHEMA)
    agent.function_map['dismiss_agent'] = DismissAgent(agent_pool=agent_pool)
    agent.function_map['list_agents'] = ListAgents(agent_pool=agent_pool)

    # ── Read-only tools (free access) ──
    read_tool = ReadFile()
    read_tool.agent_pool = agent_pool
    agent.function_map['read_file'] = read_tool

    view_tool = ViewImage()
    view_tool.agent_pool = agent_pool
    agent.function_map['view_image'] = view_tool

    list_tool = ListDir()
    list_tool.agent_pool = agent_pool
    agent.function_map['list_dir'] = list_tool

    grep_tool = Grep()
    grep_tool.agent_pool = agent_pool
    agent.function_map['grep'] = grep_tool

    # ── Mutating file tools (require user approval) ──
    write_tool = WriteFile()
    write_tool.agent_pool = agent_pool
    write_tool.agent_name = agent_name
    agent.function_map['write_file'] = write_tool

    edit_tool = EditFile()
    edit_tool.agent_pool = agent_pool
    edit_tool.agent_name = agent_name
    agent.function_map['edit_file'] = edit_tool

    delete_tool = DeleteFile()
    delete_tool.agent_pool = agent_pool
    delete_tool.agent_name = agent_name
    agent.function_map['delete_file'] = delete_tool

    copy_tool = CopyFile()
    copy_tool.agent_pool = agent_pool
    copy_tool.agent_name = agent_name
    agent.function_map['copy_file'] = copy_tool

    move_tool = MoveFile()
    move_tool.agent_pool = agent_pool
    move_tool.agent_name = agent_name
    agent.function_map['move_file'] = move_tool

    # ── Context compression ──
    compress_tool = CompressContext()
    compress_tool.agent_pool = agent_pool
    compress_tool.agent_name = agent_name
    agent.function_map['compress_context'] = compress_tool

    # ── Shell execution ──
    shell_tool = ShellCmd()
    shell_tool.agent_pool = agent_pool
    shell_tool.agent_name = agent_name
    agent.function_map['shell_cmd'] = shell_tool

    info_tool = SystemInfo()
    info_tool.agent_pool = agent_pool
    info_tool.agent_name = agent_name
    agent.function_map['system_info'] = info_tool

    # ── Log Reading ──
    read_logs_tool = ReadLogs()
    read_logs_tool.agent_pool = agent_pool
    agent.function_map['read_logs'] = read_logs_tool

    # ── Code Mapping ──
    code_map_tool = CodeMap()
    code_map_tool.agent_pool = agent_pool
    agent.function_map['code_map'] = code_map_tool

    # ── Code Interpreter (sandbox) ──
    try:
        om = agent_pool.operation_manager
        code_cfg = {'work_dir': str(om.base_dir)}
        if hasattr(om, 'extra_work_folders_ro') and om.extra_work_folders_ro:
            code_cfg['extra_work_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
        if hasattr(om, 'extra_work_folders_rw') and om.extra_work_folders_rw:
            code_cfg['extra_work_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]
        # Debug log when no extra folders are configured so operators know why nothing is mounted
        if not code_cfg.get('extra_work_folders_ro') and not code_cfg.get('extra_work_folders_rw'):
            logger.debug("CodeInterpreter: No extra work folders configured (RO/RW)")
        code_tool = CodeInterpreter(cfg=code_cfg)
        # Pass operation_manager reference so _start_kernel can read updated extra folders dynamically
        code_tool._operation_manager = om
        agent.function_map['code_interpreter'] = code_tool
    except Exception as e:
        logger.warning(f"Failed to load CodeInterpreter for agent {agent_name}: {e}")

    # ── Built-in agent_cascade tools ──
    from agent_cascade.tools.web_extractor import WebExtractor
    agent.function_map['web_extractor'] = WebExtractor(cfg={'work_dir': DEFAULT_WORKSPACE})

    from agent_cascade.tools.storage import Storage
    agent.function_map['storage'] = Storage()

    from agent_cascade.tools.retrieval import Retrieval
    agent.function_map['retrieval'] = Retrieval(cfg={'work_dir': DEFAULT_WORKSPACE})

    from agent_cascade.tools.extract_doc_vocabulary import ExtractDocVocabulary
    agent.function_map['extract_doc_vocabulary'] = ExtractDocVocabulary(cfg={'work_dir': DEFAULT_WORKSPACE})
    
    # ── Calculation Tool ──
    agent.function_map['calculate'] = Calculate()

    # ── User approval system notice ──
    agent.system_message += """
    
User Approval System:
- All mutating operations (file write, edit, delete, move, copy) require explicit user approval.
- When you call a tool like write_file or edit_file, the user will see a prompt and can approve or reject.
- If rejected, you'll receive the user's reason. Adjust your approach accordingly.
- Read operations (read_file, list_dir, grep, view_image) are free access.

Workspace & Path Reference:
- ALL file tool paths (read_file, write_file, edit_file, list_dir, grep, etc.) are RELATIVE to the workspace root.
  Example: to read "src/main.py" within your workspace, use path "src/main.py" (NOT an absolute host path).
- shell_cmd executes commands with the workspace directory as the working directory.
- code_interpreter runs Python inside a Docker container where the workspace is mounted at "/workspace/".
  So a file at "src/main.py" (used by host tools) is available at "/workspace/src/main.py" inside Docker.
  The container's working directory is /workspace, so relative paths like "src/main.py" also work.
  Additional mounted folders (if configured) appear as "/workspace/extra_rw_N" (writable) and
  "/workspace/extra_ro_N" (read-only). A path mapping file "path_mapping_{kernel_id}.json" in the
  work_dir lists all mounts with their host paths and access modes — read it to discover available folders.
- NEVER use host-absolute paths (like "N:\\..." or "/home/user/...") in any tool — they will not resolve correctly.
"""


def load_agent(agent_pool, agent_name: str, llm_cfg: dict = None) -> Assistant:
    """
    Load any agent (including the orchestrator) from its soul.md.
    
    Every agent is an OrchestratorAgent — fully capable of spawning and
    managing sub-agents. The "main orchestrator" is just another agent
    whose soul.md gives it a supervisor personality.
    
    Args:
        agent_pool: The AgentPool instance.
        agent_name: The agent's role name (e.g. 'orchestrator', 'coder').
        llm_cfg: Legacy fallback parameter (ignored if APIRouter is active).
        
    Returns:
        Fully configured OrchestratorAgent instance.
    """
    from agent_cascade.orchestrator_agent import OrchestratorAgent
    import copy

    # Ensure each agent gets its own distinct LLM instance config
    # to avoid state bleed across parallel threads.
    if hasattr(agent_pool, 'api_router'):
        agent_llm_cfg = agent_pool.api_router.get_llm_config(agent_name)
    else:
        agent_llm_cfg = llm_cfg or agent_pool.llm_cfg
        
    # Deepcopy to prevent shared references in the LLM object tree
    agent_llm_cfg = copy.deepcopy(agent_llm_cfg)

    soul_path = agent_pool.agents_dir / f'{agent_name}_soul.md'

    if soul_path.exists():
        agent, config = create_agent_from_soul(
            agent_llm_cfg,
            str(soul_path),
            agent_class=OrchestratorAgent,
            agent_pool=agent_pool,
            role_name=agent_name,
        )
    else:
        # Fallback: no soul.md — create with a generic prompt
        config = {}
        system_prompt = _default_agent_prompt(agent_pool, agent_name)
        agent = OrchestratorAgent(
            agent_pool=agent_pool,
            llm=agent_llm_cfg,
            name=agent_name.replace('_', ' ').title(),
            agent_type=agent_name.replace('_', ' ').title(),
            description=f"{agent_name.replace('_', ' ').title()} agent",
            system_message=system_prompt,
            function_list=[],
        )
        agent.agent_configs = {agent_name: config}
        agent.base_system_message = system_prompt

    # ── Full tool suite (same for every agent) ──
    register_standard_tools(agent, agent_pool, agent_name)

    return agent


# ── Backward-compatible aliases ──────────────────────────────────────────────

def load_orchestrator_agent(agent_pool, llm_cfg: dict) -> Assistant:
    """Load the orchestrator. Delegates to load_agent('orchestrator')."""
    return load_agent(agent_pool, 'orchestrator', llm_cfg)


def load_agent_template(agent_pool, agent_name: str, llm_cfg: dict) -> Assistant:
    """Load an agent template. Delegates to load_agent()."""
    return load_agent(agent_pool, agent_name, llm_cfg)


def _default_agent_prompt(agent_pool, agent_name: str) -> str:
    """Fallback system prompt when no soul.md exists."""
    prompt = f"""You are {agent_name.replace('_', ' ').title()}, an AI assistant that can coordinate with specialized agent instances.

Available sub-agents:
"""
    for name in agent_pool.list_agents():
        info = agent_pool.get_agent_info(name)
        if info:
            prompt += f"\n- **{info['name']}**: {info['tagline']}"

    prompt += """

Tools:
- call_agent: Delegate tasks to a specialized sub-agent
- dismiss_agent: Clear a sub-agent's conversation context
- list_agents: Show available sub-agents

Example of delegating a task:
{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "Write a script"}}
"""
    return prompt
