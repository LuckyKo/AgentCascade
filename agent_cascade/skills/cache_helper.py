"""Cache helper for mtime-based skill discovery caching."""

import os
from pathlib import Path
from typing import FrozenSet, List, Tuple


def _max_mtime_in_skill_dir(skill_dir: Path) -> float:
    """Return the newest mtime among a skill dir, its entry, and its SKILL.md.

    A skill lives at ``<root>/<skill-name>/SKILL.md`` — one level below the scan
    root. Editing that file updates the file's own mtime but NOT the parent
    dir's mtime, so we must stat the nested SKILL.md directly to detect in-place
    edits (see compute_scan_signature). Falls back to the dir mtime if absent.
    """
    try:
        m = skill_dir.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0
    skill_file = skill_dir / 'SKILL.md'
    try:
        fm = skill_file.stat(follow_symlinks=False).st_mtime
        if fm > m:
            m = fm
    except OSError:
        pass
    return m


def compute_scan_signature(
    dirs: List[Path],
    disabled: FrozenSet[str] = frozenset(),
) -> Tuple[Tuple, FrozenSet]:
    """Compute a change-signature for skill scan inputs.

    Catches BOTH whole-skill add/remove (top-level dir mtime changes) AND
    in-place edits to a nested ``<skill>/SKILL.md`` file (which does NOT update
    the parent dir's mtime). O(#dirs + #skills) stat calls.
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
                            # Skill dirs hold SKILL.md one level down; stat the
                            # nested file too so in-place edits change the signature.
                            em = _max_mtime_in_skill_dir(Path(d) / entry.name)
                            if em > m:
                                m = em
                        elif entry.name == 'SKILL.md':
                            # Flat layout (SKILL.md directly under root) — keep supporting it.
                            fm = entry.stat(follow_symlinks=False).st_mtime
                            if fm > m:
                                m = fm
                    except OSError:
                        continue
        except OSError:
            pass
        sig.append((str(d), m))
    return (tuple(sig), disabled)
