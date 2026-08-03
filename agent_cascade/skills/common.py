"""
Shared constants and patterns for the skills system.
"""

import re

# Semver pattern: X.Y.Z (e.g., "1.0.0", "2.1.3")
SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')