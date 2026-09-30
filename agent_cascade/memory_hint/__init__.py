"""Memory-Hint feature — surface relevant ``.agent_lessons`` memories to an agent.

Best-effort, non-critical: when a turn's text strongly matches lessons the agent
hasn't read recently, a short hint is queued onto the instance's existing tool-
warning queue and drained into the next tool result. There is **no** USER-message
injection (plan §0 / R1). Delivery is async on a daemon worker; any failure is
swallowed so hints can never affect the agent.

See docs/skills_system_architecture.md-style design in plans/memory_hint_PLAN.md.
"""

from .manager import (
    MemoryHintManager,
    EWMA_ALPHA,
    FLOOR_MIN,
    FLOOR_SEED,
    GAP,
    HINT_COOLDOWN_SECONDS,
    HINT_ENTRY_MAX_CHARS,
    JOB_TTL_SECONDS,
    MAX_HINTS_PER_TURN,
    SKILL_EWMA_ALPHA,
    SKILL_FLOOR_MIN,
    SKILL_FLOOR_SEED,
    SKILL_HINT_MAX_ENTRIES,
    # Nominal AUTO-mode score retained for backwards compatibility — NOT the hint gate.
    SKILL_HINT_MIN_SCORE,
    SKILL_HINT_NOISE_RATIO,
    SKILL_JUNK_MIN,
    SKILL_REF_TOKENS,
)
from .matcher import MemoryMatcher
from .vault import VaultIndex, discover_vaults, parse_frontmatter, identity_text, is_under_vault
from .stats import bump_read_count, load_stats

__all__ = [
    'MemoryHintManager',
    'EWMA_ALPHA',
    'FLOOR_MIN',
    'FLOOR_SEED',
    'GAP',
    'HINT_COOLDOWN_SECONDS',
    'HINT_ENTRY_MAX_CHARS',
    'JOB_TTL_SECONDS',
    'MAX_HINTS_PER_TURN',
    'SKILL_EWMA_ALPHA',
    'SKILL_FLOOR_MIN',
    'SKILL_FLOOR_SEED',
    'SKILL_HINT_MAX_ENTRIES',
    'SKILL_HINT_MIN_SCORE',
    'SKILL_HINT_NOISE_RATIO',
    'SKILL_JUNK_MIN',
    'SKILL_REF_TOKENS',
    'MemoryMatcher',
    'VaultIndex',
    'discover_vaults',
    'parse_frontmatter',
    'identity_text',
    'is_under_vault',
    'bump_read_count',
    'load_stats',
]
