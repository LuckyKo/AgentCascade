"""
Feature flags for tab unification project.
Set to True permanently starting with Phase 7; legacy code paths fully removed in Phase 8.
"""

__all__ = ['USE_UNIFIED_STATE', 'USE_UNIFIED_LOOP']

# Gates state read/write path (Phase 2) — always enabled post-Phase 7
USE_UNIFIED_STATE = True

# Gates message loop unification (Phase 3) — always enabled post-Phase 7
USE_UNIFIED_LOOP = True