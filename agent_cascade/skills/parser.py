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

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from agent_cascade.log import logger

from .common import SEMVER_RE as _SEMVER_RE


def normalize_version(raw) -> str:
    """Return valid semver string or default '1.0.0'.

    Args:
        raw: Version value from frontmatter (any type).

    Returns:
        Normalized semver string like "1.0.0".
    """
    if isinstance(raw, str) and _SEMVER_RE.match(raw):
        return raw
    return '1.0.0'


_KEY_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$')
_LIST_ITEM_RE = re.compile(r'^\s*-\s+(.*)$')


def _strip_quotes(value: str) -> str:
    """Remove one layer of matching surrounding single/double quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _lenient_parse(yaml_lines: List[str]) -> Optional[Dict[str, Any]]:
    """Best-effort line-based `key: value` parse used ONLY when yaml.safe_load fails.

    Returns None when the block cannot be confidently interpreted, so the caller keeps its
    existing "return {}" behaviour for genuinely malformed input.
    """
    out: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for line in yaml_lines:
        if not line.strip():
            continue
        m = _LIST_ITEM_RE.match(line)
        if m:
            if current_key is None:
                return None
            out[current_key].append(_strip_quotes(m.group(1).strip()))
            continue
        m = _KEY_RE.match(line)
        if m:
            key, raw = m.group(1), m.group(2).strip()
            if raw == '':
                current_key = key
                out[key] = []
            else:
                current_key = None
                out[key] = _strip_quotes(raw)
        else:
            return None  # unrecognized line -> do not guess
    return out or None


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Split content into YAML frontmatter dict and remaining body text.

    Uses pyyaml's safe_load for robust parsing that handles edge cases like:
      - Body text containing '---' sequences
      - Whitespace before closing delimiter
      - Missing or malformed delimiters gracefully

    If strict YAML parsing fails (e.g. an unescaped colon in a value), a lenient
    line-based fallback (_lenient_parse) is attempted before giving up; it bails on
    any line it cannot confidently interpret, so genuinely broken input still yields
    an empty dict with the full content as body.

    Expects the first line to be '---' with a closing '---' delimiter.
    If no valid frontmatter is found, returns an empty dict with the full content as body.

    Args:
        content: Raw file content string.

    Returns:
        Tuple of (frontmatter_dict, body_text).
    """
    stripped = content.lstrip('\ufeff').strip()
    if not stripped.startswith('---'):
        logger.debug('[SKILLS] No YAML frontmatter delimiter found in content')
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
        logger.debug('[SKILLS] No closing frontmatter delimiter found')
        return {}, content

    # Reconstruct YAML text from collected lines
    yaml_text = '\n'.join(yaml_lines)

    # Body is everything after the closing delimiter
    body = '\n'.join(lines[body_start:])

    # Parse YAML frontmatter using safe_load
    try:
        frontmatter = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        logger.warning('[SKILLS] Failed to parse YAML frontmatter: %s', e)
        lenient = _lenient_parse(yaml_lines)
        if lenient is not None:
            logger.info('[SKILLS] Frontmatter recovered by lenient parse: keys=%s', sorted(lenient))
            return lenient, body.strip()
        return {}, content

    if not isinstance(frontmatter, dict):
        # Delimiters were found and the block parsed (as a non-dict), so the body after
        # the closing '---' is returned — consistent with every other exit path below.
        logger.debug('[SKILLS] Frontmatter parsed as non-dict type: %s', type(frontmatter).__name__)
        return {}, body

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
        'frontmatter': frontmatter,
        'body': body,
        'path': str(skill_path),
        'version': normalize_version(frontmatter.get('version')),
    }

    # logger.debug('[SKILLS] Parsed skill file: %s (name=%s)', skill_path, frontmatter.get('name', 'unknown'))
    return result
