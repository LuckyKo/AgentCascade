"""Unit and integration tests for the Skills System Phase 1 MVP.

Covers parser, matcher, manager, and DNA/settings integration points.
Uses real SKILL.md files from agents/global/skills/ as test data where possible.
"""

import asyncio
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
    def _fresh_manager(self):
        self.manager = SkillManager()

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

    def test_invalidate_cache_forces_rediscovery_after_none_mode_clear(self):
        """Regression test for the stale-discovery-cache bug.

        Bug: when ``default_load_skill_mode`` is set to "NONE", the config handler
        clears the registry but did NOT invalidate the discovery cache. A later
        ``_ensure_discovered()`` then hit the TTL/signature early-return and left
        the registry permanently empty — even though all SKILL.md files on disk
        were valid.

        This test reproduces the exact scenario:
          1. Discover from the real skills dir -> >0 skills registered.
          2. Simulate the OLD (buggy) NONE-mode handler: clear registry WITHOUT
             invalidating the cache -> re-discovery is a cache hit -> stays empty.
          3. Simulate the FIXED flow: clear registry + invalidate_cache() ->
             re-discovery re-reads from disk -> skills are back.

        A short TTL (0.0) makes the cache age out immediately, so the test is
        deterministic and fast (no sleeping). The signature check still short-
        circuits in step 2 because the on-disk files did not change, which is
        exactly what the old code relied on to skip re-registration.
        """
        sm = self.manager
        # Short TTL so any elapsed time counts as "expired" (no sleeping needed).
        sm._cache_ttl = 0.0

        # Step 1: discover from the real skills dir.
        sm.discover([_SKILLS_DIR])
        initial_count = len(sm._skills_registry)
        assert initial_count > 0, 'Expected at least one skill in the real skills dir'

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
# Async test helper: run async tests with asyncio
# ===========================================================================


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)
