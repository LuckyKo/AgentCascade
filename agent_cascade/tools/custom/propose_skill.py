"""
Propose Skill Tool — Allows agents to propose new reusable skills.

Writes full SKILL.md content (including YAML frontmatter) and registers it
via SkillManager. The skill name is a required argument; if the frontmatter
name disagrees, the argument wins and the frontmatter is patched. Rating-only
mode (name + rating, no content) records a rating without modifying content.
"""

import logging
import re
from difflib import get_close_matches

from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.settings import (
    SKILL_DUP_SIM_THRESHOLD,
    SKILL_MATCH_THRESHOLD,
    SKILL_REF_MAX_REPORT,
    SKILL_REF_SCAN_MODE,
    SKILL_REF_SUGGEST_CUTOFF,
)
from agent_cascade.skills.matcher import find_similar_skills, skill_frontmatter_text
from agent_cascade.skills.parser import (
    _needs_quoting,
    _quote_scalar,
    normalize_version,
    parse_frontmatter,
    strict_frontmatter_check,
    auto_fix_frontmatter,
    frontmatter_span,
)
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)


def _next_patch_version(version: str) -> str:
    """Return the next patch version for a semver string (fallback '1.0.1')."""
    try:
        parts = version.split('.')
        padded = parts + ['0'] * (3 - len(parts))
        return f"{padded[0]}.{padded[1]}.{int(padded[2]) + 1}"
    except (ValueError, IndexError):
        return '1.0.1'


def _version_tuple(version: str):
    """Parse a strict ``X.Y.Z`` string into an int tuple, or None when it is not one."""
    parts = str(version).split('.')
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _choose_update_version(proposed_version: str, existing_version: str, name: str = '') -> str:
    """Return the version an update should carry — always newer than ``existing_version``.

    Rules (BUG_0120):
    - no explicit valid version, or one EQUAL to the incumbent -> next patch (pre-existing rule);
    - an explicitly LOWER version -> next patch from the incumbent. Accepting the downgrade
      would revive a historical ``ratings_by_version`` key (or, on an exact match, share the
      incumbent's), corrupting the candidate gate's evidence.
    A higher explicit version is kept as-is.
    """
    proposed_norm = normalize_version(proposed_version)
    existing_norm = normalize_version(existing_version)
    proposed_key = _version_tuple(proposed_norm)
    existing_key = _version_tuple(existing_norm)

    if proposed_key and existing_key and proposed_key < existing_key:
        effective = _next_patch_version(existing_norm)
        logger.info('[PROPOSE-SKILL] %s: proposed v%s is older than incumbent v%s; using v%s',
                    name or '<skill>', proposed_norm, existing_norm, effective)
        return effective
    if proposed_norm == '1.0.0' or proposed_norm == existing_norm:
        return _next_patch_version(existing_norm)
    return proposed_norm


def _coerce_rating(value):
    """Return (float_value or None, error_string or None) for a 0-10 rating."""
    if value is None:
        return None, None
    try:
        float_val = float(value)
    except (TypeError, ValueError):
        return None, f"Invalid rating value: {value!r} (expected a number 0-10)"
    if not (0.0 <= float_val <= 10.0):
        return None, f"Invalid rating {float_val}: must be between 0 and 10."
    return float_val, None


def _source_task_text(agent_pool, instance_name) -> str:
    """Return the task text that produced this proposal, or '' when it cannot be resolved.

    Tier-2 self-match (``validate_skill``) must not be opt-out-able by the proposing model,
    so the run's real task is preferred over the self-reported ``generated_from_task`` field
    (BUG_0123). ``Engine.run`` stashes that text on the instance at run start; an empty result
    keeps the legacy behavior (frontmatter field) for direct/API callers.
    """
    if not instance_name:
        return ''
    instances = getattr(agent_pool, 'instances', None)
    if not isinstance(instances, dict):
        # Direct/API callers and MagicMock pools: no live instance table to read.
        return ''
    instance = instances.get(instance_name)
    if instance is None:
        return ''

    stashed = getattr(instance, '_auto_skill_source_task', None)
    if isinstance(stashed, str) and stashed.strip():
        return stashed
    # No stash: direct/API callers (and instances that never ran through Engine.run) keep the
    # legacy generated_from_task fallback. Deliberately do NOT guess from the live conversation:
    # during reflection its last user message is the reflection prompt itself.
    return ''


