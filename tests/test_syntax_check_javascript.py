"""syntax_check: JavaScript now uses an authoritative ``node --check`` gate.

``javascript`` is routed to ``_check_javascript`` (see ``_CHECKER_MAP``):
  - node on PATH + plain JS (``.js``/``.mjs``/``.cjs``) -> ``node --check`` is
    authoritative (clean exit -> 'Valid', non-zero -> node's diagnostic).
  - node unavailable, or ``.jsx`` (JSX not parseable by node) -> falls back to
    the shared C-family bracket heuristic with an advisory label.

The node gate is exercised via a temporary ``.mjs`` file (ESM is a superset of
CJS) so ESM/CJS module-type inference never produces a false positive.

These tests monkeypatch ``shutil.which`` and ``_run_node_check`` so they are
deterministic and do NOT require node to be installed.
"""

import pytest

from agent_cascade.tools.custom import syntax_check as sc


def _split(result):
    """Normalize a checker result to ``(base, advisory_label)``."""
    if isinstance(result, tuple):
        return result
    return result, None


class TestNodeGate:
    """node available + plain JS: the node gate is authoritative."""

    def test_valid_js_is_valid(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        monkeypatch.setattr(sc, '_run_node_check', lambda node, content: (0, ''))
        content = 'function f() { return 1; }\n'
        base, label = _split(sc._check_javascript(content, 'x.js'))
        assert base == 'Valid'
        assert label is None  # authoritative, no advisory label

    def test_broken_js_reports_node_diagnostic(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        monkeypatch.setattr(
            sc, '_run_node_check',
            lambda node, content: (1, 'SyntaxError: Unexpected end of input'))
        content = 'function f( {\n'
        base, label = _split(sc._check_javascript(content, 'x.js'))
        assert base.startswith('Syntax Issues')
        assert 'SyntaxError' in base
        assert label is None

    def test_esm_in_js_no_false_positive(self, monkeypatch):
        # import/export in a .js file must parse cleanly via the temp .mjs gate.
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        monkeypatch.setattr(sc, '_run_node_check', lambda node, content: (0, ''))
        content = 'import x from "y";\nexport default x;\n'
        base, _label = _split(sc._check_javascript(content, 'mod.js'))
        assert base == 'Valid'

    def test_cjs_in_js_no_false_positive(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        monkeypatch.setattr(sc, '_run_node_check', lambda node, content: (0, ''))
        content = 'const m = require("x");\nmodule.exports = m;\n'
        base, _label = _split(sc._check_javascript(content, 'mod.js'))
        assert base == 'Valid'

    def test_mjs_extension_uses_node(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        calls = []
        def _fake(node, content):
            calls.append((node, content))
            return (0, '')
        monkeypatch.setattr(sc, '_run_node_check', _fake)
        base, _label = _split(sc._check_javascript('export const a = 1;\n', 'x.mjs'))
        assert base == 'Valid'
        assert calls, 'node gate must run for .mjs'


class TestFallback:
    """node unavailable, or JSX: fall back to the labelled heuristic."""

    def test_node_missing_falls_back(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: None)
        content = 'function f() { return 1; }\n'
        base, label = _split(sc._check_javascript(content, 'x.js'))
        assert base == 'Valid'  # heuristic says the balanced sample is fine
        assert 'node not available' in label

    def test_node_missing_reports_real_imbalance(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: None)
        content = 'function f() { return 1;\n'
        base, label = _split(sc._check_javascript(content, 'x.js'))
        assert base.startswith('Syntax Issues')
        assert "Unclosed '{'" in base
        assert 'node not available' in label

    def test_jsx_falls_back_even_with_node(self, monkeypatch):
        # .jsx: JSX is not parseable by node --check, so the heuristic is used.
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        calls = []
        monkeypatch.setattr(sc, '_run_node_check',
                           lambda node, content: calls.append(1) or (0, ''))
        content = 'const a = 1;\n'
        base, label = _split(sc._check_javascript(content, 'a.jsx'))
        assert not calls, 'node gate must NOT run for .jsx'
        assert base == 'Valid'
        assert 'JSX' in label

    def test_node_run_returns_none_falls_back(self, monkeypatch):
        # node present but the gate is unusable (timeout/OS error -> None):
        # degrade to the labelled heuristic instead of crashing.
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        monkeypatch.setattr(sc, '_run_node_check', lambda node, content: None)
        content = 'function f() { return 1; }\n'
        base, label = _split(sc._check_javascript(content, 'x.js'))
        assert base == 'Valid'
        assert 'node --check unavailable' in label


class TestDispatch:
    """The dispatch map routes javascript to the node-based checker."""

    def test_javascript_uses_node_checker(self):
        assert sc._CHECKER_MAP['javascript'] is sc._check_javascript

    def test_typescript_still_uses_c_family(self):
        # Out of scope for the node gate (TS needs tsc, not node --check).
        assert sc._CHECKER_MAP['typescript'] is sc._check_c_family
