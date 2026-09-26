"""
Skill Validator — Two-tier validation for proposed auto-generated skills.

Tier 1 (Structural): Checks YAML frontmatter, required fields, uniqueness.
Tier 2 (Self-Match): Dry-run match against the generating task text.
"""

import re
from typing import List, Tuple

import yaml

from agent_cascade.log import logger
from agent_cascade.settings import (AUTO_SKILL_MAX_SIZE_KB, AUTO_SKILL_PROMOTION_THRESHOLD, MIN_DESCRIPTION_LENGTH,
                                    MIN_SKILL_BODY_LENGTH)

from .common import SEMVER_RE as _SEMVER_RE
from .parser import parse_frontmatter

# Snake-case pattern: starts with lowercase letter, allows lowercase digits, underscore, hyphen
_SNAKE_CASE_RE = re.compile(r'^[a-z][a-z0-9_-]*$')


def _frontmatter_failure_reason(skill_content: str) -> str:
    """Return the underlying YAML error message for a failed frontmatter parse (or '').

    ``parse_frontmatter`` swallows the YAMLError (logs it, returns ``{}``), so re-probe here:
    extract the first ``---``…``---`` block and try ``yaml.safe_load`` to surface an actionable
    reason (e.g. unescaped colon in a value) for the validator error message.
    """
    stripped = skill_content.strip()
    if not stripped.startswith('---'):
        return ''
    lines = stripped.split('\n')
    yaml_lines = []
    for line in lines[1:]:
        if line.strip() == '---':
            break
        yaml_lines.append(line)
    if not yaml_lines:
        return ''
    try:
        yaml.safe_load('\n'.join(yaml_lines))
        return ''
    except yaml.YAMLError as e:
        return str(e).replace('\n', ' ')


# Prompt injection patterns (borrowed from Hermes)
_INJECTION_PATTERNS: list = [
    'ignore previous instructions',
    'ignore all previous',
    'you are now',
    'disregard your',
    'forget your instructions',
    'new instructions:',
    'system prompt:',
    '<system>',
    ']]>',
]


