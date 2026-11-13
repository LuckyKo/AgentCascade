"""Shared analyzer: flag tab-indented lines in the JavaScript sources we edit.

The WebUI bundle and the node regression harnesses that extract fragments of it are
space-indented (2 spaces; see ``.editorconfig``). A tab-indented region makes the
indent unit ambiguous at any edit site, so an edit that only inserts lines can re-base
a neighbouring block's leading whitespace and land whitespace-only ``+``/``-`` pairs in
an otherwise additive diff (BUG_00305, BUG_00306).

Single source of truth for ``tests/test_js_indentation.py`` and
``scripts/check_js_indentation.py``.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_UI_DIR = PROJECT_ROOT / 'web_ui'
TESTS_DIR = PROJECT_ROOT / 'tests'
# Directories whose *.js files must be space-indented (no tabs at any indent depth).
GUARDED_DIRS = (WEB_UI_DIR, TESTS_DIR)


def find_js_files(directory):
    """Return the JavaScript files directly under ``directory``, sorted."""
    return sorted(Path(directory).glob('*.js'))


def find_guarded_js_files():
    """Return every JS file under the guarded directories, sorted."""
    files = []
    for directory in GUARDED_DIRS:
        files.extend(find_js_files(directory))
    return files


def find_tab_indented_lines(path):
    """Return ``[(line_no, line)]`` for lines whose leading whitespace has a tab."""
    text = Path(path).read_text(encoding='utf-8')
    offenders = []
    for line_no, line in enumerate(text.split('\n'), start=1):
        leading = line[:len(line) - len(line.lstrip())]
        if '\t' in leading:
            offenders.append((line_no, line))
    return offenders
