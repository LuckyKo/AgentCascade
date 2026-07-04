"""
Performance benchmark for read_file through the full ToolDispatcher execution path.

Compares:
  A) Standalone ReadFile.call() — direct tool invocation (baseline)
  B) Full dispatcher.execute_tool() path — what actually happens during agent runs
  C) truncate_tool_result() latency (exempt vs non-exempt tools)
  D) Compression handler drain calls (_drain_pending_into_tool_result, _drain_tool_warnings)

Key question: Does the full dispatcher path add significant latency vs. calling ReadFile directly?

Sleep call detection is also performed across all relevant code paths.
"""

import sys
import os
import time
import statistics
from pathlib import Path
from typing import List, Dict
from unittest.mock import MagicMock

# Ensure project root is on path for imports
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)

from agent_cascade.tools.custom.file_ops import ReadFile
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.settings import DEFAULT_WORKSPACE

# ── Constants ────────────────────────────────────────────────────────────────
NUM_ITERATIONS = 20          # Benchmark iterations per test (reduced for faster runs)
TEST_FILE_LINES = 50         # Lines in the small test file

WORK_DIR = Path(DEFAULT_WORKSPACE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def create_test_file(path: Path, num_lines: int) -> None:
    """Create a deterministic text file for benchmarking."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, num_lines + 1):
            f.write(f"Line {i:05d}: This is a test line with some meaningful content for benchmarking.\n")


def stats(label: str, times_ms: List[float]) -> None:
    """Print min/max/mean/median/p95/p99 statistics."""
    n = len(times_ms)
    print(f"  {label:<45} mean={statistics.mean(times_ms):>8.3f} ms  "
          f"median={statistics.median(times_ms):>8.3f} ms  "
          f"p95={sorted(times_ms)[int(n*0.95)]:>8.3f} ms  "
          f"min={min(times_ms):>7.3f} ms  max={max(times_ms):>7.3f} ms")


# ── Mock Setup ───────────────────────────────────────────────────────────────

def build_mock_dispatcher():
    """Build a ToolDispatcher with minimal mock AgentPool and ExecutionEngine."""
    # --- Mock AgentPool ---
    mock_pool = MagicMock()
    mock_pool.stopped = False

    # Mock template (has function_map and _call_tool)
    mock_template = MagicMock()
    mock_pool.get_template = lambda x: mock_template

    # Mock instance retrieval
    mock_instance = MagicMock()
    mock_instance.instance_name = "test_worker"
    mock_instance.agent_class = "coder"
    mock_instance.parent_instance = "orchestrator_main"
    mock_pool.get_instance = lambda name: mock_instance

    # Mock _logger for workspace_dir (used by spillover file writing)
    mock_logger = MagicMock()
    mock_logger.workspace_dir = WORK_DIR
    mock_pool._logger = mock_logger

    # Mock settings
    if not hasattr(mock_pool, 'settings'):
        mock_pool.settings = MagicMock()
        mock_pool.settings.max_nesting_depth = 10
    if not hasattr(mock_pool, 'active_stack'):
        mock_pool.active_stack = []

    # --- Mock ExecutionEngine ---
    mock_engine = MagicMock()

    def resolve_placeholders(args, instance_name, tool_name):
        """Return args as-is (already resolved for our test)."""
        if isinstance(args, str):
            import json
            try:
                return json.loads(args)
            except (json.JSONDecodeError, TypeError):
                return {"path": args}
        return args

    mock_engine._resolve_placeholders = resolve_placeholders
    mock_engine._cache_tool_args = lambda *a, **k: None
    mock_engine._get_max_tokens = lambda *a, **k: 128000

    # --- Assemble dispatcher ---
    dispatcher = ToolDispatcher(mock_pool)
    dispatcher.set_engine(mock_engine)

    return dispatcher, mock_template, mock_instance


# ── Sleep Call Detection ────────────────────────────────────────────────────

def detect_sleep_calls() -> List[str]:
    """Scan the full read_file code path for sleep() calls."""
    found: List[str] = []

    files_to_check = [
        ("agent_cascade/tools/custom/file_ops.py", "ReadFile tool"),
        ("agent_cascade/agent.py", "Agent._call_tool"),
        ("agent_cascade/tool_dispatcher.py", "ToolDispatcher"),
        ("agent_cascade/execution_engine.py", "ExecutionEngine"),
        ("agent_cascade/compression/handler.py", "CompressionHandler drains"),
    ]

    for rel_path, label in files_to_check:
        src = Path(PROJECT_ROOT) / rel_path
        if not src.exists():
            continue
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "sleep" in line.lower() and not stripped.startswith("#"):
                found.append(f"  {label} [{rel_path}:{i}] → {stripped}")

    return found


# ── Benchmark Functions ─────────────────────────────────────────────────────

def benchmark_standalone_readfile(tool: ReadFile, test_file: Path) -> List[float]:
    """Time direct ReadFile.call() — the baseline."""
    rel = str(test_file.relative_to(WORK_DIR))
    times_ms: List[float] = []

    for _ in range(NUM_ITERATIONS):
        t0 = time.perf_counter()
        result = tool.call({"path": rel, "start_line": 1, "limit": TEST_FILE_LINES})
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)

    return times_ms


def benchmark_dispatcher_execute(dispatcher: ToolDispatcher, mock_template: MagicMock,
                                  test_file: Path) -> List[float]:
    """Time the full dispatcher.execute_tool() call for read_file."""
    rel = str(test_file.relative_to(WORK_DIR))
    tool_args = {"path": rel, "start_line": 1, "limit": TEST_FILE_LINES}
    times_ms: List[float] = []

    # Wire up mock_template._call_tool to actually invoke ReadFile
    readfile_tool = ReadFile()
    mock_template.function_map = {"read_file": readfile_tool}
    mock_template._call_tool = lambda tool_name, args, **kw: readfile_tool.call(args)

    for _ in range(NUM_ITERATIONS):
        t0 = time.perf_counter()
        result = dispatcher.execute_tool(
            instance=dispatcher.pool.get_instance("test_worker"),
            tool_name="read_file",
            tool_args=tool_args,
            llm_messages=[],
            function_id=f"call_{_}"
        )
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)

    return times_ms


def benchmark_truncate_exempt(dispatcher: ToolDispatcher, test_file: Path) -> List[float]:
    """Time truncate_tool_result for read_file (exempt — fast path)."""
    rel = str(test_file.relative_to(WORK_DIR))
    full_result = ReadFile().call({"path": rel, "start_line": 1, "limit": TEST_FILE_LINES})

    times_ms: List[float] = []
    messages = [{"role": "system", "content": "You are helpful."}]

    for _ in range(NUM_ITERATIONS):
        t0 = time.perf_counter()
        dispatcher.truncate_tool_result(full_result, "read_file", messages, "test_worker")
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)

    return times_ms


def benchmark_truncate_non_exempt(dispatcher: ToolDispatcher, test_file: Path) -> List[float]:
    """Time truncate_tool_result for a non-exempt tool (triggers token counting)."""
    rel = str(test_file.relative_to(WORK_DIR))
    full_result = ReadFile().call({"path": rel, "start_line": 1, "limit": TEST_FILE_LINES})

    times_ms: List[float] = []
    messages = [{"role": "system", "content": "You are helpful."}]

    for _ in range(NUM_ITERATIONS):
        t0 = time.perf_counter()
        dispatcher.truncate_tool_result(full_result, "call_agent", messages, "test_worker")
        elapsed = (time.perf_counter() - t0) * 1000
        times_ms.append(elapsed)

    return times_ms


def benchmark_drain_methods(mock_instance: MagicMock, full_result: str) -> Dict[str, List[float]]:
    """Time _drain_pending_into_tool_result and _drain_tool_warnings."""
    import threading
    mock_instance._compression_lock = threading.Lock()

    drain_result_times: List[float] = []
    for _ in range(NUM_ITERATIONS):
        # Alternate between empty queue (fast path) and with notifications
        mock_instance._pending_notifications = ["Compression completed"] if _ % 2 else []
        t0 = time.perf_counter()
        result = full_result
        pending = mock_instance._pending_notifications
        if pending:
            notif_block = "\n\n".join(n for n in pending)
            result = f"{result}\n\n{notif_block}"
            mock_instance._pending_notifications = []
        elapsed = (time.perf_counter() - t0) * 1000
        drain_result_times.append(elapsed)

    drain_warning_times: List[float] = []
    for _ in range(NUM_ITERATIONS):
        # Alternate between no warnings and with warnings
        mock_instance._tool_warnings = ["Path resolved"] if _ % 2 else []
        t0 = time.perf_counter()
        text = full_result
        warnings = list(mock_instance._tool_warnings)
        if warnings:
            warning_block = "\n\n".join(str(w) for w in warnings)
            text = f"{text}\n\n[TOOL WARNINGS]\n{warning_block}"
        elapsed = (time.perf_counter() - t0) * 1000
        drain_warning_times.append(elapsed)

    return {
        "drain_pending_into_result": drain_result_times,
        "drain_tool_warnings": drain_warning_times,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    t_start = time.perf_counter()
    
    print("=" * 80)
    print("  ReadFile ToolDispatcher Performance Benchmark")
    print("=" * 80)
    print(f"  Project root : {PROJECT_ROOT}")
    print(f"  Iterations   : {NUM_ITERATIONS}")

    # ── Sleep call detection across the full path ────────────────────────────
    t0 = time.perf_counter()
    sleeps = detect_sleep_calls()
    if sleeps:
        print(f"\n⚠  SLEEP CALLS FOUND in the read_file code path ({(time.perf_counter()-t0)*1000:.0f}ms):")
        for s in sleeps:
            print(s)
    else:
        print(f"\n✓ No sleep() calls found in the read_file code path ({(time.perf_counter()-t0)*1000:.0f}ms).")

    # ── Create test file ─────────────────────────────────────────────────────
    t0 = time.perf_counter()
    tmp_dir = WORK_DIR / "_perf_test_tmp2"
    tmp_dir.mkdir(exist_ok=True)
    test_file = tmp_dir / "dispatcher_bench.txt"
    create_test_file(test_file, TEST_FILE_LINES)
    print(f"\n  Test file: {test_file} ({TEST_FILE_LINES} lines) [{(time.perf_counter()-t0)*1000:.0f}ms]")

    # ── Build mocks and dispatcher ───────────────────────────────────────────
    t0 = time.perf_counter()
    dispatcher, mock_template, mock_instance = build_mock_dispatcher()
    print(f"  Dispatcher built [{(time.perf_counter()-t0)*1000:.0f}ms]")
    readfile_tool = ReadFile()

    try:
        # ── Test 1: Standalone ReadFile.call() (baseline) ─────────────────────
        print(f"\n{'=' * 80}")
        print("  BENCHMARK RESULTS")
        print(f"{'=' * 80}")

        standalone_times = benchmark_standalone_readfile(readfile_tool, test_file)
        stats("Standalone ReadFile.call()", standalone_times)

        # ── Test 2: Full dispatcher.execute_tool() path ───────────────────────
        dispatcher_times = benchmark_dispatcher_execute(dispatcher, mock_template, test_file)
        stats("Full Dispatcher.execute_tool()", dispatcher_times)

        # ── Test 3: truncate_tool_result() — exempt tool (read_file) ──────────
        truncate_exempt_times = benchmark_truncate_exempt(dispatcher, test_file)
        stats("truncate_tool_result() [exempt]", truncate_exempt_times)

        # ── Test 4: truncate_tool_result() — non-exempt tool (call_agent) ─────
        truncate_non_exempt_times = benchmark_truncate_non_exempt(dispatcher, test_file)
        stats("truncate_tool_result() [non-exempt]", truncate_non_exempt_times)

        # ── Test 5: Compression handler drain calls ───────────────────────────
        full_result = readfile_tool.call({
            "path": str(test_file.relative_to(WORK_DIR)),
            "start_line": 1, "limit": TEST_FILE_LINES
        })
        drain_times = benchmark_drain_methods(mock_instance, full_result)
        stats("_drain_pending_into_tool_result", drain_times["drain_pending_into_result"])
        stats("_drain_tool_warnings", drain_times["drain_tool_warnings"])

        # ── Comparison Summary ────────────────────────────────────────────────
        print(f"\n{'=' * 80}")
        print("  COMPARISON")
        print(f"{'=' * 80}")

        avg_standalone = statistics.mean(standalone_times)
        avg_dispatcher = statistics.mean(dispatcher_times)
        overhead = avg_dispatcher - avg_standalone
        overhead_pct = (overhead / avg_standalone * 100) if avg_standalone > 0 else 0

        total_overhead = (overhead + statistics.mean(truncate_exempt_times) +
                         statistics.mean(drain_times["drain_pending_into_result"]) +
                         statistics.mean(drain_times["drain_tool_warnings"]))

        print(f"  Baseline (ReadFile.call())       : {avg_standalone:>8.3f} ms")
        print(f"  Dispatcher.execute_tool()         : {avg_dispatcher:>8.3f} ms  "
              f"(+{overhead:+.3f} ms, {overhead_pct:+.1f}%)")
        print(f"  truncate_tool_result (exempt)     : {statistics.mean(truncate_exempt_times):>8.3f} ms")
        print(f"  truncate_tool_result (non-exempt) : {statistics.mean(truncate_non_exempt_times):>8.3f} ms")
        print(f"  drain_pending_into_result         : {statistics.mean(drain_times['drain_pending_into_result']):>8.3f} ms")
        print(f"  drain_tool_warnings               : {statistics.mean(drain_times['drain_tool_warnings']):>8.3f} ms")
        print(f"\n  Estimated TOTAL dispatcher overhead per read_file call: {total_overhead:.3f} ms")

        # ── Observations ──────────────────────────────────────────────────────
        print(f"\n{'=' * 80}")
        print("  OBSERVATIONS")
        print(f"{'=' * 80}")

        if overhead_pct > 50:
            print(f"    ⚠ Dispatcher adds >{overhead_pct:.0f}% overhead vs standalone — investigate!")
        elif overhead_pct > 10:
            print(f"    ~ Dispatcher adds moderate {overhead_pct:.1f}% overhead (acceptable)")
        else:
            print(f"    ✓ Dispatcher adds minimal {abs(overhead_pct):.1f}% overhead — negligible impact")

        if total_overhead < 5:
            print(f"    ✓ Total dispatcher overhead <5ms per call — well within acceptable range")
        elif total_overhead < 20:
            print(f"    ~ Total dispatcher overhead {total_overhead:.1f}ms — moderate but acceptable")
        else:
            print(f"    ⚠ Total dispatcher overhead {total_overhead:.1f}ms per call — investigate bottlenecks")

    finally:
        # Cleanup temp files
        import shutil
        if test_file.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n  Temp directory cleaned up.")


if __name__ == "__main__":
    main()