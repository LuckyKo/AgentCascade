"""
Skill Validator — Two-tier validation for proposed auto-generated skills.

Tier 1 (Structural): Checks YAML frontmatter, required fields, uniqueness.
Tier 2 (Self-Match): Dry-run match against the generating task text.
"""

import re
from typing import List, Tuple

from agent_cascade.log import logger
from agent_cascade.settings import (
    AUTO_SKILL_MAX_SIZE_KB,
    AUTO_SKILL_PROMOTION_THRESHOLD,
    MIN_DESCRIPTION_LENGTH,
    MIN_SKILL_BODY_LENGTH,
)

from .parser import parse_frontmatter


# Snake-case pattern: starts with lowercase letter, allows lowercase digits, underscore, hyphen
_SNAKE_CASE_RE = re.compile(r'^[a-z][a-z0-9_-]*$')

# Prompt injection patterns (borrowed from Hermes)
_INJECTION_PATTERNS: list = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "disregard your",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "<system>",
    "]]>",
]


def validate_skill(
    skill_content: str,
    skill_name: str,
    existing_names: set,
    task_text: str = "",
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
        Tuple of (passed, error_list). If passed is True, error_list is empty.
    """
    errors: List[str] = []

    # Size check (raw content)
    max_bytes = AUTO_SKILL_MAX_SIZE_KB * 1024
    byte_count = len(skill_content.encode('utf-8'))
    if byte_count > max_bytes:
        errors.append(f"Skill content too large ({byte_count} bytes > {max_bytes} bytes)")

    # Parse frontmatter
    frontmatter, body = parse_frontmatter(skill_content)
    if not frontmatter:
        errors.append("No valid YAML frontmatter found in skill content")
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

    # Uniqueness check
    if name and name in existing_names:
        errors.append(f"Skill name '{name}' already exists in registry")

    # Body check
    if not body:
        errors.append("Skill body is empty")
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
        return False, errors

    if task_text:
        # Lightweight self-match: check if enough skill keywords appear in the task text.
        # Reuses the same tokenization as SkillMatcher without building a full index.
        _token_re = re.compile(r'[a-zA-Z0-9_]+(?:[-][a-zA-Z0-9_]+)*')
        skill_text = f"{name} {description} {' '.join(triggers)}"
        skill_keywords = set(_token_re.findall(skill_text.lower()))
        query_tokens = set(_token_re.findall(task_text.lower()))
        if skill_keywords and query_tokens:
            overlap = len(skill_keywords & query_tokens)
            score = min(overlap / max(len(query_tokens), 1), 1.0)
        else:
            score = 0.0
        if score < AUTO_SKILL_PROMOTION_THRESHOLD:
            errors.append(
                f"Self-match score {score:.3f} below threshold "
                f"{AUTO_SKILL_PROMOTION_THRESHOLD} — skill may not match its generating task"
            )

    if errors:
        logger.debug("[SKILLS] Tier 2 validation failed for '%s': %s", skill_name, errors)
        return False, errors

    logger.info("[SKILLS] Validation passed for skill '%s'", skill_name)
    return True, []