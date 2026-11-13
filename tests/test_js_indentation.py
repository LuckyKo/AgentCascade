"""Pin: the JS sources we edit are space-indented, never tab-indented.

Covers ``web_ui/*.js`` (the browser bundle) and ``tests/*.js`` (the node regression
harnesses that extract fragments of it). A tab-indented region gives the indent unit
two possible meanings at any edit site, so a line-only insertion can re-base a
neighbouring block's leading whitespace and land whitespace-only ``+``/``-`` pairs in
an otherwise additive diff (BUG_00305 in ``app.js``, BUG_00306 in two harnesses).

Indent width itself is not asserted - only the absence of tabs, which is what makes the
style unambiguous. ``syntax_check``/``node --check`` cannot catch this: JS parses
identically no matter the leading whitespace.
"""

import sys
from pathlib import Path

# Ensure tests/ is on sys.path so we can import the shared analyzer.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _js_indent_check import find_guarded_js_files, find_tab_indented_lines  # noqa: E402


def test_guarded_js_has_no_tab_indentation():
    js_files = find_guarded_js_files()
    assert js_files, 'no JavaScript files found under the guarded directories'

    offenders = {}
    for path in js_files:
        bad = find_tab_indented_lines(path)
        if bad:
            offenders[str(path.relative_to(path.parent.parent))] = [
                line_no for line_no, _ in bad
            ]

    assert not offenders, (
        'tab-indented lines found (web_ui/ and tests/ JS are 2-space indented, see '
        '.editorconfig): ' + ', '.join(
            f'{name}: lines {lines}' for name, lines in offenders.items()
        )
    )
