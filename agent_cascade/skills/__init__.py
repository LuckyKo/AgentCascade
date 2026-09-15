"""
Skills System — Phase 1 MVP for Agent Cascade.

Provides skill discovery, parsing, keyword matching, and management for
SKILL.md files stored in agents/global/skills/ directories.

See docs/skills_system_architecture.md for full design rationale.
"""

from .advisor import SkillAdvisorResult, build_skill_advisor_prompt, parse_advisor_output, run_skill_advisor
from .manager import SkillManager
from .matcher import SkillMatcher
from .parser import parse_frontmatter, parse_skill_file
from .validator import validate_skill

__all__ = [
    'parse_skill_file',
    'parse_frontmatter',
    'SkillMatcher',
    'SkillManager',
    'validate_skill',
    'SkillAdvisorResult',
    'build_skill_advisor_prompt',
    'parse_advisor_output',
    'run_skill_advisor',
]
