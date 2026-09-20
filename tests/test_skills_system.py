"""Unit and integration tests for the Skills System Phase 1 MVP.

Covers parser, matcher, manager, and DNA/settings integration points.
Uses real SKILL.md files from agents/global/skills/ as test data where possible.
"""

import asyncio
import copy as _copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so imports resolve correctly
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_cascade.skills.manager import SkillManager
from agent_cascade.skills.matcher import SkillMatcher
from agent_cascade.skills.parser import parse_frontmatter, parse_skill_file

# ===========================================================================
# Fixtures — paths to real skill files in the repo
# ===========================================================================

_SKILLS_DIR = _PROJECT_ROOT / 'agents' / 'global' / 'skills'


def _skill_path(name: str) -> Path:
    """Return path to a SKILL.md inside agents/global/skills/<name>/"""
    return _SKILLS_DIR / name / 'SKILL.md'


# ===========================================================================
# Skill Invalidation (Phase 1) — helpers for hermetic skill trees
# ===========================================================================

def _write_skill_file(root: Path, name: str, version: str = '1.0.0', subdir: str = None) -> Path:
    """Write a minimal valid SKILL.md under ``root`` and return its path.

    Default location is the servable one-level layout ``<root>/<name>/SKILL.md``;
    pass ``subdir`` to place it deeper (e.g. ``'INACTIVE'`` → ``<root>/INACTIVE/<name>``).
    """
    skill_dir = root / subdir / name if subdir else root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / 'SKILL.md'
    path.write_text(
        f'---\nname: {name}\ndescription: fixture skill for invalidation tests\n'
        f'version: "{version}"\ntriggers:\n  - test\n---\n# Body\n',
        encoding='utf-8')
    return path


def _seed_v12_store(metrics_file: Path, entries: dict) -> None:
    """Write a legacy schema 1.2 metrics store (no status/last_used fields)."""
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    metrics_file.write_text(json.dumps({'schema_version': '1.2', 'skills': entries}, indent=2),
                            encoding='utf-8')


def _iso_utc(ts: float) -> str:
    """ISO-8601 UTC string — mirrors manager._iso_utc for seed comparisons."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _read_store(metrics_file: Path) -> dict:
    """Load the on-disk metrics JSON store."""
    return json.loads(metrics_file.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def version_control_skill_file():
    """Path to the version-control skill file."""
    p = _skill_path('version-control')
    assert p.exists(), f"version-control skill not found at {p}"
    return p


@pytest.fixture(scope='module')
def debugging_skill_file():
    """Path to the systematic-debugging skill file."""
    p = _skill_path('systematic-debugging')
    assert p.exists(), f"systematic-debugging skill not found at {p}"
    return p


# ===========================================================================
# 1. Parser Tests — agent_cascade.skills.parser
# ===========================================================================


class TestParseFrontmatter:
    """Test parse_frontmatter with valid, missing and malformed YAML."""

    def test_valid_frontmatter_returns_dict_and_body(self):
        content = ('---\n'
                   'name: my-skill\n'
                   'description: Does a thing\n'
                   'triggers:\n'
                   '  - trigger1\n'
                   '---\n'
                   '\n'
                   '# Instructions body\n')
        fm, body = parse_frontmatter(content)
        assert isinstance(fm, dict)
        assert fm['name'] == 'my-skill'
        assert fm['description'] == 'Does a thing'
        assert fm['triggers'] == ['trigger1']
        assert '# Instructions body' in body

    def test_missing_frontmatter_returns_empty_dict(self):
        content = 'Just plain markdown text\nwith no frontmatter at all.'
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body.strip() == content.strip()

    def test_malformed_yaml_returns_empty_dict(self):
        # Truly malformed YAML: wrong indentation in a list causes ScannerError
        content = '---\nname: my-skill\ntriggers:\n- trigger1\nsub\n---\nBody text'
        fm, body = parse_frontmatter(content)
        # Malformed YAML -> empty dict, full content as body
        assert fm == {}

    def test_empty_content(self):
        fm, body = parse_frontmatter('')
        assert fm == {}
        assert body == ''

    def test_only_delimiters_no_yaml(self):
        content = '---\n---\nsome body'
        fm, body = parse_frontmatter(content)
        assert isinstance(fm, dict)  # empty YAML parses to None → {}
        assert 'some body' in body

    def test_numeric_frontmatter_returns_empty_dict(self):
        """Frontmatter that is a plain number should be treated as non-dict."""
        content = '---\n42\n---\nbody'
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert 'body' in body

    def test_list_frontmatter_returns_empty_dict(self):
        """Frontmatter that is a plain list should be treated as non-dict."""
        content = '---\n- a\n- b\n---\nbody'
        fm, body = parse_frontmatter(content)
        assert fm == {}

    def test_name_and_description_extracted_correctly(self):
        content = ('---\n'
                   'name: httpx-pooling\n'
                   'description: Fix connection reuse issues\n'
                   'source: auto-skill\n'
                   '---\n'
                   '# Body here\n')
        fm, _ = parse_frontmatter(content)
        assert fm['name'] == 'httpx-pooling'
        assert fm['description'] == 'Fix connection reuse issues'
        assert fm['source'] == 'auto-skill'


class TestParseSkillFile:
    """Test parse_skill_file with real SKILL.md files."""

    def test_parse_real_version_control_skill(self, version_control_skill_file):
        result = parse_skill_file(version_control_skill_file)
        assert 'frontmatter' in result
        assert 'body' in result
        assert 'path' in result
        assert isinstance(result['frontmatter'], dict)
        assert result['frontmatter']['name'] == 'version-control'
        assert len(result['body']) > 100

    def test_parse_real_debugging_skill(self, debugging_skill_file):
        result = parse_skill_file(debugging_skill_file)
        assert result['frontmatter']['name'] == 'systematic-debugging'
        assert len(result['body']) > 50

    def test_missing_file_raises(self):
        p = Path('/tmp/nonexistent_skill_SKILL.md')
        with pytest.raises(FileNotFoundError, match='Skill file not found'):
            parse_skill_file(p)

    def test_path_in_result(self, version_control_skill_file):
        result = parse_skill_file(version_control_skill_file)
        assert str(version_control_skill_file) in result['path']


# ===========================================================================
# 2. Matcher Tests — agent_cascade.skills.matcher
# ===========================================================================


class TestSkillMatcher:
    """Test SkillMatcher inverted index building and keyword matching."""

    @pytest.fixture(autouse=True)
    def _fresh_matcher(self):
        self.matcher = SkillMatcher()

    # -- Index Building --

    def test_build_index_from_metadata(self):
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix connection reuse issues in HTTP clients'
            },
            {
                'name': 'startup-error-audit',
                'description': 'Audit entry points for missing error handling'
            },
        ]
        self.matcher.build_index(skills_meta)
        assert len(self.matcher._inverted_index) > 0
        # The regex groups hyphenated words, so the full compound token is indexed
        assert 'httpx-connection-pooling' in self.matcher._inverted_index

    def test_build_index_ignores_empty_name(self):
        skills_meta = [
            {
                'name': '',
                'description': 'No name'
            },
            {
                'name': 'good-skill',
                'description': 'Has a name'
            },
        ]
        self.matcher.build_index(skills_meta)
        # Only good-skill should be in index values
        for kw, names in self.matcher._inverted_index.items():
            assert '' not in names

    def test_build_index_deduplicates_per_skill(self):
        """Same keyword appearing twice in one skill's text should only add once."""
        skills_meta = [
            {
                'name': 'repeat-repeat',
                'description': 'repeat repeat'
            },
        ]
        self.matcher.build_index(skills_meta)
        for kw, names in self.matcher._inverted_index.items():
            # No duplicate entries for the same skill name
            assert len(set(names)) == len(names)

    def test_build_empty_list(self):
        self.matcher.build_index([])
        assert len(self.matcher._inverted_index) == 0

    # -- Matching --

    def test_match_returns_sorted_results(self):
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix slow API connection reuse issues'
            },
            {
                'name': 'startup-error-audit',
                'description': 'Audit entry points for errors'
            },
        ]
        self.matcher.build_index(skills_meta)

        results = self.matcher.match('slow API calls')
        assert len(results) > 0
        # Results should be sorted by score descending
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_match_returns_tuples_with_name_and_score(self):
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix connection reuse issues'
            },
        ]
        self.matcher.build_index(skills_meta)
        results = self.matcher.match('connection pooling')
        for item in results:
            assert isinstance(item, tuple)
            assert len(item) == 2
            name, score = item
            assert isinstance(name, str)
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

    def test_match_empty_query_returns_empty(self):
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix connection issues'
            },
        ]
        self.matcher.build_index(skills_meta)
        assert self.matcher.match('') == []

    def test_match_on_empty_index_returns_empty(self):
        # No index built at all
        assert self.matcher.match('anything') == []

    def test_match_only_positive_scores(self):
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix connection reuse issues'
            },
            {
                'name': 'startup-error-audit',
                'description': 'Audit entry points for errors'
            },
        ]
        self.matcher.build_index(skills_meta)
        results = self.matcher.match('slow API calls')
        for _, score in results:
            assert score > 0

    def test_match_score_capped_at_1_0(self):
        """Score normalization should cap at 1.0."""
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix connection reuse issues'
            },
        ]
        self.matcher.build_index(skills_meta)
        results = self.matcher.match('httpx connection pooling fix reuse')
        for _, score in results:
            assert score <= 1.0

    def test_match_relevance_ranked_correctly(self):
        """A more relevant skill should rank higher."""
        skills_meta = [
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix slow API connection reuse issues'
            },
            {
                'name': 'startup-error-audit',
                'description': 'Audit entry points for errors'
            },
        ]
        self.matcher.build_index(skills_meta)
        results = self.matcher.match('slow API connection issues')
        # httpx skill should rank higher than startup audit
        assert len(results) >= 1
        top_name = results[0][0]
        assert 'httpx' in top_name or 'connection' in top_name.lower()


