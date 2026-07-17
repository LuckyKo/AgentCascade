"""
Skills System — Phase 1 MVP for Agent Cascade.

Provides skill discovery, parsing, keyword matching, and management for
SKILL.md files stored in .qwen/skills/ directories.

See docs/skills_system_architecture.md for full design rationale.
"""

from .parser import parse_skill_file, parse_frontmatter
from .matcher import SkillMatcher
from .manager import SkillManager

__all__ = [
    'parse_skill_file',
    'parse_frontmatter',
    'SkillMatcher',
    'SkillManager',
]