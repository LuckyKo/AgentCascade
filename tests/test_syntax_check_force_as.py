"""syntax_check: the ``force_as`` parameter overrides extension-based detection.

``force_as`` lets a caller check a file *as* a known language regardless of its
extension — its whole point is files whose extension is unknown or absent
(``force_as='python'`` on a no-extension dump), and it always wins over the
extension when both are present. Validation is against ``_CHECKER_MAP`` (the
single source of truth for supported languages); an invalid value short-circuits
with an error listing every supported language and never reads the file.

These tests exercise ``SyntaxCheck.call()`` (the ``force_as`` logic lives there,
not in the bare checker functions other test files drive). ``call()`` resolves
the path via a per-invocation import of ``resolve_tool_path``, so the fixture
patches that attribute on its home module and points it at a real tmp file.

The forced language is asserted from the ``(lang...)`` token echoed in the
returned header — comma-agnostic so it matches both the valid header
(``(python, N lines)``) and the invalid header (``(python)``), i.e. which
language was *selected*, not whether it passed.
"""

import pytest

from agent_cascade.tools.custom import syntax_check as sc
from agent_cascade.utils import tool_path_resolver as tpr


VALID_PY = 'def f():\n    return 1\n'
BALANCED_JS = 'function f() { return 1; }\n'


def _reported_lang(out: str, lang: str) -> bool:
    """True if *lang* is the language token echoed in a checker header.

    Matches both ``({lang}, N lines)`` (valid) and ``({lang}) — ...`` (invalid),
    so it asserts the selected language regardless of pass/fail.
    """
    return f'({lang}' in out


@pytest.fixture
def make_file(tmp_path, monkeypatch):
    """Return a factory that writes a tmp file and points resolve_tool_path at it.

    ``call()`` re-imports ``resolve_tool_path`` per invocation, so patching the
    attribute on its home module redirects the *next* call — no real workspace
    path resolution and no dependence on the file's location.
    """
    tool = sc.SyntaxCheck()

    def _make(name: str, content: str = ''):
        p = tmp_path / name
        p.write_text(content, encoding='utf-8')
        monkeypatch.setattr(
            tpr, 'resolve_tool_path',
            lambda rel, mode='ro', agent_pool=None: p,
        )
        return p

    return tool, _make


class TestForceAs:
    """force_as overrides extension detection and wins over a conflicting extension."""

    def test_force_as_overrides_unrecognized_extension(self, make_file):
        """A no-extension file checks as the forced language instead of erroring."""
        tool, _make = make_file
        _make('payload', VALID_PY)
        out = tool.call({'path': 'payload', 'force_as': 'python'})
        assert _reported_lang(out, 'python')
        # Without force_as this same file would hit "Unsupported file type" (test 5).
        assert 'Unsupported file type' not in out

    def test_force_as_beats_extension(self, make_file):
        """A conflicting extension is ignored: force_as wins over auto-detect."""
        tool, _make = make_file
        _make('app.js', BALANCED_JS)
        out = tool.call({'path': 'app.js', 'force_as': 'typescript'})
        assert _reported_lang(out, 'typescript')
        assert not _reported_lang(out, 'javascript')

    def test_force_as_invalid_lists_languages_and_does_not_read(self, make_file, monkeypatch):
        """An unsupported force_as returns a DRY supported-languages error and
        short-circuits before auto-detect, file read, or any checker/compiler."""
        tool, _make = make_file
        _make('app.js', BALANCED_JS)

        # Auto-detect must never run on the force_as path; raising here proves the
        # if/else is not reordered (it short-circuits before detection).
        def _boom(_path):
            raise AssertionError('auto-detect must not run when force_as is invalid')
        monkeypatch.setattr(sc, '_detect_language', _boom)

        # Content read must never happen either: validation returns first. If a future
        # refactor moved the read above validation, _no_read's AssertionError is swallowed
        # by the production try/except, but the resulting wrong output trips the assertions.
        def _no_read(*_a, **_k):
            raise AssertionError('file must not be read when force_as is invalid')
        monkeypatch.setattr(sc.Path, 'read_text', _no_read)

        out = tool.call({'path': 'app.js', 'force_as': 'cooblag'})
        assert 'not a supported language' in out
        assert 'Supported languages:' in out
        # Single source of truth: the list is derived only from the live map.
        expected = ', '.join(sorted(sc._CHECKER_MAP.keys()))
        assert expected in out

    def test_no_force_as_unchanged_auto_detect(self, make_file, monkeypatch):
        """Regression guard: with no force_as, the extension drives detection."""
        tool, _make = make_file
        _make('a.js', BALANCED_JS)
        # Keep hermetic: stub the node gate so the javascript checker runs deterministically.
        monkeypatch.setattr(sc.shutil, 'which', lambda _n: r'C:\node\node.exe')
        monkeypatch.setattr(sc, '_run_node_check', lambda node, content: (0, ''))
        out = tool.call({'path': 'a.js'})
        assert _reported_lang(out, 'javascript')

    def test_unrecognized_extension_without_force_still_errors(self, make_file):
        """The auto-detect error branch is intact for callers that pass no force_as."""
        tool, _make = make_file
        _make('payload', VALID_PY)
        out = tool.call({'path': 'payload'})
        assert 'Unsupported file type' in out

    def test_force_as_case_insensitive(self, make_file):
        """force_as is normalized to lower-case before the _CHECKER_MAP lookup."""
        tool, _make = make_file
        _make('payload', VALID_PY)
        out = tool.call({'path': 'payload', 'force_as': 'PYTHON'})
        assert _reported_lang(out, 'python')

    def test_force_as_blank_boundary(self, make_file):
        """Empty force_as falls through to auto-detect; whitespace-only surfaces a
        malformed-param error rather than silently masking the caller's intent.
        Pins the boundary so the plan's `if force_as:` truthiness gate is intentional."""
        tool, _make = make_file
        _make('payload', VALID_PY)  # no extension: auto-detect can't resolve it
        # '' is falsy -> auto-detect runs -> no-extension file errors as before.
        out_empty = tool.call({'path': 'payload', 'force_as': ''})
        assert 'Unsupported file type' in out_empty
        # '   ' is truthy -> override branch -> stripped '' is not a supported language.
        out_ws = tool.call({'path': 'payload', 'force_as': '   '})
        assert 'is not a supported language' in out_ws
