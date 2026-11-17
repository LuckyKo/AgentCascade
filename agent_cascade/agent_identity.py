"""Root identity vs. display name helpers (BUG_0323 S1).

Root instances may carry an immutable birth stamp in their identity
(``Maine_20261115_143022``) so two stacks rooted at the same display name have
distinct dict keys, log filenames and KV labels. The stamp is part of the
persisted identity and must be reused verbatim on restore — never re-minted.

Sub-agents are never stamped; they are scoped through their root.
"""

from __future__ import annotations

import datetime
import re
import threading
from typing import Optional

#: Filename-safe, sortable stamp shape shared with the JSONL logger.
BIRTH_STAMP_FORMAT = '%Y%m%d_%H%M%S'

_BIRTH_STAMP_RE = re.compile(r'_(\d{8}_\d{6})$')

_stamp_lock = threading.Lock()
_last_stamp_dt: Optional[datetime.datetime] = None


def mint_birth_stamp(now: Optional[datetime.datetime] = None) -> str:
    """Return a unique stamp in ``BIRTH_STAMP_FORMAT``.

    Two roots created in the same wall-clock second would otherwise collide, so
    successive calls in this process are monotonically advanced by one second.
    Stamps remain sortable and keep the documented shape.
    """
    global _last_stamp_dt
    with _stamp_lock:
        stamp_dt = (now or datetime.datetime.now()).replace(microsecond=0)
        if _last_stamp_dt is not None and stamp_dt <= _last_stamp_dt:
            stamp_dt = _last_stamp_dt + datetime.timedelta(seconds=1)
        _last_stamp_dt = stamp_dt
        return stamp_dt.strftime(BIRTH_STAMP_FORMAT)


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


def make_birth_stamped_name(display_name: str, stamp: Optional[str] = None) -> str:
    """Build ``<display>_<stamp>``, stripping any pre-existing stamp first."""
    base = strip_birth_stamp(str(display_name)) or str(display_name)
    return f'{base}_{stamp or mint_birth_stamp()}'


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
