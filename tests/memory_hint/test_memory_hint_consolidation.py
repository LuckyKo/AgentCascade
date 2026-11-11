"""Tests for the memory-vault consolidation features A, B and C.

Feature A — ``status:`` de-index filter in ``VaultIndex.rescan()``: a lesson is
            dropped from the hint index ONLY when its ``status:`` is exactly
            ``superseded``/``archived``. Every other status (the 17 free-form
            values, ``active``, ``deprecated``) and no-frontmatter stay ACTIVE,
            and the de-indexed file remains on disk (greppable).

Feature C — optional scope filter in ``MemoryMatcher.match()``. Ships dark:
            ``allowed_scope=None`` (the default) is a byte-for-byte no-op. The
            scope set is carried on the doc tuple (3rd element) and ``set_documents``
            stays tolerant of legacy 2-tuples.

Feature B — ``hub:`` breadcrumb on a hinted leaf. The leaf's ``hub:`` slug is
            carried on the doc tuple (4th element), surfaced through the manager's
            ``_hubs`` map, and rendered as `` (hub: #<slug>)`` — display-only, a
            byte-identical render when no hub is present.

All tests are self-contained (tmp_path vaults, MagicMock pools) and run fast.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cascade.memory_hint import MemoryHintManager, MemoryMatcher
from agent_cascade.memory_hint.vault import (
    _DEINDEX_STATUSES,
    _hub_slug,
    _is_deindexed,
    VaultIndex,
    normalize_scope,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _write(tmp_vault: Path, rel_path: str, frontmatter: str, body: str) -> Path:
    """Write a lesson with caller-supplied frontmatter lines (no leading ``---``)."""
    p = tmp_vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding='utf-8')
    return p


def _vault(tmp_path):
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True)
    return v


# ── Feature A: status de-index filter ───────────────────────────────────────

class TestDeindexFilter:
    def test_is_deindexed_allowlist(self):
        """Only superseded/archived de-index; everything else stays active."""
        assert _DEINDEX_STATUSES == frozenset({'superseded', 'archived'})
        # Allowlist hits (case + surrounding whitespace tolerated).
        assert _is_deindexed({'status': 'superseded'}) is True
        assert _is_deindexed({'status': 'Superseded'}) is True
        assert _is_deindexed({'status': '  archived  '}) is True
        # Non-triggers: free-form, orthogonal, active, empty, missing, non-string.
        assert _is_deindexed({}) is False                       # no frontmatter → active
        assert _is_deindexed({'status': 'in-progress'}) is False
        assert _is_deindexed({'status': 'REMOVED — do not reintroduce'}) is False
        assert _is_deindexed({'status': 'active'}) is False
        assert _is_deindexed({'status': 'deprecated'}) is False  # orthogonal field
        assert _is_deindexed({'status': ''}) is False
        assert _is_deindexed({'status': None}) is False          # non-string guard

    def test_rescan_drops_only_superseded_and_archived(self, tmp_path):
        """A superseded/archived file leaves the index; in-progress + no-frontmatter stay."""
        v = _vault(tmp_path)
        _write(v, 'superseded.md', 'name: S\ndescription: old\nstatus: superseded', 'zookeeper notes')
        _write(v, 'archived.md', 'name: A\ndescription: hist\nstatus: archived', 'zookeeper notes')
        _write(v, 'status-caps.md', 'name: C\ndescription: cap\nStatus: Superseded', 'zookeeper notes')
        _write(v, 'in-progress.md', 'name: I\ndescription: live\nstatus: in-progress', 'zookeeper notes')
        _write(v, 'deprecated.md', 'name: D\ndescription: dep\nstatus: deprecated', 'zookeeper notes')

        idx = VaultIndex(v)
        assert idx.rescan() is True

        docs = idx.documents
        # De-indexed (superseded/archived, incl. capitalised key) → NOT indexed.
        assert 'superseded.md' not in docs
        assert 'archived.md' not in docs
        assert 'status-caps.md' not in docs
        # Free-form + orthogonal status stay active.
        assert 'in-progress.md' in docs
        assert 'deprecated.md' in docs

        # Recoverability: nothing deleted from disk — only the index dropped them.
        assert (v / 'superseded.md').is_file()
        assert (v / 'archived.md').is_file()

    def test_no_frontmatter_is_indexed_active(self, tmp_path):
        """The ~402 no-frontmatter lessons must stay indexable (active by default)."""
        v = _vault(tmp_path)
        p = v / 'plain.md'
        p.write_text('Just a body with zookeeper notes and no frontmatter at all.\n',
                     encoding='utf-8')
        idx = VaultIndex(v)
        assert idx.rescan() is True
        assert 'plain.md' in idx.documents

    def test_deindexed_files_never_match(self, tmp_path):
        """End-to-end at the matcher: only the active lesson is ever a candidate."""
        v = _vault(tmp_path)
        _write(v, 'old.md', 'name: Old\ndescription: zookeeper thing\nstatus: superseded', 'zookeeper notes')
        _write(v, 'live.md', 'name: Live\ndescription: zookeeper thing\nstatus: in-progress', 'zookeeper notes')
        idx = VaultIndex(v)
        idx.rescan()
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        paths = {p for p, _s in m.match('zookeeper')}
        assert paths == {'live.md'}  # the superseded one was never indexed → never hinted


# ── Feature C: scope filter (ships dark) ────────────────────────────────────

class TestScopeFilter:
    @pytest.fixture
    def scoped_docs(self):
        """Docs built directly as 4-tuples, all sharing the query token 'zookeeper'."""
        return {
            'ninfer.md': ('Ninfer', 'zookeeper engine notes', frozenset({'ninfer'}), None),
            'global.md': ('Global', 'zookeeper engine notes', frozenset({'global'}), None),
            'cascade.md': ('Cascade', 'zookeeper engine notes', frozenset({'agentcascade'}), None),
        }

    def test_scope_absent_defaults_to_global(self):
        """normalize_scope: missing/empty/non-string → {'global'}; lists parse."""
        assert normalize_scope({}) == frozenset({'global'})
        assert normalize_scope({'scope': ''}) == frozenset({'global'})
        assert normalize_scope({'scope': None}) == frozenset({'global'})
        assert normalize_scope({'scope': 'ninfer'}) == frozenset({'ninfer'})
        # parse_frontmatter strips [ ], leaving "global, ninfer".
        assert normalize_scope({'scope': 'global, ninfer'}) == frozenset({'global', 'ninfer'})
        # Case is normalized so filter comparisons are symmetric.
        assert normalize_scope({'scope': 'AgentCascade'}) == frozenset({'agentcascade'})

    def test_ninfer_doc_excluded_from_agentcascade_query(self, scoped_docs):
        m = MemoryMatcher()
        m.set_documents(scoped_docs)
        paths = {p for p, _s in m.match('zookeeper', allowed_scope={'global', 'agentcascade'})}
        assert 'ninfer.md' not in paths
        assert paths == {'global.md', 'cascade.md'}

    def test_global_doc_always_included(self, scoped_docs):
        m = MemoryMatcher()
        m.set_documents(scoped_docs)
        # A scope that matches nothing but 'global' still returns the global doc.
        paths = {p for p, _s in m.match('zookeeper', allowed_scope={'strata'})}
        assert paths == {'global.md'}

    def test_none_scope_doc_treated_as_global(self, scoped_docs):
        """A doc with no scope (legacy 2-tuple) is global → always eligible."""
        docs = dict(scoped_docs)
        docs['legacy.md'] = ('Legacy', 'zookeeper engine notes')  # 2-tuple, no scope
        m = MemoryMatcher()
        m.set_documents(docs)
        # Excludes everything but global: legacy.md is global-by-default → kept.
        paths = {p for p, _s in m.match('zookeeper', allowed_scope={'strata'})}
        assert paths == {'global.md', 'legacy.md'}

    def test_allowed_scope_none_is_identical_to_today(self, scoped_docs):
        """allowed_scope=None is a byte-for-byte no-op vs the old signature behavior."""
        m = MemoryMatcher()
        m.set_documents(scoped_docs)
        none_result = m.match('zookeeper', allowed_scope=None)
        bare_result = m.match('zookeeper')  # default arg
        assert none_result == bare_result
        # And a scoped result is a strict subset of the unscoped one.
        scoped_result = m.match('zookeeper', allowed_scope={'global', 'agentcascade'})
        assert {p for p, _s in scoped_result} < {p for p, _s in bare_result}

    def test_set_documents_tolerant_of_2_3_4_tuples(self):
        """A mixed tree (legacy 2-tuple + 3-tuple + 4-tuple) must not crash."""
        m = MemoryMatcher()
        docs = {
            'a.md': ('Alpha', 'zookeeper body'),                 # 2-tuple (legacy)
            'b.md': ('Beta', 'zookeeper body', frozenset({'ninfer'})),  # 3-tuple
            'c.md': ('Gamma', 'zookeeper body', frozenset({'global'}), 'media-gen'),  # 4-tuple
        }
        m.set_documents(docs)  # must not raise
        # Unscoped → all three are candidates.
        assert {p for p, _s in m.match('zookeeper')} == {'a.md', 'b.md', 'c.md'}
        # 2-tuple defaults to global → survives an agentcascade-only scope; 3-tuple ninfer dropped.
        assert {p for p, _s in m.match('zookeeper', allowed_scope={'agentcascade'})} == {'a.md', 'c.md'}


# ── Feature B: hub breadcrumb ───────────────────────────────────────────────

class TestHubBreadcrumb:
    def test_hub_slug_parsing(self):
        assert _hub_slug({'hub': 'media-gen'}) == 'media-gen'
        assert _hub_slug({'hub': '  media-gen  '}) == 'media-gen'
        assert _hub_slug({'hub': ''}) is None
        assert _hub_slug({}) is None
        assert _hub_slug({'hub': None}) is None

    def test_rescan_populates_hubs_map(self, tmp_path):
        """rescan_vaults captures hub: slugs (only for leaves that carry them)."""
        v = _vault(tmp_path)
        _write(v, 'media-leaf.md', 'name: L\ndescription: media thing\nhub: media-gen', 'zookeeper notes')
        _write(v, 'plain-leaf.md', 'name: P\ndescription: plain thing', 'zookeeper notes')
        pool = SimpleNamespace(
            operation_manager=SimpleNamespace(
                base_dir=str(tmp_path / 'proj'),
                extra_work_folders_ro=[],
                extra_work_folders_rw=[],
            ),
            llm_cfg={},
        )
        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()
        assert mgr._hubs.get('media-leaf.md') == 'media-gen'
        assert 'plain-leaf.md' not in mgr._hubs

    def test_build_hint_appends_breadcrumb(self):
        mgr = MemoryHintManager(MagicMock())
        text = mgr._build_hint(['/vault/media-leaf.md'], ['media-gen'])
        assert '  - /vault/media-leaf.md (hub: #media-gen)' in text

    def test_build_hint_without_hub_is_unchanged(self):
        mgr = MemoryHintManager(MagicMock())
        # No hubs arg at all → no breadcrumb, byte-identical to legacy render.
        assert '(hub:' not in mgr._build_hint(['/vault/leaf.md'])
        # An all-None hubs list is byte-identical to omitting the argument.
        assert mgr._build_hint(['/a.md', '/b.md']) == mgr._build_hint(['/a.md', '/b.md'], [None, None])

    def test_build_hint_mixes_hubbed_and_plain(self):
        mgr = MemoryHintManager(MagicMock())
        text = mgr._build_hint(['/a.md', '/b.md'], [None, 'foo-cluster'])
        lines = text.splitlines()
        assert '  - /a.md' in lines[1] and '(hub:' not in lines[1]
        assert lines[2] == '  - /b.md (hub: #foo-cluster)'

    def test_rescan_then_render_end_to_end(self, tmp_path):
        """The full B path: frontmatter hub → _hubs → rendered breadcrumb."""
        v = _vault(tmp_path)
        _write(v, 'media-leaf.md', 'name: L\ndescription: media thing\nhub: media-gen', 'zookeeper notes')
        pool = SimpleNamespace(
            operation_manager=SimpleNamespace(
                base_dir=str(tmp_path / 'proj'),
                extra_work_folders_ro=[],
                extra_work_folders_rw=[],
            ),
            llm_cfg={},
        )
        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()
        hub = mgr._hubs['media-leaf.md']
        display = mgr._display_path('media-leaf.md')
        combined = mgr._build_combined([display], [], None, None, [hub])
        assert '(hub: #media-gen)' in combined
