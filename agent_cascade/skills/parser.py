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


def strict_frontmatter_check(skill_content: str) -> Tuple[bool, str]:
    """Probe whether the frontmatter block parses under STRICT yaml.safe_load.

    Extracts the first ``---``…``---`` block and attempts a strict parse. Returns:
    - ``(True, '')`` when strict parse succeeds, or when no frontmatter block is present
      (absence is not a parse failure — callers that need to detect absence should check
      the content themselves).
    - ``(False, reason)`` when strict parse fails — ``reason`` is the one-line YAMLError
      message (newlines replaced with spaces, matching validator.py's historical format).

    This is a public utility shared by validator.py and propose_skill.py so the
    extraction + probe logic lives in exactly one place.
    """
    stripped = skill_content.strip()
    if not stripped.startswith('---'):
        return (True, '')
    lines = stripped.split('\n')
    yaml_lines: List[str] = []
    for line in lines[1:]:
        if re.fullmatch(r'-{3,}', line.strip()):
            break
        yaml_lines.append(line)
    if not yaml_lines:
        return (True, '')
    try:
        yaml.safe_load('\n'.join(yaml_lines))
        return (True, '')
    except yaml.YAMLError as e:
        return (False, str(e).replace('\n', ' '))


def _needs_quoting(value: str) -> bool:
    """True if ``value`` must be quoted to survive strict yaml.safe_load as the SAME string.

    Empirical probe (no hand-rolled character lists): a value needs quoting iff
    ``yaml.safe_load('probe: <value>')`` either raises OR does not round-trip to the
    exact original string. This catches mid-value colons, special leading characters,
    AND number/bool/null-like strings that must stay ``str`` (e.g. ``1.0`` -> float).
    An empty string is a block-list parent marker, not a scalar — never quoted.
    """
    if value == '':
        return False
    try:
        probe = yaml.safe_load(f'probe: {value}')
    except yaml.YAMLError:
        return True
    return not (isinstance(probe, dict) and probe.get('probe') == value)


# Line-shape regexes for the surgical re-quoter. The leading-whitespace group is preserved
# verbatim on rewrite so indented keys / list items keep their indentation.
_FM_LINE_KEY_RE = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_-]*):(.*)$')
_FM_LINE_ITEM_RE = re.compile(r'^(\s*-\s+)(.*)$')


def _quote_scalar(value: str) -> str:
    """Render ``value`` as a safe double-quoted YAML scalar (escape embedded double quotes)."""
    return f'"{value.replace(chr(34), chr(92) + chr(34))}"'


def _fix_fm_line(line: str) -> str:
    """Re-quote a frontmatter ``key: value`` / ``- item`` line iff its scalar would not
    survive strict YAML as the same string. Returns the line UNCHANGED when the value is
    already safe — this is what keeps every valid frontmatter line byte-for-byte. A
    ``key:`` block-list parent (empty value) and non-key/non-item lines are left alone.
    """
    m = _FM_LINE_ITEM_RE.match(line)
    if m:
        prefix, item = m.group(1), m.group(2)
        intended = _strip_quotes(item.strip())
        if _needs_quoting(intended):
            return f'{prefix}{_quote_scalar(intended)}'
        return line
    m = _FM_LINE_KEY_RE.match(line)
    if m:
        indent, key, val = m.group(1), m.group(2), m.group(3)
        if val.strip() == '':
            return line
        intended = _strip_quotes(val.strip())
        if _needs_quoting(intended):
            return f'{indent}{key}: {_quote_scalar(intended)}'
        return line
    return line


