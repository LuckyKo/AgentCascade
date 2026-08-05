"""
Performance benchmark for the ReadFile tool (agent_cascade.tools.custom.file_ops).

Measures:
  - Total read_file.call() latency across multiple iterations
  - Per-step timing breakdown (_resolve_path, _is_binary_file, file I/O, formatting)
  - Min / max / mean / median latencies
  - Sleep call detection in the code path
"""

import sys
import os
import time
import statistics
import tempfile
from pathlib import Path
from typing import List

# Ensure project root is on path for imports
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)

from agent_cascade.tools.custom.file_ops import ReadFile
from agent_cascade.settings import DEFAULT_WORKSPACE


# ── Constants ────────────────────────────────────────────────────────────────
NUM_ITERATIONS  = 50          # Benchmark iterations per test file
TEST_FILE_LINES = 200         # Lines in the small test file
LARGE_FILE_LINES = 10_000     # Lines in the large test file

# Use DEFAULT_WORKSPACE for test files so _resolve_path() works correctly
WORK_DIR = Path(DEFAULT_WORKSPACE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def create_test_file(path: Path, num_lines: int) -> None:
    """Create a deterministic text file for benchmarking."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, num_lines + 1):
            # ~60 chars per line → realistic content size
            f.write(f"Line {i:05d}: This is a test line with some meaningful content for benchmarking.\n")


def stats(label: str, times_ms: List[float]) -> None:
    """Print min/max/mean/median statistics for a list of millisecond timings."""
    n = len(times_ms)
    print(f"\n{'─' * 60}")
    print(f"  {label}  (n={n})")
    print(f"{'─' * 60}")
    print(f"  Min   : {min(times_ms):>10.3f} ms")
    print(f"  Max   : {max(times_ms):>10.3f} ms")
    print(f"  Mean  : {statistics.mean(times_ms):>10.3f} ms")
    if n >= 2:
        print(f"  Median: {statistics.median(times_ms):>10.3f} ms")
        print(f"  StdDev: {statistics.stdev(times_ms):>10.3f} ms")
    else:
        print(f"  StdDev: N/A (single sample)")
    p95 = sorted(times_ms)[int(n * 0.95)] if n > 1 else times_ms[0]
    p99 = sorted(times_ms)[int(n * 0.99)] if n > 1 else times_ms[0]
    print(f"  P95   : {p95:>10.3f} ms")
    print(f"  P99   : {p99:>10.3f} ms")


def detect_sleep_calls() -> List[str]:
    """Scan the read_file code path for sleep() calls."""
    found: List[str] = []
    
    # Check file_ops.py
    src = Path(PROJECT_ROOT) / "agent_cascade" / "tools" / "custom" / "file_ops.py"
    with open(src, "r") as f:
        lines = f.readlines()
    for i, line in enumerate(lines, 1):
        if "sleep" in line and not line.strip().startswith("#"):
            found.append(f"  file_ops.py:{i} → {line.strip()}")
    
    # Check base tool class
    src2 = Path(PROJECT_ROOT) / "agent_cascade" / "tools" / "base.py"
    with open(src2, "r") as f:
        lines = f.readlines()
    for i, line in enumerate(lines, 1):
        if "sleep" in line and not line.strip().startswith("#"):
            found.append(f"  base.py:{i} → {line.strip()}")

    # Check settings module (lazy imports can cause delays)
    src3 = Path(PROJECT_ROOT) / "agent_cascade" / "settings.py"
    with open(src3, "r") as f:
        content = f.read()
    if "sleep" in content and "# sleep" not in content.lower():
        found.append(f"  settings.py → contains sleep() calls")

    return found


# ── Benchmark: _resolve_path latency ────────────────────────────────────────

def benchmark_resolve_path(tool: ReadFile, test_file: Path) -> List[float]:
    """Time the _resolve_path method alone."""
    rel = test_file.relative_to(WORK_DIR)
    times_ms: List[float] = []
    
    for _ in range(NUM_ITERATIONS):
        t0 = time.perf_counter()
        resolved = tool._resolve_path(str(rel))
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)
    
    return times_ms


# ── Benchmark: full call() latency ───────────────────────────────────────

def benchmark_full_call(tool: ReadFile, test_file: Path, label: str) -> List[float]:
    """Time the complete read_file.call() invocation."""
    rel = test_file.relative_to(WORK_DIR)
    times_ms: List[float] = []
    
    for _ in range(NUM_ITERATIONS):
        t0 = time.perf_counter()
        result = tool.call({"path": str(rel), "start_line": 1, "limit": TEST_FILE_LINES})
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)
        
        # Sanity: verify we got content back
        assert isinstance(result, str) and len(result) > 50, f"Unexpected result on iteration {_}"
    
    return times_ms


# ── Benchmark: per-step breakdown (single iteration with fine-grained timing) ─

def benchmark_step_breakdown(tool: ReadFile, test_file: Path) -> None:
    """Dissect a single call() into component timings."""
    rel = str(test_file.relative_to(WORK_DIR))
    
    print(f"\n{'=' * 60}")
    print("  Per-Step Breakdown (single iteration)")
    print(f"{'=' * 60}")
    
    steps: List[tuple] = []  # (step_name, elapsed_ms)
    
    # Step 1: _verify_json_format_args (inside call())
    t0 = time.perf_counter()
    params = tool._verify_json_format_args({"path": rel, "start_line": 1, "limit": TEST_FILE_LINES})
    steps.append(("JSON arg parsing", (time.perf_counter() - t0) * 1000))
    
    # Step 2: _resolve_path
    t0 = time.perf_counter()
    resolved = tool._resolve_path(params["path"])
    steps.append(("_resolve_path", (time.perf_counter() - t0) * 1000))
    
    # Step 3: file existence checks
    t0 = time.perf_counter()
    _ = resolved.exists() and resolved.is_file()
    steps.append(("exists + is_file check", (time.perf_counter() - t0) * 1000))
    
    # Step 4: _calculate_char_limit
    t0 = time.perf_counter()
    char_limit = tool._calculate_char_limit({}, True, 10000)
    steps.append(("_calculate_char_limit", (time.perf_counter() - t0) * 1000))
    
    # Step 5: _is_binary_file
    from agent_cascade.tools.custom.file_ops import _is_binary_file
    t0 = time.perf_counter()
    is_bin = _is_binary_file(resolved)
    steps.append(("_is_binary_file", (time.perf_counter() - t0) * 1000))
    
    # Step 6: _determine_limits
    t0 = time.perf_counter()
    limit, is_wild = tool._determine_limits(TEST_FILE_LINES)
    steps.append(("_determine_limits", (time.perf_counter() - t0) * 1000))
    
    # Step 7: _read_text_file (the actual file I/O + formatting)
    t0 = time.perf_counter()
    result = tool._read_text_file(rel, resolved, 1, TEST_FILE_LINES, char_limit)
    steps.append(("_read_text_file (I/O + format)", (time.perf_counter() - t0) * 1000))
    
    # Print breakdown table
    total = sum(s[1] for s in steps)
    print(f"  {'Step':<45} {'Time (ms)':>10} {'% of Total':>12}")
    print(f"  {'─' * 68}")
    for name, ms in steps:
        pct = (ms / total * 100) if total > 0 else 0
        print(f"  {name:<45} {ms:>10.3f}  {pct:>11.1f}%")
    print(f"  {'─' * 68}")
    print(f"  {'TOTAL':<45} {total:>10.3f}  {'100.0%':>12}")


# ── Benchmark: large file read ─────────────────────────────────────────────

def benchmark_large_file(tool: ReadFile, test_file: Path) -> List[float]:
    """Time reading a large file (tests streaming / line counting overhead)."""
    rel = test_file.relative_to(WORK_DIR)
    times_ms: List[float] = []
    
    for _ in range(min(NUM_ITERATIONS, 20)):  # Fewer iterations for large files
        t0 = time.perf_counter()
        result = tool.call({"path": str(rel), "start_line": 1, "limit": -1})
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)
    
    return times_ms


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  ReadFile Tool Performance Benchmark")
    print(f"{'=' * 60}")
    print(f"  Project root : {PROJECT_ROOT}")
    print(f"  Iterations   : {NUM_ITERATIONS}")
    
    # ── Sleep call detection ───────────────────────────────────────────────
    sleeps = detect_sleep_calls()
    if sleeps:
        print("\n⚠  SLEEP CALLS FOUND in the read_file code path:")
        for s in sleeps:
            print(s)
    else:
        print("\n✓ No sleep() calls found in the read_file code path.")

    # ── Create test files inside DEFAULT_WORKSPACE (so _resolve_path works) ──
    tmp_dir = WORK_DIR / "_perf_test_tmp"
    tmp_dir.mkdir(exist_ok=True)
    
    small_file = tmp_dir / "small_test.txt"
    large_file = tmp_dir / "large_test.txt"
    
    create_test_file(small_file, TEST_FILE_LINES)
    create_test_file(large_file, LARGE_FILE_LINES)
    
    print(f"\n  DEFAULT_WORKSPACE: {DEFAULT_WORKSPACE}")
    print(f"  Small test file: {small_file} ({TEST_FILE_LINES} lines)")
    print(f"  Large test file: {large_file} ({LARGE_FILE_LINES} lines)")

    # ── Instantiate ReadFile tool (no agent_pool = fallback path resolution) ─
    tool = ReadFile()

    try:
        # ── Test 1: _resolve_path latency ───────────────────────────────────
        resolve_times = benchmark_resolve_path(tool, small_file)
        stats("Path Resolution (_resolve_path)", resolve_times)

        # ── Test 2: Full call() latency (small file) ────────────────────────
        full_small_times = benchmark_full_call(
            tool, small_file, "Full read_file.call() — Small File"
        )
        stats("Full call() — Small File", full_small_times)

        # ── Test 3: Full call() latency (large file) ────────────────────────
        full_large_times = benchmark_large_file(tool, large_file)
        stats("Full call() — Large File", full_large_times)

        # ── Test 4: Per-step breakdown ──────────────────────────────────────
        benchmark_step_breakdown(tool, small_file)

        # ── Summary ─────────────────────────────────────────────────────────
        print(f"\n{'=' * 60}")
        print("  SUMMARY")
        print(f"{'=' * 60}")
        
        avg_small = statistics.mean(full_small_times)
        avg_large = statistics.mean(full_large_times) if full_large_times else 0
        avg_resolve = statistics.mean(resolve_times)
        
        print(f"  Average call() small file : {avg_small:.2f} ms")
        print(f"  Average call() large file : {avg_large:.2f} ms")
        print(f"  Average _resolve_path     : {avg_resolve:.2f} ms")
        
        overhead_pct = (avg_resolve / avg_small * 100) if avg_small > 0 else 0
        print(f"  Path resolution % of total: {overhead_pct:.1f}%")
        
        # Flag potential issues
        print(f"\n  Observations:")
        if avg_small > 50:
            print(f"    ⚠ Small file reads average >50ms — investigate bottlenecks")
        else:
            print(f"    ✓ Small file reads are fast (<50ms)")
        
        if avg_resolve > 10:
            print(f"    ⚠ Path resolution averages >10ms — consider caching")
        else:
            print(f"    ✓ Path resolution is fast (<10ms)")
            
        if avg_large > 500:
            print(f"    ⚠ Large file reads average >500ms — streaming overhead?")
        else:
            print(f"    ✓ Large file reads are reasonable (<500ms)")

    finally:
        # Cleanup temp files
        import shutil
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n  Temp directory cleaned up.")


if __name__ == "__main__":
    main()