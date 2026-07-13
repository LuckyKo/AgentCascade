"""Comprehensive test suite for re_indent tool — all 4 modes.

Covers: min, shift, flat, convert modes plus cross-mode edge cases.
Pattern follows existing tests in test_edit_file_modes.py and test_oob_fixes.py.
Uses OperationManager directly with a temporary directory as base_dir.
"""
import sys
import os
import tempfile
from pathlib import Path

# Resolve project root (tests/tools/ → project_root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.operation_manager import OperationManager


class ReIndentTester:
    """Centralized test runner for re_indent modes."""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp()
        self.op_mgr = OperationManager(base_dir=self.tmpdir)
        self.op_mgr.file_ownership = {}
        self.passed = 0
        self.failed = 0
        self.bugs = []  # Collected bug reports

    def _own(self, rel_path):
        """Mark a file as owned by test_agent so no approval is needed."""
        resolved = (Path(self.tmpdir) / rel_path).resolve()
        self.op_mgr.file_ownership[str(resolved)] = "test_agent"

    def _write(self, name, content):
        """Write content to a temp file and auto-own it. Return the path object."""
        p = Path(self.tmpdir) / name
        p.write_text(content, encoding='utf-8')
        self._own(name)
        return p

    def _read(self, name):
        return (Path(self.tmpdir) / name).read_text(encoding='utf-8')

    # ──────────────── check helpers ────────────────

    def ok(self, label, condition, detail=""):
        if condition:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            self.failed += 1
            print(f"  [FAIL] {label}")
            if detail:
                print(f"         Detail: {detail[:300]}")

    def bug(self, mode, scenario, inp, expected, actual):
        """Record a suspected bug for the final report."""
        self.bugs.append({
            "mode": mode,
            "scenario": scenario,
            "input": repr(inp),
            "expected": repr(expected),
            "actual": repr(actual),
        })

    # ═══════════════════════════════════════════════
    #  MIN MODE TESTS (1-6)
    # ═══════════════════════════════════════════════

    def test_min_01_basic_spaces(self):
        """MIN-1: Basic re-indent with spaces — trim to min, apply target indent."""
        print("\n── MIN Mode ──")
        p = self._write("min1.py", "    def foo():\n        pass\n    bar()\n")
        res = self.op_mgr.re_indent(
            path="min1.py", agent_name="test_agent",
            lines="1:3", indent=2, indent_type="space", mode="min"
        )
        content = self._read("min1.py")
        # min ws = 4 (line 1 and 3), line 2 has 8 → relative +4
        # After trim 4: line1→0, line2→4, line3→0
        # Add indent=2: line1→"  ", line2→"      ", line3→"  "
        expected = "  def foo():\n      pass\n  bar()\n"
        self.ok("MIN-1 basic spaces", content == expected and "OK:" in res,
                f"got [{content}]")
        if content != expected:
            self.bug("min", "basic_spaces", "    def\n        pass\n    bar",
                     expected, content)

    def test_min_02_tabs(self):
        """MIN-2: Re-indent with tabs."""
        p = self._write("min2.py", "\t\tdef foo():\n\t\t\tpass\n\tbar()\n")
        res = self.op_mgr.re_indent(
            path="min2.py", agent_name="test_agent",
            lines="1:3", indent=1, indent_type="tab", mode="min"
        )
        content = self._read("min2.py")
        # min ws count = 1 tab (line 3), line1 has 2 tabs → rel +1, line2 has 3 → rel +2
        # indent=1 tab: line1→\t+\t=\t\t, line2→\t+\t\t=\t\t\t, line3→\t
        expected = "\t\tdef foo():\n\t\t\tpass\n\tbar()\n"
        self.ok("MIN-2 tabs", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_min_03_mixed_levels(self):
        """MIN-3: Mixed indentation levels preserved as relative offsets."""
        p = self._write("min3.py", "      a\n     b\n   c\n d\n")
        res = self.op_mgr.re_indent(
            path="min3.py", agent_name="test_agent",
            lines="1:4", indent=0, indent_type="space", mode="min"
        )
        content = self._read("min3.py")
        # min ws = 1 (line 4). Relative offsets from line 4: +5,+4,+2,+0
        # indent=0 so just relative: "     a\n    b\n  c\nd\n"
        expected = "     a\n    b\n  c\nd\n"
        self.ok("MIN-3 mixed levels preserved", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_min_04_blank_lines(self):
        """MIN-4: Blank lines in the middle pass through unchanged."""
        p = self._write("min4.py", "    a\n\n    b\n  \n    c\n")
        res = self.op_mgr.re_indent(
            path="min4.py", agent_name="test_agent",
            lines="1:5", indent=2, indent_type="space", mode="min"
        )
        content = self._read("min4.py")
        # min ws among non-blank = 4 (all have 4). Relative = 0 for all.
        # Blank/whitespace-only lines are replaced with just their line ending "\n".
        expected = "  a\n\n  b\n\n  c\n"
        self.ok("MIN-4 blank lines preserved", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_min_05_single_line(self):
        """MIN-5: Single line block."""
        p = self._write("min5.py", "    only_line\nother\n")
        res = self.op_mgr.re_indent(
            path="min5.py", agent_name="test_agent",
            lines="1:1", indent=2, indent_type="space", mode="min"
        )
        content = self._read("min5.py")
        expected = "  only_line\nother\n"
        self.ok("MIN-5 single line block", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_min_06_all_blank(self):
        """MIN-6: All blank lines block."""
        p = self._write("min6.py", "\n\n\nother\n")
        res = self.op_mgr.re_indent(
            path="min6.py", agent_name="test_agent",
            lines="1:3", indent=4, indent_type="space", mode="min"
        )
        content = self._read("min6.py")
        # Should be a no-op; blank lines unchanged.
        expected = "\n\n\nother\n"
        self.ok("MIN-6 all blank block", content == expected and "OK:" in res,
                f"got [{content}]")

    # ═══════════════════════════════════════════════
    #  SHIFT MODE TESTS (7-12)
    # ═══════════════════════════════════════════════

    def test_shift_07_positive_spaces(self):
        """SHIFT-7: Positive shift — add spaces to existing ws (input has 0 ws)."""
        print("\n── Shift Mode ──")
        p = self._write("sh7.py", "def foo():\nbar()\n")
        res = self.op_mgr.re_indent(
            path="sh7.py", agent_name="test_agent",
            lines="1:2", indent=4, indent_type="space", mode="shift"
        )
        content = self._read("sh7.py")
        # Positive shift prepends 4 spaces to existing ws (0+4=4)
        expected = "    def foo():\n    bar()\n"
        self.ok("SHIFT-7 positive spaces", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_shift_08_positive_tabs(self):
        """SHIFT-8: Positive shift — add tabs."""
        p = self._write("sh8.py", "def foo():\nbar()\n")
        res = self.op_mgr.re_indent(
            path="sh8.py", agent_name="test_agent",
            lines="1:2", indent=3, indent_type="tab", mode="shift"
        )
        content = self._read("sh8.py")
        expected = "\t\t\tdef foo():\n\t\t\tbar()\n"
        self.ok("SHIFT-8 positive tabs", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_shift_09_negative(self):
        """SHIFT-9: Negative shift — remove chars (type-agnostic)."""
        p = self._write("sh9.py", "        deep()\n            deeper()\n")
        res = self.op_mgr.re_indent(
            path="sh9.py", agent_name="test_agent",
            lines="1:2", indent=-4, indent_type="space", mode="shift"
        )
        content = self._read("sh9.py")
        # Remove min(4, ws_count) leading chars from each line: 8→4, 12→8
        expected = "    deep()\n        deeper()\n"
        self.ok("SHIFT-9 negative shift", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_shift_10_over_remove(self):
        """SHIFT-10: Negative shift removes more than available (clamp to 0)."""
        p = self._write("sh10.py", "  a\n\tb\n")
        res = self.op_mgr.re_indent(
            path="sh10.py", agent_name="test_agent",
            lines="1:2", indent=-5, indent_type="space", mode="shift"
        )
        content = self._read("sh10.py")
        expected = "a\nb\n"
        self.ok("SHIFT-10 over-remove clamp", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_shift_11_zero_noop(self):
        """SHIFT-11: Zero indent — no-op."""
        p = self._write("sh11.py", "    a\n\t\tb\n")
        res = self.op_mgr.re_indent(
            path="sh11.py", agent_name="test_agent",
            lines="1:2", indent=0, indent_type="space", mode="shift"
        )
        content = self._read("sh11.py")
        expected = "    a\n\t\tb\n"
        self.ok("SHIFT-11 zero no-op", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_shift_12_mixed_ws(self):
        """SHIFT-12: Mixed whitespace types in input."""
        p = self._write("sh12.py", "\t  mixed()\n  \tabc()\n")
        res = self.op_mgr.re_indent(
            path="sh12.py", agent_name="test_agent",
            lines="1:2", indent=-3, indent_type="space", mode="shift"
        )
        content = self._read("sh12.py")
        # Line 1: "\t  mixed()" → remove 3 chars → "mixed()"
        # Line 2: "  \tabc()" → remove 3 chars → "abc()"
        expected = "mixed()\nabc()\n"
        self.ok("SHIFT-12 mixed ws removal", content == expected and "OK:" in res,
                f"got [{content}]")

    # ═══════════════════════════════════════════════
    #  FLAT MODE TESTS (13-17)
    # ═══════════════════════════════════════════════

    def test_flat_13_spaces(self):
        """FLAT-13: All lines flattened to same indent level with spaces."""
        print("\n── Flat Mode ──")
        p = self._write("fl13.py", "    a\n      b\n  c\n")
        res = self.op_mgr.re_indent(
            path="fl13.py", agent_name="test_agent",
            lines="1:3", indent=2, indent_type="space", mode="flat"
        )
        content = self._read("fl13.py")
        # All non-blank lines → exactly 2 spaces prefix
        expected = "  a\n  b\n  c\n"
        self.ok("FLAT-13 flat spaces", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_flat_14_tabs(self):
        """FLAT-14: All lines flattened to same indent level with tabs."""
        p = self._write("fl14.py", "      a\n  b\n\t\tc\n")
        res = self.op_mgr.re_indent(
            path="fl14.py", agent_name="test_agent",
            lines="1:3", indent=2, indent_type="tab", mode="flat"
        )
        content = self._read("fl14.py")
        # All → 2 tabs prefix
        expected = "\t\ta\n\t\tb\n\t\tc\n"
        self.ok("FLAT-14 flat tabs", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_flat_15_blank_preserved(self):
        """FLAT-15: Blank lines preserved in flat mode."""
        p = self._write("fl15.py", "    a\n\n      b\n  c\n")
        res = self.op_mgr.re_indent(
            path="fl15.py", agent_name="test_agent",
            lines="1:4", indent=3, indent_type="space", mode="flat"
        )
        content = self._read("fl15.py")
        expected = "   a\n\n   b\n   c\n"
        self.ok("FLAT-15 blank preserved", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_flat_16_single_line(self):
        """FLAT-16: Single line block."""
        p = self._write("fl16.py", "      only\nother\n")
        res = self.op_mgr.re_indent(
            path="fl16.py", agent_name="test_agent",
            lines="1:1", indent=4, indent_type="space", mode="flat"
        )
        content = self._read("fl16.py")
        expected = "    only\nother\n"
        self.ok("FLAT-16 single line", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_flat_17_mixed_uniform(self):
        """FLAT-17: Mixed indentation levels all become uniform."""
        p = self._write("fl17.py", "\t\ta\n\tb\n  c\n d\n")
        res = self.op_mgr.re_indent(
            path="fl17.py", agent_name="test_agent",
            lines="1:4", indent=0, indent_type="space", mode="flat"
        )
        content = self._read("fl17.py")
        # All → 0 spaces (no indent)
        expected = "a\nb\nc\nd\n"
        self.ok("FLAT-17 mixed→uniform zero", content == expected and "OK:" in res,
                f"got [{content}]")

    # ═══════════════════════════════════════════════
    #  CONVERT MODE TESTS (18-22)
    # ═══════════════════════════════════════════════

    def test_convert_18_spaces_to_tabs(self):
        """CONVERT-18: Convert spaces to tabs (indent=4 as tab width)."""
        print("\n── Convert Mode ──")
        p = self._write("cv18.py", "    a\n        b\n  c\n")
        res = self.op_mgr.re_indent(
            path="cv18.py", agent_name="test_agent",
            lines="1:3", indent=4, indent_type="tab", mode="convert"
        )
        content = self._read("cv18.py")
        # min visual col (tab_width=indent=4) = 2 (line 3). base_trim = 2.
        # line1: vis=4, rel=4-2=2, total_vis=4+2=6 → 6//4=1 tab + 6%4=2 sp → "\t  "
        # line2: vis=8, rel=8-2=6, total_vis=4+6=10 → 10//4=2 tabs + 10%4=2 sp → "\t\t  "
        # line3: vis=2, rel=2-2=0, total_vis=4+0=4 → 4//4=1 tab + 0 sp → "\t"
        expected = "\t  a\n\t\t  b\n\tc\n"
        self.ok("CONVERT-18 spaces→tabs", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_convert_19_tabs_to_spaces(self):
        """CONVERT-19: Convert tabs to spaces (indent as tab width)."""
        p = self._write("cv19.py", "\t\ta\n\tb\nc\n")
        res = self.op_mgr.re_indent(
            path="cv19.py", agent_name="test_agent",
            lines="1:3", indent=4, indent_type="space", mode="convert"
        )
        content = self._read("cv19.py")
        # indent=4 as tab width. min_visual_col (tab_width=4): c→0, b→4, a→8 → min=0
        # line1: visual=8, rel=8-0=8, total=4+8=12 spaces
        # line2: visual=4, rel=4-0=4, total=4+4=8 spaces
        # line3: visual=0, rel=0, total=4+0=4 spaces
        expected = "            a\n        b\n    c\n"
        self.ok("CONVERT-19 tabs→spaces", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_convert_20_mixed_ws(self):
        """CONVERT-20: Mixed whitespace conversion."""
        p = self._write("cv20.py", "\t  a\n   \tb\nc\n")
        # tab_width=4 (indent param). Line1: \t(4)+sp(1)+sp(1) = visual 6. Line2: 3 sp + \t(4) = visual 7.
        res = self.op_mgr.re_indent(
            path="cv20.py", agent_name="test_agent",
            lines="1:3", indent=4, indent_type="space", mode="convert"
        )
        content = self._read("cv20.py")
        # min_visual_col (tab_width=4): c→0, a→6, b→7 → min=0
        # line1: total_vis=4+6=10 spaces, line2: total_vis=4+7=11 spaces, line3: total_vis=4+0=4 spaces
        expected = "          a\n           b\n    c\n"
        self.ok("CONVERT-20 mixed ws", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_convert_21_hierarchy(self):
        """CONVERT-21: Preserving relative indentation hierarchy after conversion."""
        p = self._write("cv21.py", "    if True:\n        pass\n    else:\n        pass\n")
        res = self.op_mgr.re_indent(
            path="cv21.py", agent_name="test_agent",
            lines="1:4", indent=4, indent_type="tab", mode="convert"
        )
        content = self._read("cv21.py")
        # min_visual_col (tw=4) = 4. rel offsets: line1=0, line2=4-4=4→total=4+4=8→\t\t
        # line3=0→total=4→\t, line4=4→total=8→\t\t
        expected = "\tif True:\n\t\tpass\n\telse:\n\t\tpass\n"
        self.ok("CONVERT-21 hierarchy preserved", content == expected and "OK:" in res,
                f"got [{content}]")

    def test_convert_22_blank_preserved(self):
        """CONVERT-22: Blank lines preserved in convert mode."""
        p = self._write("cv22.py", "    a\n\n        b\n  c\n")
        res = self.op_mgr.re_indent(
            path="cv22.py", agent_name="test_agent",
            lines="1:4", indent=4, indent_type="space", mode="convert"
        )
        content = self._read("cv22.py")
        # min_visual_col (tw=4) = 2. line1 vis=4 rel=2 total_vis=6→6 sp. blank→"\n". line3 vis=8 rel=6 total_vis=10→10 sp. line4 vis=2 rel=0 total_vis=4→4 sp.
        expected = "      a\n\n          b\n    c\n"
        self.ok("CONVERT-22 blank preserved", content == expected and "OK:" in res,
                f"got [{content}]")

    # ═══════════════════════════════════════════════
    #  EDGE CASES (23-28)
    # ═══════════════════════════════════════════════

    def test_edge_23_empty_file(self):
        """EDGE-23: Empty file handling."""
        print("\n── Edge Cases ──")
        p = self._write("edge23.py", "")
        res = self.op_mgr.re_indent(
            path="edge23.py", agent_name="test_agent",
            lines="1:5", indent=4, indent_type="space", mode="min"
        )
        self.ok("EDGE-23 empty file error", "ERROR" in res and "exceeds file length" in res)

    def test_edge_24_oob_ranges(self):
        """EDGE-24: Out of bounds line ranges."""
        p = self._write("edge24.py", "a\nb\nc\n")
        # start too high
        res1 = self.op_mgr.re_indent(
            path="edge24.py", agent_name="test_agent",
            lines="5:10", indent=2, indent_type="space", mode="min"
        )
        self.ok("EDGE-24a start OOB", "ERROR" in res1 and "exceeds file length" in res1)
        # end too high
        res2 = self.op_mgr.re_indent(
            path="edge24.py", agent_name="test_agent",
            lines="1:10", indent=2, indent_type="space", mode="min"
        )
        self.ok("EDGE-24b end OOB", "ERROR" in res2 and "exceeds file length" in res2)

    def test_edge_25_invalid_mode(self):
        """EDGE-25: Invalid mode name."""
        p = self._write("edge25.py", "a\nb\nc\n")
        res = self.op_mgr.re_indent(
            path="edge25.py", agent_name="test_agent",
            lines="1:3", indent=2, indent_type="space", mode="quantum"
        )
        self.ok("EDGE-25 invalid mode", "ERROR" in res and "Invalid mode" in res)

    def test_edge_26_negative_indent(self):
        """EDGE-26: Negative indent for non-shift modes."""
        p = self._write("edge26.py", "a\nb\nc\n")
        # min mode with negative indent
        res1 = self.op_mgr.re_indent(
            path="edge26.py", agent_name="test_agent",
            lines="1:3", indent=-2, indent_type="space", mode="min"
        )
        self.ok("EDGE-26a min negative", "ERROR" in res1 and "non-negative" in res1)
        # flat mode with negative indent
        p2 = self._write("edge26b.py", "a\nb\nc\n")
        res2 = self.op_mgr.re_indent(
            path="edge26b.py", agent_name="test_agent",
            lines="1:3", indent=-2, indent_type="space", mode="flat"
        )
        self.ok("EDGE-26b flat negative", "ERROR" in res2 and "non-negative" in res2)

    def test_edge_27_crlf(self):
        """EDGE-27: CRLF line endings preservation.

        Note: read_text() normalizes \\r\\n → \\n on all platforms, so the tool
        effectively converts CRLF to LF. We verify the re-indentation logic is
        correct (ws recalculation) and that the output lines are properly formed.
        """
        p = self._write("edge27.py", "    a\r\n        b\r\n  c\r\n")
        res = self.op_mgr.re_indent(
            path="edge27.py", agent_name="test_agent",
            lines="1:3", indent=2, indent_type="space", mode="min"
        )
        content = self._read("edge27.py")
        # CRLF→LF normalized by read_text. min ws=2. rel: line1=4-2=2 total=4, line2=8-2=6 total=8, line3=0 total=2
        expected = "    a\n        b\n  c\n"
        self.ok("EDGE-27 CRLF preserved", content == expected and "OK:" in res,
                f"got [{repr(content)}]")

    def test_edge_28_mixed_endings(self):
        """EDGE-28: Mixed line endings (CRLF and LF).

        Note: read_text() normalizes all \\r\\n → \\n, so mixed endings become uniform.
        We verify the re-indentation logic handles the content correctly regardless.
        """
        p = self._write("edge28.py", "    a\r\n        b\n  c\r\n")
        res = self.op_mgr.re_indent(
            path="edge28.py", agent_name="test_agent",
            lines="1:3", indent=0, indent_type="space", mode="min"
        )
        content = self._read("edge28.py")
        # All CRLF→LF by read_text. min ws=2. rel: line1=4-2=2 total=2, line2=8-2=6 total=6, line3=0 total=0
        expected = "  a\n      b\nc\n"
        self.ok("EDGE-28 mixed endings", content == expected and "OK:" in res,
                f"got [{repr(content)}]")

    # ═══════════════════════════════════════════════
    #  RUNNER
    # ═══════════════════════════════════════════════

    def run_all(self):
        print("=" * 60)
        print("re_indent Comprehensive Test Suite — All Modes")
        print("=" * 60)

        # MIN mode tests
        self.test_min_01_basic_spaces()
        self.test_min_02_tabs()
        self.test_min_03_mixed_levels()
        self.test_min_04_blank_lines()
        self.test_min_05_single_line()
        self.test_min_06_all_blank()

        # SHIFT mode tests
        self.test_shift_07_positive_spaces()
        self.test_shift_08_positive_tabs()
        self.test_shift_09_negative()
        self.test_shift_10_over_remove()
        self.test_shift_11_zero_noop()
        self.test_shift_12_mixed_ws()

        # FLAT mode tests
        self.test_flat_13_spaces()
        self.test_flat_14_tabs()
        self.test_flat_15_blank_preserved()
        self.test_flat_16_single_line()
        self.test_flat_17_mixed_uniform()

        # CONVERT mode tests
        self.test_convert_18_spaces_to_tabs()
        self.test_convert_19_tabs_to_spaces()
        self.test_convert_20_mixed_ws()
        self.test_convert_21_hierarchy()
        self.test_convert_22_blank_preserved()

        # Edge cases
        self.test_edge_23_empty_file()
        self.test_edge_24_oob_ranges()
        self.test_edge_25_invalid_mode()
        self.test_edge_26_negative_indent()
        self.test_edge_27_crlf()
        self.test_edge_28_mixed_endings()

        # ── Summary ────────────────────────────────
        total = self.passed + self.failed
        print("\n" + "=" * 60)
        print(f"RESULTS: {self.passed}/{total} tests passed, {self.failed} failed")
        if self.bugs:
            print(f"\nBUGS FOUND ({len(self.bugs)}):")
            for i, b in enumerate(self.bugs, 1):
                print(f"\n  Bug #{i}: mode={b['mode']}, scenario={b['scenario']}")
                print(f"    Input:    {b['input']}")
                print(f"    Expected: {b['expected']}")
                print(f"    Actual:   {b['actual']}")
        if self.failed == 0 and not self.bugs:
            print("VERDICT: PASS — All modes verified correctly")
        else:
            print(f"VERDICT: FAIL — {self.failed} test(s) need attention")
        print("=" * 60)

        return 0 if (self.failed == 0 and not self.bugs) else 1


# ──────────────── pytest compatibility ────────────────

def test_min_basic_spaces():
    t = ReIndentTester()
    t.test_min_01_basic_spaces()
    assert t.failed == 0, f"test_min_basic_spaces failed: {t.passed} passed, {t.failed} failed"


# ──────────────── main entry point ────────────────

if __name__ == "__main__":
    tester = ReIndentTester()
    sys.exit(tester.run_all())