"""
Side-by-side comparison test: operation_manager.grep vs shell find/grep/ripgrep.

Tests the grep tool against equivalent shell commands on the actual AgentCascade codebase.
Reports match counts, result overlap, and any discrepancies.

Run with:  python test_grep_compare.py
           or via code_interpreter from within AgentCascade
"""
import os, sys, re, subprocess, json
from pathlib import Path

# ──────────────────────────────────────────────
#  Configuration — auto-detect base directory
# ──────────────────────────────────────────────
_file_dir = Path(__file__).parent.resolve()
# When running inside Docker (code_interpreter), AgentCascade is at extra_rw_0
if (_file_dir / "agent_cascade").exists():
    BASE_DIR = _file_dir
elif (_file_dir / "extra_rw_0" / "agent_cascade").exists():
    BASE_DIR = _file_dir / "extra_rw_0"
else:
    # Fallback: try common Docker mount paths
    for candidate in [Path("/workspace/extra_rw_0"), Path("/workspace/extra_ro_0")]:
        if (candidate / "agent_cascade").exists():
            BASE_DIR = candidate
            break
    else:
        BASE_DIR = _file_dir

AGENT_CASCADE_DIR = BASE_DIR / "agent_cascade"

def is_windows():
    return os.name == "nt"

# ──────────────────────────────────────────────
#  Helpers: Shell command execution
# ──────────────────────────────────────────────
def run_shell_cmd(cmd, cwd=None, timeout=15):
    """Run a shell command and return (stdout_text, stderr_text, return_code)."""
    if cwd is None:
        cwd = str(BASE_DIR)
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=True
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1

def detect_available_tools():
    """Detect which grep tools are available on this system."""
    tools = {}
    tool_list = [
        ("ripgrep", "rg --version"),
        ("grep", "grep --version"),
        ("findstr", "findstr"),
    ]
    if is_windows():
        tool_list.append(("powershell", "powershell -Command '$PSVersionTable'"))
    
    for tool_name, cmd in tool_list:
        try:
            out, err, rc = run_shell_cmd(cmd)
            tools[tool_name] = True
        except Exception:
            tools[tool_name] = False
    return tools

