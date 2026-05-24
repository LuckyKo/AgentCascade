"""
Feature flags for tab unification project.
All default to False (legacy mode). Set environment variables to enable.
"""
import os

__all__ = ['USE_UNIFIED_ARCHITECTURE', 'USE_UNIFIED_STATE', 'USE_UNIFIED_LOOP']

# Master toggle - gates all unified behavior
USE_UNIFIED_ARCHITECTURE = os.environ.get('AC_USE_UNIFIED_ARCHITECTURE', '0') == '1'

# Gates state read/write path (Phase 2)
USE_UNIFIED_STATE = os.environ.get('AC_USE_UNIFIED_STATE', '0') == '1'

# Gates message loop unification (Phase 3)
USE_UNIFIED_LOOP = os.environ.get('AC_USE_UNIFIED_LOOP', '0') == '1'