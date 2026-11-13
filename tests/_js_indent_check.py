"""Shared analyzer: flag tab-indented lines in the WebUI JavaScript bundle.

The WebUI frontend is space-indented (2 spaces; see ``.editorconfig``). A
tab-indented region makes the indent unit ambiguous at any edit site, so an edit
that only inserts lines can re-base a neighbouring block's leading whitespace and
land whitespace-only ``+``/``-`` pairs in an otherwise additive diff (BUG_00305).

Single source of truth for both ``tests/test_web_ui_indentation.py`` and
``scripts/check_js_indentation.py``.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_UI_DIR = PROJECT_ROOT / 'web_ui'


def find_js_files(directory=WEB_UI_DIR):
    """Return the JavaScript files directly under ``directory``, sorted."""
    return sorted(Path(directory).glob('*.js'))


def find_tab_indented_lines(path):
    """Return ``[(line_no, line)]`` for lines whose leading whitespace has a tab."""
    text = Path(path).read_text(encoding='utf-8')
    offenders = []
    for line_no, line in enumerate(text.split('\n'), start=1):
        leading = line[:len(line) - len(line.lstrip())]
        if '\t' in leading:
            offenders.append((line_no, line))
    return offenders