# ===========================================================================
# 3. Manager Tests — agent_cascade.skills.manager
# ===========================================================================


class TestSkillManager:
    """Test SkillManager discovery, metadata queries, loading and resolution."""

    @pytest.fixture(autouse=True)
    def _fresh_manager(self, tmp_path):
        self.manager = SkillManager()
        # Isolate metrics writes: SkillManager.__init__ loads the REAL
        # agents/global/skills-metrics.json into memory and defaults _metrics_file to it.
        # Tests here call load_full_instructions() (which increments load counts) over the
        # real skills dir; without redirection a flush would clobber the production file.
        self.manager._metrics_file = tmp_path / 'skills-metrics.json'

    # -- Discovery --

    def test_discover_from_real_skills_dir(self):
        """Discover skills from the real agents/global/skills/ directory."""
        self.manager.discover([_SKILLS_DIR])
        assert len(self.manager._skills_registry) >= 2
        assert 'version-control' in self.manager._skills_registry
        assert 'systematic-debugging' in self.manager._skills_registry

    def test_discover_from_nonexistent_dir(self):
        """Should not crash on missing directory."""
        self.manager.discover([Path('/tmp/no_such_dir_123')])
        assert len(self.manager._skills_registry) == 0

    def test_discover_empty_dir(self):
        """Empty dir should register zero skills without error."""
        tmp = Path('/tmp/empty_skills_test')
        tmp.mkdir(exist_ok=True)
        self.manager.discover([tmp])
        assert len(self.manager._skills_registry) == 0

    # -- Cache signature (regression: in-place edit of nested SKILL.md not detected) --

    def test_scan_signature_detects_nested_skillmd_edit(self, tmp_path):
        """Regression: editing <root>/<skill>/SKILL.md must change the scan signature.

        Bug: compute_scan_signature only stat'd TOP-LEVEL entries of each scan root.
        Skills live one level deeper (root/<name>/SKILL.md), and editing that file
        updates the file's mtime but NOT the parent dir's mtime — so the signature
        never changed, discover() short-circuited as a cache hit, and agents kept
        serving stale skill content after an in-place edit.

        Uses a temp dir (deterministic) rather than the real skills dir.
        """
        from agent_cascade.skills.cache_helper import compute_scan_signature

        root = tmp_path / 'skills'
        skill_dir = root / 'demo-skill'
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / 'SKILL.md'
        skill_file.write_text('---\nname: demo-skill\ndescription: x\n---\n# Body v1\n', encoding='utf-8')

        sig_before = compute_scan_signature([root], frozenset())

        # In-place edit of the nested SKILL.md (content change -> new mtime).
        time.sleep(0.02)  # ensure the mtime actually advances on coarse filesystems
        skill_file.write_text('---\nname: demo-skill\ndescription: x\n---\n# Body v2 (edited)\n', encoding='utf-8')

        sig_after = compute_scan_signature([root], frozenset())
        assert sig_before != sig_after, ('Editing a nested <skill>/SKILL.md must change the scan signature so that '
                                         'discover() re-reads from disk instead of serving a stale cache hit.')

    def test_discover_picks_up_nested_skillmd_edit(self, tmp_path):
        """End-to-end: after an in-place edit, discover() re-loads the new body."""
        root = tmp_path / 'skills'
        skill_dir = root / 'demo-skill'
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / 'SKILL.md'
        skill_file.write_text('---\nname: demo-skill\ndescription: x\n---\n# Body v1\n', encoding='utf-8')

        sm = self.manager
        sm._cache_ttl = 0.0  # force the TTL branch to age out immediately (no sleeping)
        sm.discover([root])
        body1 = sm.load_full_instructions('demo-skill')
        assert 'Body v1' in body1

        time.sleep(0.02)
        skill_file.write_text('---\nname: demo-skill\ndescription: x\n---\n# Body v2 (edited)\n', encoding='utf-8')

        sm._ensure_discovered()  # same path scan_skills / load_skill use
        body2 = sm.load_full_instructions('demo-skill')
        assert 'Body v2' in body2, ('After an in-place edit of a nested SKILL.md, discover() must re-read the '
                                    'file so the updated body is served (regression for stale-cache bug).')

    # -- Cache invalidation (regression: stale discovery cache after NONE-mode clear) --

    def test_invalidate_cache_forces_rediscovery_after_none_mode_clear(self, tmp_path):
        """Regression test for the stale-discovery-cache bug.

        Bug: when ``default_load_skill_mode`` is set to "NONE", the config handler
        clears the registry but did NOT invalidate the discovery cache. A later
        ``_ensure_discovered()`` then hit the TTL/signature early-return and left
        the registry permanently empty — even though all SKILL.md files on disk
        were valid.

        This test reproduces the exact scenario:
          1. Discover from a skills dir -> >0 skills registered.
          2. Simulate the OLD (buggy) NONE-mode handler: clear registry WITHOUT
             invalidating the cache -> re-discovery is a cache hit -> stays empty.
          3. Simulate the FIXED flow: clear registry + invalidate_cache() ->
             re-discovery re-reads from disk -> skills are back.

        Uses an isolated tmp_path skills dir (not the real agents/global/skills) so
        the scan signature and skill count are stable for the duration of the test.
        The old version read the shared real dir, which under parallel xdist runs can
        be mutated by concurrent workers / the skill system between step 1 and steps
        2/3 — changing the signature (breaking the "stale cache hit" assumption in
        step 2) or the count (breaking the == initial_count check in step 3).

        A short TTL (0.0) makes the cache age out immediately, so the test is
        deterministic and fast (no sleeping). The signature check still short-
        circuits in step 2 because the on-disk files did not change, which is
        exactly what the old code relied on to skip re-registration.
        """
        # Build a hermetic skills dir with two fixed, valid skills so discovery
        # registers a known count and the signature is stable across all steps.
        skills_root = tmp_path / 'skills'
        for name in ('version-control', 'systematic-debugging'):
            skill_dir = skills_root / name
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                f'---\nname: {name}\ndescription: fixture skill for cache-invalidation test\n'
                f'version: "1.0.0"\n---\n# Body\n',
                encoding='utf-8')

        sm = self.manager
        # Short TTL so any elapsed time counts as "expired" (no sleeping needed).
        sm._cache_ttl = 0.0

        # Step 1: discover from the isolated skills dir.
        sm.discover([skills_root])
        initial_count = len(sm._skills_registry)
        assert initial_count == 2, f'Expected exactly 2 fixture skills, got {initial_count}'

        # Step 2 (OLD/buggy behavior): clear registry + rebuild index WITHOUT
        # invalidating the cache. The cache still holds a valid signature and a
        # recent timestamp, so re-discovery must short-circuit (cache hit).
        with sm._write_lock:
            sm._skills_registry.clear()
            sm._rebuild_index()
        assert len(sm._skills_registry) == 0

        # _ensure_discovered() is the same path scan_skills / tools use. With a
        # stale-but-valid cache it must NOT re-register (documents the old bug).
        sm._ensure_discovered()
        assert len(sm._skills_registry) == 0, ('Old buggy behavior: registry stayed empty because the discovery '
                                               'cache was not invalidated after the NONE-mode clear')

        # Step 3 (FIXED flow): clear again, then invalidate the cache. The next
        # discovery must bypass the early-return and re-read from disk.
        with sm._write_lock:
            sm._skills_registry.clear()
            sm._rebuild_index()
        sm.invalidate_cache()
        assert sm._cache_signature is None
        assert sm._cache_timestamp == 0.0

        sm._ensure_discovered()
        assert len(sm._skills_registry) == initial_count, (
            'Fixed flow: registry should be fully repopulated after invalidate_cache()')
        assert 'version-control' in sm._skills_registry

    def test_invalidate_cache_resets_fields_under_lock(self):
        """invalidate_cache() must reset both cache fields (thread-safe)."""
        sm = self.manager
        sm.discover([_SKILLS_DIR])
        # After a real discovery the signature is set and timestamp is recent.
        assert sm._cache_signature is not None
        assert sm._cache_timestamp > 0.0

        sm.invalidate_cache()
        assert sm._cache_signature is None
        assert sm._cache_timestamp == 0.0

    # -- Tier 1 Metadata Queries --

    def test_get_skill_metadata_known_name(self):
        self.manager.discover([_SKILLS_DIR])
        meta = self.manager.get_skill_metadata('version-control')
        assert meta is not None
        assert meta['name'] == 'version-control'
        assert len(meta['description']) > 10

    def test_get_skill_metadata_unknown_name(self):
        self.manager.discover([_SKILLS_DIR])
        meta = self.manager.get_skill_metadata('nonexistent-skill')
        assert meta is None

    def test_get_all_metadata_excludes_internal_fields(self):
        """get_all_metadata should not leak _priority or _parsed_data."""
        self.manager.discover([_SKILLS_DIR])
        all_meta = self.manager.get_all_metadata()
        for m in all_meta:
            assert '_priority' not in m
            assert '_parsed_data' not in m
            assert 'name' in m
            assert 'description' in m

    def test_get_all_metadata_returns_list(self):
        self.manager.discover([_SKILLS_DIR])
        all_meta = self.manager.get_all_metadata()
        assert isinstance(all_meta, list)
        assert len(all_meta) >= 2

    # -- Tier 2 Loading --

    def test_load_full_instructions_known_skill(self):
        self.manager.discover([_SKILLS_DIR])
        body = self.manager.load_full_instructions('version-control')
        assert body is not None
        assert len(body) > 100
        # Body should contain markdown content from the SKILL.md
        assert '##' in body or '#' in body

    def test_load_full_instructions_unknown_skill(self):
        self.manager.discover([_SKILLS_DIR])
        body = self.manager.load_full_instructions('nonexistent-skill')
        assert body is None

    # -- Resolution: load_skill argument handling --

    def _setup_manager_with_skills(self):
        """Helper to discover real skills before resolution tests."""
        self.manager.discover([_SKILLS_DIR])

    def test_resolve_list_value_returns_loaded_skills(self):
        """resolve_load_skill with a list should return instruction strings."""
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill(
            ['version-control'],
            task_text='Fix connection issues',
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert len(result[0]) > 50

    def test_resolve_auto_mode_uses_matcher(self):
        """resolve_load_skill with 'AUTO' should use the matcher."""
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill(
            'AUTO',
            task_text='slow API connection pooling issues',
        )
        # Should match httpx skill based on keywords
        assert isinstance(result, list)
        if len(result) > 0:
            # At least some content loaded
            for body in result:
                assert len(body) > 10

    def test_resolve_none_returns_empty_list(self):
        self._setup_manager_with_skills()
        assert self.manager.resolve_load_skill('NONE') == []

    def test_resolve_null_returns_empty_list(self):
        self._setup_manager_with_skills()
        assert self.manager.resolve_load_skill(None) == []

    def test_resolve_missing_skill_name_skips_gracefully(self):
        """Missing skill names in a list should not crash, just be skipped."""
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill(
            ['version-control', 'nonexistent-skill'],
            task_text='test',
        )
        assert len(result) == 1  # Only the valid skill loaded

    def test_resolve_unknown_string_value_returns_empty(self):
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill('UNKNOWN_MODE')
        assert result == []

    def test_resolve_list_multiple_skills(self):
        """Loading multiple skills by name should return all available."""
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill(['version-control', 'systematic-debugging'],)
        assert len(result) == 2

    def test_resolve_auto_no_match_returns_empty(self):
        """AUTO mode with no relevant query should return empty."""
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill(
            'AUTO',
            task_text='quantum physics entanglement experiments',
        )
        assert isinstance(result, list)

    def test_resolve_auto_caps_at_max_skills(self):
        """Basic AUTO mode must return at most MAX_AUTO_SKILLS_PER_CALL matched skills."""
        from agent_cascade.settings import MAX_AUTO_SKILLS_PER_CALL
        self._setup_manager_with_skills()
        # Broad query that should match multiple indexed skills.
        result = self.manager.resolve_load_skill(
            'AUTO',
            task_text='skill matching docker testing code review debugging git version control',
        )
        assert len(result) <= MAX_AUTO_SKILLS_PER_CALL

    # -- resolve_load_skill_names (shared name-computation) --

    def test_resolve_names_explicit_returns_only_loadable(self):
        """Explicit list returns only the names that are actually loadable."""
        self._setup_manager_with_skills()
        result = self.manager.resolve_load_skill_names(['version-control', 'nonexistent-skill'],)
        assert result == ['version-control']

    def test_resolve_names_auto_returns_above_threshold_and_loadable(self):
        """AUTO returns names that are above threshold AND loadable."""
        self._setup_manager_with_skills()
        bodies = self.manager.resolve_load_skill(
            'AUTO',
            task_text='slow API connection pooling issues',
        )
        names = self.manager.resolve_load_skill_names(
            'AUTO',
            task_text='slow API connection pooling issues',
        )
        # Every name must be a real registered skill, and count matches bodies.
        assert len(names) == len(bodies)
        for n in names:
            assert n in self.manager._skills_registry

    def test_resolve_names_none_and_null_return_empty(self):
        self._setup_manager_with_skills()
        assert self.manager.resolve_load_skill_names('NONE') == []
        assert self.manager.resolve_load_skill_names(None) == []

    def test_resolve_names_matches_bodies_drift_guard(self):
        """Drift guard: names must be exactly the set backing resolve_load_skill bodies.

        Regression guard against the name-computation and body-loading diverging.
        Verifies not just equal lengths but that each returned name corresponds to a
        real, loadable body (and vice versa) — i.e. the two methods share one source of truth.
        """
        self._setup_manager_with_skills()
        for value, kwargs in [
            (['version-control', 'systematic-debugging'], {}),
            ('AUTO', {
                'task_text': 'slow API connection pooling issues'
            }),
            ('NONE', {}),
            (None, {}),
            ('UNKNOWN_MODE', {}),
        ]:
            bodies = self.manager.resolve_load_skill(value, **kwargs)
            names = self.manager.resolve_load_skill_names(value, **kwargs)
            assert len(names) == len(bodies), f"drift for {value!r}: {len(names)} names vs {len(bodies)} bodies"

            # Each name must be a real registered skill with a loadable (non-empty) body.
            for n in names:
                assert n in self.manager._skills_registry, f"{n!r} not registered for {value!r}"
                assert self.manager.load_full_instructions(n), f"{n!r} has no loadable body for {value!r}"

            # Every produced body must be the body of one of the returned names (no orphans).
            expected_bodies = [self.manager.load_full_instructions(n) for n in names]
            assert sorted(map(repr, bodies)) == sorted(map(repr, expected_bodies)), \
                f"bodies do not correspond to names for {value!r}"

    # -- resolve_load_skill_pairs (name + body) --

    def test_resolve_pairs_explicit_returns_name_body_tuples(self):
        """resolve_load_skill_pairs with an explicit list returns (name, body) tuples."""
        self._setup_manager_with_skills()
        pairs = self.manager.resolve_load_skill_pairs(['version-control'])
        assert isinstance(pairs, list) and len(pairs) == 1
        name, body = pairs[0]
        assert name == 'version-control'
        assert isinstance(body, str) and len(body) > 50
        # Body must match what load_full_instructions returns for that name.
        assert body == self.manager.load_full_instructions('version-control')

    def test_resolve_pairs_skips_missing_names(self):
        """Missing skill names are dropped from the pairs (consistent with resolve_load_skill)."""
        self._setup_manager_with_skills()
        pairs = self.manager.resolve_load_skill_pairs(['version-control', 'nonexistent-skill'])
        assert [n for n, _ in pairs] == ['version-control']

    def test_resolve_pairs_auto_mode(self):
        """AUTO mode returns (name, body) pairs matching the name resolver."""
        self._setup_manager_with_skills()
        kwargs = {'task_text': 'slow API connection pooling issues'}
        pairs = self.manager.resolve_load_skill_pairs('AUTO', **kwargs)
        names = self.manager.resolve_load_skill_names('AUTO', **kwargs)
        # Names from pairs must exactly match the shared name resolver.
        assert [n for n, _ in pairs] == names
        for n, body in pairs:
            assert body == self.manager.load_full_instructions(n)

    def test_resolve_pairs_none_and_null_return_empty(self):
        self._setup_manager_with_skills()
        assert self.manager.resolve_load_skill_pairs('NONE') == []
        assert self.manager.resolve_load_skill_pairs(None) == []

    def test_resolve_pairs_matches_bodies_drift_guard(self):
        """Drift guard: pairs' names must be exactly the shared name resolver's output."""
        self._setup_manager_with_skills()
        for value, kwargs in [
            (['version-control', 'systematic-debugging'], {}),
            ('AUTO', {
                'task_text': 'slow API connection pooling issues'
            }),
            ('NONE', {}),
            (None, {}),
        ]:
            pairs = self.manager.resolve_load_skill_pairs(value, **kwargs)
            names = self.manager.resolve_load_skill_names(value, **kwargs)
            assert [n for n, _ in pairs] == names, f"drift for {value!r}"

    # -- Fix B: Multi-tier discovery with priority resolution --

    def test_discover_picks_up_workspace_tier(self, tmp_path):
        """Skills in workspace/skills/ are discovered with _PRIORITY_USER."""
        from agent_cascade.skills.manager import _PRIORITY_USER

        root = tmp_path / 'workspace' / 'skills' / 'ws-skill'
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_text('---\nname: ws-skill\ndescription: test\n---\n# Body\n', encoding='utf-8')

        sm = self.manager
        sm._cache_ttl = 0.0
        sm.discover([tmp_path / 'workspace' / 'skills'])

        assert 'ws-skill' in sm._skills_registry
        assert sm._skills_registry['ws-skill']['_priority'] == _PRIORITY_USER

    def test_discover_picks_up_agent_tier(self, tmp_path):
        """Skills in agents/<name>/skills/ are discovered with _PRIORITY_AGENT."""
        from agent_cascade.skills.manager import _PRIORITY_AGENT

        root = tmp_path / 'agents' / 'coder' / 'skills' / 'agent-skill'
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_text('---\nname: agent-skill\ndescription: test\n---\n# Body\n', encoding='utf-8')

        sm = self.manager
        sm._cache_ttl = 0.0
        sm.discover([tmp_path / 'agents' / 'coder' / 'skills'])

        assert 'agent-skill' in sm._skills_registry
        assert sm._skills_registry['agent-skill']['_priority'] == _PRIORITY_AGENT

    def test_priority_resolution_user_overrides_system(self, tmp_path):
        """When the same skill name exists in system and user tiers, user wins."""
        from agent_cascade.skills.manager import _PRIORITY_USER

        # System tier: .qwen/skills/dup-skill
        sys_root = tmp_path / '.qwen' / 'skills' / 'dup-skill'
        sys_root.mkdir(parents=True)
        (sys_root / 'SKILL.md').write_text(
            '---\nname: dup-skill\ndescription: system version\n---\n# System body\n',
            encoding='utf-8',
        )

        # User tier: workspace/skills/dup-skill
        user_root = tmp_path / 'workspace' / 'skills' / 'dup-skill'
        user_root.mkdir(parents=True)
        (user_root / 'SKILL.md').write_text(
            '---\nname: dup-skill\ndescription: user version\n---\n# User body\n',
            encoding='utf-8',
        )

        sm = self.manager
        sm._cache_ttl = 0.0
        sm.discover([tmp_path / '.qwen' / 'skills', tmp_path / 'workspace' / 'skills'])

        assert 'dup-skill' in sm._skills_registry
        assert sm._skills_registry['dup-skill']['_priority'] == _PRIORITY_USER
        # User version should be the one registered (higher priority wins).
        assert 'user version' in sm._skills_registry['dup-skill']['description']

    def test_priority_resolution_agent_overrides_system(self, tmp_path):
        """Agent-tier skill should override system-tier duplicate."""
        from agent_cascade.skills.manager import _PRIORITY_AGENT

        # System tier: .qwen/skills/dup-skill
        sys_root = tmp_path / '.qwen' / 'skills' / 'dup-skill'
        sys_root.mkdir(parents=True)
        (sys_root / 'SKILL.md').write_text(
            '---\nname: dup-skill\ndescription: system version\n---\n# System body\n',
            encoding='utf-8',
        )

        # Agent tier: agents/coder/skills/dup-skill
        agent_root = tmp_path / 'agents' / 'coder' / 'skills' / 'dup-skill'
        agent_root.mkdir(parents=True)
        (agent_root / 'SKILL.md').write_text(
            '---\nname: dup-skill\ndescription: agent version\n---\n# Agent body\n',
            encoding='utf-8',
        )

        sm = self.manager
        sm._cache_ttl = 0.0
        sm.discover([tmp_path / '.qwen' / 'skills', tmp_path / 'agents' / 'coder' / 'skills'])

        assert 'dup-skill' in sm._skills_registry
        assert sm._skills_registry['dup-skill']['_priority'] == _PRIORITY_AGENT
        # Agent version should be the one registered (higher priority than system).
        assert 'agent version' in sm._skills_registry['dup-skill']['description']

    def test_ensure_discovered_covers_all_tiers(self, tmp_path):
        """Hot-reload via _ensure_discovered() works for non-system tiers."""
        user_root = tmp_path / 'workspace' / 'skills' / 'ws-skill'
        user_root.mkdir(parents=True)
        skill_file = user_root / 'SKILL.md'
        skill_file.write_text('---\nname: ws-skill\ndescription: test\n---\n# Body v1\n', encoding='utf-8')

        sm = self.manager
        sm._cache_ttl = 0.0
        sm.discover([tmp_path / 'workspace' / 'skills'])
        body1 = sm.load_full_instructions('ws-skill')
        assert 'Body v1' in body1

        # Edit the user-tier skill in place.
        time.sleep(0.02)
        skill_file.write_text(
            '---\nname: ws-skill\ndescription: test\n---\n# Body v2 (edited)\n',
            encoding='utf-8',
        )

        sm._ensure_discovered()
        body2 = sm.load_full_instructions('ws-skill')
        assert 'Body v2' in body2, ('Hot-reload via _ensure_discovered must pick up edits in non-system tiers.')


# ===========================================================================
# 4. Integration Tests — DNA schema and settings wiring
# ===========================================================================


class TestIntegration:
    """Verify skills system is wired into dna.py TOOL_METADATA and settings."""

    def test_scan_skills_in_tool_metadata(self):
        from agent_cascade.prompts.dna import TOOL_METADATA
        assert 'scan_skills' in TOOL_METADATA, ('scan_skills tool schema missing from TOOL_METADATA')
        meta = TOOL_METADATA['scan_skills']
        assert 'description' in meta
        assert 'parameters' in meta
        assert 'query' in meta['parameters']

    def test_load_skill_in_call_agent_metadata(self):
        """Verify load_skill parameter is defined for call_agent tool."""
        from agent_cascade.prompts.dna import TOOL_METADATA
        assert 'call_agent' in TOOL_METADATA, ('call_agent missing from TOOL_METADATA')
        params = TOOL_METADATA['call_agent']['parameters']
        assert 'load_skill' in params, ('load_skill parameter missing from call_agent TOOL_METADATA')

    def test_default_load_skill_mode_exists(self):
        """Verify DEFAULT_LOAD_SKILL_MODE setting exists and has a valid value."""
        from agent_cascade.settings import DEFAULT_LOAD_SKILL_MODE
        assert isinstance(DEFAULT_LOAD_SKILL_MODE, str)
        # Should be either AUTO or NONE (or empty string for env override)
        upper = DEFAULT_LOAD_SKILL_MODE.upper()
        assert upper in ('AUTO', 'NONE'), (f"DEFAULT_LOAD_SKILL_MODE has unexpected value: {DEFAULT_LOAD_SKILL_MODE}")

    def test_scan_skills_description_mentions_load_skill(self):
        """scan_skills description should mention load_skill for discoverability."""
        from agent_cascade.prompts.dna import TOOL_METADATA
        desc = TOOL_METADATA['scan_skills']['description'].lower()
        assert 'load_skill' in desc or 'load skill' in desc

    def test_available_tools_includes_scan_skills(self):
        """scan_skills should be listed in AVAILABLE_TOOLS."""
        from agent_cascade.prompts.dna import AVAILABLE_TOOLS
        assert 'scan_skills' in AVAILABLE_TOOLS


# ===========================================================================
# 5. Edge Cases and Cross-Module Tests
# ===========================================================================


class TestEdgeCases:
    """Cross-cutting edge cases for the skills system."""

    def test_parse_frontmatter_with_unicode(self):
        content = '---\nname: café-skill\ndescription: Réglage des problèmes\n---\nCorps'
        fm, body = parse_frontmatter(content)
        assert fm['name'] == 'café-skill'
        assert 'Réglage' in fm['description']

    def test_matcher_with_special_characters_in_query(self):
        """Matcher should handle queries with punctuation and special chars."""
        m = SkillMatcher()
        m.build_index([
            {
                'name': 'httpx-connection-pooling',
                'description': 'Fix connection issues'
            },
        ])
        results = m.match('What about slow API calls??? (very slow!)')
        assert isinstance(results, list)

    def test_manager_resolve_with_empty_list(self):
        """Empty list should return empty result."""
        mgr = SkillManager()
        assert mgr.resolve_load_skill([]) == []

    def test_parse_frontmatter_preserves_body_order(self):
        """Body text order should be preserved after frontmatter removal."""
        content = ('---\nname: test\n---\n\n'
                   '## Section 1\nFirst paragraph.\n\n'
                   '## Section 2\nSecond paragraph.')
        fm, body = parse_frontmatter(content)
        assert body.index('Section 1') < body.index('Section 2')

    def test_matcher_rebuild_index_clears_old(self):
        """Rebuilding the index should clear previous entries."""
        m = SkillMatcher()
        m.build_index([{'name': 'old-skill', 'description': 'Old description'}])
        assert 'old' in m._inverted_index

        m.build_index([{'name': 'new-skill', 'description': 'New stuff here'}])
        # Old keyword should be gone if it doesn't appear in new data
        for kw, names in m._inverted_index.items():
            assert 'old-skill' not in names


# ===========================================================================
# 9. Skills Block Formatting — named skills in system prompt injection
# ===========================================================================


class TestSkillsBlockFormatting:
    """_build_skills_block / _inject_skills_to_system_message label skills by name."""

    def test_build_block_named_tuples_use_real_name(self):
        from agent_cascade.engine.helpers import _build_skills_block
        block = _build_skills_block([('docker-best-practices', 'DOCKER BODY')])
        assert '### Skill docker-best-practices' in block
        assert 'DOCKER BODY' in block
        # Must NOT fall back to a positional index label.
        assert '### Skill 1' not in block

    def test_build_block_multiple_named(self):
        from agent_cascade.engine.helpers import _build_skills_block
        block = _build_skills_block([
            ('skill-a', 'BODY A'),
            ('skill-b', 'BODY B'),
        ])
        assert '### Skill skill-a' in block
        assert '### Skill skill-b' in block
        assert '### Skill 1' not in block and '### Skill 2' not in block

    def test_build_block_plain_strings_backward_compat(self):
        """Plain strings (no name) still render with the old positional label."""
        from agent_cascade.engine.helpers import _build_skills_block
        block = _build_skills_block(['BODY ONE', 'BODY TWO'])
        assert '### Skill 1' in block
        assert '### Skill 2' in block

    def test_build_block_mixed_tuple_and_string(self):
        from agent_cascade.engine.helpers import _build_skills_block
        block = _build_skills_block([('named-skill', 'NAMED'), 'PLAIN BODY'])
        assert '### Skill named-skill' in block
        assert '### Skill 2' in block  # plain string gets positional label

    def test_build_block_empty_returns_empty(self):
        from agent_cascade.engine.helpers import _build_skills_block
        assert _build_skills_block([]) == ''

    def test_inject_named_skills_into_system_message(self):
        """End-to-end: named skills produce '### Skill <name>' in the system message."""
        from agent_cascade.engine.helpers import _inject_skills_to_system_message
        from agent_cascade.llm.schema import SYSTEM, Message

        sys_msg = Message(role=SYSTEM, content='You are a helpful agent.')
        injected = _inject_skills_to_system_message(
            pool=None,
            instance_or_sysmsg=sys_msg,
            skills_to_inject=[('docker-best-practices', 'DOCKER INSTRUCTIONS')],
        )
        assert injected is True
        assert '## Active Skills' in sys_msg.content
        assert '### Skill docker-best-practices' in sys_msg.content
        assert 'DOCKER INSTRUCTIONS' in sys_msg.content
        assert '### Skill 1' not in sys_msg.content

    def test_inject_plain_string_backward_compat(self):
        from agent_cascade.engine.helpers import _inject_skills_to_system_message
        from agent_cascade.llm.schema import SYSTEM, Message

        sys_msg = Message(role=SYSTEM, content='You are a helpful agent.')
        injected = _inject_skills_to_system_message(
            pool=None,
            instance_or_sysmsg=sys_msg,
            skills_to_inject=['PLAIN BODY'],
        )
        assert injected is True
        assert '### Skill 1' in sys_msg.content


# ===========================================================================
# Async test helper: run async tests with asyncio
# ===========================================================================


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# 6. scan_skills display — rating column + no-query ordering
# ===========================================================================


class TestScanSkillsRatingDisplay:
    """scan_skills shows quality ratings; no-query mode sorts by rating desc (unrated last)."""

    @pytest.fixture(autouse=True)
    def _tool(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cascade.tools.custom.scan_skills import ScanSkills

        manager = SkillManager()
        manager._metrics_file = tmp_path / 'skills-metrics.json'  # isolate metrics writes
        # Seed a deterministic registry (bypasses disk discovery).
        for name in ('alpha', 'bravo', 'charlie'):
            manager._skills_registry[name] = {
                'name': name,
                'description': f'desc {name}',
                'source': 'system',
                'version': '1.0.2',
            }
        pool = MagicMock()
        pool.skill_manager = manager
        self.manager = manager
        self.tool = ScanSkills(agent_pool=pool)

    def test_no_query_orders_by_rating_desc_unrated_last_name_tiebreak(self):
        # alpha 8.0, bravo 6.5 (tie with charlie), charlie 6.5 → order: alpha, bravo, charlie, delta(unrated)
        self.manager.record_rating('alpha', 8.0)
        self.manager.record_rating('bravo', 6.5)
        self.manager.record_rating('charlie', 6.5)
        # delta stays unrated
        self.manager._skills_registry['delta'] = {
            'name': 'delta',
            'description': 'desc delta',
            'source': 'system',
            'version': '1.0.2',
        }

        out = self.tool.call({'query': ''})
        lines = [l for l in out.splitlines() if l.startswith('- **')]
        names = [l.split('**')[1] for l in lines]
        assert names == ['alpha', 'bravo', 'charlie', 'delta']

        # Rating column present: rated shows avg, unrated shows n/a.
        # Chars field is also displayed (e.g. "~0.0k chars" for empty-body test skills).
        alpha_line = next(l for l in lines if '**alpha**' in l)
        assert 'rating: 8.0' in alpha_line
        delta_line = next(l for l in lines if '**delta**' in l)
        assert 'rating: n/a' in delta_line

    def test_query_mode_appends_rating_without_reordering(self):
        """Query mode keeps matcher ordering; rating is appended to each line."""
        self.manager.record_rating('alpha', 9.0)
        # Force a deterministic matcher order (charlie first, alpha second).
        self.manager.match_skills = lambda q: [('charlie', 0.9), ('alpha', 0.5)]

        out = self.tool.call({'query': 'some query'})
        lines = [l for l in out.splitlines() if l.startswith('- **')]
        names = [l.split('**')[1] for l in lines]
        # Order follows the matcher, NOT the rating (charlie unrated comes first).
        assert names == ['charlie', 'alpha']

        alpha_line = next(l for l in lines if '**alpha**' in l)
        assert 'score: 0.50' in alpha_line
        assert 'rating: 9.0' in alpha_line
        charlie_line = next(l for l in lines if '**charlie**' in l)
        assert 'rating: n/a' in charlie_line


# ===========================================================================
# Skill Invalidation — Phase 1 (manual toggle + schema 1.3 + migration + Q7 guard)
# ===========================================================================


def _invalidation_manager(tmp_path, skill_names=('inv-a', 'inv-b')):
    """Fresh SkillManager with isolated metrics + a hermetic one-level skills tree."""
    m = SkillManager()
    # Isolate metrics (the production store loaded at __init__ must not leak in).
    m._metrics_file = tmp_path / 'skills-metrics.json'
    with m._metrics_lock:
        m._metrics = {}
    root = tmp_path / 'skills'
    for name in skill_names:
        _write_skill_file(root, name)
    # discover() is the canonical way to set _skill_paths (prune/servable scans read it);
    # also force a real scan so the cache signature matches the current tree.
    m._cache_ttl = 0.0
    m.discover([root])
    return m


class TestSkillInvalidationToggle:
    """disable_skill/enable_skill: status flips, persistence, cache invalidation."""

    @pytest.fixture(autouse=True)
    def _toggle_manager(self, tmp_path):
        self.manager = _invalidation_manager(tmp_path)
        yield

    def test_disable_skill_flips_status_and_disables(self):
        m = self.manager
        ok, msg = m.disable_skill('inv-a')
        assert ok, msg
        with m._metrics_lock:
            assert m._metrics['inv-a']['status'] == 'inactive'
        assert 'inv-a' in m._disabled_names
        # Persisted on disk at schema 1.3 with the status field.
        store = _read_store(m._metrics_file)
        assert store['schema_version'] == '1.3'
        assert store['skills']['inv-a']['status'] == 'inactive'

    def test_enable_skill_reverses(self):
        m = self.manager
        ok, _ = m.disable_skill('inv-a')
        assert ok
        ok, msg = m.enable_skill('inv-a')
        assert ok, msg
        with m._metrics_lock:
            assert m._metrics['inv-a']['status'] == 'active'
        assert 'inv-a' not in m._disabled_names
        store = _read_store(m._metrics_file)
        assert store['skills']['inv-a']['status'] == 'active'

    def test_toggle_invalidates_cache_immediately(self, tmp_path):
        """Regression: a toggle must force the very next discover() to re-scan (E1).

        Without invalidate_cache(), discover() short-circuits on TTL+signature and the
        toggled skill stays in (or out of) the registry until the TTL expires.
        """
        m = self.manager
        root = Path(m._skill_paths[0])
        m._cache_ttl = 0.0  # force the TTL branch to age out immediately (no sleeping)

        m.discover([root])
        assert 'inv-a' in m._skills_registry
        assert m._cache_signature is not None  # cache primed by the real scan

        ok, _ = m.disable_skill('inv-a')
        assert ok
        # Cache must be invalidated so the next scan re-reads from disk.
        assert m._cache_signature is None
        assert m._cache_timestamp == 0.0

        m.discover([root])
        assert 'inv-a' not in m._skills_registry, ('disabled skill must be absent after the '
                                                   'immediate post-toggle re-scan')
        assert 'inv-b' in m._skills_registry

        ok, _ = m.enable_skill('inv-a')
        assert ok
        m.discover([root])
        assert 'inv-a' in m._skills_registry, ('re-enabled skill must be back after the '
                                               'immediate post-toggle re-scan')

    def test_disable_unknown_name_returns_error(self):
        m = self.manager
        before_metrics = _copy.deepcopy(m._metrics)
        ok, msg = m.disable_skill('nope-does-not-exist')
        assert not ok
        assert 'not found' in msg
        assert 'nope-does-not-exist' not in m._disabled_names
        assert m._metrics == before_metrics

    def test_get_inactive_names_and_list_skills_with_status(self):
        m = self.manager
        assert m.get_inactive_names() == set()
        m.disable_skill('inv-a')
        assert m.get_inactive_names() == {'inv-a'}

        listing = {s['name']: s for s in m.list_skills_with_status()}
        assert listing['inv-a']['status'] == 'inactive'
        assert listing['inv-a']['active'] is False
        assert listing['inv-b']['status'] == 'active'
        assert listing['inv-b']['active'] is True
        assert listing['inv-a']['total_loads'] == 0
        assert listing['inv-a']['rating_avg'] is None


class TestSkillInvalidationMigration:
    """One-time schema 1.2 → 1.3 migration (orphan guard + servable-set logic)."""

    @pytest.fixture(autouse=True)
    def _mig_manager(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / 'skills'
        self.metrics_file = tmp_path / 'skills-metrics.json'
        yield

    def _make_manager(self):
        m = SkillManager()
        m._metrics_file = self.metrics_file
        with m._metrics_lock:
            m._metrics = {}
        # Load the seeded legacy store (fresh manager → __init__'s _load_metrics read
        # whatever was on disk at construction; re-point + reload to be explicit).
        m._load_metrics()
        m._cache_ttl = 0.0
        return m

    def test_migration_defaults_present_skills_active(self):
        _write_skill_file(self.root, 'mig-active')
        time.sleep(0.02)  # ensure a distinct mtime is observable
        _seed_v12_store(self.metrics_file, {
            'mig-active': {'total_loads': 3, 'by_version': {'1.0.0': 3}},
        })
        m = self._make_manager()
        m.discover([self.root])
        m._migrate_metrics_to_v13()

        with m._metrics_lock:
            entry = m._metrics['mig-active']
        assert entry['status'] == 'active'
        # last_used seeded from the SKILL.md mtime (D-B).
        expected_seed = _iso_utc((self.root / 'mig-active' / 'SKILL.md').stat().st_mtime)
        assert entry['last_used'] == expected_seed
        store = _read_store(self.metrics_file)
        assert store['schema_version'] == '1.3'
        assert store['skills']['mig-active']['status'] == 'active'

    def test_migration_marks_inactive_location_skills(self):
        """D-A: file only under <root>/INACTIVE/<name>/ → inactive; standard location → active."""
        _write_skill_file(self.root, 'mig-servable')
        _write_skill_file(self.root, 'mig-retired', subdir='INACTIVE')
        # NOTE: the frontmatter name must match the directory name — prune_stale_metrics'
        # live set contains BOTH the frontmatter name and the dir name (rglob walk), so a
        # mismatched frontmatter name would keep the entry alive as an "orphan" of its own.
        _seed_v12_store(self.metrics_file, {
            'mig-servable': {'total_loads': 1, 'by_version': {}},
            'mig-retired': {'total_loads': 7, 'by_version': {}},
        })
        m = self._make_manager()
        m.discover([self.root])
        m._migrate_metrics_to_v13()

        with m._metrics_lock:
            assert m._metrics['mig-servable']['status'] == 'active'
            assert m._metrics['mig-retired']['status'] == 'inactive'
        # The retired skill's name also joins _disabled_names (it must not be served).
        assert 'mig-retired' in m._disabled_names

    def test_migration_creates_record_for_disk_skill_without_entry(self):
        _write_skill_file(self.root, 'mig-fresh')
        _seed_v12_store(self.metrics_file, {})  # empty legacy store
        m = self._make_manager()
        m.discover([self.root])
        m._migrate_metrics_to_v13()

        with m._metrics_lock:
            entry = m._metrics['mig-fresh']
        assert entry == {
            'total_loads': 0,
            'by_version': {},
            'status': 'active',
            'last_used': _iso_utc((self.root / 'mig-fresh' / 'SKILL.md').stat().st_mtime),
        }

    def test_migration_orphan_guard(self):
        """A metrics entry whose file was deleted out-of-band is pruned, never resurrected."""
        skill_file = _write_skill_file(self.root, 'mig-orphan')
        _seed_v12_store(self.metrics_file, {
            'mig-orphan': {'total_loads': 5, 'by_version': {}},
        })
        # Delete the backing file out-of-band (simulates manual removal).
        skill_file.unlink()

        m = self._make_manager()
        m.discover([self.root])
        m._migrate_metrics_to_v13()

        with m._metrics_lock:
            assert 'mig-orphan' not in m._metrics, ('orphaned entry must be pruned by the '
                                                    'migration pre-pass, never resurrected as active')
        store = _read_store(self.metrics_file)
        assert 'mig-orphan' not in store['skills']

    def test_migration_idempotent(self):
        _write_skill_file(self.root, 'mig-idem-a')
        _write_skill_file(self.root, 'mig-idem-b', subdir='INACTIVE')
        _seed_v12_store(self.metrics_file, {
            'mig-idem-a': {'total_loads': 2, 'by_version': {}},
            'mig-idem-b': {'total_loads': 1, 'by_version': {}},
        })
        m = self._make_manager()
        m.discover([self.root])
        m._migrate_metrics_to_v13()
        with m._metrics_lock:
            first_run = _copy.deepcopy(m._metrics)

        # Second run (fresh manager, same on-disk store) must change nothing.
        m2 = self._make_manager()
        m2.discover([self.root])
        m2._migrate_metrics_to_v13()
        with m2._metrics_lock:
            second_run = _copy.deepcopy(m2._metrics)

        assert first_run == second_run, 'migration must be idempotent (fill-if-missing only)'


class TestSkillInvalidationQ7Guard:
    """Candidate promotion guard: an inactive incumbent holds its candidate pending."""

    @pytest.fixture(autouse=True)
    def _q7_manager(self, tmp_path):
        self.manager = SkillManager()
        base = tmp_path / 'agents' / 'global'
        self.manager._metrics_file = tmp_path / 'skills-metrics.json'
        with self.manager._metrics_lock:
            self.manager._metrics = {}
        # Isolate the candidate flow roots (mirrors test_skill_generation.fresh_manager).
        self.manager._pending_dir = base / 'pending-skills'
        self.manager._candidates_dir = base / 'candidates'
        self.manager._production_skills_dir = base / 'skills'
        yield

    def _write_incumbent(self, name: str, version: str = '1.0.0'):
        """Write a production SKILL.md to the manager's production root and register it."""
        _, prod_root = self.manager._candidate_dirs()
        d = prod_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Incumbent skill for Q7 guard test\n'
            f'version: "{version}"\ntriggers:\n  - candidate\n  - test\n---\n\nINCUMBENT_BODY\n',
            encoding='utf-8')
        from agent_cascade.skills.manager import _PRIORITY_SYSTEM
        from agent_cascade.skills.parser import parse_skill_file
        parsed = parse_skill_file(d / 'SKILL.md')
        self.manager._skills_registry[name] = {
            'name': name,
            'description': parsed.get('frontmatter', {}).get('description', ''),
            'source': 'system',
            'triggers': parsed.get('frontmatter', {}).get('triggers', []),
            'version': version,
            'file_path': str(d / 'SKILL.md'),
            '_priority': _PRIORITY_SYSTEM,
            '_parsed_data': parsed,
        }

    def test_candidate_held_when_incumbent_inactive(self):
        m = self.manager
        name = f'q7-guard-skill-{os.getpid()}'
        self._write_incumbent(name, '1.0.0')

        # Upgrade proposal → live-serving candidate (distinct version 2.0.0).
        # Body must clear MIN_SKILL_BODY_LENGTH (validator.py) — keep it comfortably long.
        content = ('---\nname: %s\n'
                   'description: Better candidate version for the Q7 guard test\n'
                   'version: "2.0.0"\ntriggers:\n  - candidate\n  - test\n---\n\n'
                   '## Instructions\n'
                   'Candidate body with enough characters to pass validation. This paragraph '
                   'exists purely so the body length clears the validator minimum; the actual '
                   'behavior under test is the Q7 hold-when-inactive promotion guard.\n') % name
        success, errors = m.register_skill_from_content(content, task_text='candidate test upgrade')
        assert success, f'candidate registration failed: {errors}'

        # Rated candidate that would normally promote over the unrated incumbent.
        for r in (9.0, 9.0, 9.0, 9.0, 10.0):
            m.record_rating(name, r)

        # Disable the incumbent → the gate must HOLD the candidate (no promote/discard).
        ok, _ = m.disable_skill(name)
        assert ok
        m.evaluate_candidates()

        from agent_cascade.skills.manager import _PRIORITY_CANDIDATE
        reg = m._skills_registry.get(name)
        assert reg is not None and reg['_priority'] == _PRIORITY_CANDIDATE, (
            'candidate must stay pending while the incumbent is inactive')
        assert (m._candidates_dir / name / 'SKILL.md').exists(), (
            'candidate file must remain on disk while held')

        # Re-activate → the gate now promotes.
        ok, _ = m.enable_skill(name)
        assert ok
        m.evaluate_candidates()

        from agent_cascade.skills.manager import _PRIORITY_SYSTEM
        reg = m._skills_registry.get(name)
        assert reg is not None and reg['_priority'] == _PRIORITY_SYSTEM, (
            'candidate must promote after the incumbent is re-activated')
        assert reg['version'] == '2.0.0'
        assert not (m._candidates_dir / name).exists(), 'candidate dir must be deleted on promote'


class TestSkillInvalidationScanMarker:
    """scan_skills no-query listing marks status=inactive skills with " (inactive)"."""

    @pytest.fixture(autouse=True)
    def _marker_tool(self, tmp_path):
        from unittest.mock import MagicMock

        from agent_cascade.tools.custom.scan_skills import ScanSkills

        manager = SkillManager()
        manager._metrics_file = tmp_path / 'skills-metrics.json'
        with manager._metrics_lock:
            manager._metrics = {}
        # Deterministic registry (bypasses disk discovery) + one inactive skill.
        for name in ('alpha', 'bravo'):
            manager._skills_registry[name] = {
                'name': name,
                'description': f'desc {name}',
                'source': 'system',
                'version': '1.0.2',
            }
        # A status=inactive skill is also in _disabled_names (that is what makes the
        # all=False filter hide it — see scan_skills L72-77).
        manager._metrics['bravo'] = {'total_loads': 0, 'by_version': {}, 'status': 'inactive'}
        manager._disabled_names.add('bravo')
        pool = MagicMock()
        pool.skill_manager = manager
        self.manager = manager
        self.tool = ScanSkills(agent_pool=pool)

    def test_no_query_marks_inactive_skills(self):
        out = self.tool.call({'query': '', 'all': True})
        lines = [l for l in out.splitlines() if l.startswith('- **')]
        bravo_line = next(l for l in lines if '**bravo**' in l)
        alpha_line = next(l for l in lines if '**alpha**' in l)
        assert ' (inactive)' in bravo_line
        assert ' (inactive)' not in alpha_line

    def test_default_filter_hides_inactive_skills(self):
        """all=False already hides disabled/inactive skills via the disabled filter."""
        out = self.tool.call({'query': ''})
        lines = [l for l in out.splitlines() if l.startswith('- **')]
        names = [l.split('**')[1] for l in lines]
        assert 'bravo' not in names
        assert 'alpha' in names
