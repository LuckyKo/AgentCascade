"""
Soul Loader - Load agent personality from soul.md file
"""

import yaml
from pathlib import Path


def _preprocess_soul_content(content: str) -> str:
    """
    Pre-process soul.md content to fix common YAML formatting quirks.
    
    Handles:
    - Trailing whitespace on each line
    - Indented continuation lines in list items (e.g., a list item followed
      by a more-indented line without a "- " prefix gets merged into it)
    - Nested list indentation normalization (nested items at irregular indent
      levels are normalized to parent_indent + 2)
    - Colon quoting for merged list items to prevent YAML key-value parsing
    """
    lines = content.split('\n')
    
    # Strip trailing whitespace from every line
    lines = [line.rstrip() for line in lines]
    
    # Merge indented continuation lines into their parent list item.
    # A continuation line is one that:
    #   - starts with more leading whitespace than the list marker
    #   - does NOT start with "- " (i.e., it's not a new list item)
    merged = []
    for i, line in enumerate(lines):
        if not line:
            merged.append(line)
            continue
        
        # Check if this is a continuation of the previous list item
        if i > 0 and merged[-1]:
            prev = merged[-1]
            prev_stripped = prev.lstrip()
            if prev_stripped.startswith('- '):
                prev_indent = len(prev) - len(prev_stripped)
                curr_stripped = line.lstrip()
                curr_indent = len(line) - len(curr_stripped)
                if curr_indent > prev_indent and not curr_stripped.startswith('- '):
                    # Append continuation text to previous line
                    merged[-1] = prev + ' ' + curr_stripped
                    continue
        
        merged.append(line)
    
    # Normalize nested list indentation: track indent stack so each level
    # is exactly 2 spaces deeper than its parent, preserving hierarchy.
    normalized = []
    indent_stack = []
    for line in merged:
        stripped = line.lstrip()
        if stripped.startswith('- '):
            curr_indent = len(line) - len(stripped)
            while indent_stack and indent_stack[-1] >= curr_indent:
                indent_stack.pop()
            if indent_stack:
                expected = indent_stack[-1] + 2
                if curr_indent != expected:
                    line = ' ' * expected + stripped
                    curr_indent = expected
            indent_stack.append(curr_indent)
        elif not stripped:
            indent_stack.clear()
        normalized.append(line)
    
    # Quote list items containing colons to prevent YAML key-value parsing.
    # Skip quoting if the next line is a nested list item (the colon is the key).
    quoted = []
    for i, line in enumerate(normalized):
        stripped = line.lstrip()
        if stripped.startswith('- '):
            item_text = stripped[2:]
            # Check if next line is a nested list item at deeper indent
            has_nested = False
            if i + 1 < len(normalized):
                next_line = normalized[i + 1]
                next_stripped = next_line.lstrip()
                if next_stripped.startswith('- '):
                    next_indent = len(next_line) - len(next_stripped)
                    curr_indent = len(line) - len(stripped)
                    if next_indent > curr_indent:
                        has_nested = True
            if ':' in item_text and not has_nested:
                item_text = item_text.replace('"', '\\"')
                line = line[:len(line) - len(stripped)] + '- ' + '"' + item_text + '"'
        quoted.append(line)
    
    return '\n'.join(quoted)


