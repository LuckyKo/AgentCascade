"""BUG_0249 — copy_file must treat the DESTINATION as authoritative for dir-ness.

Pre-fix, ``copy_file`` keyed its copy branch on ``src_path.is_dir()`` and ran
``shutil.copytree(src, dest)`` whenever the source was a directory — which materialises
``dest`` as a DIRECTORY even when the destination names a file (the real repro restored a
``delete_file`` directory-backup onto ``.../SKILL.md`` and produced ``SKILL.md/SKILL.md``).

The destination is authoritative: a directory is created ONLY when explicitly requested
(raw trailing separator) or when it already exists as one. A dir source onto a file
destination is now a clear type-mismatch ERROR with nothing written at dest.

Harness reuses ``_make_om`` from ``tests/test_justification_provenance.py`` (a real
OperationManager over a tmpdir). Dests are new → the auto-approve fast path runs, so no
approval round-trip; the approval stub is only a safety net.
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_justification_provenance import _make_om


def _stub_approval(om):
    """Safety net: auto-approve on the off chance a dest is not the new-file fast path."""
    om.request_user_approval = lambda **kwargs: (True, '')


def _make_srcdir(root):
    """A directory source with one child file — mimics a delete_file directory backup."""
    srcdir = Path(root, 'srcdir')
    srcdir.mkdir()
    (srcdir / 'child.txt').write_text('child contents', encoding='utf-8')
    return srcdir


def test_dir_source_to_file_destination_is_error_and_creates_nothing():
    """RED pre-fix: dest was created as a DIRECTORY (isdir(dest) True).
    Post-fix: returns ERROR and nothing is written at dest (no file, no dir)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        _stub_approval(om)
        srcdir = _make_srcdir(d)

        parent = Path(d, 'parent')
        parent.mkdir()                       # destination's parent exists, like the ticket
        dest = parent / 'NAME.md'            # a FILE path that does not yet exist

        result = om.copy_file(str(srcdir), str(dest), 'coder')

        assert result.startswith('ERROR:'), f'expected type-mismatch ERROR, got: {result!r}'
        assert not os.path.isfile(str(dest))
        assert not os.path.isdir(str(dest))  # the core regression: dest must not become a dir


def test_dir_source_to_explicit_dir_destination_still_copies():
    """Behavior-preservation: a trailing separator marks the destination as a directory,
    so a directory source still copies the tree into it."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        _stub_approval(om)
        srcdir = _make_srcdir(d)

        outdir = Path(d, 'outdir')
        dest = str(outdir) + os.sep          # explicit directory

        result = om.copy_file(str(srcdir), dest, 'coder')

        assert result.startswith('OK:'), f'expected success, got: {result!r}'
        assert os.path.isdir(str(outdir))
        assert (outdir / 'child.txt').read_text(encoding='utf-8') == 'child contents'


def test_file_source_to_different_basename_file_destination_is_flat_file():
    """Behavior-preservation: the common path — a file source with a DIFFERENT basename and
    an existing parent — must yield a flat file at dest (never a directory)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        _stub_approval(om)
        srcfile = Path(d, 'src.txt')
        srcfile.write_text('file contents', encoding='utf-8')

        parent = Path(d, 'parent')
        parent.mkdir()
        dest = parent / 'REnamed.md'         # different basename than src

        result = om.copy_file(str(srcfile), str(dest), 'coder')

        assert result.startswith('OK:'), f'expected success, got: {result!r}'
        assert os.path.isfile(str(dest))
        assert not os.path.isdir(str(dest))
        assert dest.read_text(encoding='utf-8') == 'file contents'
