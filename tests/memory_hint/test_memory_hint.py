"""Unit tests for the memory-hint feature (plan §8).

Covers: matcher TF-IDF/cosine behavior, vault discovery + frontmatter parsing,
stats sidecar atomicity, read-tracking + dedup/cooldown logic in the manager,
the instance reset helper, and the engine query-extraction helper. All tests are
self-contained (tmp_path vaults, MagicMock pools/instances) and run fast with no
network or real LLM.
"""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cascade.memory_hint import (
    EWMA_ALPHA,
    FLOOR_MIN,
    FLOOR_SEED,
    GAP,
    HINT_COOLDOWN_SECONDS,
    JOB_TTL_SECONDS,
    MAX_HINTS_PER_TURN,
    MemoryHintManager,
    MemoryMatcher,
)
from agent_cascade.memory_hint.stats import bump_read_count, load_stats
from agent_cascade.memory_hint.vault import (
    VaultIndex,
    discover_vaults,
    identity_text,
    is_under_vault,
    parse_frontmatter,
)


# ── Vault fixtures / helpers ────────────────────────────────────────────────

def _make_om(base_dir, ro=None, rw=None):
    """Minimal OperationManager stand-in for discover_vaults()."""
    return SimpleNamespace(
        base_dir=str(base_dir),
        extra_work_folders_ro=list(ro or []),
        extra_work_folders_rw=list(rw or []),
    )


def _write_lesson(vault: Path, rel_path: str, name: str, description: str, body: str) -> Path:
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntags: [test]\n---\n{body}\n",
        encoding='utf-8',
    )
    return p


@pytest.fixture
def vault(tmp_path):
    """A single .agent_lessons vault with two distinct lessons."""
    v = tmp_path / 'proj' / '.agent_lessons'
    v.mkdir(parents=True)
    _write_lesson(v, 'compression-debug.md', 'Compression Debug',
                  'How to debug compression hangs in the engine loop',
                  'When compression hangs check the daemon thread and lock ordering.')
    _write_lesson(v, 'skill-creator-notes.md', 'Skill Creator Notes',
                  'Notes on writing reusable skills with frontmatter',
                  'Skills need a name description tags and a clear body section.')
    return v


# ── 1. Matcher: basic TF-IDF / cosine behavior ──────────────────────────────

class TestMatcher:
    def test_match_ranks_relevant_lesson_first(self, vault):
        """A query about compression should rank the compression lesson above skill notes."""
        idx = VaultIndex(vault)
        assert idx.rescan() is True
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        results = m.match('debugging a compression hang in the engine loop')
        assert results, 'expected at least one match'
        # Top result must be the compression lesson (identity + body both hit).
        assert results[0][0] == 'compression-debug.md'
        assert results[0][1] > 0

    def test_match_scores_in_range(self, vault):
        """Cosine scores are in [0, 1]."""
        idx = VaultIndex(vault)
        idx.rescan()
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        for _path, score in m.match('compression engine loop hang'):
            assert 0.0 <= score <= 1.0 + 1e-9

    def test_match_empty_query_returns_empty(self, vault):
        idx = VaultIndex(vault)
        idx.rescan()
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        assert m.match('') == []
        assert m.match('   ') == []

    def test_match_no_docs_returns_empty(self):
        m = MemoryMatcher()
        m.set_documents({})
        assert m.match('anything at all') == []

    def test_match_frontmatter_only_doc_matches_nothing(self, vault):
        """A degenerate lesson with identity text but an EMPTY body must not raise.

        Guards the matcher's empty-body path (_chunk on '' and match() against a doc
        whose vector comes only from the identity field). The doc is still indexable
        via its identity (by design, W_ID), so the assertion is: no exception, scores
        in range, and an unrelated query matches nothing.
        """
        (vault / 'frontmatter-only.md').write_text(
            '---\nname: Frontmatter Only\ndescription: A lesson with no body text at all\n'
            'tags: [test]\n---\n',
            encoding='utf-8',
        )
        idx = VaultIndex(vault)
        idx.rescan()
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        # Empty-body doc is indexed (identity-only vector) — no exception on rescan/index.
        assert 'frontmatter-only.md' in idx.documents
        # Query overlapping the identity text: no exception, scores stay in range.
        for _path, score in m.match('frontmatter only lesson with no body text'):
            assert 0.0 <= score <= 1.0 + 1e-9
        # An unrelated query must not match the degenerate doc at all.
        assert all(p != 'frontmatter-only.md' for p, _ in m.match('compression hang daemon'))

    def test_identity_weighting_boosts_name_match(self, vault):
        """A query matching ONLY a lesson's name/tags (not body) still scores it.

        Identity is weighted 3× so an identity-only hit is non-zero even when the
        body has no overlap — this is what makes 're-read' hints work from short queries.
        """
        idx = VaultIndex(vault)
        idx.rescan()
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        # 'Skill Creator Notes' identity only; no body tokens shared with the query.
        results = dict(m.match('skill creator notes frontmatter'))
        assert 'skill-creator-notes.md' in results
        assert results['skill-creator-notes.md'] > 0

    def test_unrelated_query_scores_below_relevant(self, vault):
        """A strongly relevant lesson must outscore an unrelated one for the same query."""
        idx = VaultIndex(vault)
        idx.rescan()
        m = MemoryMatcher()
        m.set_documents(idx.documents)
        res = dict(m.match('compression hang daemon thread lock ordering'))
        comp = res.get('compression-debug.md', 0.0)
        skill = res.get('skill-creator-notes.md', 0.0)
        assert comp > skill


