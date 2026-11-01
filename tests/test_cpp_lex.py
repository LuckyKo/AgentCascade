"""Unit tests for ``agent_cascade.tools.custom.cpp_lex`` (BUG_0184 / BUG_0193).

Covers ``neutralize`` (the length- and line-preserving code-only view) and
``find_compiler`` (best-effort, never raises).
"""

import os

import pytest

from agent_cascade.tools.custom import cpp_lex


# The synthetic fixture from plan §2.1 — a raw string and a block comment each
# contain a whole fake class that must NOT produce phantom map entries.
SYNTH = '''#include <string>
const char* r = R"code(
class InRawString {
    void member_in_raw();
}
)code";
int real;
/*
class InBlockComment {
    void in_comment();
}
*/
class RealClass {
    void real_method();
};
'''


def _invariants_ok(src, out):
    """Length- and newline-preserving, and idempotent."""
    return (
        len(out) == len(src)
        and out.count('\n') == src.count('\n')
        and cpp_lex.neutralize(out) == out
    )


class TestNeutralizeComments:
    def test_line_comment_blank(self):
        src = 'int x; // a { brace\n'
        out = cpp_lex.neutralize(src)
        assert out == 'int x; ' + ' ' * 12 + '\n'
        assert _invariants_ok(src, out)

    def test_block_comment_blank_multiline(self):
        src = '/* a\nb\nc */ int x;\n'
        out = cpp_lex.neutralize(src)
        # 3 newlines preserved; the comment body is blanked to spaces
        assert out.count('\n') == 3
        assert 'a' not in out and 'b' not in out and 'c' not in out
        assert out.endswith('int x;\n')
        assert _invariants_ok(src, out)

    def test_block_comment_does_not_eat_real_code(self):
        out = cpp_lex.neutralize(SYNTH)
        assert 'RealClass' in out          # real class survives
        assert 'InBlockComment' not in out  # comment body blanked
        assert 'InRawString' not in out     # raw-string body blanked
        assert 'member_in_raw' not in out
        assert 'in_comment' not in out


class TestNeutralizeStrings:
    def test_double_string(self):
        src = 'const char* s = "a { b }";\n'
        out = cpp_lex.neutralize(src)
        assert 'a { b }' not in out
        assert out.startswith('const char* s = ')
        assert _invariants_ok(src, out)

    def test_single_string(self):
        src = "const char* s = 'a { b }';\n"
        out = cpp_lex.neutralize(src)
        assert 'a { b }' not in out
        assert _invariants_ok(src, out)

    def test_char_literal(self):
        src = "char c = '{';\n"
        out = cpp_lex.neutralize(src)
        assert "'{'" not in out
        assert out.startswith('char c = ')
        assert _invariants_ok(src, out)

    def test_escaped_quote_in_string(self):
        src = 'const char* s = "a \\" b";\n'
        out = cpp_lex.neutralize(src)
        # The \" must not close the string; the whole literal is blanked
        assert 'a \\" b' not in out
        assert _invariants_ok(src, out)

    def test_quote_types_do_not_cross(self):
        # A single quote inside a double-quoted string must not close it.
        src = 'const char* s = "it\'s { ok }";\n'
        out = cpp_lex.neutralize(src)
        assert "it's { ok }" not in out
        assert _invariants_ok(src, out)


class TestNeutralizeRawStrings:
    def test_raw_string_empty_delimiter(self):
        src = 'R"( { } )"\n'
        out = cpp_lex.neutralize(src)
        assert '{' not in out and '}' not in out
        assert _invariants_ok(src, out)

    def test_raw_string_named_delimiter(self):
        src = 'R"code( { } )code"\n'
        out = cpp_lex.neutralize(src)
        assert '{' not in out and '}' not in out
        assert _invariants_ok(src, out)

    def test_raw_string_multiline(self):
        src = 'R"x(\n{ }\n)x"\n'
        out = cpp_lex.neutralize(src)
        assert out.count('\n') == 3
        assert '{' not in out
        assert _invariants_ok(src, out)

    def test_raw_string_in_synthetic(self):
        out = cpp_lex.neutralize(SYNTH)
        assert 'InRawString' not in out
        assert _invariants_ok(SYNTH, out)


class TestNeutralizeLineContinuation:
    def test_macro_continuation(self):
        # A backslash-newline continues a line comment / preprocessor line, so
        # the following "code" stays part of the same logical line.
        src = '#define X(a) \\\n    (a) { return 1; }\n'
        out = cpp_lex.neutralize(src)
        assert len(out) == len(src)
        assert out.count('\n') == src.count('\n')
        # The { on the continued line must survive as code
        assert '{' in out
        assert _invariants_ok(src, out)

    def test_line_comment_continuation(self):
        src = '// comment \\\nint real;\n'
        out = cpp_lex.neutralize(src)
        # The backslash-newline splices the next line into the line comment,
        # so 'int real;' is part of the comment and gets blanked.
        assert 'real' not in out
        assert _invariants_ok(src, out)


class TestNeutralizeInvariants:
    def test_length_preserving(self):
        out = cpp_lex.neutralize(SYNTH)
        assert len(out) == len(SYNTH)

    def test_line_count_preserving(self):
        out = cpp_lex.neutralize(SYNTH)
        assert out.count('\n') == SYNTH.count('\n')

    def test_idempotent(self):
        once = cpp_lex.neutralize(SYNTH)
        twice = cpp_lex.neutralize(once)
        assert once == twice

    def test_plain_code_unchanged(self):
        src = 'int main() { return 0; }\n'
        assert cpp_lex.neutralize(src) == src


class TestFindCompiler:
    def test_returns_tuple_or_none(self):
        result = cpp_lex.find_compiler()
        assert result is None or (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], str)
            and isinstance(result[1], (list, tuple))
        )

    def test_never_raises(self):
        # Must not raise regardless of environment.
        cpp_lex.find_compiler()

    def test_is_cached(self):
        a = cpp_lex.find_compiler()
        b = cpp_lex.find_compiler()
        assert a == b

    def test_compiler_paths_exist_if_found(self):
        result = cpp_lex.find_compiler()
        if result is not None:
            exe, _ = result
            assert os.path.exists(exe)
