"""
Soul Loader - Load agent personality from a soul.md file.

A soul file is authored as YAML frontmatter (a small, well-formed structured
header) followed by a free-form markdown body. The header carries only the
fields the loader needs to transform (identity, and optional tool/rule
declarations); the prose lives in the markdown body and is passed through
verbatim. This replaces the old "whole file is YAML" format, whose
`_preprocess_soul_content` band-aid could not survive prose (plain lines after
a list, `* ` sub-list markers) and turned the prompt into a lossy
author-YAML -> dict -> markdown round-trip.
"""

from pathlib import Path

import yaml


_FRONTMATTER_DELIMS = ('---', '...')


def _split_frontmatter(content: str, soul_path: str) -> tuple[str, str]:
    """Split a soul file into a YAML frontmatter block and a markdown body.

    A soul file is: an opening '---' fence, a YAML header, a closing '---'
    (or '...') fence, then free-form markdown prose. Returns a
    ``(frontmatter_text, body_text)`` tuple. The body is returned exactly as
    written (no re-flowing) so the rendered prompt is byte-stable.

    Raises:
        ValueError: if the file does not open with a frontmatter fence, or the
            fence is never closed.
    """
    if content.startswith('\ufeff'):
        content = content[1:]

    lines = content.split('\n')
    if not lines or lines[0].strip() != '---':
        raise ValueError(
            f"Soul file must start with a '---' frontmatter fence: {soul_path}")

    for i in range(1, len(lines)):
        if lines[i].strip() in _FRONTMATTER_DELIMS:
            frontmatter = '\n'.join(lines[1:i])
            body = '\n'.join(lines[i + 1:])
            return frontmatter, body

    raise ValueError(
        f"Soul frontmatter is not closed with a '---' fence: {soul_path}")


def load_soul(soul_path: str = 'soul.md') -> dict:
    """
    Load agent configuration from a soul.md file.

    The file is split into a YAML frontmatter header and a markdown body.
    Only the frontmatter is parsed as YAML; the body is kept as raw markdown
    and handed to ``build_system_prompt`` for verbatim inclusion.

    Args:
        soul_path: Path to the soul.md configuration file

    Returns:
        Dict with two keys:
            - 'frontmatter': the parsed YAML header (a mapping)
            - 'body': the markdown prose, verbatim

    Raises:
        FileNotFoundError: If the soul file doesn't exist
        ValueError: If the file lacks a frontmatter fence, or the frontmatter
            is not a YAML mapping
        yaml.YAMLError: If the frontmatter YAML is malformed (with context)
    """
    path = Path(soul_path)
    if not path.exists():
        raise FileNotFoundError(f"Soul file not found: {soul_path}")

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    frontmatter_text, body = _split_frontmatter(content, soul_path)

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        # Build a helpful error message with file path and error details
        error_msg = f"Failed to parse soul frontmatter: {soul_path}\n"
        mark = getattr(e, 'problem_mark', None)
        if mark is not None:
            error_msg += f"  Line {mark.line + 1}: {e.problem or e}"
        else:
            error_msg += f"  {e}"
        raise yaml.YAMLError(error_msg) from e

    if not isinstance(frontmatter, dict):
        raise ValueError(
            'Soul frontmatter must be a YAML mapping, got '
            f"{type(frontmatter).__name__}: {soul_path}")

    return {'frontmatter': frontmatter, 'body': body}


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
    Build a system prompt from a loaded soul configuration.

    Renders the frontmatter identity header, then includes the markdown body
    verbatim. If the frontmatter additionally declares ``capabilities.tools``
    or ``rules``, those are appended as generated sections (auto tool
    descriptions, numbered rules). Authored prose (including any tools or
    rules written directly in the body) is passed through untouched, so the
    author controls ordering and wording exactly.

    Args:
        config: {'frontmatter': dict, 'body': str} from load_soul()

    Returns:
        Formatted system prompt string
    """
    frontmatter = config.get('frontmatter') or {}
    body = config.get('body') or ''

    system_prompt = f"You are {frontmatter.get('name', 'Assistant')}.\n"
    tagline = frontmatter.get('tagline')
    if tagline:
        system_prompt += f"{tagline}\n"

    # Markdown body — verbatim prose. This is the single source of truth for
    # the agent's descriptive content; nothing here is parsed or reordered.
    system_prompt += body

    # Optional generated footer sections, only when declared in frontmatter.
    capabilities = frontmatter.get('capabilities')
    tools = capabilities.get('tools') if isinstance(capabilities, dict) else None
    if isinstance(tools, list) and tools:
        system_prompt += '\n## Your Tools\nYou have access to these tools:\n'
        for tool in tools:
            system_prompt += (
                f"- **{tool}**: Use when you need to "
                f"{get_tool_description(tool)}\n")

    rules = frontmatter.get('rules')
    if isinstance(rules, list) and rules:
        system_prompt += '\n## Your Rules\n'
        for i, rule in enumerate(rules, 1):
            system_prompt += f"{i}. {rule}\n"

    return system_prompt


def create_agent_from_soul(llm_cfg: dict,
                           soul_path: str = 'soul.md',
                           agent_class=None,
                           role_name: str = None,
                           **agent_kwargs):
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

    # Load soul configuration (frontmatter header + markdown body)
    config = load_soul(soul_path)
    frontmatter = config['frontmatter']

    # Build system prompt
    system_prompt = build_system_prompt(config)

    # Note: Tools are added separately by the framework
    # Don't use function_list here as tools are added manually later

    # Build formatted name: "Role Name" (e.g., "Writer Bob")
    # Avoid duplication if the role name is already part of the name (e.g. "Writer Writer")
    raw_name = frontmatter.get('name', 'Assistant')

    # Normalize for comparison (replace underscores with spaces)
    role_norm = (role_name or '').lower().replace('_', ' ')
    name_norm = raw_name.lower().replace('_', ' ')

    if role_name and not name_norm.startswith(role_norm):
        formatted_name = f"{role_name.replace('_', ' ').title()} {raw_name}"
    else:
        formatted_name = raw_name

    # Create agent
    agent = agent_class(
        llm=llm_cfg,
        name=formatted_name,
        description=frontmatter.get('tagline', 'A helpful AI assistant'),
        system_message=system_prompt,
        function_list=[],  # Empty - tools added manually by agent_orchestrator
        **agent_kwargs)

    # Store role info for tool filtering and logging
    if role_name:
        agent.agent_type = role_name.replace('_', ' ').title()

    # Store config and base system message for later dynamic updates
    config_key = role_name if role_name else frontmatter.get('name', 'assistant')
    agent.agent_configs = {config_key: config}
    agent.base_system_message = system_prompt

    return agent, config


# Example usage
if __name__ == '__main__':
    # Test loading the soul
    config = load_soul()
    print(f"Loaded agent: {config['frontmatter']['name']}")
    print(f"Tagline: {config['frontmatter']['tagline']}")
    print('\nSystem prompt preview:')
    print(build_system_prompt(config)[:500] + '...')