def load_soul(soul_path: str = 'soul.md') -> dict:
    """
    Load agent configuration from a soul.md file.
    
    Args:
        soul_path: Path to the soul.md configuration file
        
    Returns:
        Dictionary with agent configuration
        
    Raises:
        FileNotFoundError: If the soul file doesn't exist
        yaml.YAMLError: If the YAML is malformed (with helpful context)
    """
    path = Path(soul_path)
    if not path.exists():
        raise FileNotFoundError(f"Soul file not found: {soul_path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Pre-process to handle common formatting quirks
    content = _preprocess_soul_content(content)
    
    # Parse YAML with error handling
    try:
        config = yaml.safe_load(content)
    except yaml.YAMLError as e:
        # Build a helpful error message with file path and error details
        error_msg = f"Failed to parse soul file: {soul_path}\n"
        mark = getattr(e, 'problem_mark', None)
        if mark is not None:
            error_msg += f"  Line {mark.line + 1}: {e.problem or e}"
        else:
            error_msg += f"  {e}"
        raise yaml.YAMLError(error_msg) from e
    
    if not isinstance(config, dict):
        raise ValueError(f"Soul file must contain a YAML mapping, got {type(config).__name__}: {soul_path}")
    
    return config


def _format_value(v, indent=0):
    """
    Recursively format a value (list, dict, or scalar) into markdown text.
    
    Dict-in-list items are formatted with bold key prefixes.
    Nested dicts use ### sub-headings.
    Empty dicts are skipped.
    """
    res = ""
    spacing = "  " * indent
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict):
                if not item:
                    continue
                elif len(item) == 1:
                    k, val = next(iter(item.items()))
                    k_title = str(k).replace('_', ' ').title()
                    if isinstance(val, (list, dict)):
                        res += f"{spacing}- **{k_title}**\n"
                        res += _format_value(val, indent + 1)
                    else:
                        res += f"{spacing}- **{k_title}**: {val}\n"
                else:
                    for k, val in item.items():
                        k_title = str(k).replace('_', ' ').title()
                        if isinstance(val, (list, dict)):
                            res += f"{spacing}- **{k_title}**\n"
                            res += _format_value(val, indent + 1)
                        else:
                            res += f"{spacing}- **{k_title}**: {val}\n"
            elif isinstance(item, list):
                # Format nested list without extra dash prefix
                formatted = _format_value(item, indent + 1)
                res += formatted
            else:
                res += f"{spacing}- {item}\n"
    elif isinstance(v, dict):
        for k, val in v.items():
            k_title = str(k).replace('_', ' ').title()
            if isinstance(val, (list, dict)):
                res += f"{spacing}### {k_title}\n{_format_value(val, indent)}\n"
            else:
                res += f"{spacing}**{k_title}**: {val}\n"
    else:
        res += f"{spacing}{v}\n"
    return res


def get_tool_description(tool_name: str) -> str:
    """Get a brief description of what each tool is used for."""
    descriptions = {
        'get_weather': 'check current weather conditions',
        'web_search': 'search for current information online',
        'visit_website': 'read content from a specific URL',
        'code_interpreter': 'execute code for calculations or analysis',
    }
    return descriptions.get(tool_name, 'access external information')


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
    if isinstance(identity, dict) and identity:
        system_prompt += "\n## Who You Are\n"
        role = identity.get('role')
        if isinstance(role, str) and role.strip():
            system_prompt += f"Role: {role.strip()}\n"
        mission = identity.get('mission')
        if isinstance(mission, str) and mission.strip():
            system_prompt += f"Mission: {mission.strip()}\n"
        bg = identity.get('background')
        if isinstance(bg, str) and bg.strip():
            system_prompt += f"{bg.strip()}\n"
        
        traits = identity.get('personality_traits', [])
        if isinstance(traits, list) and traits:
            system_prompt += "\nPersonality traits:\n"
            for trait in traits:
                system_prompt += f"- {trait}\n"
    
    # 2. Communication style
    comm_cfg = config.get('communication', {})
    if isinstance(comm_cfg, dict) and comm_cfg:
        system_prompt += "\n## How You Communicate\n"
        if comm_cfg.get('tone'):
            system_prompt += f"Tone: {comm_cfg.get('tone')}\n"
        
        notes = comm_cfg.get('style_notes', [])
        if isinstance(notes, list) and notes:
            system_prompt += "\nStyle guidelines:\n"
            for note in notes:
                system_prompt += f"- {note}\n"
        
        principles = comm_cfg.get('principles', [])
        if isinstance(principles, list) and principles:
            system_prompt += "\nPrinciples:\n"
            for principle in principles:
                system_prompt += f"- {principle}\n"

    # 3. Features / Capabilities
    cap = config.get('capabilities', {})
    if isinstance(cap, dict):
        tools = cap.get('tools', [])
        if isinstance(tools, list) and tools:
            system_prompt += "\n## Your Tools\nYou have access to these tools:\n"
            for tool in tools:
                system_prompt += f"- **{tool}**: Use when you need to {get_tool_description(tool)}\n"

    # 4. Rules
    rules = config.get('rules', [])
    if isinstance(rules, list) and rules:
        system_prompt += "\n## Your Rules\n"
        for i, rule in enumerate(rules, 1):
            if isinstance(rule, dict):
                # YAML may parse multi-line strings as dicts — normalize to key: value format
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
        system_prompt += _format_value(value)

    # 6. Final Notes / Remember
    final_notes = config.get('notes') or config.get('remember')
    if isinstance(final_notes, str) and final_notes.strip():
        system_prompt += "\n## Remember\n"
        system_prompt += f"{final_notes.strip()}\n"
    
    return system_prompt


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
        **agent_kwargs
    )

    # Store role info for tool filtering and logging
    if role_name:
        agent.agent_type = role_name.replace('_', ' ').title()
    
    # Store config and base system message for later dynamic updates
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
