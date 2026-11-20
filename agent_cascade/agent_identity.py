"""Instance identity vs. display name helpers.

A root's identity is its clean display name (``ACDev``). When two live stacks
share a display name the pool appends a numeric suffix (``ACDev_2``) so they do
not collide on dict keys, KV labels or log paths. The identity is therefore
stable across reloads and swaps.

Legacy sessions persisted a birth stamp in the identity
(``Maine_20261115_143022``). Those stamps are preserved verbatim on restore;
the ``birth_stamp`` / ``strip_birth_stamp`` helpers exist to read them and
present the clean label. Never mint a new stamp.

Sub-agents are scoped through their root and are never suffixed.
"""

from __future__ import annotations

import re
from typing import Optional

_BIRTH_STAMP_RE = re.compile(r'_(\d{8}_\d{6})$')


def birth_stamp(name: str) -> Optional[str]:
    """Return the trailing birth stamp on *name*, or ``None`` when unstamped."""
    if not isinstance(name, str):
        return None
    match = _BIRTH_STAMP_RE.search(name.strip())
    return match.group(1) if match else None


def is_birth_stamped(name: str) -> bool:
    """True when *name* ends with a birth stamp."""
    return birth_stamp(name) is not None


def strip_birth_stamp(name: str) -> str:
    """Return the display portion of *name* (the single strip helper)."""
    if not isinstance(name, str):
        return name
    name = name.strip()
    match = _BIRTH_STAMP_RE.search(name)
    return name[:match.start()] if match else name


def _name_taken(candidate: str, taken) -> bool:
    """True when *candidate* matches a name in *taken* (case-insensitive)."""
    names = taken.keys() if hasattr(taken, 'keys') else taken
    needle = candidate.lower()
    return any(isinstance(name, str) and name.lower() == needle for name in names)


def disambiguate_instance_name(base: str, taken, *, force_suffix: bool = False) -> str:
    """Return *base* or ``<base>_N`` (N >= 2) not present in *taken*.

    Matching is case-insensitive to mirror ``_resolve_instance_name``. With
    ``force_suffix=True`` the result is always suffixed (add-mode clones).
    A legacy birth stamp on *base* is stripped first so suffixes never stack on
    top of a timestamp.
    """
    base = strip_birth_stamp(str(base)) or str(base)
    if not force_suffix and not _name_taken(base, taken):
        return base
    idx = 2
    while _name_taken(f'{base}_{idx}', taken):
        idx += 1
    return f'{base}_{idx}'


def get_display_name(instance) -> str:
    """Return an instance's display name, falling back to its stripped identity."""
    display = getattr(instance, 'display_name', None)
    if isinstance(display, str) and display.strip():
        return display.strip()
    identity = getattr(instance, 'instance_name', '') or ''
    return strip_birth_stamp(identity)


def _root_for(instance, all_instances):
    """Walk ``parent_instance`` links to the root, guarding against cycles."""
    seen = set()
    current = instance
    while current is not None:
        parent = getattr(current, 'parent_instance', None)
        if not parent:
            return current
        if parent in seen:
            return None
        seen.add(parent)
        current = all_instances.get(parent)
    return None


def display_label(instance, all_instances=None) -> str:
    """Display label for UI surfaces.

    A sub-agent whose display name is duplicated across the snapshot is
    qualified with its root's display name (``Coder1 (Maine)``) so repeated
    ``Coder1`` rows are unambiguous.
    """
    display = get_display_name(instance)
    if not all_instances:
        return display
    if getattr(instance, 'parent_instance', None) is None:
        return display

    duplicates = [inst for inst in all_instances.values()
                  if get_display_name(inst) == display]
    if len(duplicates) <= 1:
        return display

    root = _root_for(instance, all_instances)
    root_display = get_display_name(root) if root is not None else ''
    if root_display and root_display != display:
        return f'{display} ({root_display})'
    return display