# ── 2. Vault discovery + frontmatter ────────────────────────────────────────

class TestVault:
    def test_discover_finds_vault_under_base(self, vault):
        om = _make_om(vault.parent)
        roots = discover_vaults(om)
        assert Path(str(vault)) in [Path(r) for r in roots]

    def test_discover_includes_extra_ro_rw(self, tmp_path):
        base = tmp_path / 'base'
        ro_dir = tmp_path / 'ro'
        rw_dir = tmp_path / 'rw'
        (base / '.agent_lessons').mkdir(parents=True)
        (ro_dir / '.agent_lessons').mkdir(parents=True)
        (rw_dir / '.agent_lessons').mkdir(parents=True)
        om = _make_om(base, ro=[str(ro_dir)], rw=[str(rw_dir)])
        roots = {str(Path(r)) for r in discover_vaults(om)}
        assert str(base / '.agent_lessons') in roots
        assert str(ro_dir / '.agent_lessons') in roots
        assert str(rw_dir / '.agent_lessons') in roots

    def test_discover_none_om_returns_empty(self):
        assert discover_vaults(None) == []

    def test_parse_frontmatter_extracts_fields_and_body(self, vault):
        meta, body = parse_frontmatter(vault / 'compression-debug.md')
        assert meta.get('name') == 'Compression Debug'
        assert meta.get('description').startswith('How to debug compression')
        # Body must NOT include the frontmatter block.
        assert '---' not in body.splitlines()[0]
        assert 'daemon thread' in body

    def test_parse_frontmatter_no_meta_returns_body(self, tmp_path):
        p = tmp_path / 'plain.md'
        p.write_text('just a body line\nsecond line\n', encoding='utf-8')
        meta, body = parse_frontmatter(p)
        assert meta == {}
        assert body.strip() == 'just a body line\nsecond line'

    def test_identity_text_joins_fields(self):
        fm = {'name': 'My Mem', 'description': 'A desc', 'tags': 'a b', 'aliases': 'x'}
        it = identity_text(fm)
        assert 'My Mem' in it and 'A desc' in it

    def test_is_under_vault_hit_and_miss(self, vault):
        roots = [vault]
        hit = is_under_vault(vault / 'compression-debug.md', roots)
        assert hit is not None
        root, rel = hit
        assert Path(root) == Path(str(vault))
        assert rel == 'compression-debug.md'
        # A path outside the vault returns None.
        assert is_under_vault(Path('C:/elsewhere/file.md'), roots) is None

    def test_index_rescan_drops_deleted(self, vault):
        idx = VaultIndex(vault)
        assert idx.rescan() is True
        n_before = len(idx.documents)
        (vault / 'skill-creator-notes.md').unlink()
        assert idx.rescan() is True  # changed: a doc removed
        assert len(idx.documents) == n_before - 1

    def test_index_rescan_unchanged_returns_false(self, vault):
        idx = VaultIndex(vault)
        idx.rescan()
        # Second rescan with no file changes → False.
        assert idx.rescan() is False


# ── 3. Stats sidecar ────────────────────────────────────────────────────────

class TestStats:
    def test_bump_and_load(self, vault):
        assert load_stats(vault) == {}
        bump_read_count(vault, 'compression-debug.md')
        bump_read_count(vault, 'compression-debug.md')
        stats = load_stats(vault)
        assert stats['compression-debug.md'] == 2

    def test_bump_multiple_paths(self, vault):
        bump_read_count(vault, 'a.md')
        bump_read_count(vault, 'b.md')
        bump_read_count(vault, 'a.md')
        stats = load_stats(vault)
        assert stats['a.md'] == 2
        assert stats['b.md'] == 1

    def test_load_corrupt_returns_empty(self, vault):
        (vault / 'memory_stats.json').write_text('not json {', encoding='utf-8')
        assert load_stats(vault) == {}

    def test_bump_writes_atomic_no_tmp_left(self, vault):
        bump_read_count(vault, 'x.md')
        assert not (vault / 'memory_stats.json.tmp').exists()
        # File is valid JSON.
        json.loads((vault / 'memory_stats.json').read_text(encoding='utf-8'))


# ── 4. Manager: read-tracking + dedup/cooldown ──────────────────────────────

class _FakeInst:
    """Minimal instance exposing the memory-hint state fields + lock."""

    def __init__(self, name='w'):
        self.instance_name = name
        self.agent_class = 'test_agent'
        self.state = 'RUNNING'  # not SLEEPING
        self._compression_lock = threading.RLock()
        self._tool_warnings = []
        self._memories_read = set()
        self._recently_hinted = {}
        self._last_memory_hint_turn = -1