def _parse_lightweight_fm(skill_content):
    """Return ``(fm_dict, fm_span, proposed_version)`` for a SKILL.md frontmatter block.

    Lightweight line-based scalar parse (name/version/description) — sufficient for the
    fields ``propose_skill`` patches, NOT a full YAML interpretation (block-style /
    multi-line values are not read; use the parser's ``parse_frontmatter`` for that).
    """
    fm = {}
    fm_span = frontmatter_span(skill_content)
    if fm_span:
        fm_text = skill_content[fm_span[0]:fm_span[1]]
        for line in fm_text.splitlines():
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$', line)
            if m:
                fm[m.group(1)] = m.group(2).strip().strip('"\'')
    return fm, fm_span, fm.get('version', '1.0.0')


def _merge_registered_frontmatter(existing_meta, skill_content):
    """Rebuild a SKILL.md frontmatter from an existing skill's registered metadata and
    prepend it to a body-only body (BUG_0053).

    ``load_skill`` strips the YAML frontmatter to save context tokens, so an agent that
    only ever saw a skill via ``load_skill`` supplies a body-only SKILL.md when updating
    it. This merges the already-registered frontmatter (description, triggers, source,
    and any other scalar fields such as ``generated_by`` / ``generated_from_task``) back
    onto the body so the agent does not have to reconstruct a header it never saw. The
    version is left at the incumbent's value here; the downstream update-version patch
    bumps it.

    Called ONLY for an EXISTING skill whose supplied content has NO frontmatter block,
    so a supplied block (or a NEW skill) is never touched. Returns ``skill_content``
    unchanged when the metadata carries no usable registered frontmatter (defensive:
    fall through to the existing rejection path).
    """
    parsed = (existing_meta or {}).get('_parsed_data') or {}
    registered = parsed.get('frontmatter') or {}
    if not registered:
        return skill_content
    lines = ['---']
    for key, value in registered.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f'{key}:')
            for item in value:
                item = str(item)
                lines.append(f'  - {_quote_scalar(item) if _needs_quoting(item) else item}')
        else:
            value = str(value)
            lines.append(f'{key}: {_quote_scalar(value) if _needs_quoting(value) else value}')
    lines.append('---')
    return '\n'.join(lines) + '\n\n' + skill_content


# ── BUG_0199: unresolved inline skill-reference scan (advisory, non-fatal) ────
# A skill body conventionally points at sibling skills by name (`Pair with
# `skill-name`` / `[[wiki-target]]`). Those pointers are not validated today, so a
# typo (e.g. a misspelled sibling skill) serves silently. The scan below reports
# names that resolve to NO registered skill as a non-fatal warning — it never
# blocks registration or candidate promotion (rating-driven, manager side).
#
# Two tiers keep the false-positive rate low (measured over the live library):
#   cue tier  — shaped tokens (>=3 hyphen segments) in a reference-cue position
#              ("Pair with", "Related", "see `x`", ...); line-level, so a chain
#              like "Pair with `a` (…) and `b`" counts for every token on the line.
#   typo tier — shaped tokens (>=2 hyphen segments) within Levenshtein distance 2
#              of a registered name, ANY position; catches typos of short names.
# `[[wiki]]` targets additionally resolve against the in-memory .agent_lessons
# memory index (memory names are not skills; flagging them is a false positive).
_SKILL_REF_HEADER = '⚠️ SKILL-REFERENCE WARNING'
# Backtick-quoted and [[wiki]] targets, >=3 hyphen-separated segments (cue tier).
_SKILL_REF_TOKEN_RE = re.compile(r'`([a-z][a-z0-9_]+(?:-[a-z0-9_]+){2,})`')
_SKILL_REF_WIKI_RE = re.compile(r'\[\[([a-z][a-z0-9_]+(?:-[a-z0-9_]+){2,})\]\]')
# Typo tier: >=2 segments, so 2-segment skill names are typo-checkable too.
_SKILL_REF_TOKEN_LOOSE_RE = re.compile(r'`([a-z][a-z0-9_]+(?:-[a-z0-9_]+){1,})`')
_SKILL_REF_WIKI_LOOSE_RE = re.compile(r'\[\[([a-z][a-z0-9_]+(?:-[a-z0-9_]+){1,})\]\]')
# Line-level cue test (NOT token-adjacency: chained references on one line must all count).
_SKILL_REF_CUE_RE = re.compile(
    r'(?i)\b(pair with|paired with|pair|related|see also|see|use|load_skill|'
    r'combine with|follow[- ]?up|supersedes?)\b')


