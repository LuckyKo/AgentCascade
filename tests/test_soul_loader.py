"""Unit tests for agent_cascade.soul_loader (frontmatter + markdown format).

Covers load_soul (frontmatter parsing, error handling), build_system_prompt
(header rendering, verbatim body passthrough, optional generated tool/rule
sections), the adversarial prose cases the old YAML band-aid broke on, and a
byte-equivalence gate over all 9 real soul files against the pre-migration
golden prompts.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

# Ensure the project root is on sys.path so imports resolve
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.soul_loader import (build_system_prompt,
                                       create_agent_from_soul,
                                       get_tool_description, load_soul)

# ---------------------------------------------------------------------------
# Paths to the real soul files and the golden prompt fixture
# ---------------------------------------------------------------------------
_AGENTS_DIR = PROJECT_ROOT / 'agents'
_GOLDEN_PATH = PROJECT_ROOT / 'tests' / 'golden' / 'soul_prompts_pre.json'

SOUL_FILES = {
    'orchestrator': _AGENTS_DIR / 'orchestrator_soul.md',
    'coder': _AGENTS_DIR / 'coder_soul.md',
    'security': _AGENTS_DIR / 'Security_soul.md',
    'researcher': _AGENTS_DIR / 'researcher_soul.md',
}

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(params=list(SOUL_FILES.keys()))
def soul_file(request):
    """Parameterized fixture yielding each real soul file path."""
    return SOUL_FILES[request.param]


@pytest.fixture
def temp_soul_file(tmp_path):
    """Helper to write content to a temp .md file and return its path."""

    def _write(content: str) -> str:
        p = tmp_path / 'test_soul.md'
        p.write_text(content, encoding='utf-8')
        return str(p)

    return _write


# ===========================================================================
# 1. load_soul — frontmatter parsing
# ===========================================================================


class TestLoadSoulFrontmatter:
    """load_soul with the real soul files (frontmatter + body)."""

    def test_load_returns_frontmatter_and_body(self, soul_file):
        config = load_soul(str(soul_file))
        assert isinstance(config, dict)
        assert set(config) == {'frontmatter', 'body'}
        assert isinstance(config['frontmatter'], dict)
        assert isinstance(config['body'], str)

    def test_frontmatter_has_name(self, soul_file):
        config = load_soul(str(soul_file))
        assert isinstance(config['frontmatter']['name'], str)
        assert config['frontmatter']['name']

    def test_frontmatter_has_tagline(self, soul_file):
        config = load_soul(str(soul_file))
        assert isinstance(config['frontmatter']['tagline'], str)

    def test_body_starts_with_prose_not_frontmatter(self, soul_file):
        config = load_soul(str(soul_file))
        # The body must not begin with the frontmatter fence or the name key.
        assert not config['body'].lstrip().startswith('---')
        assert not config['body'].lstrip().startswith('name:')


class TestLoadSoulEdgeCases:
    """load_soul with malformed / edge-case inputs."""

    def test_valid_frontmatter_and_body(self, temp_soul_file):
        content = ('---\n'
                   'name: Tester\n'
                   'tagline: A test agent\n'
                   '---\n'
                   '\n'
                   '## Who You Are\n'
                   'Role: Tester\n')
        path = temp_soul_file(content)
        config = load_soul(path)
        assert config['frontmatter']['name'] == 'Tester'
        assert config['body'].startswith('\n## Who You Are')

    def test_missing_frontmatter_fence(self, temp_soul_file):
        content = 'name: Tester\ntagline: no fence\n'
        path = temp_soul_file(content)
        with pytest.raises(ValueError, match='frontmatter fence'):
            load_soul(path)

    def test_unclosed_frontmatter_fence(self, temp_soul_file):
        content = '---\nname: Tester\ntagline: unclosed\n'
        path = temp_soul_file(content)
        with pytest.raises(ValueError, match='not closed'):
            load_soul(path)

    def test_dots_closing_fence(self, temp_soul_file):
        # '...' is an accepted alternative closing fence (YAML document end).
        content = '---\nname: x\n...\nbody text'
        path = temp_soul_file(content)
        config = load_soul(path)
        assert config['frontmatter'] == {'name': 'x'}
        assert config['body'] == 'body text'

    def test_frontmatter_not_a_mapping(self, temp_soul_file):
        content = '---\n- one\n- two\n---\nbody\n'
        path = temp_soul_file(content)
        with pytest.raises(ValueError, match='YAML mapping'):
            load_soul(path)

    def test_malformed_frontmatter_yaml(self, temp_soul_file):
        content = '---\nname: [unclosed bracket\n---\nbody\n'
        path = temp_soul_file(content)
        with pytest.raises(yaml.YAMLError):
            load_soul(path)

    def test_body_is_not_parsed_as_yaml(self, temp_soul_file):
        # Prose that is invalid YAML must load fine — it lives in the body.
        content = ('---\n'
                   'name: Tester\n'
                   '---\n'
                   '\n'
                   'Some prose: with a colon, and a * bullet list:\n'
                   '* one\n'
                   '* two\n')
        path = temp_soul_file(content)
        config = load_soul(path)
        assert config['frontmatter']['name'] == 'Tester'
        assert '* one' in config['body']

    def test_bom_is_stripped(self, temp_soul_file):
        content = '\ufeff---\nname: Tester\n---\nbody\n'
        path = temp_soul_file(content)
        config = load_soul(path)
        assert config['frontmatter']['name'] == 'Tester'

    def test_missing_file(self):
        with pytest.raises(FileNotFoundError, match='Soul file not found'):
            load_soul('/no/such/path.md')


# ===========================================================================
# 2. build_system_prompt — header, body passthrough, generated sections
# ===========================================================================


class TestBuildSystemPrompt:
    """build_system_prompt with the frontmatter+body contract."""

    def test_empty_config_defaults_to_assistant(self):
        prompt = build_system_prompt({})
        assert 'You are Assistant' in prompt

    def test_header_from_frontmatter(self):
        config = {'frontmatter': {'name': 'Bob', 'tagline': 'A helper'},
                  'body': ''}
        prompt = build_system_prompt(config)
        assert prompt.startswith('You are Bob.\nA helper\n')

    def test_body_rendered_verbatim(self):
        body = '\n## Custom Section\n- alpha\n- beta\nplain trailing line\n'
        config = {'frontmatter': {'name': 'Bob'}, 'body': body}
        prompt = build_system_prompt(config)
        assert body in prompt

    def test_body_ordering_preserved(self):
        # Whatever order the author writes the body in is preserved exactly.
        body = '\n## Second\nx\n\n## First\ny\n'
        config = {'frontmatter': {'name': 'Bob'}, 'body': body}
        prompt = build_system_prompt(config)
        assert prompt.index('## Second') < prompt.index('## First')

    def test_tools_section_generated_from_frontmatter(self):
        config = {'frontmatter': {'name': 'Bob',
                                  'capabilities': {'tools': ['web_search']}},
                  'body': ''}
        prompt = build_system_prompt(config)
        assert '## Your Tools' in prompt
        assert '**web_search**' in prompt
        assert get_tool_description('web_search') in prompt

    def test_unknown_tool_gets_default_description(self):
        # Direct unit assertion on the pure helper (not driven through the
        # renderer, which is covered by test_tools_section_generated_from_frontmatter).
        assert get_tool_description('mystery_tool') == 'access external information'

    def test_rules_section_numbered_from_frontmatter(self):
        config = {'frontmatter': {'name': 'Bob',
                                  'rules': ['First rule', 'Second rule']},
                  'body': ''}
        prompt = build_system_prompt(config)
        assert '## Your Rules' in prompt
        assert '1. First rule\n' in prompt
        assert '2. Second rule\n' in prompt

    def test_no_generated_sections_when_absent(self):
        config = {'frontmatter': {'name': 'Bob'}, 'body': '\nhello\n'}
        prompt = build_system_prompt(config)
        assert '## Your Tools' not in prompt
        assert '## Your Rules' not in prompt


# ===========================================================================
# 3. Adversarial prose — the cases the old YAML band-aid broke on
# ===========================================================================


class TestAdversarialProse:
    """A body mixing a list with trailing prose, and `* ` sub-lists, must load
    and render verbatim. These are exactly the inputs that made the previous
    whole-file-YAML parser raise cryptic errors."""

    def test_list_followed_by_trailing_paragraph(self, temp_soul_file):
        content = (
            '---\n'
            'name: Tester\n'
            'tagline: adversarial\n'
            '---\n'
            '\n'
            '## Calibrate to blast radius BEFORE planning:\n'
            '  - Contained: small change, minimal review.\n'
            '  - Moderate: multi-file, targeted review.\n'
            '  - High-risk: architecture, exhaustive review.\n'
            '  Right-size the plan to it; reserve exhaustive tables for high-risk.\n')
        path = temp_soul_file(content)
        config = load_soul(path)
        assert config['frontmatter']['name'] == 'Tester'
        # The trailing plain paragraph after the list survives verbatim.
        prompt = build_system_prompt(config)
        assert 'Right-size the plan to it' in prompt
        assert '- Contained: small change' in prompt

    def test_star_sublist_renders(self, temp_soul_file):
        content = (
            '---\n'
            'name: Tester\n'
            '---\n'
            '\n'
            '## Workflow\n'
            '- Contained\n'
            '  * Minor edit\n'
            '  * Skip review\n')
        path = temp_soul_file(content)
        config = load_soul(path)  # would have raised YAMLError under old loader
        prompt = build_system_prompt(config)
        assert '* Minor edit' in prompt
        assert '* Skip review' in prompt


# ===========================================================================
# 4. Byte-equivalence gate — the migration's acceptance criterion
# ===========================================================================


class TestGoldenEquivalence:
    """build_system_prompt(load_soul(p)) must be byte-identical to the
    pre-migration golden prompts for all 9 real soul files."""

    def _golden(self):
        return json.loads(_GOLDEN_PATH.read_text(encoding='utf-8'))

    def test_golden_fixture_present(self):
        golden = self._golden()
        assert len(golden) == 9, 'golden fixture must cover all 9 soul files'

    def test_all_prompts_byte_equivalent(self):
        golden = self._golden()
        for fname, expected in golden.items():
            path = _AGENTS_DIR / fname
            assert path.exists(), f"missing soul file {fname}"
            actual = build_system_prompt(load_soul(str(path)))
            assert actual == expected, f"prompt diverges for {fname}"

    def test_all_soul_files_covered_by_golden(self):
        golden = self._golden()
        on_disk = {p.name for p in _AGENTS_DIR.glob('*_soul.md')}
        assert set(golden) == on_disk


# ===========================================================================
# 5. create_agent_from_soul — end-to-end with the new format
# ===========================================================================


class TestCreateAgentFromSoul:

    def test_returns_agent_and_config(self, temp_soul_file):
        content = ('---\n'
                   'name: Bob\n'
                   'tagline: A helper\n'
                   '---\n'
                   '\n'
                   '## Who You Are\n'
                   'Role: Tester\n')
        path = temp_soul_file(content)
        llm_cfg = {'model': 'test_model',
                   'model_server': 'http://localhost:1234/v1',
                   'api_key': 'EMPTY'}
        agent, config = create_agent_from_soul(llm_cfg, path,
                                               role_name='tester')
        assert agent is not None
        assert config is not None
        assert config['frontmatter']['name'] == 'Bob'
        assert 'You are Bob.' in agent.base_system_message