class TestManagerDedup:
    def _manager_with_lesson(self, tmp_path, enabled=True):
        v = tmp_path / 'proj' / '.agent_lessons'
        v.mkdir(parents=True)
        # Two lessons with DISJOINT topic words: the compression query must make
        # compression-debug.md a CLEAR winner (top1 − top2 ≥ GAP) under the
        # self-calibrating gate, not a near-tie.
        _write_lesson(v, 'compression-debug.md', 'Compression Debug',
                      'How to debug compression hangs in the engine loop',
                      'When compression hangs check the daemon thread and lock ordering.')
        _write_lesson(v, 'skill-creator-notes.md', 'Skill Creator Notes',
                      'Notes on writing reusable skills with frontmatter',
                      'Skills need a name description tags and a clear body section.')
        inst = _FakeInst()
        pool = MagicMock()
        # threshold 0/absent = pure adaptive gate (the primary gate is now the
        # EWMA floor + specificity gap, not this fixed value).
        pool.llm_cfg = {'memory_hint_enabled': enabled,
                        'memory_hint_max_entries': 3,
                        'memory_hint_cooldown_seconds': 600,
                        'memory_hint_query_chars': 1000}
        pool.operation_manager = _make_om(v.parent)
        pool.get_instance.return_value = inst
        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()
        return mgr, inst

    def test_process_job_delivers_via_tool_warning(self, tmp_path):
        """A clear-winner match not yet read is delivered onto _tool_warnings (NOT a user msg)."""
        mgr, inst = self._manager_with_lesson(tmp_path)
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert len(inst._tool_warnings) == 1
        hint = inst._tool_warnings[0]
        assert '[MEMORY HINT]' in hint
        assert 'compression-debug.md' in hint
        # Cooldown recorded.
        assert 'compression-debug.md' in inst._recently_hinted

    def test_display_path_is_absolute_and_points_to_existing_vault(self, tmp_path):
        """Two vaults each holding a SAME-named lesson → the hinted path is absolute and
        points into exactly ONE existing vault dir (os.path.isfile true).

        This is the real value-add of showing absolute paths: with two same-named
        lessons, the bare rel_path alone can't tell the agent which file to read. The
        merged index keys on the bare rel_path ("first vault wins"), so exactly one
        lesson is matched/hinted; _display_path must resolve it to a real absolute path.
        """
        import os

        v1 = tmp_path / 'projA' / '.agent_lessons'
        v2 = tmp_path / 'projB' / '.agent_lessons'
        for v in (v1, v2):
            v.mkdir(parents=True)
            _write_lesson(v, 'shared-lesson.md', 'Shared Lesson',
                          'How to debug compression hangs in the engine loop',
                          'When compression hangs check the daemon thread and lock ordering.')

        inst = _FakeInst()
        pool = MagicMock()
        pool.llm_cfg = {'memory_hint_enabled': True,
                        'memory_hint_max_entries': 3,
                        'memory_hint_cooldown_seconds': 600,
                        'memory_hint_query_chars': 1000}
        # Both vault roots are discoverable (base_dir + one extra RW folder).
        pool.operation_manager = _make_om(v1.parent, rw=[v2.parent])
        pool.get_instance.return_value = inst
        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()

        # Sanity: both vaults are indexed and the bare rel_path is a single merged key.
        assert len(mgr._vault_indexes) == 2, f'expected 2 vault indexes, got {len(mgr._vault_indexes)}'
        with mgr._index_lock:
            matches = mgr._matcher.match('debugging a compression hang in the engine loop')
        matched_rels = [p for p, _s in matches]
        assert 'shared-lesson.md' in matched_rels, f'shared lesson not matched: {matches}'

        # The displayed path resolves to an ABSOLUTE file that actually exists.
        disp = mgr._display_path('shared-lesson.md')
        assert os.path.isabs(disp), f'display path must be absolute, got {disp!r}'
        assert os.path.isfile(disp), f'display path must point to a real file: {disp!r}'

        # It points into exactly ONE of the two vault dirs (first-hit-wins), not both.
        in_v1 = str(v1) in disp
        in_v2 = str(v2) in disp
        assert in_v1 != in_v2, \
            f'display path must point to exactly one vault dir: {disp!r} (v1={in_v1}, v2={in_v2})'

        # End-to-end: the delivered hint carries that absolute path.
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert len(inst._tool_warnings) == 1
        hint = inst._tool_warnings[0]
        assert disp in hint, f'absolute display path missing from hint: {hint}'

    def test_process_job_dedup_already_read(self, tmp_path):
        """A lesson already in _memories_read is never re-hinted."""
        mgr, inst = self._manager_with_lesson(tmp_path)
        with inst._compression_lock:
            inst._memories_read.add('compression-debug.md')
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_process_job_cooldown_blocks_repeat(self, tmp_path):
        """A recently-hinted lesson is not hinted again within the cooldown window."""
        mgr, inst = self._manager_with_lesson(tmp_path)
        q = 'debugging a compression hang in the engine loop'
        job = {'instance_name': 'w', 'query': q, 'agent_class': 'test_agent',
               'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert len(inst._tool_warnings) == 1
        # Immediately re-process: still within cooldown → no second hint.
        job2 = {'instance_name': 'w', 'query': q, 'agent_class': 'test_agent',
                'submitted_at': time.monotonic(), 'turn': 4}
        mgr._process_job(job2)
        assert len(inst._tool_warnings) == 1

    def test_process_job_prunes_expired_recently_hinted(self, tmp_path):
        """Entries in _recently_hinted older than the cooldown are pruned during dedup.

        Bounds the dict: expired entries can no longer affect the gate.
        """
        mgr, inst = self._manager_with_lesson(tmp_path)
        now = time.monotonic()
        with inst._compression_lock:
            # One long-expired (prunable) and one fresh (must survive) entry.
            inst._recently_hinted['stale-lesson.md'] = now - 601.0  # > 600s cooldown
            inst._recently_hinted['fresh-lesson.md'] = now - 1.0
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert 'stale-lesson.md' not in inst._recently_hinted
        assert 'fresh-lesson.md' in inst._recently_hinted

    def test_process_job_disabled_no_hint(self, tmp_path):
        """memory_hint_enabled=False → no hint even on a strong match."""
        mgr, inst = self._manager_with_lesson(tmp_path, enabled=False)
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_process_job_ttl_drop(self, tmp_path):
        """A job older than JOB_TTL_SECONDS is dropped without hinting."""
        mgr, inst = self._manager_with_lesson(tmp_path)
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent',
               'submitted_at': time.monotonic() - (JOB_TTL_SECONDS + 5), 'turn': 3}
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_process_job_sleeping_instance_skipped(self, tmp_path):
        """A SLEEPING instance is skipped at delivery time."""
        from agent_cascade.agent_instance import AgentState
        mgr, inst = self._manager_with_lesson(tmp_path)
        inst.state = AgentState.SLEEPING
        job = {'instance_name': 'w', 'query': 'debugging a compression hang in the engine loop',
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_on_memory_read_records_and_bumps_stats(self, tmp_path):
        """on_memory_read adds to the read-set and bumps the stats sidecar."""
        v = tmp_path / 'proj' / '.agent_lessons'
        v.mkdir(parents=True)
        _write_lesson(v, 'compression-debug.md', 'Compression Debug', 'desc', 'body text here')
        inst = _FakeInst()
        pool = MagicMock()
        pool.get_instance.return_value = inst
        mgr = MemoryHintManager(pool)
        mgr.on_memory_read('w', v, 'compression-debug.md')
        assert 'compression-debug.md' in inst._memories_read
        assert load_stats(v).get('compression-debug.md') == 1

    def test_submit_most_recent_wins_replaces_pending(self, tmp_path):
        """A second submit REPLACES the still-pending job (most-recent-wins = new wins)."""
        mgr, _inst = self._manager_with_lesson(tmp_path)
        mgr.submit('w', 'query one', 'test_agent', turn=1)
        assert len(mgr._pending) == 1
        # Second submit while first still pending → replaces it (no double-queue in dict).
        mgr.submit('w', 'query two', 'test_agent', turn=2)
        assert len(mgr._pending) == 1
        # The retained job is the SECOND one submitted (most-recent-wins = new wins).
        assert mgr._pending['w']['query'] == 'query two'
        assert mgr._pending['w']['turn'] == 2
        # Generation bumped so the worker can tell the stale first job apart.
        assert mgr._generation['w'] == 2

    def test_submit_replacement_processes_new_drops_stale(self, tmp_path):
        """End-to-end most-recent-wins: submit A then B (A still pending) → B is processed,
        A is dropped as stale by the worker's generation filter. NOT the reverse."""
        mgr, inst = self._manager_with_lesson(tmp_path)

        # Two lessons that match DISTINCT queries, so we can tell which job actually ran.
        v = tmp_path / 'proj' / '.agent_lessons'
        _write_lesson(v, 'alpha.md', 'Alpha', 'alpha topic words unique-alpha',
                      'alpha topic words unique-alpha body')
        mgr.rescan_vaults()  # rebuild index to include the new lesson

        # Submit A (matches alpha) then B (matches compression-debug) back-to-back so A is
        # still pending when B lands. Both are enqueued; only B's generation is current.
        mgr.submit('w', 'alpha topic words unique-alpha', 'test_agent', turn=1)  # A
        mgr.submit('w', 'debugging a compression hang in the engine loop',
                   'test_agent', turn=2)                                          # B

        # Drain the transport queue through the real worker path (no daemon needed):
        # pop each queued job and run it exactly as _run() would, applying the stale filter.
        import queue as _queue
        processed = []
        while True:
            try:
                job = mgr._job_queue.get_nowait()
            except _queue.Empty:
                break
            name = job.get('instance_name')
            with mgr._pending_lock:
                if name is not None and job.get('_gen') != mgr._generation.get(name):
                    continue  # stale duplicate — dropped, never processed
            processed.append(job['query'])
            mgr._process_job(job)
            with mgr._pending_lock:
                if name is not None and job.get('_gen') == mgr._generation.get(name):
                    mgr._pending.pop(name, None)
                    mgr._generation.pop(name, None)

        # Only the most recent (B) was processed; A was dropped as stale.
        assert processed == ['debugging a compression hang in the engine loop']
        # B's hint landed on _tool_warnings; A's (alpha.md) did NOT.
        assert len(inst._tool_warnings) == 1
        assert 'compression-debug.md' in inst._tool_warnings[0]
        assert 'alpha.md' not in inst._tool_warnings[0]

    def test_submit_empty_query_ignored(self, tmp_path):
        mgr, _inst = self._manager_with_lesson(tmp_path)
        mgr.submit('w', '   ', 'test_agent')
        mgr.submit('', 'real query', 'test_agent')
        assert mgr._pending == {}

    def test_build_hint_truncates_long_entries(self, tmp_path):
        """Each entry's path is clipped to HINT_ENTRY_MAX_CHARS for readability."""
        from agent_cascade.memory_hint import HINT_ENTRY_MAX_CHARS
        mgr, _inst = self._manager_with_lesson(tmp_path)
        long_path = 'x' * (HINT_ENTRY_MAX_CHARS + 50)
        text = mgr._build_hint([long_path])
        # The single entry line must not exceed the per-entry cap.
        for line in text.splitlines():
            assert len(line) <= HINT_ENTRY_MAX_CHARS + 4  # "  - " prefix (4 chars)

    def test_build_hint_lists_all_paths_in_order(self, tmp_path):
        """_build_hint lists every path it is given, in order (no internal cap)."""
        mgr, _inst = self._manager_with_lesson(tmp_path)
        paths = [f'memory-{i}.md' for i in range(5)]
        text = mgr._build_hint(paths)
        assert '(5)' in text  # header count
        for p in paths:
            assert p in text

    def test_max_entries_caps_to_top3_of_4(self, tmp_path):
        """With 4 matches and max_entries=3, the hint lists exactly top-3 by score.

        The 4th (lowest-scoring) match is excluded even though it passes the noise gate
        (<= MAX_HINTS_PER_TURN). This proves the cap applies to the score-ordered list.

        Under the self-calibrating gate the fixture must make doc-one.md a CLEAR
        winner (top1 − top2 ≥ GAP, above the adaptive floor) — so the docs share only
        weak filler words while doc-one carries the query's distinctive tokens.
        """
        v = tmp_path / 'proj' / '.agent_lessons'
        v.mkdir(parents=True)
        # All four docs share the same topic words ("pipeline worker retry …"), so all
        # clear the adaptive floor (≥ FLOOR_SEED); doc-one adds the query's distinctive
        # "backoff tuning" words → clear top-1 with gap ≥ GAP. The other three are
        # exact ties, so their relative order is the matcher's path-name tiebreak —
        # assertions below use the ACTUAL ranking, not a guessed one. (Scores verified:
        # 0.78 / 0.26 / 0.26 / 0.26.)
        _write_lesson(v, 'doc-one.md', 'Doc One',
                      'pipeline worker retry backoff tuning notes about engine work',
                      'pipeline worker retry backoff tuning notes about engine work')
        _write_lesson(v, 'doc-two.md', 'Doc Two',
                      'pipeline worker retry scheduling notes about engine work',
                      'pipeline worker retry scheduling notes about engine work')
        _write_lesson(v, 'doc-three.md', 'Doc Three',
                      'pipeline worker retry batching notes about engine work',
                      'pipeline worker retry batching notes about engine work')
        _write_lesson(v, 'doc-four.md', 'Doc Four',
                      'pipeline worker retry queueing notes about engine work',
                      'pipeline worker retry queueing notes about engine work')

        inst = _FakeInst()
        pool = MagicMock()
        # threshold 0/absent = pure adaptive gate; max_entries=3 caps to top-3.
        pool.llm_cfg = {'memory_hint_enabled': True,
                        'memory_hint_max_entries': 3,    # cap to top-3
                        'memory_hint_cooldown_seconds': 600,
                        'memory_hint_query_chars': 1000}
        pool.operation_manager = _make_om(v.parent)
        pool.get_instance.return_value = inst
        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()

        query = 'pipeline worker retry backoff tuning'
        # Sanity: the matcher ranks all 4 above the floor, and doc-one is a clear winner.
        with mgr._index_lock:
            matches = mgr._matcher.match(query)
        assert len(matches) == 4, f"expected 4 matches, got {len(matches)}: {matches}"
        top1 = matches[0]
        top2 = matches[1]
        assert top1[0] == 'doc-one.md', f'expected doc-one.md to rank first, got {top1}'
        assert top1[1] - top2[1] >= GAP, \
            f"fixture must be a clear winner: gap={top1[1] - top2[1]:.4f} < GAP={GAP}: {matches}"
        floor = mgr._current_floor(0.0)
        assert all(s >= floor for _p, s in matches), \
            f"fixture must have all 4 above the floor {floor:.3f}: {matches}"

        job = {'instance_name': 'w', 'query': query,
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        mgr._process_job(job)

        assert len(inst._tool_warnings) == 1
        hint = inst._tool_warnings[0]
        # Exactly top-3 by score are listed; the 4th (lowest, per the ACTUAL ranking)
        # is excluded. The cap applies to the score-ordered list regardless of tie order.
        ranked = [p for p, _s in matches]
        expected_in = ranked[:3]
        excluded = ranked[3]
        assert '(3)' in hint  # header count reflects the cap, not the 4 raw matches
        for p in expected_in:
            assert p in hint, f'{p} (top-3) missing from hint: {hint}'
        assert excluded not in hint, f'{excluded} (4th) must be capped out of hint'


# ── 4b. Manager: self-calibrating gate (floor / gap / noise) ───────────────

class TestManagerGate:
    """The adaptive specificity gate in _process_job (replaces the fixed threshold)."""

    def _make_mgr(self, tmp_path, lessons, query, **cfg_over):
        v = tmp_path / 'proj' / '.agent_lessons'
        v.mkdir(parents=True)
        for rel, name, desc, body in lessons:
            _write_lesson(v, rel, name, desc, body)
        inst = _FakeInst()
        pool = MagicMock()
        cfg = {'memory_hint_enabled': True,
               'memory_hint_max_entries': 3,
               'memory_hint_cooldown_seconds': 600,
               'memory_hint_query_chars': 1000}
        cfg.update(cfg_over)
        pool.llm_cfg = cfg
        pool.operation_manager = _make_om(v.parent)
        pool.get_instance.return_value = inst
        mgr = MemoryHintManager(pool)
        mgr.rescan_vaults()
        job = {'instance_name': 'w', 'query': query,
               'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 3}
        return mgr, inst, job

    def test_gate_clear_winner_fires(self, tmp_path):
        """(a) A single clear winner (top1 ≥ floor, gap ≥ GAP) delivers a hint."""
        lessons = [
            ('compression-debug.md', 'Compression Debug',
             'How to debug compression hangs in the engine loop',
             'When compression hangs check the daemon thread and lock ordering.'),
            ('skill-creator-notes.md', 'Skill Creator Notes',
             'Notes on writing reusable skills with frontmatter',
             'Skills need a name description tags and a clear body section.'),
        ]
        mgr, inst, job = self._make_mgr(tmp_path, lessons,
                                        'debugging a compression hang in the engine loop')
        with mgr._index_lock:
            matches = mgr._matcher.match(job['query'])
        assert len(matches) >= 1
        floor = mgr._current_floor(0.0)
        assert matches[0][1] >= floor, \
            f'top1 {matches[0][1]:.3f} below adaptive floor {floor:.3f} — bad fixture'
        top2 = matches[1][1] if len(matches) > 1 else 0.0
        assert matches[0][1] - top2 >= GAP, 'fixture must be a clear winner'
        mgr._process_job(job)
        assert len(inst._tool_warnings) == 1
        assert 'compression-debug.md' in inst._tool_warnings[0]

    def test_gate_single_match_fires(self, tmp_path):
        """(f) Exactly ONE lesson in the vault with top1 ≥ floor fires (lone-match path).

        With a single match there is no top-2: gap = top1 − 0.0 = top1 ≥ GAP always
        holds, so the lone match is delivered whenever it clears the adaptive floor.
        """
        lessons = [
            ('compression-debug.md', 'Compression Debug',
             'How to debug compression hangs in the engine loop',
             'When compression hangs check the daemon thread and lock ordering.'),
        ]
        mgr, inst, job = self._make_mgr(tmp_path, lessons,
                                        'debugging a compression hang in the engine loop')
        with mgr._index_lock:
            matches = mgr._matcher.match(job['query'])
        assert len(matches) == 1, f'expected exactly one match, got {matches}'
        floor = mgr._current_floor(0.0)
        top1 = matches[0][1]
        assert top1 >= floor, \
            f'top1 {top1:.3f} below adaptive floor {floor:.3f} — bad fixture'
        assert top1 - 0.0 >= GAP, 'lone-match gap (top1 − 0) must clear GAP'
        mgr._process_job(job)
        assert len(inst._tool_warnings) == 1
        assert 'compression-debug.md' in inst._tool_warnings[0]

    def test_gate_diffuse_tie_skips_with_reason_gap(self, tmp_path):
        """(b) A diffuse tie (top1 − top2 < GAP) skips even when top1 ≥ floor."""
        # Both lessons carry the same distinctive words → near-equal scores.
        lessons = [
            ('alpha.md', 'Alpha', 'quantum flux capacitor calibration procedure',
             'quantum flux capacitor calibration procedure steps'),
            ('beta.md', 'Beta', 'quantum flux capacitor calibration routine',
             'quantum flux capacitor calibration routine steps'),
        ]
        query = 'quantum flux capacitor calibration'
        mgr, inst, job = self._make_mgr(tmp_path, lessons, query)
        with mgr._index_lock:
            matches = mgr._matcher.match(query)
        assert len(matches) == 2, f'expected both docs to match, got {matches}'
        floor = mgr._current_floor(0.0)
        top1, top2 = matches[0][1], matches[1][1]
        # The fixture must be a genuine near-tie above the EFFECTIVE adaptive floor
        # (fresh manager → FLOOR_SEED, not just FLOOR_MIN — else the FLOOR gate would
        # skip first and the gap gate is never exercised).
        assert top1 >= floor, f'top1 {top1:.3f} below adaptive floor {floor:.3f} — bad fixture'
        assert top1 - top2 < GAP, f"fixture must be a tie: gap={top1 - top2:.4f} ≥ GAP={GAP}"
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_gate_no_signal_skips_with_reason_floor(self, tmp_path):
        """(c) No signal (top1 < floor) skips before the gap gate is even reached."""
        lessons = [
            ('compression-debug.md', 'Compression Debug',
             'How to debug compression hangs in the engine loop',
             'When compression hangs check the daemon thread and lock ordering.'),
        ]
        # A query that only weakly overlaps the lesson ("thread lock" → 0.124):
        # non-zero score, but below the adaptive floor (seed FLOOR_SEED).
        mgr, inst, job = self._make_mgr(tmp_path, lessons, 'thread lock')
        with mgr._index_lock:
            matches = mgr._matcher.match(job['query'])
        assert len(matches) == 1, f'expected exactly one weak match, got {matches}'
        top1 = matches[0][1]
        # Fresh manager → floor is the seed; the fixture must sit below it.
        assert FLOOR_MIN <= top1 < FLOOR_SEED, \
            f'top1 {top1:.3f} not in [{FLOOR_MIN}, {FLOOR_SEED}) — bad fixture'
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_gate_noise_skips_when_more_than_max_strong(self, tmp_path):
        """(d) More than MAX_HINTS_PER_TURN docs at/above the floor → noise skip.

        The fixture must pass the earlier gates (top1 ≥ floor AND gap ≥ GAP) so the
        NOISE gate is what actually skips — otherwise this test proves nothing about
        it. One doc carries distinctive words (clear winner, gap ≈ 0.53) while all
        five share the topic phrase (all ≥ floor).
        """
        lessons = [
            ('bulk-0.md', 'Bulk 0', 'shared bulk processing pipeline tokens zebra quokka falcon',
             'shared bulk processing pipeline tokens zebra quokka falcon'),
        ] + [
            (f'bulk-{i}.md', f'Bulk {i}', 'shared bulk processing pipeline tokens',
             'shared bulk processing pipeline tokens')
            for i in range(1, MAX_HINTS_PER_TURN + 1)
        ]
        query = 'shared bulk processing pipeline zebra quokka falcon'
        mgr, inst, job = self._make_mgr(tmp_path, lessons, query)
        with mgr._index_lock:
            matches = mgr._matcher.match(query)
        floor = mgr._current_floor(0.0)
        top1, top2 = matches[0][1], matches[1][1]
        n_strong = sum(1 for _p, s in matches if s >= floor)
        assert len(matches) == MAX_HINTS_PER_TURN + 1, f'expected 5 matches, got {matches}'
        # The earlier gates must NOT trip: this isolates the noise gate.
        assert top1 >= floor, f'top1 {top1:.3f} below floor {floor:.3f} — bad fixture'
        assert top1 - top2 >= GAP, \
            f"gap {top1 - top2:.4f} < GAP would skip before the noise gate: {matches}"
        assert n_strong > MAX_HINTS_PER_TURN, \
            f"fixture must trip the noise gate: only {n_strong}/{len(matches)} ≥ floor {floor:.3f}"
        mgr._process_job(job)
        assert inst._tool_warnings == []

    def test_ewma_floor_stays_above_min_after_low_stream(self, tmp_path):
        """(e) A stream of low top-1 scores can never push the EFFECTIVE floor below FLOOR_MIN.

        The bound is applied at read time (``_current_floor``); ``_update_floor``
        stores the unbounded EWMA value, so this test drives the full read path.
        """
        lessons = [
            ('compression-debug.md', 'Compression Debug',
             'How to debug compression hangs in the engine loop',
             'When compression hangs check the daemon thread and lock ordering.'),
        ]
        mgr, _inst, _job = self._make_mgr(tmp_path, lessons, 'thread lock')
        assert mgr._adaptive_floor == FLOOR_SEED
        # Feed a long stream of weak scores (each below FLOOR_MIN).
        for _ in range(200):
            mgr._update_floor(0.01)
        floor = mgr._current_floor(0.0)
        assert floor >= FLOOR_MIN - 1e-9, f'effective floor {floor:.6f} below FLOOR_MIN'
        assert abs(floor - FLOOR_MIN) < 1e-9, \
            f'effective floor should have settled at the bound: {floor}'
        # And a high score must be able to raise it again (EWMA is two-way): after
        # enough strong turns the STORED state climbs above FLOOR_MIN, and the
        # effective floor then tracks that stored value (read-time bound no longer
        # clamps it). With α=0.05, 60 updates of 0.5 drive the EWMA from its current
        # low value (~0.03 after the weak stream) to ~0.48 — well above FLOOR_MIN.
        for _ in range(60):
            mgr._update_floor(0.5)
        assert mgr._adaptive_floor > FLOOR_MIN, \
            f'high scores must lift stored EWMA: {mgr._adaptive_floor}'
        assert mgr._current_floor(0.0) == mgr._adaptive_floor, \
            'effective floor should equal the stored value once it is above FLOOR_MIN'

    def test_threshold_override_raises_floor(self, tmp_path):
        """memory_hint_threshold > 0 acts as a MINIMUM floor (can only make hints rarer)."""
        lessons = [
            ('compression-debug.md', 'Compression Debug',
             'How to debug compression hangs in the engine loop',
             'When compression hangs check the daemon thread and lock ordering.'),
            ('skill-creator-notes.md', 'Skill Creator Notes',
             'Notes on writing reusable skills with frontmatter',
             'Skills need a name description tags and a clear body section.'),
        ]
        mgr, inst, job = self._make_mgr(tmp_path, lessons,
                                        'debugging a compression hang in the engine loop')
        with mgr._index_lock:
            matches = mgr._matcher.match(job['query'])
        top1 = matches[0][1]
        # Without override the gate fires; with an override above top1 it must skip.
        mgr._process_job(job)
        assert len(inst._tool_warnings) == 1, 'baseline: clear winner must fire'

        inst2 = _FakeInst()
        mgr._pool.get_instance.return_value = inst2
        mgr._pool.llm_cfg['memory_hint_threshold'] = min(1.0, top1 + 0.1)
        job2 = {'instance_name': 'w', 'query': job['query'],
                'agent_class': 'test_agent', 'submitted_at': time.monotonic(), 'turn': 4}
        mgr._process_job(job2)
        assert inst2._tool_warnings == [], 'override above top1 must suppress the hint'


# ── 5. Instance reset helper + defaults ─────────────────────────────────────

class TestInstanceReset:
    def test_reset_clears_all_state(self):
        from agent_cascade.agent_instance import AgentInstance
        inst = AgentInstance.__new__(AgentInstance)
        inst._compression_lock = threading.RLock()
        inst._memories_read = {'a.md'}
        inst._recently_hinted = {'a.md': 123.0}
        inst._last_memory_hint_turn = 7
        inst._reset_memory_hint_state()
        assert inst._memories_read == set()
        assert inst._recently_hinted == {}
        assert inst._last_memory_hint_turn == -1

    def test_reset_never_raises_on_broken_lock(self):
        """Best-effort: a broken lock must not propagate (swallowed at debug)."""
        from agent_cascade.agent_instance import AgentInstance
        inst = AgentInstance.__new__(AgentInstance)
        inst._compression_lock = None  # deliberately broken
        inst._memories_read = {'a.md'}
        # Must not raise.
        inst._reset_memory_hint_state()

    def test_dataclass_defaults_present(self):
        """Fresh instances carry the memory-hint fields with safe defaults."""
        from agent_cascade.agent_instance import AgentInstance
        inst = AgentInstance.__new__(AgentInstance)
        # Simulate field() defaults without a full __init__ (heavy).
        assert hasattr(AgentInstance, '_memories_read')
        assert hasattr(AgentInstance, '_recently_hinted')
        assert hasattr(AgentInstance, '_last_memory_hint_turn')


# ── 6. Engine query-extraction helper ───────────────────────────────────────

class TestEngineQueryExtraction:
    def test_text_and_tool_turn_has_query(self):
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import ASSISTANT, Message
        turn = [Message(role=ASSISTANT, content='Let me check the compression logs')]
        q = ExecutionEngine._extract_memory_hint_query(turn, 1000)
        assert 'compression' in q

    def test_tool_call_only_turn_returns_empty(self):
        """A turn with only a tool call (no text/reasoning) must yield '' (excluded)."""
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import ASSISTANT, Message
        # content is empty; the tool call lives in function_call, not content.
        msg = Message(role=ASSISTANT, content='')
        turn = [msg]
        q = ExecutionEngine._extract_memory_hint_query(turn, 1000)
        assert q == ''

    def test_tool_call_only_turn_with_real_function_call_returns_empty(self):
        """Regression: a tool-call-only turn (empty content + non-empty function_call, no
        reasoning) must yield '' — the shared extract_text_from_message() tool-call fallback
        used to leak "[TOOL CALL: ...]" into the query and fire the hint on such turns.

        The message shape mirrors the REAL one built in llm/oai.py:716-719 (top-level
        ``function_call`` kwarg + ``function_id`` in ``extra``), not a synthetic dict.
        """
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import ASSISTANT, FunctionCall, Message
        # content is empty; a REAL function_call is present (the exact reported bug shape).
        msg = Message(role=ASSISTANT,
                      content='',
                      function_call=FunctionCall(name='read_file', arguments='{"path": "x"}'),
                      extra={'function_id': 'call_0'})
        turn = [msg]
        q = ExecutionEngine._extract_memory_hint_query(turn, 1000)
        assert q == '', f'tool-call-only turn must not trigger a hint, got query: {q!r}'

    def test_text_content_list_parts_joined(self):
        """List content: the ``text`` fields of the parts are joined into the query."""
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import ASSISTANT, ContentItem, Message
        msg = Message(role=ASSISTANT,
                      content=[ContentItem(text='compression'), ContentItem(text='hang debug')])
        q = ExecutionEngine._extract_memory_hint_query([msg], 1000)
        assert 'compression' in q and 'hang debug' in q

    def test_reasoning_only_turn_has_query(self):
        """A pure-thinking turn (reasoning_content, no text) still produces a query."""
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import ASSISTANT, Message
        msg = Message(role=ASSISTANT, content='')
        msg.reasoning_content = 'I should look at the compression daemon thread'
        turn = [msg]
        q = ExecutionEngine._extract_memory_hint_query(turn, 1000)
        assert 'compression' in q

    def test_query_capped_at_max_chars(self):
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import ASSISTANT, Message
        long_text = 'x' * 5000
        turn = [Message(role=ASSISTANT, content=long_text)]
        q = ExecutionEngine._extract_memory_hint_query(turn, 100)
        assert len(q) <= 100

    def test_non_assistant_messages_ignored(self):
        from agent_cascade.engine.core import ExecutionEngine
        from agent_cascade.llm.schema import USER, Message
        turn = [Message(role=USER, content='user text should be ignored')]
        q = ExecutionEngine._extract_memory_hint_query(turn, 1000)
        assert q == ''


# ── 7. Constants sanity (orchestrator hard constraints) ─────────────────────

class TestConstants:
    def test_cooldown_fallback_default_is_600(self):
        """HINT_COOLDOWN_SECONDS is the fallback default when the UI setting is absent."""
        assert HINT_COOLDOWN_SECONDS == 600.0

    def test_settings_reads_cooldown_and_max_entries_live(self, tmp_path):
        """_settings() reads cooldown + max_entries from llm_cfg with safe fallbacks."""
        pool = MagicMock()
        mgr = MemoryHintManager(pool)
        # Absent keys → defaults (600.0 / 3).
        pool.llm_cfg = {}
        s = mgr._settings()
        assert s['cooldown_seconds'] == 600.0
        assert s['max_entries'] == 3
        # Present keys → live values.
        pool.llm_cfg = {'memory_hint_cooldown_seconds': 120, 'memory_hint_max_entries': 4}
        s = mgr._settings()
        assert s['cooldown_seconds'] == 120.0
        assert s['max_entries'] == 4

    def test_max_hints_per_turn(self):
        assert MAX_HINTS_PER_TURN == 4

    def test_job_ttl_positive(self):
        assert JOB_TTL_SECONDS > 0

    def test_gate_constants_tuned_values(self):
        """The gate constants carry the TUNED values from the labeled replay."""
        assert GAP == 0.03
        assert FLOOR_SEED == 0.20
        assert FLOOR_MIN == 0.12
        assert EWMA_ALPHA == 0.05
        # Invariants the gate logic depends on:
        assert 0.0 < EWMA_ALPHA < 1.0
        assert FLOOR_MIN <= FLOOR_SEED

    def test_settings_threshold_defaults_to_adaptive(self, tmp_path):
        """Absent memory_hint_threshold → 0.0 (pure adaptive), not the retired 0.35."""
        pool = MagicMock()
        mgr = MemoryHintManager(pool)
        pool.llm_cfg = {}
        assert mgr._settings()['threshold'] == 0.0
        pool.llm_cfg = {'memory_hint_threshold': 0.25}
        assert mgr._settings()['threshold'] == 0.25
