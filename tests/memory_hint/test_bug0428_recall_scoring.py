"""BUG_0428: cross-session recall blends recency × importance × relevance.

Proves the matcher's ``score = w_rec·recency + w_imp·importance + w_rel·relevance``:
  * a high-importance-but-older lesson reorders above a low-importance-but-recent one, and
  * the default weights are relevance-dominant; a relevance-only weight set reduces the
    blend to the pure cosine (no regression to existing recall).

The two lessons carry IDENTICAL bodies so their pure-cosine relevance is equal; any
reordering the blend produces is therefore driven purely by the recency/importance
difference, never the relevance term.
"""
from agent_cascade.memory_hint.matcher import (
    MemoryMatcher,
    DEFAULT_RECALL_WEIGHTS,
    _resolve_weights,
)


def _build_matcher():
    """Two lessons with EQUAL relevance (identical bodies). ``a.md`` is the
    high-importance, older lesson; ``b.md`` is the low-importance, recent one."""
    m = MemoryMatcher()
    body = 'compression engine loop hang debug timeout'
    m.set_documents({
        'a.md': ('compression', body),
        'b.md': ('compression', body),
    })
    return m


_QUERY = 'compression engine loop hang debug timeout'
# a.md is older (lower recency) but high-importance; b.md is recent but low-importance.
_RECENCY = {'a.md': 0.2, 'b.md': 0.9}
_IMPORTANCE = {'a.md': 1.0, 'b.md': 0.0}


class TestRecallScoring:
    def test_default_weights_are_relevance_dominant(self):
        """Sane defaults: relevance out-weights both recency and importance."""
        w = DEFAULT_RECALL_WEIGHTS
        assert w['relevance'] > w['importance']
        assert w['relevance'] > w['recency']
        assert all(v >= 0.0 for v in w.values())

    def test_pure_cosine_order_unchanged_without_signals(self):
        """No recency/importance supplied → pure cosine; equal-relevance docs tie-break by path."""
        m = _build_matcher()
        order = [p for p, _ in m.match(_QUERY)]
        assert order == ['a.md', 'b.md']  # equal cosine → a.md first (path tie-break)

    def test_recency_alone_reorders_recent_lesson_first(self):
        """With recency only (importance weight 0), the recent lesson b.md rises to the top."""
        m = _build_matcher()
        order = [p for p, _ in m.match(
            _QUERY,
            recency=_RECENCY,
            importance={'a.md': 0.0, 'b.md': 0.0},
            weights={'recency': 1.0, 'importance': 0.0, 'relevance': 1.0})]
        assert order[0] == 'b.md', 'recency-only blend must rank the recent lesson first'

    def test_high_importance_beats_low_importance_by_default(self):
        """With the default weights (recency OFF, importance 0.3, relevance 1.0) the
        high-importance lesson a.md ranks above the low-importance b.md — driven purely by
        the importance term (age is irrelevant with recency off)."""
        m = _build_matcher()
        order = [p for p, _ in m.match(_QUERY, recency=_RECENCY, importance=_IMPORTANCE)]
        assert order[0] == 'a.md', 'high-importance lesson must rank first'

    def test_full_blend_older_high_importance_beats_recent_low_importance(self):
        """The headline BUG_0428 property with BOTH recency and importance active: a
        high-importance-but-older lesson ranks above a low-importance-but-recent one when
        the importance gap dominates the recency gap."""
        m = _build_matcher()
        order = [p for p, _ in m.match(
            _QUERY,
            recency=_RECENCY,          # a.md older (0.2), b.md recent (0.9)
            importance=_IMPORTANCE,    # a.md high (1.0), b.md low (0.0)
            weights={'recency': 0.2, 'importance': 0.3, 'relevance': 1.0})]
        assert order[0] == 'a.md', 'high-importance-older lesson must rank first'

    def test_relevance_only_weights_reduce_to_pure_cosine(self):
        """No-regression guard: weights (0,0,1) make the blend exactly the pure cosine, so
        existing recall is byte-identical when only relevance is weighted."""
        m = _build_matcher()
        pure = m.match(_QUERY)
        blended = m.match(_QUERY, recency=_RECENCY, importance=_IMPORTANCE,
                          weights={'recency': 0.0, 'importance': 0.0, 'relevance': 1.0})
        assert [p for p, _ in pure] == [p for p, _ in blended]
        for (_pa, sa), (_pb, sb) in zip(pure, blended):
            assert sa == sb, 'relevance-only blend must equal the pure cosine score'

    def test_resolve_weights_falls_back_on_bad_input(self):
        """_resolve_weights never raises: a missing dict / non-coercible value falls back
        to the sane defaults for that key."""
        assert _resolve_weights(None) == (
            DEFAULT_RECALL_WEIGHTS['recency'],
            DEFAULT_RECALL_WEIGHTS['importance'],
            DEFAULT_RECALL_WEIGHTS['relevance'])
        w = _resolve_weights({'recency': 'not-a-number', 'importance': 0.5, 'relevance': 0.1})
        assert w == (DEFAULT_RECALL_WEIGHTS['recency'], 0.5, 0.1)
