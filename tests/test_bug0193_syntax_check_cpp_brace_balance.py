"""BUG_0193: syntax_check false-positives on valid C++ (brace balance).

The C/C++ path now runs a shared bracket scan over a ``cpp_lex.neutralize``d
view (strings/comments/raw-strings blanked) plus an optional 20 s compiler gate.
A missing include must never be reported as a *syntax* finding — it degrades to
the labelled heuristic. The 13 other C-family languages keep ``_check_c_family``
(the D-4 quote-type fix), guarded here by control tests.
"""

import os

import pytest

from agent_cascade.tools.custom import cpp_lex
from agent_cascade.tools.custom import syntax_check as sc

CONV = r'N:\work\WD\Strata\src\core\conversation_file.cpp'
QWEN = r'N:\work\WD\ninfer-all\tests\models\qwen3_5\test_engine_stages_real.cpp'


def _split(result):
    """Normalize a checker result to ``(base, advisory_label)``."""
    if isinstance(result, tuple):
        return result
    return result, None


def _read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


class TestRealFixtures:
    """The two real C++ fixtures must be reported Valid (no false positive)."""

    def test_conversation_file_valid(self):
        if not os.path.isfile(CONV):
            pytest.skip(f"External fixture not available: {CONV}")
        base, _label = _split(sc._check_cpp(_read(CONV), CONV))
        assert base == 'Valid', f"Expected Valid, got:\n{base}"

    def test_qwen3_5_real_valid(self):
        if not os.path.isfile(QWEN):
            pytest.skip(f"External fixture not available: {QWEN}")
        base, _label = _split(sc._check_cpp(_read(QWEN), QWEN))
        assert base == 'Valid', f"Expected Valid, got:\n{base}"


class TestSynthetic:
    def test_lambda_and_brace_string(self):
        content = (
            'auto f = [](int x) { return x; };\n'
            'std::string s = "{ not a brace }";\n'
            'void g() { int a = 1; }\n'
        )
        base, _label = _split(sc._check_cpp(content, 'x.cpp'))
        assert base == 'Valid', f"A lambda + brace-in-string must be Valid:\n{base}"

    def test_unbalanced_still_reported(self):
        content = 'void g() { int a = 1;\n'
        base, _label = _split(sc._check_cpp(content, 'x.cpp'))
        assert base.startswith('Syntax Issues'), f"Expected a syntax finding:\n{base}"
        assert "Unclosed '{'" in base


class TestCompilerGate:
    """The 20 s compiler gate: clean → authoritative Valid; fatal → reported;
    include-only → labelled heuristic (NOT a syntax finding)."""

    def test_missing_include_is_advisory_not_finding(self, monkeypatch):
        monkeypatch.setattr(cpp_lex, 'find_compiler', lambda: ('fake_cl.exe', []))
        monkeypatch.setattr(
            sc, '_run_compiler',
            lambda exe, path: (1, 'fatal error: C:\\inc\\vector: No such file or directory'),
        )
        content = 'void g() { int a = 1; }\n'
        base, label = _split(sc._check_cpp(content, 'x.cpp'))
        assert base == 'Valid', f"A missing include is not a syntax finding:\n{base}"
        assert 'includes unresolved' in label

    def test_fatal_compiler_error_reported(self, monkeypatch):
        monkeypatch.setattr(cpp_lex, 'find_compiler', lambda: ('fake_cl.exe', []))
        monkeypatch.setattr(sc, '_run_compiler',
                           lambda exe, path: (1, "error: expected ';' before '}'"))
        content = 'void g() { int a = 1; }\n'
        base, label = _split(sc._check_cpp(content, 'x.cpp'))
        assert base.startswith('Compiler Issues'), f"Fatal diagnostics must be reported:\n{base}"
        assert label is None

    def test_clean_compiler_exit_is_valid(self, monkeypatch):
        monkeypatch.setattr(cpp_lex, 'find_compiler', lambda: ('fake_cl.exe', []))
        monkeypatch.setattr(sc, '_run_compiler', lambda exe, path: (0, ''))
        content = 'void g() { int a = 1; }\n'
        base, label = _split(sc._check_cpp(content, 'x.cpp'))
        assert base == 'Valid'
        assert label is None  # authoritative, no advisory label

    def test_no_compiler_falls_back_to_labelled_heuristic(self, monkeypatch):
        monkeypatch.setattr(cpp_lex, 'find_compiler', lambda: None)
        content = 'void g() { int a = 1; }\n'
        base, label = _split(sc._check_cpp(content, 'x.cpp'))
        assert base == 'Valid'
        assert 'no C++ compiler found' in label


class TestClassification:
    def test_fatal_markers(self):
        assert sc._classify_compiler_output("error: expected ';' before '}'") == 'fatal'
        assert sc._classify_compiler_output('C2143: syntax error') == 'fatal'
        assert sc._classify_compiler_output('mismatched close delimiter') == 'fatal'

    def test_include_failure_is_nonfatal(self):
        assert sc._classify_compiler_output(
            'fatal error: C:\\inc\\vector: No such file or directory') == 'nonfatal'
        assert sc._classify_compiler_output('C1083: Cannot open include file: "vector"') == 'nonfatal'

    def test_fatal_trumps_include(self):
        out = 'C1083: Cannot open include file: "vector"\nerror: expected \';\' before \'}\''
        assert sc._classify_compiler_output(out) == 'fatal'


class TestSharedScannerControl:
    """Guard the D-4 quote-type fix for the 13 non-C/C++ C-family languages.

    These record the *post-fix* behaviour of ``_check_c_family`` so any future
    change to the shared scanner that regresses a sibling language is caught.
    """

    def test_js_string_with_single_quote(self):
        js = 'var s = "it\'s { fine }";\nfunction f() { return 1; }\n'
        assert sc._check_c_family(js, 'x.js') == 'Valid'

    def test_ts_template_literal(self):
        ts = 'const t = `a ${ { } } b`;\nfunction f() { return 1; }\n'
        assert sc._check_c_family(ts, 'x.ts') == 'Valid'

    def test_csharp_char_and_string(self):
        cs = 'string s = "a \\" b";\nchar c = \'x\';\nclass A { void M() {} }\n'
        assert sc._check_c_family(cs, 'x.cs') == 'Valid'

    def test_java_string_with_escaped_quote(self):
        java = 'String s = "a \\" b";\nclass A { void m() {} }\n'
        assert sc._check_c_family(java, 'x.java') == 'Valid'

    def test_unbalanced_js_still_reported(self):
        js = 'function f() { return 1;\n'
        result = sc._check_c_family(js, 'x.js')
        assert result.startswith('Syntax Issues')
        assert "Unclosed '{'" in result
