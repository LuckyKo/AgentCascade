"""Content gate for memory-hint skill suggestions (BUG_0226).

Why this exists
---------------
``SkillMatcher`` accepts any skill sharing at least ``_MIN_MATCHED_TERMS`` (= 2)
distinct query tokens with the skill's name/description/triggers. That rule is
deliberately loose — it is shared by AUTO mode, ``scan_skills`` and the skill
advisor, all of which tolerate a wide recall net. Used as a *proactive hint*
during an unrelated edit, the same looseness surfaces skills whose only overlap
with the turn is English function words: ``webui-feature-mirroring-research``
was suggested during a C++ engine edit on ``add`` + ``existing`` (turn 1) and on
``section`` + ``the``/``and``/``for``/``its`` (turn 2).

The fix is a POST-MATCH gate, not a scoring change:

* the scorer is untouched, so every surviving score is bit-identical and the
  tuned constants (``SKILL_FLOOR_SEED``, ``SKILL_JUNK_MIN``) stay valid;
* only candidates that were carried by stopwords are removed, so the fire/skip
  decision, the EWMA feed and the noise-gate denominator are unchanged;
* it is applied only by ``MemoryHintManager._match_skills_for_hint``, so the
  other ``SkillMatcher`` callers keep their existing behaviour.

Measured on 745 real skills x 3,064 real turns: 132 of 2,732 delivered
suggestions were stopword-carried; this gate removes all of them while keeping
95 % of the good ones and leaving retrieval metrics identical.

Two lists, kept separate on purpose
-----------------------------------
``STOP_FUNCTION`` cannot remove topical signal — these are pure English
function words (plus contraction stems, because ``_TOKEN_RE`` splits ``doesn't``
into ``doesn`` + ``t``).

``STOP_GENERIC`` is high-frequency engineering boilerplate. It *can* be topical
for a few skills, so it is only used to count content terms, never to change
scores. It is still required: the function-word list alone does not fix the
incident, because ``add``/``existing`` are not function words.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from agent_cascade.skills.matcher import _TOKEN_RE

# Minimum number of distinct NON-stopword query terms a candidate must share
# with its own indexed frontmatter. Matches ``_MIN_MATCHED_TERMS`` so the gate
# reads as "the existing rule, but only content words count".
MIN_CONTENT_TERMS = 2

STOP_FUNCTION = frozenset("""
a about above after again against all am an and any are aren't as at be because
been before being below between both but by can cannot could couldn't did didn't
do does doesn't doing don't down during each few for from further had hadn't has
hasn't have haven't having he he'd he'll he's her here here's hers herself him
himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself
just let's me more most mustn't my myself no nor not of off on once only or other
ought our ours ourselves out over own same shan't she she'd she'll she's should
shouldn't so some such than that that's the their theirs them themselves then
there there's these they they'd they'll they're they've this those through to too
under until up very was wasn't we we'd we'll we're we've were weren't what what's
when when's where where's which while who who's whom why why's will with won't
would wouldn't you you'd you'll you're you've your yours yourself yourselves
aren isn wasn weren hasn haven hadn doesn didn don wouldn couldn shouldn mustn
needn shan won ain
""".split())

STOP_GENERIC = frozenset("""
add added adding also check checked checking code codes create created creating
existing file files fix fixed fixing line lines list make makes making new note
notes read reading report reports run running see seen set sets setting still
test tests tested testing use used uses using way work working write writing
""".split())

# The set actually used for content counting. Kept as one frozenset so the
# hot path does a single lookup per query token.
STOP_SKILL_HINT = STOP_FUNCTION | STOP_GENERIC


def _query_tokens(query: str) -> Set[str]:
    """Tokenize exactly like ``SkillMatcher.match`` (lowercase, same regex)."""
    return set(_TOKEN_RE.findall((query or '').lower()))


def matched_term_counts(query: str, skill_names: Iterable[str],
                        field_index: Dict[str, Dict[str, object]],
                        ) -> Dict[str, Tuple[int, int]]:
    """Return ``{skill: (matched_terms, content_terms)}`` for the candidates.

    ``matched_terms`` counts distinct query tokens present in the skill's
    indexed frontmatter; ``content_terms`` counts the subset that is not in
    ``STOP_SKILL_HINT``. Both are computed from the matcher's OWN inverted
    index, so they agree with how the candidate was scored (no re-tokenizing
    of skill text, no second source of truth).
    """
    wanted = set(skill_names)
    counts: Dict[str, Tuple[int, int]] = {n: (0, 0) for n in wanted}
    if not wanted or not field_index:
        return counts
    for token in _query_tokens(query):
        names = field_index.get(token)
        if not names:
            continue
        content = token not in STOP_SKILL_HINT
        for name in names:
            if name not in counts:
                continue
            matched, content_n = counts[name]
            counts[name] = (matched + 1, content_n + (1 if content else 0))
    return counts


def filter_stopword_only_names(query: str, names: Sequence[str],
                               skill_manager) -> List[str]:
    """Drop candidates that could only have surfaced on stopwords.

    A candidate is removed when it shares at least one query token with its own
    indexed frontmatter but fewer than ``MIN_CONTENT_TERMS`` distinct non-stop
    tokens. Candidates with NO lexical overlap at all are kept: they cannot have
    come from the keyword scorer (e.g. the API skill selector re-ranks
    semantically), so this gate has no evidence against them and must not
    second-guess another selector's choice.

    Order is preserved (the caller's list is score-descending).

    Fails open: if the manager is a mock or the index is unavailable, ``names``
    is returned unchanged. This keeps the hint path's "never break delivery"
    contract and leaves MagicMock-based tests untouched.
    """
    names = list(names)
    if not names:
        return names
    field_index = _field_index(skill_manager)
    if field_index is None:
        return names
    counts = matched_term_counts(query, names, field_index)
    return [n for n in names
            if counts.get(n, (0, 0))[0] == 0
            or counts.get(n, (0, 0))[1] >= MIN_CONTENT_TERMS]


def describe_dropped(query: str, kept: Sequence[str],
                     candidates: Sequence[str], skill_manager) -> str:
    """One-line DEBUG detail naming what the gate removed ('' when nothing was)."""
    kept_set = set(kept)
    dropped = [n for n in candidates if n not in kept_set]
    if not dropped:
        return ''
    field_index = _field_index(skill_manager)
    if field_index is None:
        return ', '.join(dropped)
    counts = matched_term_counts(query, dropped, field_index)
    return ', '.join(
        f'{n}(matched={counts.get(n, (0, 0))[0]},content={counts.get(n, (0, 0))[1]})'
        for n in dropped)


def _field_index(skill_manager) -> Optional[Dict[str, Dict[str, object]]]:
    """The live keyword index, or ``None`` when unavailable (fail-open signal)."""
    try:
        index = getattr(getattr(skill_manager, '_matcher', None),
                        '_field_index', None)
    except Exception:                                  # defensive: exotic proxies
        return None
    return index if isinstance(index, dict) and index else None
