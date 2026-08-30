"""Skill Advisor — AUTO Skill Helper (Advanced mode).

When ``call_agent`` is invoked with ``load_skill="AUTO"`` in **Advanced** mode, this
module runs a lightweight Security agent (via :func:`advisor_runner.run_lightweight_advisor`)
that:

1. Recommends ADDITIONAL skills (semantic matching, not keyword overlap)
2. Improves the task prompt with notes/context
3. Validates the delegation — denying trivial/unnecessary calls

Self-Augmentation is ALWAYS injected separately by the engine, so it is excluded from
the advisor's available-skills list and must never appear in its recommendations.

On timeout/error the advisor returns ``verdict="ambiguous"`` so the caller falls back
to basic keyword matching. Malformed output (no valid VERDICT) defaults to APPROVE with
empty skills/notes — the advisor is an optimization, not a gate.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SkillAdvisorResult:
    """Parsed result from the Skill Advisor."""

    verdict: str = "ambiguous"        # "approve" | "deny" | "ambiguous"
    reason: str = ""                  # Advisor's justification
    recommended_skills: List[str] = field(default_factory=list)  # Validated skill names
    task_notes: str = ""              # Improved task notes (empty if none)
    latency_ms: float = 0.0           # Wall-clock time for the advisor LLM call

    @property
    def is_usable(self) -> bool:
        """True when the result came from a clean, parseable advisor response."""
        return self.verdict in ("approve", "deny")


# ── Marker regexes (case-insensitive, whitespace-tolerant) ───────────────────
# Each marker is matched at the start of a line; the payload runs to end-of-line.
_RE_SKILLS = re.compile(r'^\s*\[SKILLS\]\s*(.*)$', re.IGNORECASE | re.MULTILINE)
_RE_NOTES = re.compile(r'^\s*\[NOTES\]\s*(.*)$', re.IGNORECASE | re.MULTILINE)
_RE_VERDICT = re.compile(r'^\s*\[VERDICT\]\s*(.*)$', re.IGNORECASE | re.MULTILINE)

# The meta-skill that is always injected by the engine — never recommended here.
_SELF_AUGMENTATION = "self-augmentation"


def build_skill_advisor_prompt(
    skill_manager,
    task_text: str,
    context_text: str,
    agent_class: str,
    caller_name: str,
) -> str:
    """Build the advisor prompt with available-skills metadata.

    Excludes ``self-augmentation`` from the skills list (it is always present).
    Each skill is formatted as ``- {name}: {description}``.
    """
    from agent_cascade.prompts.dna import SKILL_ADVISOR_PROMPT

    metadata_lines = []
    for meta in skill_manager.get_all_metadata():
        name = (meta.get('name') or '').strip()
        if not name or name.lower() == _SELF_AUGMENTATION:
            continue
        description = (meta.get('description') or '').strip().replace('\n', ' ')
        metadata_lines.append(f"- {name}: {description}" if description else f"- {name}")

    skills_metadata = "\n".join(metadata_lines) if metadata_lines else "(none)"

    # Escape braces in user-provided content to prevent .format() injection.
    # Task text like "Create {filename}.py" would otherwise raise KeyError.
    # Note: pre-existing doubled braces ({{x}}) become tripled ({{{x}}}) but
    # still render as literal {x} after .format() — safe, just visually odd.
    def _esc(text: str) -> str:
        return text.replace('{', '{{').replace('}', '}}')

    return SKILL_ADVISOR_PROMPT.format(
        skills_metadata=skills_metadata,
        task_text=_esc(task_text or "(no task text provided)"),
        context_text=_esc(context_text or "(no additional context)"),
        agent_class=agent_class,
        caller_name=caller_name or "unknown",
    )


def parse_advisor_output(
    output_text: str,
    skill_manager,
) -> SkillAdvisorResult:
    """Parse the advisor's ``[SKILLS]`` / ``[NOTES]`` / ``[VERDICT]`` markers.

    - Skill names are validated against the registry (unknowns skipped).
    - A missing/invalid ``[VERDICT]`` yields ``verdict="ambiguous"``.
    - An APPROVE with no parseable skills/notes returns empty lists (caller falls back).
    """
    if not output_text:
        return SkillAdvisorResult(verdict="ambiguous", reason="empty advisor output")

    # ── [VERDICT] — determines approve/deny; malformed → ambiguous ──────────
    verdict_match = _RE_VERDICT.search(output_text)
    if verdict_match is None:
        return SkillAdvisorResult(verdict="ambiguous", reason="no [VERDICT] marker found")

    verdict_line = verdict_match.group(1).strip()
    verdict_upper = verdict_line.upper()
    # Split off the reason after the APPROVE/DENY keyword (tolerates "—", "-", ":").
    m = re.match(r'^(APPROVE|DENY)\b\s*[-–—:]?\s*(.*)$', verdict_line, re.IGNORECASE)
    if m is None:
        # Verdict line present but not recognizable as APPROVE/DENY.
        return SkillAdvisorResult(verdict="ambiguous", reason=f"unrecognized verdict: {verdict_line[:120]}")

    verdict = "deny" if m.group(1).upper() == "DENY" else "approve"
    reason = (m.group(2) or "").strip()

    # ── [SKILLS] — validate names against the registry, skip unknowns ───────
    recommended: List[str] = []
    skills_match = _RE_SKILLS.search(output_text)
    if skills_match is not None:
        raw = skills_match.group(1).strip()
        # "none" (case-insensitive) or empty → no recommendations.
        if raw and raw.lower() != "none":
            known_names = set(skill_manager.get_skill_names()) if skill_manager else set()
            for part in re.split(r'[,;]', raw):
                name = part.strip().strip('`"\'')
                if not name or name.lower() == _SELF_AUGMENTATION:
                    continue
                # Validate against registry (case-insensitive match, keep canonical name).
                canonical = _resolve_known_skill(name, known_names)
                if canonical is not None and canonical not in recommended:
                    recommended.append(canonical)

    # ── [NOTES] — task improvement text ─────────────────────────────────────
    notes = ""
    notes_match = _RE_NOTES.search(output_text)
    if notes_match is not None:
        candidate = notes_match.group(1).strip()
        if candidate and candidate.lower() != "none":
            notes = candidate

    return SkillAdvisorResult(
        verdict=verdict,
        reason=reason,
        recommended_skills=recommended,
        task_notes=notes,
    )


def _resolve_known_skill(name: str, known_names: set) -> Optional[str]:
    """Return the canonical registered name for ``name`` (case-insensitive), or None."""
    if not known_names:
        return None
    if name in known_names:
        return name
    lowered = name.lower()
    for key in known_names:
        if key.lower() == lowered:
            return key
    return None


def run_skill_advisor(
    pool,
    skill_manager,
    task_text: str,
    context_text: str,
    agent_class: str,
    caller_name: str,
) -> SkillAdvisorResult:
    """Run the Skill Advisor and return a parsed result.

    Follows the SAME rules as the Security advisor (security_handler.py):
    - Agent class ``'Security'`` (same template, same soul/prompt base)
    - Turn limit ``SECURITY_AGENT_MAX_TURNS``
    - Tool restrictions via ``DEFAULT_SECURITY_DISABLED_TOOLS`` + merge helper
    - Instance naming ``f'Security_op_{uuid4().hex[:8]}'``

    On timeout/error: returns ``verdict="ambiguous"`` so the caller falls back to
    basic keyword matching. Never raises.
    """
    from agent_cascade.log import logger
    from agent_cascade.advisor_runner import run_lightweight_advisor

    instance_name = f'Security_op_{uuid.uuid4().hex[:8]}'

    try:
        prompt = build_skill_advisor_prompt(
            skill_manager, task_text, context_text, agent_class, caller_name
        )
    except Exception as e:  # noqa: BLE001 — prompt building must not crash the caller
        logger.error("[SKILL-ADVISOR] Failed to build advisor prompt: %s", e)
        return SkillAdvisorResult(verdict="ambiguous", reason=f"prompt build error: {e}")

    try:
        result = run_lightweight_advisor(
            pool=pool,
            agent_class='Security',
            instance_name=instance_name,
            task=prompt,
            caller=caller_name or "unknown",
        )
    except Exception as e:  # noqa: BLE001 — runner already catches, but be defensive
        logger.error("[SKILL-ADVISOR] run_lightweight_advisor raised for '%s': %s", instance_name, e)
        return SkillAdvisorResult(verdict="ambiguous", reason=f"runner error: {e}")

    if result.was_timeout:
        logger.warning(
            "[SKILL-ADVISOR] First-yield timeout (%.0f ms elapsed) — falling back to basic match.",
            result.latency_ms,
        )
        return SkillAdvisorResult(verdict="ambiguous", reason="first-yield timeout", latency_ms=result.latency_ms)

    if result.was_error:
        logger.error(
            "[SKILL-ADVISOR] Advisor error for '%s': %s — falling back to basic match.",
            instance_name, result.error_msg,
        )
        return SkillAdvisorResult(verdict="ambiguous", reason=f"advisor error: {result.error_msg}", latency_ms=result.latency_ms)

    parsed = parse_advisor_output(result.output_text, skill_manager)
    parsed.latency_ms = result.latency_ms
    logger.info(
        "[SKILL-ADVISOR] verdict=%s skills=%d notes=%d latency=%.0fms (%s)",
        parsed.verdict, len(parsed.recommended_skills), len(parsed.task_notes),
        result.latency_ms, instance_name,
    )
    return parsed
