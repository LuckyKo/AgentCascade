"""
Soul Loader - Load agent personality from soul.md file
"""

import yaml
from pathlib import Path


def load_soul(soul_path: str = 'soul.md') -> dict:
    """
    Load agent configuration from a soul.md file.
    
    Args:
        soul_path: Path to the soul.md configuration file
        
    Returns:
        Dictionary with agent configuration
    """
    path = Path(soul_path)
    if not path.exists():
        raise FileNotFoundError(f"Soul file not found: {soul_path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Parse YAML (ignoring comments)
    config = yaml.safe_load(content)
    
    return config


def build_system_prompt(config: dict) -> str:
    """
    Build a system prompt from the soul configuration.
    
    Args:
        config: Configuration dictionary from load_soul()
        
    Returns:
        Formatted system prompt string
    """
    system_prompt = f"You are {config.get('name', 'Assistant')}.\n"
    if config.get('tagline'):
        system_prompt += f"{config.get('tagline')}\n"
    
    # 1. Identity section
    identity = config.get('identity', {})
    if identity:
        system_prompt += "\n## Who You Are\n"
        if identity.get('background'):
            system_prompt += f"{identity.get('background').strip()}\n"
        
        traits = identity.get('personality_traits', [])
        if traits:
            system_prompt += "\nPersonality traits:\n"
            for trait in traits:
                system_prompt += f"- {trait}\n"
    
    # 2. Communication style
    comm = config.get('communication', {})
    if comm:
        system_prompt += "\n## How You Communicate\n"
        if comm.get('tone'):
            system_prompt += f"Tone: {comm.get('tone')}\n"
        
        notes = comm.get('style_notes', [])
        if notes:
            system_prompt += "\nStyle guidelines:\n"
            for note in notes:
                system_prompt += f"- {note}\n"

    # 3. Features / Capabilities
    cap = config.get('capabilities', {})
    if cap:
        tools = cap.get('tools', [])
        if tools:
            system_prompt += "\n## Your Tools\nYou have access to these tools:\n"
            for tool in tools:
                system_prompt += f"- **{tool}**: Use when you need to {get_tool_description(tool)}\n"

    # 4. Rules
    rules = config.get('rules', [])
    if rules:
        system_prompt += "\n## Your Rules\n"
        for i, rule in enumerate(rules, 1):
            if isinstance(rule, dict):
                # Handle cases where YAML still parses as dict (legacy/unquoted)
                for k, v in rule.items():
                    system_prompt += f"{i}. {k}: {v}\n"
            else:
                system_prompt += f"{i}. {rule}\n"

    # 5. Dynamic Sections (Anything else in the YAML that isn't handled above)
    handled_keys = {'name', 'tagline', 'identity', 'communication', 'rules', 'capabilities', 'notes', 'remember'}
    
    for key, value in config.items():
        if key in handled_keys:
            continue
            
        # Format key to Title Case (e.g., operation_workflow -> Operation Workflow)
        section_title = key.replace('_', ' ').title()
        system_prompt += f"\n## {section_title}\n"
        
        def format_value(v, indent=0):
            res = ""
            spacing = "  " * indent
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, (list, dict)):
                        res += f"{spacing}- {format_value(item, indent + 1).strip()}\n"
                    else:
                        res += f"{spacing}- {item}\n"
            elif isinstance(v, dict):
                for k, val in v.items():
                    k_title = str(k).replace('_', ' ').title()
                    if isinstance(val, (list, dict)):
                        res += f"{spacing}### {k_title}\n{format_value(val, indent)}\n"
                    else:
                        res += f"{spacing}**{k_title}**: {val}\n"
            else:
                res += f"{spacing}{v}\n"
            return res

        system_prompt += format_value(value)

    # 6. Final Notes / Remember
    final_notes = config.get('notes') or config.get('remember')
    if final_notes:
        system_prompt += "\n## Remember\n"
        system_prompt += f"{final_notes.strip()}\n"
    
    return system_prompt


def get_tool_description(tool_name: str) -> str:
    """Get a brief description of what each tool is used for."""
    descriptions = {
        'get_weather': 'check current weather conditions',
        'web_search': 'search for current information online',
        'visit_website': 'read content from a specific URL',
        'code_interpreter': 'execute code for calculations or analysis',
    }
    return descriptions.get(tool_name, 'access external information')


def create_agent_from_soul(llm_cfg: dict, soul_path: str = 'soul.md', agent_class=None, role_name: str = None, **agent_kwargs):
    """
    Create an Agent from a soul.md file.
    
    Args:
        llm_cfg: LLM configuration dictionary
        soul_path: Path to the soul.md file
        agent_class: Optional class to instantiate (defaults to Assistant)
        **agent_kwargs: Additional arguments to pass to the agent constructor
        
    Returns:
        Configured agent instance
    """
    from agent_cascade.agents import Assistant
    
    if agent_class is None:
        agent_class = Assistant
    
    # Load soul configuration
    config = load_soul(soul_path)
    
    # Build system prompt
    system_prompt = build_system_prompt(config)
    
    # Note: Tools are added separately by the framework
    # Don't use function_list here as tools are added manually later
    
    # Build formatted name: "Role Name" (e.g., "Writer Bob")
    # Avoid duplication if the role name is already part of the name (e.g. "Writer Writer")
    raw_name = config.get('name', 'Assistant')
    
    # Normalize for comparison (replace underscores with spaces)
    role_norm = (role_name or "").lower().replace('_', ' ')
    name_norm = raw_name.lower().replace('_', ' ')
    
    if role_name and not name_norm.startswith(role_norm):
        formatted_name = f"{role_name.replace('_', ' ').title()} {raw_name}"
    else:
        formatted_name = raw_name

    # Create agent
    agent = agent_class(
        llm=llm_cfg,
        name=formatted_name,
        description=config.get('tagline', 'A helpful AI assistant'),
        system_message=system_prompt,
        function_list=[],  # Empty - tools added manually by agent_orchestrator
        agent_type=role_name.replace('_', ' ').title() if role_name else raw_name,
        **agent_kwargs
    )
    
    # Store config and base system message for later dynamic updates
    # Use role_name if available to ensure compatibility with AgentPool tracking (slug vs display name)
    config_key = role_name if role_name else config.get('name', 'assistant')
    agent.agent_configs = {config_key: config}
    agent.base_system_message = system_prompt

    return agent, config


# Example usage
if __name__ == '__main__':
    # Test loading the soul
    config = load_soul()
    print(f"Loaded agent: {config['name']}")
    print(f"Tagline: {config['tagline']}")
    print(f"\nSystem prompt preview:")
    print(build_system_prompt(config)[:500] + "...")
