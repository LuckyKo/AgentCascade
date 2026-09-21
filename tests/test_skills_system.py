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
from agent_cascade.skills.scoring import (CLASS_BAD, CLASS_ORDINAL, CLASS_PROTECTED, CLASS_UNPROVEN,
                                          CLASS_USEFUL, CLASS_USELESS, eviction_rank_key, skill_classify,
                                          skill_score)

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
            gt = m._global_activity_turns
        assert entry == {
            'total_loads': 0,
            'by_version': {},
            'status': 'active',
            'last_used': _iso_utc((self.root / 'mig-fresh' / 'SKILL.md').stat().st_mtime),
            # D-SEED: freshly-created records get a fresh activity window (counter here is 0).
            'last_activity_turn': gt,
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


# ===========================================================================
# 9. Skill Invalidation Phase 2 — adaptive count-cap rebalance pass
# ===========================================================================

def _rebalance_manager(tmp_path, skill_names):
    """Fresh manager + hermetic one-level skills tree (all servable)."""
    m = SkillManager()
    m._metrics_file = tmp_path / 'skills-metrics.json'
    with m._metrics_lock:
        m._metrics = {}
    root = tmp_path / 'skills'
    for name in skill_names:
        _write_skill_file(root, name)
    m._cache_ttl = 0.0
    m.discover([root])
    return m


def _migrate_once(m):
    """Run the schema-1.3 migration once so a later rebalance's internal migration is a
    no-op (it only fills *missing* fields) and will not clobber statuses we set afterwards."""
    m.rebalance_active_skills(k=1.0, min_cap=len(_active_names(m)), max_cap=len(_active_names(m)))


def _age_out_all(m: 'SkillManager', age_days: float = 400.0) -> None:
    """Age out every metrics entry's activity clock so NO skill is PROTECTED (Phase C).

    Phase C makes the fair window a hard eviction floor: an entry with no explicit
    ``last_activity_turn`` gets one stamped to the current counter at migration (D-SEED), which
    keeps it PROTECTED for one window. These legacy count-cap fixtures assert cap-driven
    eviction of freshly-migrated skills, so they must first age the store past the window — via
    the wall-clock fallback (counter stays 0) with a far-past ``last_used``. Idempotent; call
    after _migrate_once / _set_metrics, before the pass under test.

    NOTE: this only works when the turns clock is frozen at 0 (fresh manager). If the counter has
    been bumped, entries whose last_activity_turn < counter age by TURNS and stay PROTECTED —
    use explicit last_activity_turn stamps in that case (see _aged_entry).
    """
    now = time.time()
    with m._metrics_lock:
        for entry in m._metrics.values():
            if isinstance(entry, dict):
                entry['last_used'] = _iso_utc(now - age_days * 86400.0)


def _disable_after_migration(m, name):
    """Disable a skill via the public API AFTER migration so the flip survives rebalance's
    internal no-op migration (rebalance re-derives status from disk first; a post-migration
    disable sets both ``_disabled_names`` and the metrics status to inactive)."""
    ok, _ = m.disable_skill(name)
    assert ok


def _set_metrics(m, mapping):
    """Replace the metrics store under the lock (exact-case keys, lowercase names).

    NOTE: rebalance_active_skills() runs the schema-1.3 migration first, which re-derives
    ``status`` from disk for standard-location skills (D-A → active) and D-SEEDs any missing
    ``last_activity_turn`` to the current counter (Phase C fair window). Use this only to set
    *ranking* fields (loads/ratings/last_used) or non-servable statuses; use
    _disable_after_migration() to set an inactive status that must survive the pass.

    Phase C: entries with no explicit ``last_activity_turn`` are stamped to the current counter
    here so a later _age_out_all() (wall-clock fallback) can age them past the fair window —
    mirroring what migration's D-SEED does on the real path.
    """
    with m._metrics_lock:
        counter = m._global_activity_turns
        m._metrics = {k: dict(v, last_activity_turn=v.get('last_activity_turn', counter))
                      for k, v in mapping.items()}


def _active_names(m):
    with m._write_lock:
        return set(m._skills_registry.keys())


def _status_of(m, name):
    with m._metrics_lock:
        entry = m._metrics.get(name) or {}
        return entry.get('status', 'active') if isinstance(entry, dict) else 'active'


class TestSkillInvalidationRebalance:
    """Phase 2: adaptive count-cap pass (target math, determinism, D-A, never-raises)."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path
        yield

    def test_rebalance_target_clamp_min_and_max(self):
        """Eviction is floored at MIN_CAP (never evict below it) and ceiling-bounded at MAX_CAP.

        min_cap is a LOWER BOUND ON EVICTION, not a force-enable floor: a tiny corpus can sit far
        below min_cap without being pruned or padded up to 20. Each metrics entry needs a real
        on-disk file (orphan prune) and the store must be at schema 1.3 before we inject qualified
        entries — so: create files, migrate once, then inject. rebalance's internal migration is
        then a no-op that won't clobber our data.
        """
        # Eviction floor: min_cap is a LOWER BOUND ON EVICTION, not a force-enable mandate. Here
        # 30 active with only 25 qualified (loads≥1) → raw=round(1.0×25)=25 ≥ min_cap(20), so
        # evict_threshold = max(min_cap, min(max_cap, raw)) = 25 (the ACTIVE count to evict DOWN TO).
        # The 5 unqualified skills (no loads, no ratings) are USELESS once past the fair window and
        # get evicted by the absolute gate; the cap budget (active_before - threshold = 30 - 25 = 5)
        # independently selects the same 5 worst-ranked. A tiny corpus can still sit far below min_cap
        # without being pruned or padded up; min_cap only stops us from evicting below it.
        # fair_window_turns=1 keeps every skill non-PROTECTED (aged past a 1-turn window).
        floor_tmp = self.tmp / 'floor'
        floor_names = [f'f{i}' for i in range(30)]
        m = _rebalance_manager(floor_tmp, floor_names)
        _migrate_once(m)
        entries = {}
        for nm in floor_names[:25]:
            # Qualified (loads≥1) → counts toward N_qualified. Rated n=2 (UNPROVEN) so the gate is silent.
            entries[nm] = {'total_loads': 1, 'by_version': {}, 'status': 'active',
                           'ratings': {'count': 2, 'sum': 3.0}}
        for nm in floor_names[25:]:
            # Unqualified (no loads, no ratings) → USELESS past the window → absolute-gate evicted.
            entries[nm] = {'total_loads': 0, 'by_version': {}, 'status': 'active'}
        _set_metrics(m, entries)
        _age_out_all(m)  # age past the fair window so the cap/gate may evict (not PROTECTED)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200, fair_window_turns=1)
        assert s['n_qualified'] == 25
        assert s['raw'] == 25
        assert s['evict_threshold'] == 25  # raw(25) ≥ min_cap → threshold = raw (clamped into [min,max])
        assert len(s['evicted']) == 5      # evict down to 25 (active_before - threshold = 30 - 25)
        assert s['target'] == 25           # post-pass active count

        # Ceiling: N_qualified=300, k=1.0 → raw=300 > max_cap(200) → evict_threshold==200 (ceiling).
        # Isolated to its own subdir so the floor sub-case's on-disk files don't leak into this
        # corpus (the shared self.tmp skills dir would otherwise inflate n_servable + metrics).
        # All UNPROVEN-rated (n=2) so only the count-cap evicts — no absolute-gate interference.
        # max_evictions_per_pass raised to 1000 so the D-SAFE cap doesn't truncate the 100 evictions
        # this sub-case is meant to exercise (the default of 25 would bound it).
        ceiling_tmp = self.tmp / 'ceiling'
        ceiling_names = [f's{i}' for i in range(300)]
        m2 = _rebalance_manager(ceiling_tmp, ceiling_names)
        _migrate_once(m2)
        _set_metrics(m2, {nm: {'total_loads': 1, 'by_version': {}, 'status': 'active',
                               'ratings': {'count': 2, 'sum': 3.0}} for nm in ceiling_names})
        _age_out_all(m2)
        s2 = m2.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200, max_evictions_per_pass=1000)
        assert s2['n_qualified'] == 300
        assert s2['raw'] == 300
        assert s2['evict_threshold'] == 200  # ceiling-bounded at max_cap
        assert len(s2['evicted']) == 100     # evict down to 200 (300 - 200)
        assert s2['target'] == 200

    def test_rebalance_evicts_over_cap(self):
        """Over-cap: evict exactly (active - threshold) lowest-ranked; highest-scored survive.

        Phase C: 40 active with only 30 qualified (loads≥1) → raw=round(1.0×30)=30,
        evict_threshold=max(min_cap,min(max_cap,raw))=30 (the ACTIVE count to evict DOWN TO). The
        10 unqualified skills (no loads, no ratings) are USELESS past the fair window and get
        evicted by the absolute gate; the cap budget (active_before - threshold = 40 - 30 = 10)
        independently selects the same 10 worst-ranked. All 30 qualified survive.
        fair_window_turns=1 keeps every skill non-PROTECTED (aged past a 1-turn window).
        """
        names = [f's{i:02d}' for i in range(40)]
        m = _rebalance_manager(self.tmp, names)
        # 30 qualified (survive), 10 unqualified (evicted). N_qualified=30.
        entries = {}
        for i in range(30):
            entries[names[i]] = {'total_loads': 3, 'by_version': {}, 'status': 'active',
                                 'ratings': {'count': 2, 'sum': 18.0}}  # avg 9.0 → q̂≈6.14, S>0
        for i in range(30, 40):
            entries[names[i]] = {'total_loads': 0, 'by_version': {}, 'status': 'active'}  # USELESS
        _set_metrics(m, entries)
        _age_out_all(m)

        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200, fair_window_turns=1)
        assert s['n_qualified'] == 30
        # raw=30 ≥ min_cap(20) → evict_threshold = 30 (the ACTIVE count to evict DOWN TO).
        # Items removed = active_before - threshold = 40 - 30 = 10: the 10 unqualified (USELESS, worst).
        assert s['evict_threshold'] == 30
        assert s['active_before'] == 40
        assert len(s['evicted']) == 10  # evict the 10 lowest-ranked (unqualified) down to threshold 30
        # The qualified (higher-scored) skills all survive.
        for nm in names[:30]:
            assert _status_of(m, nm) == 'active'
        for nm in s['evicted']:
            assert _status_of(m, nm) == 'inactive'
        # Exactly 30 active remain (the qualified corpus the cap evicts down to).
        assert len(_active_names(m)) == 30

    def test_rebalance_reenables_under_cap(self):
        """Under-cap: re-enable highest-ranked servable inactive skills up to target."""
        names = ['a', 'b', 'c', 'd', 'e']
        m = _rebalance_manager(self.tmp, names)  # all 5 files exist → d/e not pruned as orphans
        _migrate_once(m)  # schema 1.3 so rebalance's internal migration is a no-op

        # Set ranking fields (loads/ratings). All 5 are qualified → N_qualified=5 → target=min_cap=5.
        # Phase C: every skill is rated n=2 (UNPROVEN — out of the absolute gate) so nothing is
        # evicted by the quality gate; only the cap budget and re-enable logic run.
        entries = {
            'a': {'total_loads': 1, 'by_version': {}, 'status': 'active', 'ratings': {'count': 2, 'sum': 3.0}},
            'b': {'total_loads': 1, 'by_version': {}, 'status': 'active', 'ratings': {'count': 2, 'sum': 3.0}},
            'c': {'total_loads': 1, 'by_version': {}, 'status': 'active', 'ratings': {'count': 2, 'sum': 3.0}},
            'd': {'total_loads': 2, 'by_version': {}, 'status': 'active', 'ratings': {'count': 2, 'sum': 18.0}},
            'e': {'total_loads': 5, 'by_version': {}, 'status': 'active', 'ratings': {'count': 2, 'sum': 3.0}},
        }
        _set_metrics(m, entries)
        _age_out_all(m)
        # Deactivate d and e via the public API (post-migration → survives rebalance's no-op
        # migration). They are now inactive servable re-enable candidates.
        _disable_after_migration(m, 'd')
        _disable_after_migration(m, 'e')

        s = m.rebalance_active_skills(k=1.0, min_cap=5, max_cap=200)
        assert s['target'] == 5
        assert s['active_before'] == 3
        # Re-enable the best inactive (d: rated 9.0) then next (e) to reach target 5.
        assert set(s['reenabled']) == {'d', 'e'}
        assert _status_of(m, 'd') == 'active'
        assert _status_of(m, 'e') == 'active'
        assert len(_active_names(m)) == 5

    def test_rebalance_min_cap_floor(self):
        """min_cap is a LOWER BOUND ON EVICTION, not a force-enable floor.

        With a tiny corpus (1 active, N_qualified=1 → raw=1 < min_cap=20), the pass must NOT
        pad the active count up to 20 — there are no inactive candidates anyway, so nothing is
        re-enabled. min_cap only matters when evicting: it stops us from pruning below it.
        """
        m = _rebalance_manager(self.tmp, ['a'])
        # Phase C: rated n=2 (UNPROVEN — out of the absolute gate) so nothing is evicted.
        _set_metrics(m, {'a': {'total_loads': 1, 'by_version': {}, 'status': 'active',
                               'ratings': {'count': 2, 'sum': 3.0}}})
        _age_out_all(m)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['n_qualified'] == 1
        assert s['raw'] == 1
        # Eviction is floored at min_cap (never evict below 20), but re-enable is bounded by the
        # servable corpus (n_servable=1) and raw(1) — so we never force-enable up to 20.
        assert s['evict_threshold'] == 20
        assert s['reenable_target'] == 1
        assert s['n_servable'] == 1
        assert s['evicted'] == [] and s['reenabled'] == []
        assert s['target'] == 1  # post-pass active count stays at the real corpus size

    def test_rebalance_tiny_corpus_not_forced_to_min_cap(self):
        """Regression: a 2-skill corpus must NOT be padded to min_cap=20 nor pruned.

        This directly encodes "min_cap is a lower bound on eviction, not a force-enable cap":
        with only 2 active skills and no inactive candidates, the pass must be a complete no-op
        even though min_cap(20) >> corpus size. The OLD floor-as-mandate math would have tried to
        re-enable up to 20 (impossible here, but it also set target=20); the new math leaves the
        active count exactly where it is.
        """
        m = _rebalance_manager(self.tmp, ['a', 'b'])
        _migrate_once(m)
        # Phase C: rated n=2 (UNPROVEN — out of the absolute gate) so nothing is evicted.
        entries = {nm: {'total_loads': 1, 'by_version': {}, 'status': 'active',
                        'ratings': {'count': 2, 'sum': 3.0}} for nm in ('a', 'b')}
        _set_metrics(m, entries)
        _age_out_all(m)

        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['n_qualified'] == 2
        assert s['raw'] == 2
        # evict_threshold floored at min_cap(20), but active_before(2) < 20 → nothing evicted.
        assert s['evict_threshold'] == 20
        # reenable_target bounded by the servable corpus (2) and raw(2) → never 20.
        assert s['reenable_target'] == 2
        assert s['n_servable'] == 2
        assert s['evicted'] == []      # no eviction (2 < evict_threshold=20)
        assert s['reenabled'] == []    # no force-enable up to min_cap
        assert s['target'] == 2        # post-pass active count stays at the real corpus size

    def test_rebalance_full_corpus_no_ratchet(self):
        """Deactivating a batch must not shrink N_qualified → target stays stable."""
        names = [f's{i:02d}' for i in range(30)]
        m = _rebalance_manager(self.tmp, names)
        # Phase C: rated n=2 (UNPROVEN — out of the absolute gate) so only the cap evicts.
        entries = {nm: {'total_loads': 1, 'by_version': {}, 'status': 'active',
                        'ratings': {'count': 2, 'sum': 3.0}} for nm in names}
        _set_metrics(m, entries)
        _age_out_all(m)

        s1 = m.rebalance_active_skills(k=0.5, min_cap=20, max_cap=200)
        # N_qualified=30, k=0.5 → raw=15. Eviction is floored at min_cap(20): we evict down to 20
        # (not to raw=15), so the corpus never drops below min_cap. The 10 just-evicted skills are
        # inactive+servable re-enable candidates, but remaining_after_evict(20) is already >=
        # reenable_target(min(max_cap,raw,n_servable)=15), so nothing is re-enabled — the pass
        # settles at the eviction floor of 20.
        assert s1['n_qualified'] == 30
        assert s1['raw'] == 15
        assert s1['evict_threshold'] == 20
        assert len(s1['evicted']) == 10
        assert s1['reenabled'] == []
        assert s1['target'] == 20

        # Second run on the same store: inactive skills still count in N_qualified (no ratchet),
        # and the pass is a no-op (already at 20, which equals evict_threshold → nothing to do).
        s2 = m.rebalance_active_skills(k=0.5, min_cap=20, max_cap=200)
        assert s2['n_qualified'] == 30, 'N_qualified must not ratchet down after eviction'
        assert s2['evicted'] == [] and s2['reenabled'] == []
        assert s2['target'] == s1['target'] == 20

    def test_rebalance_deterministic_ordering(self):
        """Ties (equal rating/loads/last_used) resolve by name; identical on re-run.

        40 fully-tied active skills, k=0.5 → N_qualified=40, target=round(0.5×40)=20 → evict 20.
        Since the rank key is a complete tie except for name, the 20 lexicographically smallest
        names are evicted (worst-first ascending). A second identical run must produce the same set.
        """
        names = [f's{i:02d}' for i in range(40)]
        m = _rebalance_manager(self.tmp, names)  # all 40 files exist → no orphan pruning
        _migrate_once(m)  # schema 1.3 so rebalance's internal migration is a no-op
        # Phase C: rated n=2 (UNPROVEN — out of the absolute gate) so only the cap evicts.
        entries = {nm: {'total_loads': 5, 'by_version': {}, 'status': 'active',
                        'last_used': '2026-01-01T00:00:00+00:00',
                        'ratings': {'count': 2, 'sum': 3.0}} for nm in names}
        _set_metrics(m, entries)
        # last_used is far past (2026-01-01) → wall-clock A > fair window even if the turns clock
        # is 0 → nothing is PROTECTED; all 40 are cap candidates.
        s1 = m.rebalance_active_skills(k=0.5, min_cap=20, max_cap=200)
        assert s1['n_qualified'] == 40
        assert s1['target'] == 20
        # Worst-first ascending by name → evict the 20 lexicographically smallest names.
        expected_evicted = sorted(names)[:20]
        assert s1['evicted'] == expected_evicted

        # Re-run on an identical fresh store must yield the exact same evicted set.
        m2 = _rebalance_manager(self.tmp, names)
        _migrate_once(m2)
        _set_metrics(m2, entries)
        # Same far-past last_used as the first run → same (non-PROTECTED) classification.
        s2 = m2.rebalance_active_skills(k=0.5, min_cap=20, max_cap=200)
        assert s2['evicted'] == s1['evicted']

    def test_unrated_ranks_before_rated(self):
        """An unrated skill (-1.0) is evicted before any rated one, even a 0.0-rated one."""
        m = _rebalance_manager(self.tmp, ['unrated', 'zero'])
        # N_qualified=2 → target=min_cap=1. Only one slot: the rated (0.0) survives.
        entries = {
            'unrated': {'total_loads': 0, 'by_version': {}, 'status': 'active'},  # no ratings → USELESS (abs gate)
            'zero': {'total_loads': 0, 'by_version': {}, 'status': 'active',
                     'ratings': {'count': 2, 'sum': 3.0}},  # n=2 avg 1.5 → UNPROVEN (cap-only)
        }
        _set_metrics(m, entries)
        _age_out_all(m)

        s = m.rebalance_active_skills(k=1.0, min_cap=1, max_cap=200)
        assert s['target'] == 1
        assert 'unrated' in s['evicted']
        assert 'zero' not in s['evicted']
        assert _status_of(m, 'unrated') == 'inactive'
        assert _status_of(m, 'zero') == 'active'

    def test_da_non_servable_counts_in_qualified_but_not_reenabled(self):
        """D-A: an INACTIVE/-located skill counts in N_qualified but is never auto-re-enabled."""
        m = SkillManager()
        m._metrics_file = self.tmp / 'skills-metrics.json'
        with m._metrics_lock:
            m._metrics = {}
        root = self.tmp / 'skills'
        _write_skill_file(root, 'servable-a')                 # one-level → servable
        _write_skill_file(root, 'retired-b', subdir='INACTIVE')  # deeper → non-servable
        m._cache_ttl = 0.0
        m.discover([root])

        # Phase C: servable-a rated n=2 (UNPROVEN — out of the absolute gate) so it isn't evicted.
        entries = {
            'servable-a': {'total_loads': 1, 'by_version': {}, 'status': 'active',
                           'ratings': {'count': 2, 'sum': 3.0}},
            'retired-b': {'total_loads': 1, 'by_version': {}, 'status': 'inactive'},
        }
        _set_metrics(m, entries)
        _age_out_all(m)

        s = m.rebalance_active_skills(k=1.0, min_cap=2, max_cap=200)
        # N_qualified counts BOTH (full corpus, incl. the non-servable retired-b) → 2 → raw=2.
        assert s['n_qualified'] == 2
        assert s['raw'] == 2
        # Only servable-a is at a servable location (n_servable=1); reenable_target is bounded by
        # n_servable so the pass can never push the active count above what's actually servable.
        assert s['n_servable'] == 1
        assert s['reenable_target'] == 1
        # The non-servable retired-b is excluded from re-enable candidates (needs a file move), so
        # the post-pass active count is just servable-a → target=1.
        assert s['target'] == 1
        assert 'retired-b' not in s['reenabled']
        assert _status_of(m, 'retired-b') == 'inactive'

    def test_env_disabled_never_reenabled(self):
        """SKILLS_DISABLED (env) skills are never auto-re-enabled, even as top candidates."""
        m = _rebalance_manager(self.tmp, ['a', 'b'])
        _migrate_once(m)
        # Both qualified + active; then disable 'b' via the public API.
        entries = {
            'a': {'total_loads': 3, 'by_version': {}, 'status': 'active'},
            'b': {'total_loads': 9, 'by_version': {}, 'status': 'active'},  # top-ranked
        }
        _set_metrics(m, entries)
        _disable_after_migration(m, 'b')

        # Monkeypatch the module-level SKILLS_DISABLED so rebalance treats 'b' as env-disabled.
        import agent_cascade.skills.manager as mgr_mod
        original = list(mgr_mod.SKILLS_DISABLED)
        try:
            mgr_mod.SKILLS_DISABLED = ['b']
            s = m.rebalance_active_skills(k=1.0, min_cap=2, max_cap=200)
        finally:
            mgr_mod.SKILLS_DISABLED = original

        # 'b' is the only inactive candidate but is env-disabled → never re-enabled.
        assert 'b' not in s['reenabled']
        assert _status_of(m, 'b') == 'inactive'

    def test_rebalance_never_raises(self):
        """Any internal failure is swallowed: rebalance returns a dict and never raises."""
        m = _rebalance_manager(self.tmp, ['a'])
        # Force an internal step to blow up.
        m._migrate_metrics_to_v13 = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        result = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)  # must not raise
        assert isinstance(result, dict)
        assert 'target' in result and 'evicted' in result

    def test_rebalance_disabled_flag_noop(self):
        """When nothing to evict/re-enable (already at target), the pass is a no-op."""
        m = _rebalance_manager(self.tmp, ['a'])
        _set_metrics(m, {'a': {'total_loads': 1, 'by_version': {}, 'status': 'active'}})
        # min_cap=1, N_qualified=1 → target=1 == active count. No flips.
        s = m.rebalance_active_skills(k=1.0, min_cap=1, max_cap=200)
        assert s['evicted'] == [] and s['reenabled'] == []
        assert _status_of(m, 'a') == 'active'

    def test_compute_rebalance_preview_matches_and_is_side_effect_free(self):
        """compute_rebalance_preview returns the SAME threshold math as a real rebalance pass AND
        mutates nothing (no status flips, no _disabled_names change, no metrics flush to disk)."""
        names = [f's{i:02d}' for i in range(30)]
        m = _rebalance_manager(self.tmp, names)
        # 15 rated high (qualified), 15 unrated → N_qualified=15, raw=round(1.0*15)=15 < min_cap(20).
        entries = {}
        for i in range(15):
            entries[names[i]] = {'total_loads': 3, 'by_version': {}, 'status': 'active',
                                 'ratings': {'count': 2, 'sum': 18.0}}  # avg 9.0
        for i in range(15, 30):
            entries[names[i]] = {'total_loads': 0, 'by_version': {}, 'status': 'active'}
        _set_metrics(m, entries)
        _age_out_all(m)

        # Snapshot state BEFORE the preview so we can prove it is side-effect-free.
        with m._metrics_lock:
            metrics_before = _copy.deepcopy(m._metrics)
        disabled_before = set(m._disabled_names)
        store_file = m._metrics_file
        store_before = store_file.read_text(encoding='utf-8') if store_file.exists() else None

        # READ-ONLY preview (no migration, no mutation).
        p = m.compute_rebalance_preview(k=1.0, min_cap=20, max_cap=200)
        assert p['ok'] is True and 'error' not in p
        assert p['n_qualified'] == 15
        assert p['raw'] == 15
        assert p['evict_threshold'] == 20   # floored at min_cap (not raw=15)
        assert p['reenable_target'] == 15   # min(max_cap, raw, n_servable)=min(200,15,30)=15
        assert p['n_servable'] == 30
        assert p['active_count'] == 30      # all 30 start active

        # Prove the preview left EVERYTHING untouched (the core "read-only" guarantee).
        with m._metrics_lock:
            metrics_after = _copy.deepcopy(m._metrics)
        assert metrics_after == metrics_before          # no status flip, no counter change
        assert set(m._disabled_names) == disabled_before  # no disable/enable side effect
        store_after = store_file.read_text(encoding='utf-8') if store_file.exists() else None
        assert store_after == store_before              # no flush to disk

        # Now run a REAL pass on an identical fresh manager and confirm the preview's numbers
        # match exactly what rebalance_active_skills would compute (the "always matches" guarantee).
        m2 = _rebalance_manager(self.tmp / 'real', names)
        entries2 = {}
        for i in range(15):
            entries2[names[i]] = {'total_loads': 3, 'by_version': {}, 'status': 'active',
                                  'ratings': {'count': 2, 'sum': 18.0}}
        for i in range(15, 30):
            entries2[names[i]] = {'total_loads': 0, 'by_version': {}, 'status': 'active'}
        _set_metrics(m2, entries2)
        _age_out_all(m2)
        s = m2.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['n_qualified'] == p['n_qualified']
        assert s['raw'] == p['raw']
        assert s['evict_threshold'] == p['evict_threshold']
        assert s['reenable_target'] == p['reenable_target']
        assert s['n_servable'] == p['n_servable']
        assert s['active_before'] == p['active_count']

    def test_compute_rebalance_preview_never_raises(self):
        """Any internal failure is swallowed: preview returns a dict with ok=False and never raises."""
        m = _rebalance_manager(self.tmp, ['a'])
        # Force the servable walk (the one I/O step) to blow up.
        m._servable_skill_names = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        p = m.compute_rebalance_preview(k=1.0, min_cap=20, max_cap=200)  # must not raise
        assert isinstance(p, dict)
        assert p['ok'] is False
        assert 'error' in p


# ===========================================================================
# Phase B — Score + Classify as PURE functions (research §5/§7/§8/§17)
# ===========================================================================

class TestSkillScoringPure:
    """skill_score / skill_classify / eviction_rank_key are pure, side-effect-free, and reproduce
    the research-doc worked numbers (§8) exactly. No fixtures needed — these take plain scalars."""

    # ------------------------------------------------------------------ score
    def test_score_reproduces_research_section8(self):
        """Regression anchor: every §8 worked case reproduces q̂, S (and class) to ~3 decimals."""
        # (name, n, avg, L, A) — A chosen so the documented class holds (fresh for case1, aged for the rest).
        cases = [
            ('brand-new',           1, 5.0,   1, 0),    # q̂≈5.000  S≈0.0588  PROTECTED
            ('perfect-rare',        2, 10.0,  2, 60),   # q̂≈6.429  S≈0.1422  UNPROVEN
            ('popular-bad',         30, 3.0,  30, 60),  # q̂≈3.286  S≈0.3208  BAD
            ('loaded-never-rated',  0, None,  50, 60),  # q̂=5.000  S==0.0    USELESS
            ('high-count-nearbase', 40, 5.1,  40, 60),  # q̂≈5.089  S≈0.5055  USELESS
            ('good-rare',           5, 8.0,   5, 60),   # q̂==6.5    S≈0.3021  USEFUL
            ('clearly-bad-low-n',   3, 2.0,   3, 60),   # q̂≈3.875  S≈0.1212  UNPROVEN
        ]
        expected = {
            'brand-new':           (5.000, 0.0588, CLASS_PROTECTED),
            'perfect-rare':        (6.429, 0.1422, CLASS_UNPROVEN),
            'popular-bad':         (3.286, 0.3208, CLASS_BAD),
            'loaded-never-rated':  (5.000, 0.0000, CLASS_USELESS),
            'high-count-nearbase': (5.089, 0.5055, CLASS_USELESS),
            'good-rare':           (6.500, 0.3021, CLASS_USEFUL),
            'clearly-bad-low-n':   (3.875, 0.1212, CLASS_UNPROVEN),
        }
        for name, n, avg, L, A in cases:
            sc = skill_score(n, avg, L, A)
            cls = skill_classify(n, avg, A)
            eq, es, ec = expected[name]
            assert round(sc['qhat'], 3) == eq, f"{name}: q̂ {sc['qhat']} != {eq}"
            assert round(sc['S'], 4) == es, f"{name}: S {sc['S']} != {es}"
            assert cls == ec, f"{name}: class {cls} != {ec}"

    def test_neutral_point_is_half(self):
        """Fully used (u→1), no waste (L<=n), fresh (A=0), q̂≈5 ⇒ S ≈ 0.5 (neutral midpoint)."""
        sc = skill_score(200, 5.0, 0, 0)   # u≈1-4e-11, w=1, r=1, q̂=(1000+25)/205≈4.976→S≈0.498
        assert abs(sc['S'] - 0.5) < 0.01
        # Exact neutral: pick n so q̂ is exactly 5 (avg=5) and u is effectively 1.
        sc2 = skill_score(1000, 5.0, 0, 0)
        assert abs(sc2['S'] - 0.5) < 1e-6

    def test_brand_new_low_score_and_loaded_never_zero(self):
        """Brand-new (n=1,avg=5,L=1,A=0) → low score; loaded-never-rated (n=0,L=50) → S==0.0."""
        assert 0.0 <= skill_score(1, 5.0, 1, 0)['score'] < 0.1
        assert skill_score(0, None, 50, 60)['S'] == 0.0   # u=0 ⇒ core is exactly zero

    def test_monotonic_in_n_for_fixed_quality(self):
        """For fixed quality (avg=5) and no waste, S is non-decreasing in n (usage drives the core)."""
        prev = -1.0
        for n in range(0, 61):
            s = skill_score(n, 5.0, n, 0)['S']
            assert s >= prev - 1e-12, f"S decreased at n={n}"
            prev = s

    def test_score_bounded_0_to_1(self):
        """Random grid over the full input space ⇒ score ∈ [0,1] and r_floor <= r <= 1."""
        import random
        rng = random.Random(20260921)
        for _ in range(5000):
            n = rng.randint(0, 60)
            avg = round(rng.uniform(0.0, 10.0), 1) if n > 0 else None
            L = rng.randint(0, 80)
            A = rng.randint(0, 1000)
            sc = skill_score(n, avg, L, A)
            assert 0.0 <= sc['score'] <= 1.0
            assert 0.5 <= sc['r'] <= 1.0          # r_floor=0.5 default
            assert 0.0 <= sc['u'] <= 1.0
            assert 0.0 < sc['w'] <= 1.0           # w>0 (exp never hits 0), ==1 when L<=n

    def test_recency_is_minor_tiebreaker(self):
        """r ∈ [0.5,1] only reduces the score; identical S with different A keeps class stable (§8 note)."""
        fresh = skill_score(1, 5.0, 1, 10)
        idle = skill_score(1, 5.0, 1, 400)
        assert fresh['S'] == idle['S']                       # core is time-stable
        assert fresh['score'] > idle['score']                # recency lowers the stale one
        assert fresh['score'] / idle['score'] <= (1.0 / 0.5) + 1e-9  # bounded by r_floor

    def test_pure_no_side_effects(self):
        """Repeated calls are deterministic and read only their arguments (no shared/global state)."""
        for _ in range(3):
            a = skill_score(7, 4.0, 9, 30)
            b = skill_classify(7, 4.0, 30)
            assert a == {'score': a['score'], 'S': a['S'], 'qhat': a['qhat'], 'u': a['u'], 'w': a['w'], 'r': a['r']}
            assert b in (CLASS_BAD, CLASS_PROTECTED, CLASS_USEFUL, CLASS_USELESS, CLASS_UNPROVEN)
        # Same inputs → byte-identical outputs across calls.
        assert skill_score(7, 4.0, 9, 30) == skill_score(7, 4.0, 9, 30)
        assert skill_classify(7, 4.0, 30) == skill_classify(7, 4.0, 30)

    # -------------------------------------------------------------- classify
    def test_each_class_hit_by_concrete_input(self):
        """Each of the 5 classes is reachable by a concrete (n, avg, A)."""
        assert skill_classify(1, 5.0, 0) == CLASS_PROTECTED       # fresh, no signal
        assert skill_classify(30, 3.0, 60) == CLASS_BAD           # high-n clearly-bad
        assert skill_classify(5, 8.0, 60) == CLASS_USEFUL         # q̂=6.5≥5.5 & n≥5
        assert skill_classify(1, 5.0, 60) == CLASS_USELESS        # neutral past window
        assert skill_classify(3, 2.0, 60) == CLASS_UNPROVEN       # bad but n<5

    def test_classify_precedence_bad_over_protected(self):
        """A young-but-proven-harmful skill (n≥5, q̂≤4.5, A<window) is BAD, not PROTECTED."""
        assert skill_classify(10, 3.0, 0) == CLASS_BAD            # A=0 < 50 but still BAD
        # Brand-new (n=1) can never be BAD — it fails n>=n_min regardless of quality/age.
        assert skill_classify(1, 1.0, 0) == CLASS_PROTECTED

    def test_useless_covers_low_and_high_count_neutral(self):
        """Both low-count-neutral and high-count-near-baseline are USELESS once past the window."""
        assert skill_classify(1, 5.0, 60) == CLASS_USELESS        # canonical (n=1, r=5) aged out
        assert skill_classify(40, 5.1, 60) == CLASS_USELESS       # confidently neutral

    def test_canonical_useless_baseline_window_flip(self):
        """The same (n=1, q̂=5) state is PROTECTED when fresh and USELESS once past the fair window."""
        assert skill_classify(1, 5.0, 20) == CLASS_PROTECTED      # A < 50
        assert skill_classify(1, 5.0, 50) == CLASS_USELESS        # A >= 50 (boundary)
        assert skill_classify(1, 5.0, 500) == CLASS_USELESS

    def test_classify_handles_none_avg_at_zero_n(self):
        """avg=None is valid when n==0 (shrinkage falls back to the pure prior q0)."""
        assert skill_classify(0, None, 60) == CLASS_USELESS       # q̂=q0=5 → neutral, aged out

    # ------------------------------------------------------- rank key ordering
    def test_rank_key_class_ordinal_ordering(self):
        """BAD(0) < USELESS(1) < UNPROVEN(2) < USEFUL(3) regardless of the score component."""
        # Give each class a deliberately "wrong" score to prove the ordinal dominates.
        keys = [
            eviction_rank_key(CLASS_BAD, 0.99, 'bad'),
            eviction_rank_key(CLASS_USELESS, 0.50, 'useless'),
            eviction_rank_key(CLASS_UNPROVEN, 0.25, 'unproven'),
            eviction_rank_key(CLASS_USEFUL, 0.01, 'useful'),
        ]
        assert keys == sorted(keys)
        # Explicit ordinal table.
        assert CLASS_ORDINAL == {CLASS_BAD: 0, CLASS_USELESS: 1, CLASS_UNPROVEN: 2, CLASS_USEFUL: 3}

    def test_rank_key_within_class_lower_score_first(self):
        """Within a single class, the lower score sorts first (score breaks intra-class ties)."""
        assert eviction_rank_key(CLASS_BAD, 0.10, 'a') < eviction_rank_key(CLASS_BAD, 0.40, 'b')
        # Name is the final stable tiebreak when scores are equal.
        assert eviction_rank_key(CLASS_USELESS, 0.3, 'alpha') < eviction_rank_key(CLASS_USELESS, 0.3, 'beta')

    def test_bad_below_useless_ordering_across_counts(self):
        """§17 guarantee: BAD ALWAYS ranks before USELESS across an n-grid (ordinal dominates)."""
        for n_bad in (5, 10, 20, 30, 40):
            bad = skill_score(n_bad, 3.0, n_bad, 0)              # high-n, clearly-bad
            assert skill_classify(n_bad, 3.0, 60) == CLASS_BAD
            for n_neut in (1, 2, 3):
                neut = skill_score(n_neut, 5.0, n_neut, 0)       # low-n, neutral
                assert skill_classify(n_neut, 5.0, 60) == CLASS_USELESS
                kb = eviction_rank_key(CLASS_BAD, bad['score'], f'bad{n_bad}')
                ku = eviction_rank_key(CLASS_USELESS, neut['score'], f'neut{n_neut}')
                assert kb < ku, f"BAD(n={n_bad}) must rank before USELESS(n={n_neut})"

    def test_section17_counterexample(self):
        """The exact §17 counterexample: S(BAD)=0.3208 > S(USELESS)=0.1106, yet BAD still ranks first."""
        bad = skill_score(30, 3.0, 30, 0)
        use = skill_score(2, 5.0, 2, 0)
        assert round(bad['S'], 4) == 0.3208
        assert round(use['S'], 4) == 0.1106
        assert bad['S'] > use['S']                              # raw score says the WRONG order...
        kb = eviction_rank_key(CLASS_BAD, bad['score'], 'bad')
        ku = eviction_rank_key(CLASS_USELESS, use['score'], 'useless')
        assert kb < ku                                          # ...but the rank key fixes it

    def test_protected_rejected_by_rank_key(self):
        """PROTECTED has no ordinal — handing it to eviction_rank_key raises KeyError by design."""
        with pytest.raises(KeyError):
            eviction_rank_key(CLASS_PROTECTED, 0.1, 'protected')


# ===========================================================================
# Phase A — Persisted Cumulative Activity-Turn Counter (research §15)
# ===========================================================================

class TestActivityClock:
    """Durable global_activity_turns + per-skill last_activity_turn + conservative age helper."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path
        yield

    def test_bump_advances_persisted_counter(self):
        """bump increments the durable counter and it survives a reload (restart persistence)."""
        m = _rebalance_manager(self.tmp, ['a'])
        for _ in range(7):
            m.bump_activity_turn()
        assert m._global_activity_turns == 7

        # Force a flush, then simulate a restart by loading the on-disk store into a fresh manager.
        m._flush_metrics_to_disk()
        store = _read_store(m._metrics_file)
        assert store['global_activity_turns'] == 7

        m2 = SkillManager()
        m2._metrics_file = m._metrics_file
        m2._load_metrics()
        assert m2._global_activity_turns == 7  # survived restart

    def test_record_rating_stamps_last_activity_turn(self):
        """A rating resets the skill's clock to the current global counter (D-ACT)."""
        m = _rebalance_manager(self.tmp, ['a'])
        for _ in range(10):
            m.bump_activity_turn()
        assert m._global_activity_turns == 10

        m.record_rating('a', 8.0)
        with m._metrics_lock:
            lat = m._metrics['a'].get('last_activity_turn')
        assert lat == 10

    def test_load_does_not_stamp_clock(self):
        """D-ACT: a load must NOT reset the activity clock (only ratings do)."""
        m = _rebalance_manager(self.tmp, ['a'])
        for _ in range(4):
            m.bump_activity_turn()
        # No rating yet → last_activity_turn is None; a load leaves it unchanged.
        m._increment_load_count('a', '1.0.0')
        with m._metrics_lock:
            lat = m._metrics['a'].get('last_activity_turn')
        assert lat is None

    def test_age_idle_break_invariance(self):
        """Idle adds zero turns (A frozen); bumping advances A by exactly the bumped count."""
        m = _rebalance_manager(self.tmp, ['a'])
        for _ in range(30):
            m.bump_activity_turn()
        m.record_rating('a', 5.0)  # A == 0 (rated at counter=30)

        now = time.time()
        with m._metrics_lock:
            entry = dict(m._metrics['a'])
            gt = m._global_activity_turns
        assert m._activity_age(entry, gt, now) == 0  # idle → frozen at 0

        for _ in range(50):
            m.bump_activity_turn()
        with m._metrics_lock:
            entry = dict(m._metrics['a'])
            gt = m._global_activity_turns
        assert m._activity_age(entry, gt, now) == 50  # +50 turns → A == 50

    def test_rollout_seed_gives_fresh_window(self):
        """D-SEED: pre-existing skills with last_activity_turn=None are seeded to the counter."""
        m = _rebalance_manager(self.tmp, ['a'])
        for _ in range(12):
            m.bump_activity_turn()
        # Pre-existing skill record with no activity stamp yet (old schema shape).
        with m._metrics_lock:
            m._metrics['a'] = {'total_loads': 3, 'by_version': {}, 'status': 'active'}

        m._migrate_metrics_to_v13()  # first-upgrade migration seeds the missing clock

        now = time.time()
        with m._metrics_lock:
            entry = dict(m._metrics['a'])
            gt = m._global_activity_turns
        assert entry.get('last_activity_turn') == 12
        assert m._activity_age(entry, gt, now) == 0  # fresh window → PROTECTED

    def test_rollout_seed_is_idempotent(self):
        """D-SEED re-run must not clobber an existing last_activity_turn."""
        m = _rebalance_manager(self.tmp, ['a'])
        for _ in range(5):
            m.bump_activity_turn()
        m.record_rating('a', 6.0)  # stamps last_activity_turn == 5
        with m._metrics_lock:
            first = m._metrics['a']['last_activity_turn']
        assert first == 5

        for _ in range(9):
            m.bump_activity_turn()  # counter now 14
        m._migrate_metrics_to_v13()  # second migration run

        with m._metrics_lock:
            lat = m._metrics['a']['last_activity_turn']
        assert lat == first  # NOT re-seeded to the new (14) counter

    def test_wallclock_fallback_when_counter_zero(self):
        """D-FALLBACK: counter==0 + past last_used → bounded wall-clock age (>0, rate-bounded)."""
        m = _rebalance_manager(self.tmp, ['a'])
        assert m._global_activity_turns == 0  # never bumped
        # last_used 2 hours in the past; no activity stamp (old-shape record).
        with m._metrics_lock:
            m._metrics['a'] = {'total_loads': 1, 'by_version': {}, 'status': 'active',
                               'last_used': _iso_utc(time.time() - 2 * 3600)}

        now = time.time()
        from agent_cascade.settings import SKILL_WALLCLOCK_SECONDS_PER_TURN
        age = m._activity_age(dict(m._metrics['a']), 0, now)
        assert age > 0  # wall-clock path engaged
        assert age <= int((2 * 3600) / SKILL_WALLCLOCK_SECONDS_PER_TURN) + 1  # bounded by rate

    def test_age_protected_when_counter_zero_and_no_last_used(self):
        """Safest case: counter==0 and no last_used → A == 0 (PROTECTED, never mass-evict)."""
        m = _rebalance_manager(self.tmp, ['a'])
        now = time.time()
        assert m._activity_age({'total_loads': 0}, 0, now) == 0

    def test_bump_no_per_turn_io_and_reaches_threshold(self):
        """A single sub-threshold bump does no disk I/O; the counter still advances in-memory.

        The "never breaks the loop" guarantee lives at the engine hook (try/except around
        ``sm.bump_activity_turn()``), not inside the manager — _flush_metrics_to_disk already
        swallows its own I/O errors internally, so we verify the no-hot-path contract here.
        """
        m = _rebalance_manager(self.tmp, ['a'])
        assert not m._metrics_file.exists()  # fresh store
        for i in range(1, 5):  # below threshold (4 < 5) → no flush, no file written
            m.bump_activity_turn()
        assert not m._metrics_file.exists()  # no per-turn I/O
        assert m._global_activity_turns == 4

        m.bump_activity_turn()  # 5th bump hits threshold → flushes (writes the file)
        assert m._global_activity_turns == 5
        assert m._metrics_file.exists()  # batched flush fired exactly at threshold


# ===========================================================================
# Phase D — Soft-Eviction Loadability Prerequisite (research §16 / plan §5)
# ===========================================================================

class TestSoftEvictionLoadability:
    """An evicted (registry-removed, inactive) skill stays loadable via the servable-disk
    fallback in ``load_full_instructions`` — without being re-added to the active registry."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path
        yield

    def _evicted_manager(self, skill_names):
        """Hermetic manager whose named skills are all SOFT-EVICTED: on disk at a servable
        location but excluded from the active registry (``_disabled_names`` + status=inactive),
        exactly as ``disable_skill`` leaves them after discovery filters them out."""
        m = _rebalance_manager(self.tmp, skill_names)  # discover → all in registry
        for name in skill_names:
            ok, _ = m.disable_skill(name)  # adds to _disabled_names + status=inactive
            assert ok
            m._cache_ttl = 0.0
            m.discover([m._skill_paths[0]])  # re-scan → evicted skills dropped from registry
        return m

    def test_evicted_skill_still_loadable_from_disk(self):
        """A soft-evicted skill (absent from registry, present in servable corpus) loads."""
        m = self._evicted_manager(['bravo'])
        with m._write_lock:
            assert 'bravo' not in m._skills_registry  # confirms it is actually evicted
        body = m.load_full_instructions('bravo')
        assert body is not None
        assert '# Body' in body

    def test_evicted_skill_counts_a_load(self):
        """Loading an evicted skill with count_load=True increments total_loads (plan §5.3)."""
        m = self._evicted_manager(['charlie'])
        with m._metrics_lock:
            before = (m._metrics.get('charlie') or {}).get('total_loads', 0)
        body = m.load_full_instructions('charlie', count_load=True)
        assert body is not None
        with m._metrics_lock:
            after = (m._metrics.get('charlie') or {}).get('total_loads', 0)
        assert after == before + 1

    def test_evicted_skill_not_readded_to_registry(self):
        """The fallback only loads instructions — it must NOT re-add the skill to the registry."""
        m = self._evicted_manager(['delta'])
        body = m.load_full_instructions('delta')
        assert body is not None
        with m._write_lock:
            assert 'delta' not in m._skills_registry  # still absent after load
        assert 'delta' in m._disabled_names  # still disabled
        assert _status_of(m, 'delta') == 'inactive'  # status unchanged

    def test_unknown_skill_still_returns_none(self):
        """A name that is neither in the registry nor in the servable corpus → None (no over-broadening)."""
        m = self._evicted_manager(['echo'])
        assert m.load_full_instructions('does-not-exist-anywhere') is None

    def test_case_insensitive_evicted_fallback(self):
        """The disk fallback matches case-insensitively, like the registry path."""
        m = self._evicted_manager(['Foxtrot'])
        body = m.load_full_instructions('fOxTrot')  # mixed case, not in registry
        assert body is not None
        assert '# Body' in body

    def test_non_servable_evicted_skill_returns_none(self):
        """A skill only reachable deeper (e.g. under INACTIVE/) is NOT servable → still None."""
        m = _rebalance_manager(self.tmp, ['golf'])
        root = m._skill_paths[0]
        # Move golf out of the one-level servable location into a deeper subfolder.
        (root / 'INACTIVE' / 'golf').mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.move(str(root / 'golf'), str(root / 'INACTIVE' / 'golf'))
        m._cache_ttl = 0.0
        m.discover([root])  # now golf is not discovered at all (deeper than one level)
        with m._write_lock:
            assert 'golf' not in m._skills_registry
        assert 'golf' not in m._servable_skill_names()  # deeper → not servable
        assert m.load_full_instructions('golf') is None  # no over-broadening to deep locations


# ===========================================================================
# Phase C — OR-composition + class-ordinal eviction ordering + PROTECTED exclusion
#             + D-SAFE safety cap + read-only per-skill preview parity
# ===========================================================================

def _aged_entry(n: int, avg, L: int = None, last_activity_turn: int = 0, age_days: float = 400.0):
    """Build a schema-1.3 metrics entry with an explicit activity clock stamp.

    ``last_activity_turn=0`` + counter advanced past the fair window ⇒ A ≥ window (aged out);
    pass ``last_activity_turn=<counter>`` for PROTECTED (A < window). ``last_used`` is set far in
    the past so that EVEN IF the turns clock is unavailable (counter == 0 → wall-clock fallback),
    A still exceeds the fair window and the entry is NOT PROTECTED — the fixtures stay robust to
    either clock path.
    """
    ratings = {'count': n, 'sum': round(avg * n, 4), 'latest': avg, 'last_version': ''} if n else None
    return {
        'total_loads': L if L is not None else n,
        'by_version': {},
        'status': 'active',
        'ratings': ratings,
        'last_used': _iso_utc(time.time() - age_days * 86400.0),
        'last_activity_turn': last_activity_turn,
    }


def _bump_to(m, n: int) -> None:
    """Advance the durable activity counter to exactly ``n`` (from its current value)."""
    for _ in range(n - m._global_activity_turns):
        m.bump_activity_turn()


class TestSkillScoringRebalance:
    """Phase C: OR-composition, PROTECTED exclusion, class-ordinal ordering, D-SAFE cap, preview parity."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path
        yield

    # ------------------------------------------------------------------ OR-composition
    def test_bad_evicted_under_cap_headroom(self):
        """§9 gap fix: at K=1.0 the cap evicts NOTHING (active ≤ threshold), yet a BAD skill is
        still removed by the absolute gate."""
        names = ['bad', 'good']
        m = _rebalance_manager(self.tmp, names)
        _bump_to(m, 200)  # counter=200 ⇒ last_activity_turn=0 → A=200 ≥ fair_window(50)
        entries = {
            'bad': _aged_entry(10, 2.0),   # q̂=(20+25)/15≈3.33 ≤ 4.5, n≥5 → BAD (even if young)
            'good': _aged_entry(5, 8.0),   # q̂=6.5 ≥ 5.5, n≥5 → USEFUL
        }
        _set_metrics(m, entries)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['n_qualified'] == 2 and s['raw'] == 2
        assert s['evict_threshold'] == 20          # min_cap floor ⇒ cap budget = 0
        assert s['evicted'] == ['bad']             # absolute gate, not the cap
        assert s['evicted_absolute_gate'] == ['bad']
        assert s['evicted_cap_only'] == []
        assert _status_of(m, 'bad') == 'inactive'
        assert _status_of(m, 'good') == 'active'

    def test_useless_past_window_evicted_under_headroom(self):
        """A neutral (|q̂−5|≤δ_q) skill past its fair window is USELESS → absolute-gate eviction
        even with full cap headroom."""
        m = _rebalance_manager(self.tmp, ['neutral'])
        _bump_to(m, 200)
        _set_metrics(m, {'neutral': _aged_entry(3, 5.0)})  # q̂=5.0, A=200 → USELESS (not BAD: n<5)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['evicted'] == ['neutral']
        assert s['evicted_absolute_gate'] == ['neutral']

    # ------------------------------------------------------------------ PROTECTED exclusion
    def test_protected_never_evicted_even_over_cap(self):
        """A fresh skill (A < fair_window) is immune to BOTH gates: the cap draws from
        non-PROTECTED candidates only, and the absolute gate skips it entirely."""
        names = ['fresh', 'stale1', 'stale2']
        m = _rebalance_manager(self.tmp, names)
        _bump_to(m, 100)
        entries = {
            # Aged-out neutral skills (A=100 ≥ 50) → USELESS: absolute-gate candidates.
            'stale1': _aged_entry(3, 5.0),
            'stale2': _aged_entry(3, 5.0),
            # Rated at counter=100 → A=0 < 50 → PROTECTED (neutral quality, not BAD).
            'fresh': _aged_entry(3, 5.0, last_activity_turn=100),
        }
        _set_metrics(m, entries)
        s = m.rebalance_active_skills(k=1.0, min_cap=1, max_cap=200)
        assert 'fresh' not in s['evicted']          # PROTECTED: immune to cap AND absolute gate
        assert set(s['evicted']) == {'stale1', 'stale2'}  # both USELESS-past-window evicted
        assert _status_of(m, 'fresh') == 'active'

    def test_young_but_bad_is_evicted(self):
        """BAD beats PROTECTED (precedence §7): a young skill with n≥n_min and q̂≤4.5 is evicted
        even though its fair window has not expired."""
        m = _rebalance_manager(self.tmp, ['youngbad'])
        _bump_to(m, 30)  # counter=30 < fair_window(50) ⇒ A=30 would be PROTECTED if not BAD
        _set_metrics(m, {'youngbad': _aged_entry(6, 2.0)})  # q̂=(12+25)/11≈3.36 ≤ 4.5, n≥5 → BAD
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['evicted'] == ['youngbad']
        assert s['class_counts'][CLASS_BAD] == 1

    # ------------------------------------------------------------------ ordering
    def test_bad_before_useless_ordering(self):
        """§17 counterexample: BAD(n=30, S≈0.32) outscores USELESS(n=2, S≈0.11), yet the
        class ordinal makes BAD evict FIRST in the ordered list."""
        names = ['badhi', 'uselesslo']
        m = _rebalance_manager(self.tmp, names)
        _bump_to(m, 200)
        entries = {
            'badhi': _aged_entry(30, 3.0),     # BAD, high volume → higher raw score
            'uselesslo': _aged_entry(2, 5.0),  # USELESS, low volume → lower raw score
        }
        _set_metrics(m, entries)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)
        assert s['evicted'] == ['badhi', 'uselesslo']  # BAD strictly before USELESS

    def test_max_evictions_per_pass_caps_mass_eviction(self):
        """D-SAFE: with many absolute-gate candidates and max_evictions_per_pass=2, exactly the
        top-2 by rank key are evicted (BAD first, then lowest-scored USELESS)."""
        names = [f'b{i}' for i in range(4)] + ['n1', 'n2']
        m = _rebalance_manager(self.tmp, names)
        _bump_to(m, 200)
        entries = {nm: _aged_entry(10, 2.0) for nm in names[:4]}   # 4× BAD
        entries['n1'] = _aged_entry(3, 5.0)                        # USELESS
        entries['n2'] = _aged_entry(3, 5.0)                        # USELESS
        _set_metrics(m, entries)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200, max_evictions_per_pass=2)
        assert len(s['evicted']) == 2
        assert s['evicted'][0] in ('b0', 'b1', 'b2', 'b3')         # a BAD is always first
        assert all(nm.startswith('b') for nm in s['evicted'])      # both slots go to BADs

    def test_max_evictions_per_pass_zero_disables_all_eviction(self):
        """D-SAFE rollback gate (plan §7): max_evictions_per_pass=0 disables BOTH gates — the
        absolute gate AND the cap — so a misconfigured corpus can never be mass-evicted. The
        master switch ``skill_auto_invalidate_enabled`` (pool level) is the full-off gate; this
        cap is the bounded gate."""
        names = ['bad', 'neutral']
        m = _rebalance_manager(self.tmp, names)
        _bump_to(m, 200)
        entries = {'bad': _aged_entry(10, 2.0), 'neutral': _aged_entry(3, 5.0)}
        _set_metrics(m, entries)
        s = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200, max_evictions_per_pass=0)
        assert s['evicted'] == []   # both gates bounded to zero evictions
        assert _status_of(m, 'bad') == 'active'
        assert _status_of(m, 'neutral') == 'active'

    def test_master_switch_off_is_pool_level_gate(self):
        """The master switch lives at pool/core.py (it decides whether to LAUNCH the rebalance
        thread at all), not inside the manager — so "switch off" = the pass never runs. This
        documents the gate location: the manager's absolute gate cannot be toggled independently;
        max_evictions_per_pass=0 is the bounded alternative (see sibling test)."""
        # The pool reads `skill_auto_invalidate_enabled` from llm_cfg before launching the thread
        # (pool/core.py); when False, rebalance_active_skills is never called. We assert the
        # branch exists in the source so a refactor that moves/removes the gate is caught here.
        import agent_cascade.pool.core as core_mod
        src = Path(core_mod.__file__).read_text(encoding='utf-8')
        assert 'skill_auto_invalidate_enabled' in src

    # ------------------------------------------------------------------ never-raises
    def test_rebalance_never_raises_with_scoring(self):
        """A scoring-path failure is swallowed: summary returned, no raise (extends the existing
        never-raises contract to the Phase C code path)."""
        m = _rebalance_manager(self.tmp, ['a'])
        _bump_to(m, 10)
        _set_metrics(m, {'a': _aged_entry(3, 5.0)})
        m._classify_active = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('boom'))
        result = m.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)  # must not raise
        assert isinstance(result, dict)
        assert 'target' in result and 'evicted' in result

    # ------------------------------------------------------------------ preview parity
    def _parity_fixture(self, base: Path):
        """Identical fresh manager + identical metrics (aged BAD/USELESS + PROTECTED)."""
        names = ['bad', 'useless', 'protected']
        m = _rebalance_manager(base, names)
        _bump_to(m, 200)
        entries = {
            'bad': _aged_entry(10, 2.0),                        # BAD (q̂≈3.33)
            'useless': _aged_entry(3, 5.0),                     # USELESS past window
            'protected': _aged_entry(3, 5.0, last_activity_turn=200),  # A=0 → PROTECTED
        }
        _set_metrics(m, entries)
        return m, names

    def test_preview_per_skill_matches_real_pass(self):
        """Preview == real pass: same per-skill class/score and the SAME projected eviction list
        (OR-composition + D-SAFE cap included), on identical fresh managers."""
        m1, _ = self._parity_fixture(self.tmp / 'preview')
        p = m1.compute_rebalance_preview(k=1.0, min_cap=20, max_cap=200)
        assert p['ok'] is True and 'error' not in p

        # Per-skill breakdown present + correct classes.
        by_name = {row['name']: row for row in p['skills']}
        assert set(by_name) == {'bad', 'useless', 'protected'}
        assert by_name['bad']['class'] == CLASS_BAD
        assert by_name['useless']['class'] == CLASS_USELESS
        assert by_name['protected']['class'] == CLASS_PROTECTED
        assert p['class_counts'][CLASS_BAD] == 1
        assert p['class_counts'][CLASS_PROTECTED] == 1

        # Now a REAL pass on an identical fresh manager.
        m2, _ = self._parity_fixture(self.tmp / 'real')
        s = m2.rebalance_active_skills(k=1.0, min_cap=20, max_cap=200)

        # Projected evictions == actual evictions (order included: BAD before USELESS).
        assert p['would_evict'] == s['evicted'] == ['bad', 'useless']
        assert p['would_evict_absolute_gate'] == s['evicted_absolute_gate']
        assert p['would_evict_cap_only'] == s['evicted_cap_only']

        # Per-skill class/score match between preview and the real pass's inputs.
        with m2._metrics_lock:
            for nm, row in by_name.items():
                r_ = (m2._metrics[nm].get('ratings') or {})
                n = r_.get('count', 0)
                avg = (r_['sum'] / n) if n else None
                L = m2._metrics[nm].get('total_loads', 0)
                A = m2._activity_age(m2._metrics[nm], m2._global_activity_turns, time.time())
                assert row['class'] == skill_classify(n, avg, A)
                assert abs(row['score'] - round(skill_score(n, avg, L, A)['score'], 4)) < 1e-9

    def test_preview_is_side_effect_free(self):
        """The Phase C preview mutates NOTHING: no status flips, no _disabled_names change, no
        flush to disk (no migration ran — parity holds because migration is idempotent over an
        already-migrated store; the fixtures here are migrated via _bump_to's flush cadence)."""
        m, names = self._parity_fixture(self.tmp / 'side')
        # Ensure a real on-disk store exists so "no flush" is observable.
        m._flush_metrics_to_disk()

        with m._metrics_lock:
            metrics_before = _copy.deepcopy(m._metrics)
        disabled_before = set(m._disabled_names)
        counter_before = m._global_activity_turns
        store_before = m._metrics_file.read_text(encoding='utf-8')

        p = m.compute_rebalance_preview(k=1.0, min_cap=20, max_cap=200)
        assert p['ok'] is True and p['would_evict'] == ['bad', 'useless']

        with m._metrics_lock:
            metrics_after = _copy.deepcopy(m._metrics)
        assert metrics_after == metrics_before                # no status flip, no counter change
        assert set(m._disabled_names) == disabled_before      # no disable/enable side effect
        assert m._global_activity_turns == counter_before     # clock untouched
        store_after = m._metrics_file.read_text(encoding='utf-8')
        assert store_after == store_before                    # no flush to disk

    def test_preview_never_raises_with_scoring(self):
        """Any scoring-path failure in the preview is swallowed (ok=False + error, no raise)."""
        m = _rebalance_manager(self.tmp / 'boom', ['a'])
        _bump_to(m, 10)
        _set_metrics(m, {'a': _aged_entry(3, 5.0)})
        m._classify_active = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('boom'))
        p = m.compute_rebalance_preview(k=1.0, min_cap=20, max_cap=200)  # must not raise
        assert isinstance(p, dict)
        assert p['ok'] is False and 'error' in p
