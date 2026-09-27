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


def _split_inline_list(raw: str) -> List[str]:
    """Split a `[a, b, c]` value into list[str] on TOP-LEVEL commas only (respect [] and quotes)."""
    inner = raw[1:-1].strip()
    if not inner:
        return []
    parts, buf, depth, quote = [], '', 0, None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch; buf += ch
        elif ch == '[':
            depth += 1; buf += ch
        elif ch == ']':
            depth -= 1; buf += ch
        elif ch == ',' and depth == 0:
            parts.append(buf); buf = ''
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [_strip_quotes(p.strip()) for p in parts]


def _lenient_parse(yaml_lines: List[str]) -> Dict[str, Any]:
    """Tolerant line-based `key: value` parse (Layer 2 fallback).

    NEVER bails: unrecognized/free lines are skipped; an indented non-key, non-list line
    following a scalar key is folded into it as a continuation; inline `[a, b]` values become
    real lists. Returns the captured dict (possibly empty) — empty means "nothing
    recognizable", which parse_frontmatter maps to its existing {} contract. The caller
    applies _normalize_frontmatter; this function returns raw shapes.
    """
    out: Dict[str, Any] = {}
    list_key: Optional[str] = None     # key currently collecting block-list items
    scalar_key: Optional[str] = None   # last scalar key (for continuation folding)
    for line in yaml_lines:
        if not line.strip():
            continue
        m = _LIST_ITEM_RE.match(line)                 # `  - item`  (checked FIRST)
        if m:
            if list_key is None:
                continue                              # orphan list item -> skip, don't bail
            out[list_key].append(_strip_quotes(m.group(1).strip()))
            scalar_key = None
            continue
        m = _KEY_RE.match(line)                       # `key: value` / `key:`
        if m:
            key, raw = m.group(1), m.group(2).strip()
            if raw == '':
                list_key, out[key], scalar_key = key, [], None   # block-list parent
            else:
                list_key, scalar_key = None, key
                if raw.startswith('[') and raw.endswith(']'):
                    out[key] = _split_inline_list(raw)           # `[a, b]` -> ['a','b']
                else:
                    out[key] = _strip_quotes(raw)                # scalar (may contain ':')
            continue
        # free line / indented continuation — no bail
        if line[:1] in (' ', '\t') and scalar_key is not None \
                and isinstance(out.get(scalar_key), str):
            out[scalar_key] = f'{out[scalar_key]} {line.strip()}'.strip()
    return out                                          # possibly empty; never None


def _normalize_frontmatter(fm: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce consumed frontmatter fields to a guaranteed clean shape (Layer 3).

    Contract after this runs — for the keys downstream actually reads:
      - name / description / version : str | absent
      - triggers                     : list[str] | absent
    No dicts / ints / nested structures survive on these keys, so matcher /
    validator / manager can drop their isinstance guards. Mutates and returns fm.
    """
    # --- triggers -> list[str] or absent ---
    if 'triggers' in fm:
        raw = fm['triggers']
        if isinstance(raw, str):
            items = [raw]                       # bare string -> single-item list
        elif isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = []                          # int / dict / other -> coerced to empty list
                                                # (validator then reports "Missing or empty 'triggers'")
        clean: List[str] = []
        for it in items:
            if isinstance(it, str):
                clean.append(it)               # already clean
            elif isinstance(it, dict) and len(it) == 1:
                (k, v), = it.items()
                clean.append(f'{k}: {v}')      # Q1 DECISION: coerce one-key dict -> "key: value"
            # else: drop non-str silently (int, nested list, multi-key dict). KNOWN CEILING
            # (out of scope per brief "no nested maps"): triggers: [[a,b],c] keeps 'c', drops [a,b].
        fm['triggers'] = clean
    # --- scalar consumed keys -> str or absent ---
    for key in ('name', 'description', 'version'):
        if key in fm and not isinstance(fm[key], str):
            del fm[key]                         # wrong type -> treat as missing
    return fm


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Split content into YAML frontmatter dict and remaining body text.

    Uses pyyaml's safe_load for robust parsing that handles edge cases like:
      - Body text containing '---' sequences
      - Whitespace before closing delimiter
      - Missing or malformed delimiters gracefully

    If strict YAML parsing fails (e.g. an unescaped colon in a value), a tolerant
    line-based fallback (_lenient_parse) recovers what it can — unrecognized lines are
    skipped, never bailed on — so genuinely broken input still yields an empty dict.

    Shape contract: the returned frontmatter is always normalized by
    _normalize_frontmatter, so consumed keys have guaranteed clean shapes:
      - name / description / version : str | absent
      - triggers                     : list[str] | absent
    Downstream consumers (matcher/validator/manager) may rely on these shapes.

    Expects the first line to be '---' with a closing delimiter of 3+ dashes.
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
        if re.fullmatch(r'-{3,}', stripped_line):
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
        # Layer-2 fallback is EXPECTED for messy third-party headers (todo.md:133 incident).
        # Demote WARNING -> DEBUG so a partially-quoted trigger no longer spams every load.
        logger.debug('[SKILLS] Strict YAML parse failed (%s); using tolerant lenient parse', type(e).__name__)
        return _normalize_frontmatter(_lenient_parse(yaml_lines)), body.strip()

    if not isinstance(frontmatter, dict):
        # Delimiters were found and the block parsed (as a non-dict), so the body after
        # the closing '---' is returned — consistent with every other exit path below.
        logger.debug('[SKILLS] Frontmatter parsed as non-dict type: %s', type(frontmatter).__name__)
        return {}, body

    return _normalize_frontmatter(frontmatter), body.strip()


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