def validate_skill(
    skill_content: str,
    skill_name: str,
    existing_names: set,
    task_text: str = '',
    check_injection: bool = True,
) -> Tuple[bool, List[str]]:
    """Validate a proposed skill. Returns (passed, error_messages).

    Args:
        skill_content: Raw SKILL.md content string.
        skill_name: The skill name to validate.
        existing_names: Set of already-registered skill names.
        task_text: Optional task text for Tier 2 self-match validation.
        check_injection: If True (default), run prompt injection check.

    Returns:
        Tuple of (passed, error_list). If passed is True, error_list contains only warnings.
    """
    errors: List[str] = []
    warnings: List[str] = []  # Soft checks that don't block registration

    # Size check (raw content)
    max_bytes = AUTO_SKILL_MAX_SIZE_KB * 1024
    byte_count = len(skill_content.encode('utf-8'))
    if byte_count > max_bytes:
        errors.append(f"Skill content too large ({byte_count} bytes > {max_bytes} bytes)")

    # Parse frontmatter
    frontmatter, body = parse_frontmatter(skill_content)
    if not frontmatter:
        reason = _frontmatter_failure_reason(skill_content)
        errors.append('No valid YAML frontmatter found in skill content'
                      + (f' ({reason})' if reason else '')
                      + ' — check for unescaped colons/quotes in values (quote generated_from_task).')
        return False, errors

    # Name check
    name = frontmatter.get('name', '')
    if not name:
        errors.append("Missing required field: 'name'")
    elif not _SNAKE_CASE_RE.match(name):
        errors.append(f"Skill name '{name}' is not valid snake_case (pattern: [a-z][a-z0-9_-]*)")

    # Description check
    description = frontmatter.get('description', '')
    if not description:
        errors.append("Missing required field: 'description'")
    elif len(description) < MIN_DESCRIPTION_LENGTH:
        errors.append(f"Description too short ({len(description)} chars, minimum {MIN_DESCRIPTION_LENGTH})")

    # Triggers check
    triggers = frontmatter.get('triggers', [])
    if not triggers or not isinstance(triggers, list) or len(triggers) < 1:
        errors.append("Missing or empty 'triggers' list (requires at least 1 entry)")
    else:
        # BUG_0019: a block-list item with an unescaped colon parses SUCCESSFULLY into a
        # one-key dict (``- fix parser: do Y`` -> {'fix parser': 'do Y'}), so safe_load never
        # raises and the BUG_0018 lenient fallback never sees it. Such an item would then raise
        # TypeError in ' '.join(triggers) downstream. Reject with an actionable message.
        non_string_indices = [i for i, t in enumerate(triggers) if not isinstance(t, str)]
        if non_string_indices:
            shown = ', '.join(repr(triggers[i]) for i in non_string_indices[:3])
            errors.append(
                f"Trigger items must be strings; item(s) at position "
                f"{', '.join(str(i) for i in non_string_indices[:3])} are not ({shown}). "
                f"Quote values containing a colon, e.g. \"fix parser: do Y\".")

    # Version format check (soft — warns but allows registration, defaults to 1.0.0 if invalid)
    version = frontmatter.get('version')
    if version and not _SEMVER_RE.match(str(version)):
        warnings.append(
            f"Skill '{skill_name}': Version '{version}' is not valid semver (X.Y.Z) — will default to '1.0.0'")

    # Uniqueness check
    if name and name in existing_names:
        errors.append(f"Skill name '{name}' already exists in registry")

    # Body check
    if not body:
        errors.append('Skill body is empty')
    elif len(body) < MIN_SKILL_BODY_LENGTH:
        errors.append(f"Skill body too short ({len(body)} chars, minimum {MIN_SKILL_BODY_LENGTH})")

    # Prompt injection check (require 2+ matches to avoid false positives)
    if check_injection:
        content_lower = skill_content.lower()
        injections = [p for p in _INJECTION_PATTERNS if p in content_lower]
        if len(injections) >= 2:
            errors.append(f"Prompt injection detected (patterns: {', '.join(injections[:3])})")

    if errors:
        logger.debug("[SKILLS] Tier 1 validation failed for '%s': %s", skill_name, errors)
        return False, errors + warnings

    if task_text:
        # Self-match vs the generating task. BUG_0017: the old score divided by len(query_tokens),
        # so a wordy task sank an on-topic skill; and a correctly-generalized skill legitimately
        # shares few tokens with the one-off task, so NO lexical threshold separates "generalized"
        # from "unrelated". We therefore (1) score containment of the SMALLER set and (2) split the
        # gate: zero shared vocabulary => hard reject (disconnected/hallucinated); some overlap but
        # below threshold => ADVISORY warning (non-blocking), with actionable repair info.
        _token_re = re.compile(r'[a-zA-Z0-9_]+(?:[-][a-zA-Z0-9_]+)*')
        skill_text = f"{name} {description} {' '.join(triggers)}"
        skill_keywords = set(_token_re.findall(skill_text.lower()))
        query_tokens = set(_token_re.findall(task_text.lower()))
        if skill_keywords and query_tokens:
            overlap = len(skill_keywords & query_tokens)
            score = min(overlap / max(min(len(skill_keywords), len(query_tokens)), 1), 1.0)
        else:
            overlap, score = 0, 0.0
        if overlap == 0:
            errors.append(
                f"Self-match score {score:.3f} below threshold {AUTO_SKILL_PROMOTION_THRESHOLD} "
                f"— skill shares NO vocabulary with its generating task; it may be unrelated or "
                f"mis-scoped. Echo key task terms in name/description/triggers, or set "
                f"generated_from_task to reflect the skill's actual scope.")
        elif score < AUTO_SKILL_PROMOTION_THRESHOLD:
            missing = sorted(query_tokens - skill_keywords)
            warnings.append(
                f"Self-match score {score:.3f} below threshold {AUTO_SKILL_PROMOTION_THRESHOLD} "
                f"(advisory, non-blocking). Task terms not echoed by the skill: "
                f"{', '.join(missing[:8])}. Consider adding relevant task vocabulary.")

    if errors:
        logger.debug("[SKILLS] Tier 2 validation failed for '%s': %s", skill_name, errors)
        return False, errors + warnings

    logger.info("[SKILLS] Validation passed for skill '%s'%s", skill_name,
                f" (warnings: {', '.join(warnings)})" if warnings else '')
    return True, warnings
