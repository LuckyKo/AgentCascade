"""Cache helper for mtime-based skill discovery caching."""

import os
from pathlib import Path
from typing import FrozenSet, List, Tuple


def compute_scan_signature(
    dirs: List[Path],
    disabled: FrozenSet[str] = frozenset(),
) -> Tuple[Tuple, FrozenSet]:
    """Compute a change-signature for skill scan inputs.

    Includes both dir mtimes (catches add/remove) AND individual SKILL.md
    file mtimes (catches in-place edits). O(#dirs + #skills) stat calls.
    Returns ((dir_path, max_mtime), ...) tuple for hashability.
    """
    sig: list = []
    for d in dirs:
        try:
            m = d.stat().st_mtime
        except OSError:
            continue
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            em = entry.stat(follow_symlinks=False).st_mtime
                            if em > m:
                                m = em
                        elif entry.name == 'SKILL.md':
                            fm = entry.stat(follow_symlinks=False).st_mtime
                            if fm > m:
                                m = fm
                    except OSError:
                        continue
        except OSError:
            pass
        sig.append((str(d), m))
    return (tuple(sig), disabled)
