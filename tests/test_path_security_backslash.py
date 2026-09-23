"""Cross-platform revert-proof tests for backslash normalization in _resolve_path (todo.md:165).

Root cause: ``path_security._resolve_path`` performed no ``\\``→``/`` normalization (unlike
``grep.py``), so resolution was platform-dependent. On Windows a *backslashed virtual prefix*
(e.g. ``workspace\\src\\a.py``) was not stripped by the forward-slash-only guards and resolved to a
non-existent path; on Linux every backslash input became a literal filename character. The fix is a
single pure helper, ``_normalize_separators``, applied at the top of ``_resolve_path``.

These tests use only hermetic temp paths (never real ``N:\\``) and are xdist-safe: each test builds
its own temp dir / OperationManager, with no shared module state. The anchors FAIL pre-fix on BOTH
Windows and Linux and PASS post-fix on both, because the fix normalizes to a platform-independent
form *before* any ``Path`` call — a single code path runs on both OSes.

Anchors:
  T1 — pure unit test of ``_normalize_separators`` (OS-independent; import fails pre-fix).
  T2 — ``_resolve_path`` backslash resolution via a real OperationManager over a temp base.
       THE cross-platform revert-proof anchor is the backslashed virtual prefix ``workspace\\src\\a.py``.
  T3 — security guard: normalization must NOT weaken containment (UNC / ``..`` escapes stay blocked).
"""

import sys
from pathlib import Path

# Resolve project root relative to this test file (tests/ → project_root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.operation_manager.path_security import _normalize_separators


def _make_om(tmpdir):
    """A real OperationManager over a temp workspace (no agent_pool). Mirrors test_delete_file.py."""
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager(base_dir=str(tmpdir))
    return om


# ── T1: pure helper, OS-independent ───────────────────────────────────────────


def test_normalize_separators_unit():
    """Backslash → forward-slash; no-op for inputs without backslashes."""
    assert _normalize_separators('src\\a.py') == 'src/a.py'
    assert _normalize_separators('/workspace/src\\a.py') == '/workspace/src/a.py'
    assert _normalize_separators('N:\\work\\WD\\src\\a.py') == 'N:/work/WD/src/a.py'
    assert _normalize_separators('..\\secret.py') == '../secret.py'
    # no-op for already-forward / relative / absolute
    assert _normalize_separators('src/a.py') == 'src/a.py'
    assert _normalize_separators('/workspace/src/a.py') == '/workspace/src/a.py'


# ── T2: _resolve_path backslash resolution via a temp base ────────────────────


def test_resolve_backslash_virtual_prefix(tmp_path):
    """THE cross-platform anchor: backslashed virtual prefix resolves to <base>/src/a.py.

    Pre-fix this input is NOT FOUND on both OSes (the ``workspace\\`` prefix is not stripped), so
    the assertion below fails; post-fix it normalizes + strips and lands on the target.
    """
    om = _make_om(tmp_path)
    target = tmp_path / 'src' / 'a.py'
    target.parent.mkdir()
    target.write_text('x')

    resolved = om._resolve_path('workspace\\src\\a.py', mode='rw')  # backslashed virtual prefix
    assert resolved == target, f"expected {target}, got {resolved}"


def test_resolve_relative_backslash(tmp_path):
    """Relative backslash path resolves correctly (fails pre-fix on Linux; already works on Windows)."""
    om = _make_om(tmp_path)
    target = tmp_path / 'src' / 'a.py'
    target.parent.mkdir()
    target.write_text('x')

    assert om._resolve_path('src\\a.py', mode='rw') == target


def test_resolve_mixed_separators(tmp_path):
    """Mixed forward/backslash separators resolve to the same in-bounds path."""
    om = _make_om(tmp_path)
    t = tmp_path / 'src' / 'a' / 'sub' / 'b.py'
    t.parent.mkdir(parents=True)
    t.write_text('x')

    assert om._resolve_path('src/a\\sub\\b.py', mode='rw') == t


# ── T3: security guard — normalization must NOT weaken containment ────────────


def test_backslash_escape_still_blocked(tmp_path):
    """Escape inputs (``..``, UNC) are still blocked by the commonpath containment check.

    Holds pre- and post-fix on both OSes; guards against a future change that weakens containment.
    """
    om = _make_om(tmp_path)
    base = Path(om.base_dir).resolve()
    for bad in ['..\\escape.py', '/workspace/..\\escape.py', '\\\\server\\share\\x.py']:
        try:
            resolved = om._resolve_path(bad, mode='rw')
        except ValueError:
            continue  # blocked — correct
        assert om._path_is_contained(resolved, base), f"{bad} escaped containment"
