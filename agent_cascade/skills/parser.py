"""
SKILL.md Parser — YAML frontmatter extraction and markdown body splitting.

Parses SKILL.md files following the standard YAML frontmatter format:
    ---
    name: my-skill
    description: What this skill does
    ...
    ---

    Markdown instructions...
"""

import yaml
from pathlib import Path
from typing import Tuple, Dict, Any

from agent_cascade.log import logger


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Split content into YAML frontmatter dict and remaining body text.

    Uses pyyaml's load_all for robust parsing that handles edge cases like:
      - Body text containing '---' sequences
      - Whitespace before closing delimiter
      - Missing or malformed delimiters gracefully

    Expects the first line to be '---' with a closing '---' delimiter.
    If no valid frontmatter is found, returns an empty dict with the full content as body.

    Args:
        content: Raw file content string.

    Returns:
        Tuple of (frontmatter_dict, body_text).
    """
    stripped = content.strip()
    if not stripped.startswith('---'):
        logger.debug("[SKILLS] No YAML frontmatter delimiter found in content")
        return {}, content

    # Split content into lines for processing
    lines = stripped.split('\n')

    # Find the closing '---' delimiter (first line that is only dashes/whitespace)
    yaml_lines = []
    body_start = 0
    for i, line in enumerate(lines):
        if i == 0:
            continue  # Skip opening '---'
        stripped_line = line.strip()
        if stripped_line == '---':
            body_start = i + 1
            break
        yaml_lines.append(line)

    if not yaml_lines and body_start == 0:
        # No closing delimiter found; treat entire content as body
        logger.debug("[SKILLS] No closing frontmatter delimiter found")
        return {}, content

    # Reconstruct YAML text from collected lines
    yaml_text = '\n'.join(yaml_lines)

    # Parse YAML frontmatter using safe_load
    try:
        frontmatter = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        logger.warning("[SKILLS] Failed to parse YAML frontmatter: %s", e)
        return {}, content

    if not isinstance(frontmatter, dict):
        logger.debug("[SKILLS] Frontmatter parsed as non-dict type: %s", type(frontmatter).__name__)
        return {}, content

    # Body is everything after the closing delimiter
    body = '\n'.join(lines[body_start:])

    return frontmatter, body.strip()


def parse_skill_file(skill_path: Path) -> Dict[str, Any]:
    """Parse a SKILL.md file and extract frontmatter metadata plus body.

    Args:
        skill_path: Path to the SKILL.md file.

    Returns:
        Dictionary with keys:
            - "frontmatter": Parsed YAML frontmatter dict
            - "body": Markdown body text (full instructions)
            - "path": Original Path object for reference

    Raises:
        FileNotFoundError: If the skill_path does not exist.
    """
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill file not found: {skill_path}")

    content = skill_path.read_text(encoding='utf-8')
    frontmatter, body = parse_frontmatter(content)

    result = {
        "frontmatter": frontmatter,
        "body": body,
        "path": str(skill_path),
    }

    logger.debug("[SKILLS] Parsed skill file: %s (name=%s)", skill_path, frontmatter.get('name', 'unknown'))
    return result