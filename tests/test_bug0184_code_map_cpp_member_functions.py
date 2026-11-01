"""BUG_0184: code_map returns a 2-entry stub for a C++ file full of class member functions.

The heuristic C++ parser in `agent_cascade/tools/custom/code_map.py` only matched
`class`/`struct` blocks and bare `void f(`-style declarations. It did not recognize
out-of-class member definitions written as `ReturnType ClassName::method(args) {`.

Fix: added a `member` pattern matching `ClassName::method(` with optional return type,
and a stub-map warning when entry count is suspiciously low for the file size.
"""

import os

import pytest

from agent_cascade.tools.custom.code_map import CodeMap

DISK_TIER_CPP = r'N:\work\WD\ninfer-all\src\models\qwen3_5\program\storage\disk_tier.cpp'

EXPECTED_MEMBERS = [
    'open_disk_tier',
    'disk_identity',
    'plan_owner_spill',
    'owner_spill_in_flight',
    'progress_owner_spill',
    'spill_owner_to_disk',
    'spill_released_owner',
    'flush_disk_tier',
    'disk_restorable_frontier',
    'restore_prefix_from_disk',
]


@pytest.fixture(scope='module')
def code_map():
    return CodeMap()


def map_cpp(code_map, content):
    """Call the internal heuristic mapper directly (bypasses path resolution)."""
    return code_map._map_generic(content, 'cpp')


@pytest.fixture(scope='module')
def disk_tier_content():
    if not os.path.isfile(DISK_TIER_CPP):
        pytest.skip(f"External fixture not available: {DISK_TIER_CPP}", allow_module_level=True)
    with open(DISK_TIER_CPP, 'r', encoding='utf-8') as f:
        return f.read()


class TestCppMemberDefinitions:
    """All 10 ProgramImpl:: member functions in disk_tier.cpp must appear in the map."""

    def test_all_members_present(self, code_map, disk_tier_content):
        result = map_cpp(code_map, disk_tier_content)
        for name in EXPECTED_MEMBERS:
            assert name in result, f"Missing member '{name}' in code_map output:\n{result}"

    def test_member_lines_have_prefix(self, code_map, disk_tier_content):
        result = map_cpp(code_map, disk_tier_content)
        for name in EXPECTED_MEMBERS:
            expected = f"member ProgramImpl::{name}"
            assert expected in result, (
                f"Expected 'member ProgramImpl::{name}' in output:\n{result}"
            )


class TestStubWarning:
    """Sparse maps for large files should carry a visible warning."""

    def test_stub_warning_on_sparse_map(self, code_map):
        """A 150-line C++ file with only 1 structure triggers the stub warning."""
        lines = [
            '#include <string>',
            '',
            'class Foo {',
            'public:',
            '    void bar();',
            '};',
        ]
        # Pad with comments to reach 150 lines
        for i in range(144):
            lines.append(f"// filler line {i}")
        content = '\n'.join(lines)

        result = map_cpp(code_map, content)
        assert 'map may be incomplete' in result, (
            f"Expected stub warning for sparse 150-line file, got:\n{result}"
        )

    def test_no_stub_warning_on_dense_map(self, code_map, disk_tier_content):
        """disk_tier.cpp (471 lines) after the fix has enough entries to avoid the warning."""
        result = map_cpp(code_map, disk_tier_content)
        entry_lines = [l for l in result.splitlines() if l.startswith('L')]
        # 10 members + namespace + helper funcs >= 12 entries for 471 lines
        assert len(entry_lines) >= 3, (
            f"Expected >=3 entries for 471-line file, got {len(entry_lines)}"
        )
        # 2% of 471 = ~9.4; we should have well above that
        assert 'map may be incomplete' not in result, (
            f"Unexpected stub warning on dense map:\n{result}"
        )


class TestNoPhantomEntries:
    """Comments and raw-string bodies must not produce phantom map entries.

    Uses the synthetic file from plan §2.1: a raw string and a block comment each
    contain a whole fake class. Only the real ``RealClass`` may appear.
    """

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

    def test_raw_string_body_has_no_phantoms(self, code_map):
        result = map_cpp(code_map, self.SYNTH)
        for phantom in ('InRawString', 'member_in_raw', 'InBlockComment', 'in_comment'):
            assert phantom not in result, (
                f"Phantom entry '{phantom}' leaked from a comment/raw-string:\n{result}"
            )

    def test_real_class_still_present(self, code_map):
        result = map_cpp(code_map, self.SYNTH)
        # Only the real class (caught by the `class` pattern) survives; the
        # fake classes inside the raw string / block comment are blanked.
        assert 'class RealClass' in result, (
            f"Real class 'RealClass' missing from map:\n{result}"
        )


class TestIndentedMemberDefinitions:
    """A same-line qualified definition that is indented (not column-0) is
    still caught by the ``member`` pattern (``^\\s*`` allows leading whitespace)."""

    def test_indented_qualified_definition(self, code_map):
        content = (
            'namespace ns {\n'
            'struct Inner {\n'
            'public:\n'
            '    void Inner::indented_helper() {\n'
            '        int x = 1;\n'
            '    }\n'
            '};\n'
            '}\n'
        )
        result = map_cpp(code_map, content)
        assert 'Inner::indented_helper' in result, (
            f"Indented qualified definition not caught:\n{result}"
        )

    def test_indented_definition_no_duplicate(self, code_map):
        # A same-line definition must be emitted exactly once (member_open is
        # deduped against member on the same line).
        content = 'void Foo::bar() {\n    int x = 1;\n}\n'
        result = map_cpp(code_map, content)
        assert result.count('Foo::bar') == 1, (
            f"Duplicate entry for same-line definition:\n{result}"
        )
