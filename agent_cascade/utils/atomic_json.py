"""Shared atomic JSON write helper.

Several subsystems (pool settings, the API router's endpoint config, session state)
persist JSON files with the same recipe: write to a sibling temp file in the same
directory, fsync, keep one generation of the previous file as ``.bak``, then
``os.replace()`` — atomic on both POSIX and Windows — so a reader never observes a
partially written file. This module is the single implementation of that recipe.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional


def atomic_write_json(path: Path, payload: Any, *,
                      tmp_path: Optional[Path] = None,
                      backup: bool = True,
                      indent: int = 2) -> None:
    """Write ``payload`` to ``path`` atomically, keeping a ``.bak`` of the previous file.

    ``tmp_path`` defaults to ``<path>.tmp`` (appended to the real extension so the temp
    file keeps the same extension and stays on the same filesystem as the target, which
    ``os.replace`` requires).

    Raises ``TypeError`` if ``path`` is not a real path type (``str`` / ``pathlib.Path``) —
    this catches test fixtures that accidentally pass a ``MagicMock`` (which stringifies
    to a junk directory name like ``MagicMock/APIRouter()._config_dir.__truediv__()``).

    Raises the original OSError on failure; the temp file is unlinked in that case so a
    failed save never leaves a stray ``.tmp`` behind.
    """
    if not isinstance(path, (str, Path)):
        raise TypeError(
            f"atomic_write_json: path must be str or pathlib.Path, got {type(path).__name__}. "
            'Did a test fixture pass a MagicMock as the config directory?'
        )
    path = Path(path)
    tmp = Path(tmp_path) if tmp_path is not None else path.with_suffix(path.suffix + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if backup and path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + '.bak'))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