def auto_fix_frontmatter(skill_content: str) -> Tuple[str, bool, str]:
    """Return ``(fixed_content, changed, original_reason)``.

    Auto-fixes a SKILL.md whose frontmatter fails STRICT ``yaml.safe_load`` (recovered
    by the lenient fallback) by re-quoting ONLY the offending ``key: value`` / ``- item``
    lines — a line-surgical edit, never a full re-dump (which would risk type-coercing
    number-like strings and rewriting every line). The markdown body and every other
    frontmatter line are returned byte-for-byte. (Line-ending convention is preserved: a
    uniform-LF or uniform-CRLF input stays as-is; a MIXED-ending input is normalized to
    its dominant convention — no real caller produces mixed endings.)

    - Strict parse OK  -> ``(skill_content, False, '')``
    - Strict parse FAILS but a line-surgical re-quote makes it parse clean AND preserves the
      lenient-recovered values -> ``(fixed_content, True, original_reason)``
    - Strict parse FAILS and it cannot be safely fixed -> ``(skill_content, False, original_reason)``

    ``original_reason`` is the one-line YAMLError message from :func:`strict_frontmatter_check`
    (same format callers already display). Verification is two-layer: the fixed block must
    (1) parse under strict ``yaml.safe_load`` and (2) match the values ``parse_frontmatter``
    recovers from the ORIGINAL content (no data loss). Only if BOTH pass is ``changed=True``
    returned; otherwise the content is returned UNCHANGED so the caller falls back to the
    existing warning-only path.
    """
    strict_ok, reason = strict_frontmatter_check(skill_content)
    if strict_ok:
        return (skill_content, False, '')

    # Preserve the caller's newline convention. Files in this repo are CRLF; a caller that
    # reads bytes/newline='' and passes CRLF through must NOT get mixed endings (a rebuilt
    # line would otherwise drop its trailing '\r'). Normalize to LF for the line-surgical
    # edit, then restore CRLF on the fixed output. Unchanged paths return the original bytes.
    crlf = '\r\n' in skill_content
    work = skill_content.replace('\r\n', '\n') if crlf else skill_content

    # Locate the frontmatter block in the content (same detection as strict_frontmatter_check)
    # but track byte positions so the body and any leading whitespace stay byte-for-byte.
    lines = work.split('\n')
    open_idx = None
    for i, line in enumerate(lines):
        if line.strip() == '':
            continue
        if line.strip() == '---':
            open_idx = i
        break
    if open_idx is None:
        return (skill_content, False, reason)
    close_idx = None
    for j in range(open_idx + 1, len(lines)):
        if re.fullmatch(r'-{3,}', lines[j].strip()):
            close_idx = j
            break
    if close_idx is None:
        return (skill_content, False, reason)
    fm_indices = range(open_idx + 1, close_idx)

    fixed_lines = {i: _fix_fm_line(lines[i]) for i in fm_indices}
    if fixed_lines == {i: lines[i] for i in fm_indices}:
        # No re-quoteable line found (the strict failure is outside the line-surgical scope).
        return (skill_content, False, reason)

    fixed_block = '\n'.join(fixed_lines[i] for i in fm_indices)
    # Layer 1: the fixed block must parse under strict YAML.
    try:
        fixed_dict = yaml.safe_load(fixed_block)
    except yaml.YAMLError:
        return (skill_content, False, reason)
    # Layer 2: values must match the lenient recovery of the ORIGINAL content (no data loss).
    recovered, _ = parse_frontmatter(skill_content)
    if fixed_dict != recovered:
        return (skill_content, False, reason)

    out = list(lines)
    for i in fm_indices:
        out[i] = fixed_lines[i]
    result = '\n'.join(out)
    if crlf:
        result = result.replace('\n', '\r\n')
    return (result, True, reason)


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


_FRONTMATTER_BLOCK_RE = re.compile(
    r'-{3,}[ \t]*(?:\r?\n)(.*?)\r?\n-{3,}[ \t]*(?:\r?\n|\Z)',
    re.DOTALL,
)


def frontmatter_span(skill_content: str) -> Optional[Tuple[int, int]]:
    """Return the ``(start, end)`` offsets of the YAML text inside the frontmatter block.

    This is the single locator for callers that need to patch the frontmatter in place
    (``propose_skill``) so they cannot disagree with :func:`parse_frontmatter`, which is
    tolerant of a UTF-8 BOM and leading whitespace/newlines. A second, stricter regex
    used to cause ``propose_skill`` to skip its name/version patch for content starting
    with ``"\\n"`` or ``"\\ufeff"`` while the parser still read the frontmatter (BUG_0119).

    Args:
        skill_content: Raw SKILL.md text.

    Returns:
        ``(inner_start, inner_end)`` such that ``skill_content[inner_start:inner_end]`` is
        the YAML block between the delimiters, or ``None`` when no ``---``...``---`` pair
        is present. The returned span never includes the delimiters or the newline that
        precedes the closing delimiter, so callers can splice a patched block back in.
    """
    if not isinstance(skill_content, str) or not skill_content:
        return None

    offset = 1 if skill_content.startswith('\ufeff') else 0
    length = len(skill_content)
    while offset < length and skill_content[offset] in ' \t\r\n':
        offset += 1

    match = _FRONTMATTER_BLOCK_RE.match(skill_content, offset)
    if not match:
        return None
    return match.start(1), match.end(1)


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
        UnicodeDecodeError: If the file is not valid UTF-8 (callers decide whether to skip or propagate).
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