# ──────────────────────────────────────────────
#  Shell grep implementations
# ──────────────────────────────────────────────
def shell_rg(pattern, search_path, include="*.py", ignore_vcs=True, context=0):
    """Use ripgrep (rg) to search for pattern."""
    cmd = ['rg', '-r', '--no-heading', '-n', '--color', 'never', '--no-mmap']
    if not ignore_vcs:
        cmd.extend(['--no-ignore'])
    if context > 0:
        cmd.extend(['-C', str(context)])
    # Smart case: lowercase pattern → case-insensitive
    has_upper = bool(re.search(r'[A-Z]', pattern))
    if not has_upper:
        cmd.append('-i')
    cmd.extend(['--glob', include, pattern])
    
    result = subprocess.run(
        cmd, cwd=str(search_path), capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
        return lines
    elif result.returncode == 1:
        return []  # No matches
    else:
        return None  # Error

def shell_grep(pattern, search_path, include="*.py", ignore_vcs=True, context=0):
    """Use GNU grep to search for pattern."""
    cmd = ['grep', '-r', '--include=' + include, '-n']
    if context > 0:
        cmd.extend(['-C', str(context)])
    has_upper = bool(re.search(r'[A-Z]', pattern))
    if not has_upper:
        cmd.append('-i')
    cmd.append(pattern)
    
    result = subprocess.run(
        cmd, cwd=str(search_path), capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
        return lines
    elif result.returncode == 1:
        return []
    else:
        return None

def shell_powershell_findstr(pattern, search_path, include="*.py", case_insensitive=True):
    """Use PowerShell Select-String (Windows native)."""
    if not is_windows():
        return None
    ps_pattern = pattern.replace('"', "'")
    include_ext = include.replace('*', '').replace('.', '')  # "*.py" → ".py"
    
    cmd = (f'Get-ChildItem -Path "{search_path}" -Recurse -File -Include "*{include_ext}" '
           f'| Select-String -Pattern "{ps_pattern}" '
           f'-CaseSensitive ${"false"' if case_insensitive else '"true"} '
           f'| ForEach-Object {{ $_.Path + ":" + $_.LineNumber }}')
    
    out, err, rc = run_shell_cmd(cmd)
    if out.strip():
        return [l.strip() for l in out.strip().splitlines() if l.strip()]
    return []

# ──────────────────────────────────────────────
#  operation_manager.grep wrapper
# ──────────────────────────────────────────────
def om_grep(pattern, path=".", include="*.py", ignore_vcs=True, context=0):
    """Call the operation_manager.grep tool and parse its output."""
    sys.path.insert(0, str(BASE_DIR))
    from operation_manager import OperationManager
    
    om = OperationManager(base_dir=str(BASE_DIR), agent_name="grep_compare_test")
    
    result = om.grep(
        pattern=pattern, path=path, include=include,
        ignore_vcs=ignore_vcs, context=context, char_limit=-1  # unlimited for testing
    )
    return result

def parse_om_result(result_str):
    """Parse operation_manager.grep output into list of match lines."""
    if "Directory not found" in result_str:
        return None, 0
    if "No matches found" in result_str:
        return [], 0
    
    # Extract count from summary like "Found X matches for..."
    count_match = re.search(r'Found\s+(\d+)\s+matches', result_str)
    count = int(count_match.group(1)) if count_match else 0
    
    # Parse individual match lines (format: file:line: content)
    lines = []
    for line in result_str.split('\n'):
        if line.startswith('Found ') or not line.strip():
            continue
        # Match lines have format "file:line_number: content"
        m = re.match(r'^(.+?):(\d+):\s*(.*)$', line)
        if m:
            lines.append(f"{m.group(1)}:{m.group(2)}:{m.group(3)}")
    
    return lines, count

# ──────────────────────────────────────────────
#  Extract file:line pairs for comparison
# ──────────────────────────────────────────────
def extract_file_line_pairs(lines):
    """Extract (file, line_number) tuples from grep output for comparison."""
    pairs = set()
    pattern = re.compile(r'^(.+?):(\d+):')
    for line in lines:
        m = pattern.match(line)
        if m:
            # Normalize path separators
            filepath = m.group(1).replace('\\', '/')
            line_num = int(m.group(2))
            pairs.add((filepath, line_num))
    return pairs

# ──────────────────────────────────────────────
#  Test runner
# ──────────────────────────────────────────────
class GrepCompareTest:
    def __init__(self):
        self.results = []
        self.passed = 0
        self.failed = 0
        self.warnings = 0
    
    def log(self, msg):
        print(msg)
    
    def section_header(self, title):
        print(f"\n{'='*70}")
        print(f"  {title}")
        print(f"{'='*70}\n")
    
    def sub_header(self, title):
        print(f"  --- {title} ---\n")
    
    def compare_results(self, test_name, om_lines, om_count, shell_lines, shell_name, 
                       expect_matches=True):
        """Compare operation_manager.grep results with shell grep results."""
        self.sub_header(test_name)
        
        om_pairs = extract_file_line_pairs(om_lines)
        shell_pairs = extract_file_line_pairs(shell_lines)
        
        # Count comparison
        print(f"  operation_manager.grep count: {om_count}")
        print(f"  {shell_name} count:          {len(shell_pairs)}")
        
        # Result overlap analysis
        common = om_pairs & shell_pairs
        only_om = om_pairs - shell_pairs
        only_shell = shell_pairs - om_pairs
        
        passed = True
        
        if expect_matches:
            # Both should find matches
            if not om_lines and not shell_lines:
                self.log(f"  ⚠ Both tools found no matches — verify pattern is correct")
                self.warnings += 1
            elif not om_lines:
                self.log(f"  ❌ operation_manager.grep found NOTHING but {shell_name} found {len(shell_pairs)} matches")
                self.log(f"     Shell-only matches (sample):")
                for pair in list(shell_pairs)[:5]:
                    self.log(f"       {pair[0]}:{pair[1]}")
                passed = False
            elif not shell_lines:
                self.log(f"  ❌ {shell_name} found NOTHING but operation_manager.grep found {om_count} matches")
                passed = False
            
            # Check overlap
            if om_pairs and shell_pairs:
                overlap_pct = len(common) / max(len(om_pairs), len(shell_pairs)) * 100
                self.log(f"  Overlap: {len(common)}/{max(len(om_pairs), len(shell_pairs))} ({overlap_pct:.0f}%)")
                
                if only_om:
                    self.log(f"  Only in operation_manager.grep ({len(only_om)}):")
                    for pair in sorted(list(only_om))[:5]:
                        self.log(f"    {pair[0]}:{pair[1]}")
                    # This is OK — OM may find more due to different path normalization
                
                if only_shell:
                    self.log(f"  Only in {shell_name} ({len(only_shell)}):")
                    for pair in sorted(list(only_shell))[:5]:
                        self.log(f"    {pair[0]}:{pair[1]}")
                
                # Significant discrepancy = warning
                if overlap_pct < 80 and om_pairs and shell_pairs:
                    self.log(f"  ⚠ Low overlap ({overlap_pct:.0f}%) — possible bug")
                    self.warnings += 1
        
        if passed:
            self.log(f"  ✅ PASS: Results are consistent")
            self.passed += 1
        else:
            self.log(f"  ❌ FAIL: Significant discrepancy detected")
            self.failed += 1
    
    def run(self):
        """Execute all comparison tests."""
        self.section_header("Grep Tool Comparison: operation_manager.grep vs Shell Tools")
        
        # Detect available shell tools
        tools = detect_available_tools()
        self.log(f"System: {'Windows' if is_windows() else 'Unix-like'}")
        self.log(f"Available tools:")
        for tool, available in tools.items():
            status = "✅" if available else "❌"
            self.log(f"  {status} {tool}")
        
        # ── TEST GROUP 1: Basic pattern searches ──
        self.section_header("TEST GROUP 1: Basic Pattern Searches")
        
        # Test 1a: "def hello" in "." with include "*.py"
        test1a_path = "."
        test1a_pattern = "def hello"
        self.log(f"Pattern: '{test1a_pattern}' | Path: '{test1a_path}' | Include: '*.py'")
        
        om_result_1a = om_grep(test1a_pattern, path=test1a_path, include="*.py")
        om_lines_1a, om_count_1a = parse_om_result(om_result_1a)
        
        # Compare with ripgrep
        if tools.get('ripgrep'):
            rg_lines_1a = shell_rg(test1a_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 1a: vs ripgrep", om_lines_1a, om_count_1a, 
                              rg_lines_1a, "ripgrep", expect_matches=True)
        
        # Compare with GNU grep
        if tools.get('grep'):
            grep_lines_1a = shell_grep(test1a_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 1a: vs GNU grep", om_lines_1a, om_count_1a, 
                               grep_lines_1a, "GNU grep", expect_matches=True)
        
        # Compare with PowerShell (Windows)
        if tools.get('powershell'):
            ps_lines_1a = shell_powershell_findstr(test1a_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 1a: vs PowerShell", om_lines_1a, om_count_1a, 
                               ps_lines_1a, "PowerShell Select-String", expect_matches=True)
        
        # Test 1b: "class.*Operation" in "." with include "*.py"
        test1b_pattern = "class.*Operation"
        self.log(f"\nPattern: '{test1b_pattern}' | Path: '.' | Include: '*.py'")
        
        om_result_1b = om_grep(test1b_pattern, path=".", include="*.py")
        om_lines_1b, om_count_1b = parse_om_result(om_result_1b)
        
        if tools.get('ripgrep'):
            rg_lines_1b = shell_rg(test1b_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 1b: vs ripgrep", om_lines_1b, om_count_1b, 
                              rg_lines_1b, "ripgrep", expect_matches=True)
        
        if tools.get('grep'):
            grep_lines_1b = shell_grep(test1b_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 1b: vs GNU grep", om_lines_1b, om_count_1b, 
                               grep_lines_1b, "GNU grep", expect_matches=True)
        
        # Test 1c: "logger\." in "agent_cascade" with include "*.py"
        test1c_pattern = r'logger\.'
        self.log(f"\nPattern: '{test1c_pattern}' | Path: 'agent_cascade' | Include: '*.py'")
        
        om_result_1c = om_grep(test1c_pattern, path="agent_cascade", include="*.py")
        om_lines_1c, om_count_1c = parse_om_result(om_result_1c)
        
        if tools.get('ripgrep'):
            rg_lines_1c = shell_rg(test1c_pattern, AGENT_CASCADE_DIR, include="*.py")
            self.compare_results("Test 1c: vs ripgrep", om_lines_1c, om_count_1c, 
                              rg_lines_1c, "ripgrep", expect_matches=True)
        
        if tools.get('grep'):
            grep_lines_1c = shell_grep(test1c_pattern, AGENT_CASCADE_DIR, include="*.py")
            self.compare_results("Test 1c: vs GNU grep", om_lines_1c, om_count_1c, 
                               grep_lines_1c, "GNU grep", expect_matches=True)
        
        # ── TEST GROUP 2: Bug reproduction - patterns known to exist ──
        self.section_header("TEST GROUP 2: Bug Reproduction (Patterns Known to Exist)")
        
        # Test 2a: Search for something we KNOW exists — "OperationManager" class
        test2a_pattern = r"class OperationManager"
        self.log(f"Pattern: '{test2a_pattern}' — should find operation_manager.py")
        
        om_result_2a = om_grep(test2a_pattern, path=".", include="*.py")
        om_lines_2a, om_count_2a = parse_om_result(om_result_2a)
        self.log(f"  operation_manager.grep found {om_count_2a} matches")
        
        if tools.get('ripgrep'):
            rg_lines_2a = shell_rg(test2a_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 2a: vs ripgrep", om_lines_2a, om_count_2a, 
                              rg_lines_2a, "ripgrep", expect_matches=True)
        
        # Test 2b: Search for import statements — should find many matches
        test2b_pattern = r'^import\s+\w+'
        self.log(f"\nPattern: '{test2b_pattern}' — should find many Python import lines")
        
        om_result_2b = om_grep(test2b_pattern, path=".", include="*.py")
        om_lines_2b, om_count_2b = parse_om_result(om_result_2b)
        self.log(f"  operation_manager.grep found {om_count_2b} matches")
        
        if tools.get('ripgrep'):
            rg_lines_2b = shell_rg(test2b_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 2b: vs ripgrep", om_lines_2b, om_count_2b, 
                              rg_lines_2b, "ripgrep", expect_matches=True)
        
        # Test 2c: Search in subdirectory — should NOT return empty
        test2c_pattern = r"def __init__"
        self.log(f"\nPattern: '{test2c_pattern}' in 'agent_cascade' — should find many __init__ methods")
        
        om_result_2c = om_grep(test2c_pattern, path="agent_cascade", include="*.py")
        om_lines_2c, om_count_2c = parse_om_result(om_result_2c)
        self.log(f"  operation_manager.grep found {om_count_2c} matches")
        
        if tools.get('ripgrep'):
            rg_lines_2c = shell_rg(test2c_pattern, AGENT_CASCADE_DIR, include="*.py")
            self.compare_results("Test 2c: vs ripgrep", om_lines_2c, om_count_2c, 
                              rg_lines_2c, "ripgrep", expect_matches=True)
        
        # Test 2d: Simple literal string search — most basic case
        test2d_pattern = "TODO"
        self.log(f"\nPattern: '{test2d_pattern}' — simple literal string")
        
        om_result_2d = om_grep(test2d_pattern, path=".", include="*.py")
        om_lines_2d, om_count_2d = parse_om_result(om_result_2d)
        self.log(f"  operation_manager.grep found {om_count_2d} matches")
        
        if tools.get('ripgrep'):
            rg_lines_2d = shell_rg(test2d_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 2d: vs ripgrep", om_lines_2d, om_count_2d, 
                              rg_lines_2d, "ripgrep", expect_matches=True)
        
        # ── TEST GROUP 3: Hidden directory search (ignore_vcs=False) ──
        self.section_header("TEST GROUP 3: Hidden Directory Search (ignore_vcs=False)")
        
        # Test 3a: Search in .pytest_cache with ignore_vcs=False
        test3a_pattern = r"def "
        self.log(f"Pattern: '{test3a_pattern}' | Path: '.' | ignore_vcs=False")
        self.log("  Should find matches in .git, .pytest_cache, __pycache__ etc.")
        
        om_result_3a = om_grep(test3a_pattern, path=".", include="*.py", ignore_vcs=False)
        om_lines_3a, om_count_3a = parse_om_result(om_result_3a)
        self.log(f"  operation_manager.grep (ignore_vcs=False) found {om_count_3a} matches")
        
        # Check if any matches are from hidden directories
        hidden_matches_3a = [l for l in om_lines_3a if '.pytest_cache' in l or '.git/' in l]
        self.log(f"  Matches from hidden dirs: {len(hidden_matches_3a)}")
        
        # Compare with ignore_vcs=True (default)
        om_result_3a_default = om_grep(test3a_pattern, path=".", include="*.py", ignore_vcs=True)
        om_lines_3a_default, om_count_3a_default = parse_om_result(om_result_3a_default)
        self.log(f"  operation_manager.grep (ignore_vcs=True) found {om_count_3a_default} matches")
        
        if om_count_3a > om_count_3a_default:
            diff = om_count_3a - om_count_3a_default
            self.log(f"  ✅ ignore_vcs=False found {diff} more matches (expected)")
            self.passed += 1
        else:
            self.log(f"  ⚠ No difference between ignore_vcs=True and False — possible bug")
            self.warnings += 1
        
        if tools.get('ripgrep'):
            # rg with --no-ignore should find hidden dirs
            rg_lines_3a = shell_rg(test3a_pattern, BASE_DIR, include="*.py", ignore_vcs=False)
            if rg_lines_3a is not None:
                self.compare_results("Test 3a: vs ripgrep (no-ignore)", om_lines_3a, om_count_3a, 
                                  rg_lines_3a, "ripgrep --no-ignore", expect_matches=True)
        
        # Test 3b: Specifically search in .pytest_cache directory
        test3b_pattern = r"cache"
        pytest_cache_dir = BASE_DIR / ".pytest_cache"
        if pytest_cache_dir.exists():
            self.log(f"\nPattern: '{test3b_pattern}' | Path: '.pytest_cache'")
            self.log("  Searching directly in .pytest_cache directory")
            
            # This may not work as .pytest_cache has .json files, not .py
            # Let's search all files instead
            om_result_3b = om_grep(test3b_pattern, path=".pytest_cache", include="*")
            om_lines_3b, om_count_3b = parse_om_result(om_result_3b)
            self.log(f"  operation_manager.grep found {om_count_3b} matches in .pytest_cache")
            
            if tools.get('ripgrep'):
                rg_lines_3b = shell_rg(test3b_pattern, pytest_cache_dir, include="*", ignore_vcs=False)
                if rg_lines_3b is not None:
                    self.compare_results("Test 3b: vs ripgrep in .pytest_cache", om_lines_3b, om_count_3b, 
                                      rg_lines_3b, "ripgrep --no-ignore", expect_matches=True)
        else:
            self.log(f"\n  ℹ .pytest_cache directory not found — skipping Test 3b")
        
        # ── TEST GROUP 4: Edge cases ──
        self.section_header("TEST GROUP 4: Edge Cases")
        
        # Test 4a: Pattern that should find NOTHING
        test4a_pattern = "xyz_nonexistent_pattern_12345"
        self.log(f"Pattern: '{test4a_pattern}' — should find no matches")
        
        om_result_4a = om_grep(test4a_pattern, path=".", include="*.py")
        om_lines_4a, om_count_4a = parse_om_result(om_result_4a)
        
        if om_count_4a == 0:
            self.log(f"  ✅ Correctly found no matches")
            self.passed += 1
        else:
            self.log(f"  ❌ Incorrectly found {om_count_4a} matches for nonexistent pattern")
            self.failed += 1
        
        # Test 4b: Case sensitivity — search for "Logger" (capital L)
        test4b_pattern = "Logger"
        self.log(f"\nPattern: '{test4b_pattern}' — should find class Logger (case-sensitive)")
        
        om_result_4b = om_grep(test4b_pattern, path=".", include="*.py")
        om_lines_4b, om_count_4b = parse_om_result(om_result_4b)
        self.log(f"  operation_manager.grep found {om_count_4b} matches")
        
        # Compare with lowercase "logger" — should find MORE matches (smart_case makes it case-insensitive)
        test4b_lower_pattern = "logger"
        om_result_4b_lower = om_grep(test4b_lower_pattern, path=".", include="*.py")
        om_lines_4b_lower, om_count_4b_lower = parse_om_result(om_result_4b_lower)
        self.log(f"  Pattern 'logger' (lowercase) found {om_count_4b_lower} matches")
        
        if om_count_4b_lower >= om_count_4b:
            self.log(f"  ✅ Lowercase pattern found >= matches (smart_case working)")
            self.passed += 1
        else:
            self.log(f"  ⚠ Lowercase pattern found fewer matches — unexpected")
            self.warnings += 1
        
        # Test 4c: Regex special characters
        test4c_pattern = r'\.py$'
        self.log(f"\nPattern: '{test4c_pattern}' — regex with special chars")
        
        om_result_4c = om_grep(test4c_pattern, path=".", include="*.py")
        om_lines_4c, om_count_4c = parse_om_result(om_result_4c)
        self.log(f"  operation_manager.grep found {om_count_4c} matches")
        
        if tools.get('ripgrep'):
            rg_lines_4c = shell_rg(test4c_pattern, BASE_DIR, include="*.py")
            self.compare_results("Test 4c: vs ripgrep", om_lines_4c, om_count_4c, 
                              rg_lines_4c, "ripgrep", expect_matches=True)
        
        # ── TEST GROUP 5: Context mode ──
        self.section_header("TEST GROUP 5: Context Mode")
        
        test5_pattern = r"class OperationManager"
        self.log(f"Pattern: '{test5_pattern}' with context=2")
        
        om_result_5 = om_grep(test5_pattern, path=".", include="*.py", context=2)
        self.log(f"  operation_manager.grep (context=2) output:\n{om_result_5[:500]}...")
        
        # Count match lines vs context lines
        match_lines = [l for l in om_result_5.split('\n') if '>>>' in l]
        self.log(f"  Match lines: {len(match_lines)}")
        
        if len(match_lines) > 0:
            self.log(f"  ✅ Context mode working — found matches with context markers")
            self.passed += 1
        else:
            self.log(f"  ❌ No match lines found in context mode output")
            self.failed += 1
        
        # ── SUMMARY ──
        self.section_header("SUMMARY")
        
        total_tests = self.passed + self.failed
        self.log(f"Tests passed: {self.passed}")
        self.log(f"Tests failed: {self.failed}")
        self.log(f"Warnings:     {self.warnings}")
        self.log(f"Total tests:  {total_tests}")
        
        if total_tests > 0:
            pass_rate = self.passed / total_tests * 100
            self.log(f"Pass rate:    {pass_rate:.0f}%")
        
        # Save detailed results to JSON for further analysis
        summary = {
            "passed": self.passed,
            "failed": self.failed,
            "warnings": self.warnings,
            "total_tests": total_tests,
            "pass_rate_pct": round(self.passed / max(total_tests, 1) * 100, 1),
            "available_tools": tools,
            "system": "Windows" if is_windows() else "Unix-like",
        }
        
        summary_path = BASE_DIR / "test_grep_compare_results.json"
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)
        self.log(f"\nDetailed results saved to: {summary_path}")
        
        return summary

# ──────────────────────────────────────────────
#  Main entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    tester = GrepCompareTest()
    result = tester.run()
    
    # Exit with non-zero code if any tests failed
    sys.exit(1 if result["failed"] > 0 else 0)