"""
Scan Skills Tool — Read-only tool that queries SkillManager and returns matching skills.

Allows agents to discover available skills and their relevance scores for a given query.
This is the primary way orchestrators decide which skills to load via call_agent(load_skill=[...]).
"""

import logging
import re
from pathlib import Path

from agent_cascade.skills.manager import rating_sort_key
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.utils import parse_tool_params

logger = logging.getLogger(__name__)

# Max match bullets rendered in query mode. The matcher returns every skill with a
# non-zero score (hundreds on a broad query — ~45k chars / ~11k tokens per call),
# which is the cost driver of perceived scan_skills latency. Capping at 10 keeps the
# ranking contract (top-10 is what any consumer acts on) and cuts output ~92%.
SKILL_SCAN_MAX_RESULTS = 10


def _format_chars(chars: int) -> str:
    """Format a character count as a rounded '~N.Nk chars' display string."""
    return f"~{chars // 1000}.{'0' if chars % 1000 < 500 else '5'}k chars"


@register_tool('scan_skills', allow_overwrite=True)
class ScanSkills(BaseTool):
    """Read-only tool to query available skills and their relevance scores."""

    name = 'scan_skills'
    description = ('Scan registered skills and return matching skills with relevance scores. '
                   'Use this to discover which skills are available before calling call_agent with load_skill. '
                   'Returns skill names, descriptions, match scores, and quality ratings for the given query. '
                   'Pass a skill name via `preview` (or as a single-word query) to get its full raw text.')
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type':
                    'string',
                'description':
                    'Search query or task description to match against available skills. Leave empty to list all registered skills.',
            },
            'active': {
                'type': 'boolean',
                'description': (
                    'If true, restrict the no-query listing to ACTIVE skills only (exclude disabled/'
                    'inactive ones). Default: false — the default listing includes disabled/inactive '
                    'skills, each marked with an " (inactive)" suffix.'
                ),
            },
            'preview': {
                'type':
                    'string',
                'description':
                    ('Skill name to preview. When provided, returns the full raw text of that skill\'s '
                     'SKILL.md file in a code block — useful for anchoring when updating an existing '
                     'skill. Overrides query/listing behavior.'),
            },
        },
        'required': [],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        """Execute the scan_skills tool.

        Args:
            params: JSON string or dict with 'query' field.
            kwargs: Additional context (agent_instance_name for logging).

        Returns:
            Formatted markdown list of matching skills.
        """
        parsed = parse_tool_params(params)
        query = parsed.get('query', '')
        active_only = parsed.get('active', False)
        preview_name = parsed.get('preview', '')

        # Get SkillManager from pool
        skill_manager = getattr(self.agent_pool, 'skill_manager', None)
        if skill_manager is None:
            return 'No skills system available. Skills may not have been initialized.'

        # Trigger a fresh discovery (cache-respecting) so new skills appear
        skill_manager._ensure_discovered()

        # Preview mode (purely additive — short-circuits before listing/matching):
        #   1. explicit `preview` param → resolve as a skill name; or
        #   2. no preview param, but the query is a single token that EXACTLY matches a
        #      registered skill name (case-insensitive) → treat it as a preview too. This
        #      catches the natural "agent types the skill name" case without ever touching
        #      multi-word queries (those still go to the matcher below).
        if isinstance(preview_name, str) and preview_name.strip():
            return self._preview_skill(skill_manager, preview_name.strip())
        # Single-word query that exactly matches a registered name (case-insensitive).
        # get_skill_names() returns exact-case keys, so compare against their lowercase set.
        if not preview_name:
            q = query.strip()
            if q and ' ' not in q and q.lower() in {n.lower() for n in skill_manager.get_skill_names()}:
                return self._preview_skill(skill_manager, q)

        # Data path: the DEFAULT listing includes disabled/inactive skills (each marked " (inactive)"
        # below); active=True restricts to the active registry only. In production disabled skills are
        # absent from the registry (discover drops them), so get_all_metadata re-surfaces them on the
        # default path via a servable-disk walk.
        # get_all_metadata(include_active_only=...) already enforces the range: active=True returns
        # registry-only skills (discover() guarantees disabled ones are absent from the registry),
        # active=False additionally re-surfaces disabled-but-servable skills. No manual filter needed.
        all_skills = skill_manager.get_all_metadata(include_active_only=active_only)
        if not all_skills:
            return 'No skills are currently registered in the system.'

        # If no query, list everything sorted by average rating (desc); unrated last, name asc tiebreak.
        if not query.strip():

            def _sort_key(skill):
                return rating_sort_key(skill['name'], skill_manager.get_rating_average(skill['name']))

            # Candidate marker data (no-query mode only): names whose registry winner is a
            # candidate file, plus the incumbent version parsed from its production file.
            candidate_names = set(skill_manager.get_candidate_names())
            _, _production_root = skill_manager._candidate_dirs()

            def _incumbent_version(name: str) -> str:
                prod_file = _production_root / name / 'SKILL.md'
                if not prod_file.exists():
                    return '?'
                try:
                    from agent_cascade.skills.parser import parse_skill_file
                    return parse_skill_file(prod_file).get('version', '?')
                except (OSError, FileNotFoundError):
                    return '?'

            # Inactive marker (no-query mode): mark every disabled/inactive skill. Driven by
            # _disabled_names via is_skill_disabled() — the SAME source the match filter uses,
            # so a SKILLS_DISABLED-style disable (in _disabled_names but with no metrics entry)
            # is marked here too, keeping marker and filter in agreement. active=True hides them.

            lines = ['## Available Skills']
            for skill in sorted(all_skills, key=_sort_key):
                source = skill.get('source', 'system')
                version = skill.get('version', '1.0.0')
                chars = skill.get('chars', 0)
                rating = skill_manager.get_rating_average(skill['name'])
                rating_str = f'{rating}' if rating is not None else 'n/a'
                candidate_note = (f" (candidate, pending decision vs v{_incumbent_version(skill['name'])})"
                                  if skill['name'] in candidate_names else '')
                inactive_note = ' (inactive)' if skill_manager.is_skill_disabled(skill['name']) else ''
                lines.append(f"- **{skill['name']}** [{source}] v{version} "
                             f"(rating: {rating_str}, {_format_chars(chars)})"
                             f"{candidate_note}{inactive_note}: "
                             f"{skill.get('description', 'No description')}")
            return '\n'.join(lines)

        # Use public API to score skills against the query. When the caller wants the
        # DEFAULT (active=False) view, match over the SAME full set the listing uses
        # (include_inactive=True) so an exact match on a retired skill surfaces here
        # instead of "No skills matched". active=True keeps matching active-only.
        matches = skill_manager.match_skills(query, include_inactive=not active_only)
        if not matches:
            # No full-catalog dump here: an out-of-domain query used to print every
            # registered skill (~45k chars), which is exactly the noise this tool
            # exists to avoid. Point at the no-query listing instead — one deliberate
            # call, capped output.
            return (f"No skills matched the query '{query}'.\n"
                    'Call scan_skills with an empty query to list all registered skills.')

        # Build response with scores. Cap the rendered bullets: the matcher returns
        # every skill with a non-zero score, and printing hundreds of bullets per call
        # is the output-volume defect (todo 123). Top-10 preserves the ranking contract.
        total_matches = len(matches)
        truncated = total_matches > SKILL_SCAN_MAX_RESULTS
        matches = matches[:SKILL_SCAN_MAX_RESULTS]

        # Resolve display fields from the already-fetched full listing (``all_skills``)
        # rather than the registry, so registry-absent (inactive) matches still show a
        # real description/source/version/chars. ``all_skills`` carries name/description/
        # source/version/chars for both active and inactive skills.
        meta_by_name = {s['name'].lower(): s for s in all_skills}
        lines = [f"## Skills Matching Query: '{query}'"]
        for name, score in matches:
            meta = meta_by_name.get(name.lower())
            desc = meta.get('description', 'No description') if meta else 'Unknown'
            source = meta.get('source', 'system') if meta else 'unknown'
            version = meta.get('version', '1.0.0') if meta else '1.0.0'
            chars = meta.get('chars', 0) if meta else 0
            metrics = skill_manager.get_metrics(name)
            loads = metrics.get('total_loads', 0)
            rating = skill_manager.get_rating_average(name)
            rating_str = f'{rating}' if rating is not None else 'n/a'
            inactive_note = ' (inactive)' if skill_manager.is_skill_disabled(name) else ''
            lines.append(f"- **{name}** [{source}] v{version} (score: {score:.2f}, loads: {loads}, "
                         f"rating: {rating_str}, {_format_chars(chars)}){inactive_note}: {desc}")

        if truncated:
            lines.append(f"(showing top {SKILL_SCAN_MAX_RESULTS} of {total_matches} matches)")
        return '\n'.join(lines)

    def _preview_skill(self, skill_manager, name: str) -> str:
        """Return the full raw SKILL.md text for ``name`` in a fenced code block.

        Preview is a READ-ONLY anchor for editing an existing skill: it returns the
        complete file (frontmatter + body), NOT just the body that
        ``load_full_instructions`` yields, and it does NOT increment any load metric
        (``count_load=False`` semantics) — previewing must not inflate usage stats.

        Path resolution mirrors ``load_full_instructions``: exact registry key first,
        then a case-insensitive fallback, then the servable-disk index for evicted/inactive
        skills (``_find_servable_skill_path``). The path is read from the registry at call
        time — never cached — because registration can relocate ``file_path`` to a promoted
        production file.

        DELIBERATE GATE SKIP: unlike ``load_full_instructions``, preview does NOT consult
        ``_disabled_names`` or platform compatibility, so it will surface the raw text of a
        disabled or platform-incompatible skill. That is intentional for an editing anchor —
        you need to see what's there to change it — but it means preview is not a substitute
        for the load path and must never be used to "load" a disabled skill's instructions.
        """
        from agent_cascade.skills.parser import parse_skill_file

        # Resolve the backing SKILL.md path. All shared-state reads (registry + servable
        # index) happen under _write_lock so they are atomic; the file read itself stays
        # OUTSIDE the lock (it's I/O and touches no shared state). _find_servable_skill_path
        # acquires no lock of its own (pure dict read), so nesting it here is safe.
        # Also capture the canonical registry key so a case-variant input ("Charlie") previews
        # under its real name ("charlie") — an editing anchor should show the actual skill name.
        file_path = None
        version = source = None
        display_name = name
        with skill_manager._write_lock:
            reg = skill_manager._skills_registry.get(name)
            if reg is None:
                lower = name.lower()
                for key, entry in skill_manager._skills_registry.items():
                    if key.lower() == lower:
                        reg = entry
                        break
            if reg is not None:
                file_path = reg.get('file_path')
                version = reg.get('version')
                source = reg.get('source')
                display_name = reg.get('name', name)
            # Servable-disk fallback for evicted/inactive skills (absent from the registry).
            if file_path is None:
                sp = skill_manager._find_servable_skill_path(name)
                if sp is not None:
                    file_path = str(sp)

        # Metadata for a servable-fallback skill isn't in the registry, so parse it from disk.
        # Best-effort (a binary/unreadable file just leaves version/source unset); the raw read
        # below re-opens the same path and is what actually decides loadability.
        if file_path is not None and reg is None:
            try:
                parsed = parse_skill_file(Path(file_path))
                version = parsed.get('version')
                source = (parsed.get('frontmatter') or {}).get('source')
            except (FileNotFoundError, OSError, UnicodeDecodeError):
                pass

        if not file_path:
            return f"Skill '{display_name}' not found or not loadable."

        try:
            content = Path(file_path).read_text(encoding='utf-8')
        except (FileNotFoundError, OSError, UnicodeDecodeError) as e:
            logger.debug('[SKILLS] preview: failed to read %s: %s', file_path, e)
            return f"Skill '{display_name}' not found or not loadable."

        # Fence widening: the raw markdown may itself contain backtick runs, so size the
        # fence one longer than the longest run of backticks inside the content (min 3) to
        # keep the code block well-formed. A body with ``` needs ````; a body with ````
        # needs ````` — a fixed 4-backtick fence would close early on the latter.
        max_run = max((len(m.group()) for m in re.finditer(r'`+', content)), default=0)
        fence = '`' * max(3, max_run + 1)

        meta = ''
        if version or source:
            parts = []
            if version:
                parts.append(f'v{version}')
            if source:
                parts.append(source)
            meta = f" ({', '.join(parts)})"
        return (f"## Skill Preview: {display_name}{meta}\n\n"
                f"{fence}markdown\n{content}\n{fence}")