def _levenshtein(a: str, b: str) -> int:
    """Levenshtein edit distance between two strings (stdlib-only DP)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _collect_skill_reference_names(skill_manager, agent_pool) -> tuple:
    """Return ``(registered_names, memory_names)`` as lowercase sets.

    ``registered_names`` comes from the full listing scan (active + candidate +
    disabled-but-servable — the same set ``scan_skills`` lists), so a reference
    to any of them is resolvable. ``memory_names`` is the in-memory
    ``.agent_lessons`` index (vault-relative ``.md`` stems); it whitelists
    ``[[wiki]]`` targets that point at project memories, not skills. No disk
    walk: a missing index simply yields an empty whitelist (defensive — the
    scan must never block registration).
    """
    registered = set()
    try:
        for meta in skill_manager.get_all_metadata(include_active_only=False):
            name = str(meta.get('name', '') or '').lower()
            if name:
                registered.add(name)
    except Exception as e:  # pragma: no cover - defensive; scan must not break propose
        logger.debug('[PROPOSE-SKILL] reference scan: registry listing failed (%s)', e)
    memory_names = set()
    hint_manager = getattr(agent_pool, 'memory_hint_manager', None)
    indexes = getattr(hint_manager, '_vault_indexes', None)
    if isinstance(indexes, dict):
        try:
            for idx in indexes.values():
                for rel_path in idx.documents:
                    stem = str(rel_path).rsplit('/', 1)[-1]
                    if stem.endswith('.md'):
                        stem = stem[:-3]
                    if stem:
                        memory_names.add(stem.lower())
        except Exception as e:  # pragma: no cover - defensive; scan must not break propose
            logger.debug('[PROPOSE-SKILL] reference scan: memory index read failed (%s)', e)
    return registered, memory_names


def _unresolved_skill_references(
    body: str,
    own_name: str,
    registered: set,
    memory_names: set,
) -> list:
    """Scan a skill BODY for cross-references that resolve to no registered skill.

    Returns a deduplicated list (case-insensitive) of
    ``(token, tier, edit_distance, suggestion)`` tuples, where tier is 1 (cue
    position) or 2 (typo of a registered name) and ``suggestion``/distance are
    None/0 when no close match exists. The skill's own name is never flagged;
    with an empty registry the scan short-circuits (nothing is resolvable, so
    nothing is reportable). Caveat: the cue-anchored tier can over-trigger on
    a >=3-segment backtick token that is a config key / non-skill name on a
    cue line (e.g. "Pair with `foo-bar-baz`"); non-fatal advisory.
    """
    if not registered or SKILL_REF_SCAN_MODE == 'off':
        return []
    # Own-name exclusion (also suppresses a reference to the OLD name on a rename
    # update — the body is renamed with it, so that is acceptable).
    own = {own_name.lower()} if own_name else set()
    sorted_registered = sorted(registered)
    items = {}
    for line in body.splitlines():
        # Cue tier: shaped (>=3 segments) tokens on a cue line, any position in the line.
        if _SKILL_REF_CUE_RE.search(line):
            tokens = _SKILL_REF_TOKEN_RE.findall(line) + _SKILL_REF_WIKI_RE.findall(line)
        else:
            # Typo tier: looser shape (>=2 segments) anywhere; only near-matches count.
            tokens = _SKILL_REF_TOKEN_LOOSE_RE.findall(line) + _SKILL_REF_WIKI_LOOSE_RE.findall(line)
        for tok in tokens:
            low = tok.lower()
            if low in own or low in registered or low in memory_names:
                continue
            close = get_close_matches(low, sorted_registered, n=1, cutoff=SKILL_REF_SUGGEST_CUTOFF)
            best = close[0] if close else None
            dist = _levenshtein(low, best) if best else 0
            n_seg = low.count('-') + 1
            in_cue = bool(_SKILL_REF_CUE_RE.search(line))
            if in_cue and n_seg >= 3:
                tier = 1
            # Typo tier needs a REAL near-registered name (best is not None): without one,
            # `dist` is 0 and every shaped non-cue token would falsely look like a typo.
            elif (not in_cue) and n_seg >= 2 and best is not None and dist <= 2:
                tier = 2
            else:
                continue
            # The SKILL_REF_SCAN_MODE knob selects a single tier when set to 'cue'/'typo'.
            if (SKILL_REF_SCAN_MODE == 'cue' and tier != 1) or (SKILL_REF_SCAN_MODE == 'typo' and tier != 2):
                continue
            if low not in items:
                items[low] = (tok, tier, dist, best)
    return list(items.values())


def _format_skill_reference_warning(items) -> str:
    """Render the non-fatal warning block for unresolved skill references (BUG_0199).

    Returns '' when there is nothing to report. At most SKILL_REF_MAX_REPORT
    items are listed; the overflow is summarized. ``items`` are the tuples
    produced by :func:`_unresolved_skill_references`.
    """
    if not items:
        return ''
    shown = items[:SKILL_REF_MAX_REPORT]
    # Neutral wording: the same block is shown in the approval prompt (BEFORE registration)
    # and in the success return (AFTER), so it must not claim registration already happened.
    lines = [f"\n\n{_SKILL_REF_HEADER} (non-blocking advisory — registration and promotion are unaffected):"]
    for tok, tier, dist, best in shown:
        if best:
            lines.append(f'  - unresolved skill reference "{tok}" — did you mean "{best}"? (edit distance {dist}).')
        else:
            lines.append(f'  - unresolved skill reference "{tok}" — no registered skill is close to it.')
        if tier == 2:
            lines.append('    (referenced outside a "Pair with"/"see" position — this looks like a typo of a registered skill name)')
    if len(items) > SKILL_REF_MAX_REPORT:
        lines.append(f'  - (+{len(items) - SKILL_REF_MAX_REPORT} more unresolved references not listed)')
    return '\n'.join(lines)


@register_tool('propose_skill', allow_overwrite=True)
class ProposeSkill(BaseTool):
    """Tool to propose a new reusable skill for future tasks."""

    name = 'propose_skill'
    description = TOOL_METADATA['propose_skill']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'description': TOOL_METADATA['propose_skill']['parameters']['name'],
            },
            'skill_content': {
                'type': 'string',
                'description': TOOL_METADATA['propose_skill']['parameters']['skill_content'],
            },
            'justification': {
                'type': 'string',
                'description': TOOL_METADATA['propose_skill']['parameters']['justification'],
            },
            'rating': TOOL_METADATA['propose_skill']['parameters']['rating'],
        },
        'required': ['name'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute propose_skill.

        Args:
            params: JSON string with 'name' (required), 'skill_content' + 'justification'
                    (required to create/update), and optional 'rating'. Rating-only mode is
                    'name' + 'rating' without content.
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Result message indicating success or failure.
        """
        parsed = parse_tool_params(params)

        proposed_name = (parsed.get('name') or '').strip()
        skill_content = parsed.get('skill_content', '')
        rating_value = parsed.get('rating')

        if not proposed_name:
            return "The 'name' argument is required for propose_skill."

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # ── Rating-only mode: name + rating, no content → record and confirm ──
        # This is NOT a content modification, so it skips frontmatter validation, version bump,
        # and the user-approval flow. Security note: this bypasses approval by design (a rating is
        # not a content change) but means any agent can adjust a skill's recorded rating at any
        # time; ratings are currently advisory-only and feed no gating/scoring decision.
        if not skill_content and rating_value is not None:
            # Existence = registry OR an installed SKILL.md on disk (production root, dir-name keyed). Inactive
            # skills are dropped from _skills_registry by discover() but still resolve in scan_skills
            # (include_inactive=True over on-disk servables); get_installed_version reads that same file. BUG_0240.
            existing_meta = skill_manager.get_skill_metadata(proposed_name)
            installed_version = skill_manager.get_installed_version(proposed_name)
            if existing_meta is None and installed_version is None:
                return f"Cannot rate unknown skill '{proposed_name}'. It is not in the registry."
            rating_float, err = _coerce_rating(rating_value)
            if err:
                return err
            skill_manager.record_rating(proposed_name, rating_float)
            logger.info('[PROPOSE-SKILL] Rating-only: %s -> %s', proposed_name, rating_value)
            # Report the INSTALLED version — authoritative; the registry copy drifts when a SKILL.md is
            # edited in place. Show both when they disagree. BUG_0234.
            registry_version = (existing_meta or {}).get('version', '')
            if installed_version and registry_version and installed_version != registry_version:
                version_str = f"(installed v{installed_version}; registry v{registry_version})"
            else:
                version_str = f"(v{installed_version or registry_version or '1.0.0'})"
            return f"Recorded rating {rating_value}/10 for skill '{proposed_name}' {version_str}."

        if not skill_content:
            return ('No skill content provided. To create/update a skill include full SKILL.md with '
                    'YAML frontmatter (description, triggers) and body. To rate an existing skill, '
                    'provide `rating` as well.')

        justification = parsed.get('justification')

        if not justification:
            return "ERROR: 'justification' is required to create or update a skill"

        # Validate rating early when supplied alongside content.
        content_rating, err = _coerce_rating(rating_value)
        if err:
            return err

        # ── Strict-YAML frontmatter probe (advisory only) ─────────────────────
        # parse_frontmatter silently falls back to a lenient parser when strict YAML fails,
        # so the agent is told "registered successfully" without ever learning its frontmatter
        # is malformed. Re-probe here via the shared strict_frontmatter_check helper and
        # surface a NON-BLOCKING warning in the success string (soft-gate pattern).
        try:
            strict_ok, strict_reason = strict_frontmatter_check(skill_content)
        except (AttributeError, TypeError) as e:  # pragma: no cover - defensive; probe must not break propose
            logger.debug('[PROPOSE-SKILL] strict YAML probe failed (%s); skipping warning', type(e).__name__)
            strict_ok, strict_reason = True, ''

        # ── Auto-fix soft-YAML frontmatter (advisory, non-breaking) ──────────
        # When the strict probe failed, attempt a line-surgical re-quote via the shared
        # auto_fix_frontmatter helper so the REGISTERED skill loads cleanly (no console spam
        # on every future load). Runs BEFORE the lightweight parse and all downstream gates,
        # so everything downstream sees clean content. Defensive: any failure (or an unfixed
        # result) leaves skill_content untouched and falls back to the warning-only path below.
        auto_fixed = False
        if not strict_ok:
            try:
                fixed_content, changed, _ = auto_fix_frontmatter(skill_content)
                if changed:
                    skill_content = fixed_content
                    auto_fixed = True
                    logger.info('[PROPOSE-SKILL] auto-fixed strict-YAML frontmatter for %s (%s)',
                                proposed_name, strict_reason)
            except Exception as e:  # pragma: no cover - defensive; fix must not break propose
                logger.debug('[PROPOSE-SKILL] auto-fix failed (%s); keeping warning-only', type(e).__name__)
                auto_fixed = False

        # The `name` argument is authoritative. Match the frontmatter block ONCE, parse its
        # scalar fields, determine whether this is an update and compute the effective version
        # BEFORE reconstruction, then patch name (+version on updates) in a single pass.
        # NOTE: lightweight line-based parse — sufficient for scalar keys (name/version/description);
        # block-style or multi-line YAML values are not interpreted (only first physical line captured).
        fm, fm_span, proposed_version = _parse_lightweight_fm(skill_content)

        agent_name = kwargs.get('agent_instance_name', 'unknown')
        # BUG_0123: the run's actual task is the authoritative input for the Tier-2
        # self-match gate. Without it the gate is controlled by the proposal's own
        # frontmatter (omit generated_from_task -> no gate; an honest unrelated value ->
        # hard reject). Empty for direct/API callers, which keeps the legacy fallback.
        run_task_text = _source_task_text(self.agent_pool, kwargs.get('agent_instance_name'))
        existing_meta = skill_manager.get_skill_metadata(proposed_name)
        is_update = existing_meta is not None

        # ── BUG_0053: merge registered frontmatter onto a body-only UPDATE ─────────
        # load_skill strips the YAML frontmatter (token-saving), so an agent that only ever
        # saw a skill via load_skill supplies a body-only SKILL.md when updating it. Rebuild
        # the header from the already-registered frontmatter so the agent does not have to
        # guess description/triggers/source. Fires ONLY for an EXISTING name whose content has
        # NO frontmatter block: a supplied block is used as-is (no regression) and a NEW skill
        # must still declare its own frontmatter (the validator rejects it below).
        if is_update and frontmatter_span(skill_content) is None:
            skill_content = _merge_registered_frontmatter(existing_meta, skill_content)
            fm, fm_span, proposed_version = _parse_lightweight_fm(skill_content)
            logger.info('[PROPOSE-SKILL] %s: merged registered frontmatter onto body-only update',
                        proposed_name)

        # Captured BEFORE any gate/approval side effect: a disabled skill is dropped from the
        # registry, so `is_update` is False and it takes the new-skill path — but its production
        # file still carries the installed version, which must not be downgraded (BUG_0120).
        was_inactive = skill_manager.is_skill_disabled(proposed_name)

        # ── Hard-reject similarity gate (NEW + UPDATE) ────────────────────────
        # Reject a proposal whose frontmatter text is too similar to an existing skill,
        # before any approval request or registration. For an UPDATE we exclude the
        # incumbent of that name (an update is expected to resemble its own current
        # version), but still compare against every OTHER skill so an update cannot
        # silently collide with a different one. Frontmatter fields only — cheap.
        try:
            # Registry-only (active) skills for the similarity gate — disabled skills are not
            # live candidates and should not block a proposal on name/description similarity.
            all_metadata = skill_manager.get_all_metadata(include_active_only=True)
        except (TypeError, AttributeError) as e:  # pragma: no cover - defensive; gate must not break propose
            # get_all_metadata() is a pure in-memory dict-building loop (no I/O), so the only
            # realistic failures are malformed registry entries. Narrowed from bare Exception.
            logger.warning('[PROPOSE-SKILL] similarity gate skipped (get_all_metadata failed): %s', e)
            all_metadata = []

        # Extract the proposal's description/triggers with the robust YAML parser so block-style
        # `triggers:` lists are read correctly (the lightweight line parse above only captures
        # scalar values and would yield an empty string for a list, making the comparison
        # asymmetric against registered metadata that stores real triggers).
        prop_fm, _ = parse_frontmatter(skill_content)
        collisions = find_similar_skills(
            proposed_name, str(prop_fm.get('description', '') or ''), prop_fm.get('triggers'),
            all_metadata, SKILL_DUP_SIM_THRESHOLD,
            exclude_names=[proposed_name] if is_update else None,
        )
        if collisions:
            lines = [f"  - '{name}' (similarity: {sim * 100:.1f}%)" for name, sim in collisions]
            logger.warning('[PROPOSE-SKILL] REJECTED duplicate %s (%d collisions, threshold %.2f)',
                           proposed_name, len(collisions), SKILL_DUP_SIM_THRESHOLD)
            return (f"REJECTED: proposed skill '{proposed_name}' is too similar to existing skills "
                    f"(threshold {SKILL_DUP_SIM_THRESHOLD * 100:.0f}%):\n"
                    + '\n'.join(lines) +
                    "\nIf this is an update, use the existing skill's name. Otherwise differentiate "
                    'the name/description/triggers and re-propose.')

        # ── Soft keyword-overlap gate (NEW + UPDATE) ───────────────────────────
        # After the hard-duplicate check above, nudge on WEAKER keyword collisions: report the
        # top-3 existing skills whose keywords/triggers overlap this proposal, so a new skill does
        # not silently steal discovery matches from one that already covers the same vocabulary.
        # SOFT by design (D1): advisory text only — no early return; the proposal still proceeds to
        # approval and registers exactly as today. The floor reuses SKILL_MATCH_THRESHOLD (D2) — the
        # same "is this a real match?" bar AUTO-mode skill loading uses, so we flag precisely the
        # strength at which keywords start competing in discovery. Reuse match_skills with
        # include_inactive=True for the FULL list (active + inactive/disabled), since retired skills
        # still occupy their keyword index and can crowd matches; exclude the incumbent name (D3) so
        # an update/re-proposal never "overlaps" with itself.
        # prop_fm is the fully-parsed frontmatter (block-style trigger lists included), so it is
        # the correct source for the overlap query — not the lightweight line-based `fm`.
        overlap_query = skill_frontmatter_text(
            proposed_name,
            str(prop_fm.get('description', '') or ''),
            prop_fm.get('triggers'),
        )
        try:
            all_matches = skill_manager.match_skills(overlap_query, include_inactive=True)   # FULL list incl. inactive
        except Exception as e:  # pragma: no cover - defensive; gate must not break propose
            logger.warning('[PROPOSE-SKILL] overlap gate skipped (match_skills failed): %s', e)
            all_matches = []
        # Exclude the incumbent (an update/re-proposal overlapping its own name is expected), keep top-3 above floor.
        overlap_top3 = [
            (n, s) for n, s in all_matches
            if n.lower() != proposed_name.lower() and s >= SKILL_MATCH_THRESHOLD
        ][:3]

        # ── BUG_0199: unresolved inline skill-reference check (advisory) ──────
        # The body is final at this point (strict-YAML auto-fix and the body-only
        # frontmatter merge both ran above; the frontmatter patch below mutates
        # only the frontmatter span), so the scanned bytes are the registered bytes.
        # Non-fatal by construction: it appends text to the approval prompt and the
        # success returns only — no early return, no content mutation, no effect on
        # the rating-driven candidate promotion.
        ref_items = []
        ref_warn = ''
        try:
            _, ref_body = parse_frontmatter(skill_content)  # body only — frontmatter is not prose
            reg_names, mem_names = _collect_skill_reference_names(skill_manager, self.agent_pool)
            ref_items = _unresolved_skill_references(ref_body, proposed_name, reg_names, mem_names)
            if ref_items:
                ref_warn = _format_skill_reference_warning(ref_items)
                logger.warning('[PROPOSE-SKILL] %s: %d unresolved skill reference(s)',
                               proposed_name, len(ref_items))
        except Exception as e:  # pragma: no cover - defensive; a broken warning must never fail propose
            ref_items, ref_warn = [], ''
            logger.debug('[PROPOSE-SKILL] reference scan skipped (%s): %r', type(e).__name__, e)

        if is_update:
            existing_version = existing_meta.get('version', '1.0.0')
            effective_version = _choose_update_version(proposed_version, existing_version, proposed_name)
        else:
            effective_version = normalize_version(proposed_version) or '1.0.0'
            # Re-proposing a DISABLED skill takes the new-skill path (absent from the registry),
            # so it never reaches `_choose_update_version`. Guard it explicitly against a
            # downgrade of the installed production file (BUG_0120).
            if was_inactive:
                installed_version = skill_manager.get_installed_version(proposed_name)
                proposed_key = _version_tuple(effective_version)
                installed_key = _version_tuple(installed_version) if installed_version else None
                if proposed_key and installed_key and proposed_key < installed_key:
                    effective_version = _next_patch_version(installed_version)
                    logger.info('[PROPOSE-SKILL] %s: proposed v%s is older than installed v%s; using v%s',
                                proposed_name, normalize_version(proposed_version), installed_version,
                                effective_version)

        # Patch the frontmatter block (name always; version only on updates) in one pass, so a
        # body line like "version: X" is never touched.
        if fm_span:

            def _patch_field(text, key, value):
                """Replace or insert a top-level scalar field within frontmatter text only."""
                if re.search(r'(?m)^%s\s*:' % key, text):
                    return re.sub(r'(?m)^%s\s*:.*$' % key, f'{key}: {value}', text, count=1)
                return f'{key}: {value}\n' + text

            fm_text = _patch_field(skill_content[fm_span[0]:fm_span[1]], 'name', proposed_name)
            # Patch the version on updates, and whenever the chosen version differs from what
            # the content declares (e.g. a downgrade-guarded re-proposal of a disabled skill,
            # which takes the new-skill path — BUG_0120). Without this the message and the
            # registered file would disagree.
            if is_update or effective_version != normalize_version(proposed_version):
                fm_text = _patch_field(fm_text, 'version', effective_version)
            skill_content = skill_content[:fm_span[0]] + fm_text + skill_content[fm_span[1]:]

        # ── Pre-approval health check (NEW) ─────────────────────────────────────
        # Screen invalid proposals BEFORE the user-approval prompt so a bad skill never wastes an
        # approval/security call. Mirrors register's validate_skill (name derivation + upgrade
        # exclusion + task derivation); register re-runs it under lock as the authoritative
        # check, so no drift can slip through. Runs on the patched content (the authoritative
        # name is already applied). `run_task_text` is the engine-provided run task (BUG_0123)
        # and is preferred over the frontmatter field by both this call and register.
        # Rating-only mode (above) and the similarity gate are unaffected. Defensive: a
        # pre-check error must not break propose — fall through to the authoritative check.
        try:
            # prevalidate_skill returns (passed, messages); on the not-passed path below,
            # messages holds only blocking errors (warnings are non-blocking and never set
            # alongside them).
            passed, health_messages = skill_manager.prevalidate_skill(skill_content, task_text=run_task_text)
        except Exception as e:  # pragma: no cover - defensive; pre-check must not break propose
            logger.warning('[PROPOSE-SKILL] pre-approval health check skipped (error): %s: %r',
                           type(e).__name__, e)
            passed, health_messages = True, []
        if not passed:
            lines = [f"  - {msg}" for msg in health_messages]
            logger.warning('[PROPOSE-SKILL] REJECTED health check %s (%d issues)',
                           proposed_name, len(health_messages))
            return (f"REJECTED: proposed skill '{proposed_name}' failed pre-approval health checks:\n"
                    + '\n'.join(lines) +
                    '\nFix the issues and re-propose.')

        if is_update:
            # Approval for UPDATE — content for an existing name is always an update;
            # the patch version above makes effective_version differ from existing_version.
            description = (f"📝 **Update Existing Skill**: {proposed_name}\n\n"
                           f"Current version: v{existing_version} → New version: v{effective_version}\n"
                           f"Justification: {justification}")
        else:
            # Approval for NEW skill
            description = (f"📝 **Propose New Skill**: {proposed_name}\n\n"
                           f"Description: {fm.get('description', '') if fm else ''}\n"
                           f"Version: v{effective_version}\n"
                           f"Justification: {justification}\n\n"
                           f"This will be registered and available to all agents via scan_skills/load_skill.")

        # Soft keyword-overlap notice (D1): advisory only, appended for the user to weigh at
        # approval time. Rendered only when the gate found overlapping skills above the floor.
        if overlap_top3:
            overlap_lines = [f"  - '{n}' (overlap: {s * 100:.0f}%)" for n, s in overlap_top3]
            description += ("\n\n⚠️  Keyword-overlap notice: this proposal's keywords/triggers "
                            'overlap with existing skills and may make discovery noisy (steal their '
                            'matches). Consider upgrading one of these instead of creating a duplicate:\n'
                            + '\n'.join(overlap_lines))

        # BUG_0199: unresolved-skill-reference notice (advisory). Surfaced BEFORE the approval
        # request so the agent can fix a broken pointer instead of wasting a registration.
        if ref_items:
            description += ('\n\n⚠️  Skill-reference notice: this body points at skill names that '
                            'are not registered:\n' + ref_warn.strip())

        # Request user approval (same pattern as shell_cmd)
        approved, reason = self.agent_pool.operation_manager.request_user_approval(
            agent_name=agent_name,
            tool_name='propose_skill',
            tool_args={
                'skill_content': skill_content,
                'justification': justification,
            },
            description=description,
        )

        if not approved:
            return f"REJECTED: {reason}"

        # Both NEW skills and UPDATES go through register_skill_from_content. For an existing
        # name it routes to _register_candidate_upgrade (candidate folder + decision gate), so
        # an update creates a candidate that the gate later promotes/discards — it never
        # overwrites the production file directly. A new name registers/auto-promotes as before.

        success, errors = skill_manager.register_skill_from_content(
            skill_content=skill_content,
            source='auto-generated',
            task_text=run_task_text,
        )

        if success:
            # BUG_0119 safety net: `name` is the documented contract, so after a successful
            # registration the skill MUST be findable under it. If the frontmatter patch was
            # skipped (an exotic delimiter form the shared locator did not match), reporting
            # success for a name that does not exist sends the caller to the wrong skill.
            if skill_manager.get_skill_metadata(proposed_name) is None:
                actual_name = str(parse_frontmatter(skill_content)[0].get('name', '') or '<unknown>')
                return (f"FAILED: registration used the frontmatter name '{actual_name}' instead "
                        f"of the required '{proposed_name}'. Ensure the frontmatter 'name: ' "
                        f'matches the `name` argument or use a standard "---" delimited block.')
            # Re-activate a previously-inactive skill that this proposal re-proposed. register's
            # new-skill path (and the candidate-upgrade path) never clear _disabled_names, so the
            # skill would otherwise stay disabled after a "successful" registration — enable_skill
            # is what actually flips it active. Guarded: a re-enable failure degrades to a warning
            # and must never break the success return (mirrors the defensive pre-check above).
            reenabled_note = ''
            if was_inactive:
                try:
                    ok, emsg = skill_manager.enable_skill(proposed_name)
                    if ok:
                        reenabled_note = ' (re-enabled: was inactive)'
                    else:
                        logger.warning('[PROPOSE-SKILL] re-enable of %s failed: %s', proposed_name, emsg)
                except Exception as e:  # pragma: no cover - defensive; never break the success path
                    logger.warning('[PROPOSE-SKILL] re-enable of %s raised: %s', proposed_name, e)
            # If a rating was supplied alongside content, record it after successful
            # registration/update. Skills start unrated (there is no synthetic 5.0 seed), so
            # this is usually the skill's first rating.
            if content_rating is not None:
                try:
                    skill_manager.record_rating(proposed_name, content_rating)
                except ValueError as e:
                    logger.warning('[PROPOSE-SKILL] Failed to record rating for %s: %s', proposed_name, e)
            # Non-blocking advisory: the frontmatter required the lenient fallback. If we
            # auto-fixed it, tell the agent it was repaired (no action needed); otherwise tell
            # it the YAML is malformed and how to fix it. Registration already succeeded —
            # this only surfaces in the success string (soft-gate pattern).
            yaml_warn = ''
            if auto_fixed:
                yaml_warn = (f"\n\n✅ YAML AUTO-FIXED: your frontmatter had a strict-YAML issue "
                             f"({strict_reason}). I re-quoted the offending value(s) so it now loads "
                             f"cleanly — no action needed.")
            elif not strict_ok:
                yaml_warn = (f"\n\n⚠️ YAML WARNING: your frontmatter did NOT parse under strict "
                             f"YAML ({strict_reason}). It was recovered via the tolerant fallback, but "
                             f"quote any value containing special characters (e.g. colons) as a single "
                             f"double-quoted string to keep it clean.")
            # The is_update branch returns above, so this generic line is only ever reached
            # for a NEW registration — 'registered' is the only reachable wording.
            if is_update:
                # Be explicit about the candidate flow: the production file is NOT changed yet,
                # and the update may be discarded by the gate (BUG_0122).
                return (f"Skill '{proposed_name}' updated successfully: candidate v{effective_version} "
                        f"is now serving and will be promoted or discarded after enough ratings."
                        f"{reenabled_note}{yaml_warn}{ref_warn}")
            return (f"Skill '{proposed_name}' registered successfully (v{effective_version})."
                    f"{reenabled_note}{yaml_warn}{ref_warn}")
        else:
            error_detail = '; '.join(errors) if errors else 'Unknown error'
            action = 'update' if is_update else 'register'
            return f"Failed to {action} skill '{proposed_name}': {error_detail}"
