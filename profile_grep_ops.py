"""
Performance profiling for AgentCascade file operations (grep, read_file, list_dir).

This script creates test fixtures and benchmarks each operation to identify bottlenecks.

Usage:
    python profile_grep_ops.py [--run-profiling]
    
With --run-profiling, it runs the benchmark and prints timing results.
Without it, it only sets up fixtures and shows file structure.
"""

import os
import sys
import time
import timeit
import tempfile
import shutil
import re
from pathlib import Path
from typing import List, Tuple

# Add workspace to path
cascade_dir = Path(r"N:\work\WD\AgentCascade")
if str(cascade_dir) not in sys.path:
    sys.path.insert(0, str(cascade_dir))

def create_test_fixtures(base_path: Path, num_files: int = 100, lines_per_file: int = 200, pattern: str = "TODO"):
    """Create a directory of test files for benchmarking."""
    test_dir = base_path / "_perf_test"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    
    # Create nested structure
    subdirs = [test_dir / f"subdir_{i}" for i in range(5)]
    for d in subdirs:
        d.mkdir(parents=True, exist_ok=True)
    
    patterns_to_insert = [
        f"# {pattern}: important implementation detail here",
        f"# TODO: this needs refactoring later on",
        f"# FIXME: broken code that causes issues",
        f"# NOTE: keep an eye on this section",
        f"# Regular line without any marker",
    ]
    
    for i in range(num_files):
        subdir = subdirs[i % len(subdirs)]
        file_path = subdir / f"file_{i:04d}.py"
        
        with open(file_path, 'w', encoding='utf-8') as f:
            for line_num in range(lines_per_file):
                pattern_line = patterns_to_insert[line_num % len(patterns_to_insert)]
                f.write(f"# Line {line_num + 1} of file {i}: {pattern_line}\n")
    
    # Also create some larger files
    for i in range(5):
        file_path = test_dir / f"large_file_{i}.py"
        with open(file_path, 'w', encoding='utf-8') as f:
            for line_num in range(5000):
                f.write(f"# Large file line {line_num}: some content here\n")
    
    # Create a very large file to test edge cases
    big_file = test_dir / "huge_file.py"
    with open(big_file, 'w', encoding='utf-8') as f:
        for line_num in range(50000):
            if line_num % 100 == 0:
                f.write(f"# {pattern}: found in huge file at line {line_num}\n")
            else:
                f.write(f"# Line {line_num}: no match here\n")
    
    return test_dir


def benchmark_py_grep(test_dir: Path, pattern: str = "TODO"):
    """
    Benchmark the Python-based grep implementation from operation_manager.py.
    This replicates the exact logic used in OperationManager.grep().
    """
    import time
    
    resolved = test_dir
    results = []
    
    try:
        pattern_re = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"ERROR: Invalid regex: {e}"
    
    start_time = time.perf_counter()
    timeout = 30.0
    
    file_count = 0
    for file_path in resolved.rglob("*"):
        if time.time() - start_time > timeout:
            break
        if file_path.is_file():
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                lines = content.split('\n')
                for line_num, line in enumerate(lines, 1):
                    if pattern_re.search(line):
                        rel_path = file_path.relative_to(cascade_dir)
                        results.append(f"{rel_path}:{line_num}: {line.strip()}")
            except Exception:
                continue
            file_count += 1
    
    elapsed = time.perf_counter() - start_time
    return {
        'elapsed': elapsed,
        'results': len(results),
        'files_scanned': file_count,
        'method': 'py_grep'
    }


def benchmark_stdlib_grep(test_dir: Path, pattern: str = "TODO"):
    """Benchmark using glob + line-by-line reading (standard library only)."""
    import time
    
    start_time = time.perf_counter()
    
    results = []
    files = list(test_dir.rglob("*"))
    
    for file_path in files:
        if file_path.is_file():
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern.lower() in line.lower():
                            rel_path = file_path.relative_to(cascade_dir)
                            results.append(f"{rel_path}:{line_num}: {line.strip()}")
            except Exception:
                continue
    
    elapsed = time.perf_counter() - start_time
    return {
        'elapsed': elapsed,
        'results': len(results),
        'method': 'stdlib_grep'
    }


def benchmark_subprocess_grep(test_dir: Path, pattern: str = "TODO"):
    """Benchmark using the system grep command via subprocess."""
    import subprocess
    
    start_time = time.perf_counter()
    
    try:
        # Try Unix-style grep first (works in Git Bash, WSL, etc.)
        result = subprocess.run(
            ['grep', '-ri', '--include=*.py', '-n', pattern, str(test_dir)],
            capture_output=True,
            text=True,
            encoding='utf-8',          # Explicit UTF-8 to prevent cp1252 decode errors on Windows
            errors='replace',          # Replace undecodable bytes with replacement character
            timeout=30,
        )
    except FileNotFoundError:
        # Fall back to find + grep on Windows
        try:
            result = subprocess.run(
                f'find "{test_dir}" -name "*.py" -exec grep -iHn "{pattern}" {{}} \\;',
                capture_output=True,
                text=True,
                encoding='utf-8',          # Explicit UTF-8 to prevent cp1252 decode errors on Windows
                errors='replace',          # Replace undecodable bytes with replacement character
                timeout=30,
                shell=True,
            )
        except Exception:
            return {'elapsed': -1, 'results': 0, 'method': 'subprocess_grep', 'error': 'No grep available'}
    
    elapsed = time.perf_counter() - start_time
    
    if result.stdout:
        lines = [l for l in result.stdout.strip().split('\n') if l]
        return {
            'elapsed': elapsed,
            'results': len(lines),
            'method': 'subprocess_grep'
        }
    
    return {'elapsed': elapsed, 'results': 0, 'method': 'subprocess_grep'}


def benchmark_read_file(test_dir: Path):
    """Benchmark the Python-based read_file implementation."""
    test_file = test_dir / "large_file_0.py"
    
    if not test_file.exists():
        return {'elapsed': -1, 'error': 'Test file missing'}
    
    start_time = time.perf_counter()
    
    # Replicate the exact read_file logic from operation_manager.py
    with open(test_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    content = "".join([f"{i+1}: {lines[i]}" for i in range(min(1000, len(lines)))])
    
    elapsed = time.perf_counter() - start_time
    
    return {
        'elapsed': elapsed,
        'lines_read': min(1000, len(lines)),
        'content_length': len(content),
        'method': 'py_read_file'
    }


def benchmark_read_file_iterative(test_dir: Path):
    """Benchmark a more memory-efficient read_file using iteration."""
    test_file = test_dir / "large_file_0.py"
    
    if not test_file.exists():
        return {'elapsed': -1, 'error': 'Test file missing'}
    
    start_time = time.perf_counter()
    
    # Use line-by-line iteration instead of readlines()
    lines = []
    with open(test_file, 'r', encoding='utf-8', errors='ignore') as f:
        for i, line in enumerate(f):
            if i >= 1000:
                break
            lines.append(line)
    
    content = "".join([f"{i+1}: {line}" for i, line in enumerate(lines)])
    
    elapsed = time.perf_counter() - start_time
    
    return {
        'elapsed': elapsed,
        'lines_read': len(lines),
        'content_length': len(content),
        'method': 'iterative_read_file'
    }


def benchmark_path_resolution(test_dir: Path):
    """Benchmark _resolve_path and _path_is_contained operations."""
    test_subdir = test_dir / "subdir_0"
    
    # Benchmark Path.resolve()
    start_time = time.perf_counter()
    for _ in range(1000):
        (test_subdir / f"file_{_}.py").resolve()
    resolve_elapsed = time.perf_counter() - start_time
    
    # Benchmark os.path.commonpath() containment check
    start_time = time.perf_counter()
    for _ in range(1000):
        try:
            common = os.path.commonpath([str(test_subdir), str(cascade_dir)])
            contained = common.lower() == str(cascade_dir).lower()
        except ValueError:
            contained = False
    commonpath_elapsed = time.perf_counter() - start_time
    
    # Benchmark old-style startswith check (for comparison)
    start_time = time.perf_counter()
    for _ in range(1000):
        contained = str(test_subdir).lower().startswith(str(cascade_dir).lower())
    startswith_elapsed = time.perf_counter() - start_time
    
    return {
        'resolve_1000x': resolve_elapsed,
        'commonpath_1000x': commonpath_elapsed,
        'startswith_1000x': startswith_elapsed,
        'method': 'path_resolution'
    }


def benchmark_list_dir(test_dir: Path):
    """Benchmark list_directory operation."""
    start_time = time.perf_counter()
    
    for _ in range(100):
        dirs = []
        files = []
        for item in test_dir.iterdir():
            if item.is_dir():
                dirs.append(item.name)
            else:
                files.append(item.name)
    
    elapsed = time.perf_counter() - start_time
    
    return {
        'elapsed_100x': elapsed,
        'dirs': len(dirs),
        'files': len(files),
        'method': 'list_dir'
    }


def benchmark_rglob_vs_walk(test_dir: Path):
    """Compare rglob() vs os.walk() for file enumeration."""
    # rglob approach (current)
    start_time = time.perf_counter()
    count_rglob = 0
    for p in test_dir.rglob("*"):
        if p.is_file():
            count_rglob += 1
    rglob_elapsed = time.perf_counter() - start_time
    
    # os.walk approach (potential alternative)
    start_time = time.perf_counter()
    count_walk = 0
    for root, dirs, files in os.walk(test_dir):
        count_walk += len(files)
    walk_elapsed = time.perf_counter() - start_time
    
    return {
        'rglob_files': count_rglob,
        'rglob_elapsed': rglob_elapsed,
        'walk_files': count_walk,
        'walk_elapsed': walk_elapsed,
        'method': 'rglob_vs_walk'
    }


def benchmark_memory_usage():
    """Measure memory overhead of current grep implementation."""
    import tracemalloc
    
    test_dir = cascade_dir / "_perf_test"
    if not test_dir.exists():
        return {'error': 'Test fixtures missing'}
    
    tracemalloc.start()
    
    # Run the grep operation and track memory
    resolved = test_dir
    pattern_re = re.compile("TODO", re.IGNORECASE)
    results = []
    file_count = 0
    
    for file_path in resolved.rglob("*"):
        if file_path.is_file():
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                lines = content.split('\n')
                for line_num, line in enumerate(lines, 1):
                    if pattern_re.search(line):
                        rel_path = file_path.relative_to(cascade_dir)
                        results.append(f"{rel_path}:{line_num}: {line.strip()}")
            except Exception:
                continue
            file_count += 1
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    return {
        'peak_memory_bytes': peak,
        'peak_memory_mb': peak / (1024 * 1024),
        'results_count': len(results),
        'files_scanned': file_count,
        'method': 'memory_usage'
    }


def benchmark_regex_compilation():
    """Benchmark regex compilation and search overhead."""
    pattern = "TODO"
    
    # Measure compilation time
    start_time = time.perf_counter()
    for _ in range(100):
        re.compile(pattern, re.IGNORECASE)
    compile_elapsed = time.perf_counter() - start_time
    
    # Measure search time on a typical line
    test_line = "# This is a TODO item that needs to be fixed"
    
    start_time = time.perf_counter()
    compiled = re.compile(pattern, re.IGNORECASE)
    for _ in range(10000):
        compiled.search(test_line)
    search_elapsed = time.perf_counter() - start_time
    
    # Compare with simple 'in' operator (no regex)
    start_time = time.perf_counter()
    for _ in range(10000):
        "TODO" in test_line.lower()
    in_operator_elapsed = time.perf_counter() - start_time
    
    return {
        'compile_100x': compile_elapsed,
        'regex_search_10000x': search_elapsed,
        'in_operator_10000x': in_operator_elapsed,
        'method': 'regex_comparison'
    }


def print_results(label: str, data: dict):
    """Format and print benchmark results."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)
    
    for key, value in data.items():
        if 'elapsed' in key or 'time' in key.lower():
            if isinstance(value, (int, float)) and value > 0:
                print(f"  {key}: {value:.4f}s")
        elif 'bytes' in key or 'mb' in key.lower() or 'memory' in key.lower():
            if isinstance(value, (int, float)):
                print(f"  {key}: {value}")
        else:
            print(f"  {key}: {value}")


def main(run_profiling=False):
    """Run all benchmarks."""
    print("AgentCascade File Operations Performance Profiler")
    print("=" * 60)
    
    # Create test fixtures
    print("\n[*] Creating test fixtures...")
    test_dir = create_test_fixtures(
        cascade_dir,
        num_files=100,
        lines_per_file=200,
        pattern="TODO"
    )
    total_size = sum(f.stat().st_size for f in test_dir.rglob("*") if f.is_file())
    total_files = sum(1 for f in test_dir.rglob("*") if f.is_file())
    print(f"    Created {total_files} files ({total_size / (1024*1024):.2f} MB) in {test_dir}")
    
    if not run_profiling:
        print("\n[!] Pass --run-profiling to execute benchmarks.")
        print("    Without it, fixtures are created for manual testing only.")
        return
    
    print("\n[*] Running benchmarks...")
    
    # 1. Grep implementations
    benchmark_py_grep(test_dir)
    benchmark_stdlib_grep(test_dir)
    benchmark_subprocess_grep(test_dir)
    
    # Run each benchmark multiple times for statistical significance
    py_times = []
    stdlib_times = []
    subprocess_times = []
    
    for i in range(3):
        print(f"\n  --- Iteration {i+1}/3 ---")
        
        r1 = benchmark_py_grep(test_dir)
        py_times.append(r1['elapsed'])
        print_results("Python rglob + read_text grep", r1)
        
        r2 = benchmark_stdlib_grep(test_dir)
        stdlib_times.append(r2['elapsed'])
        print_results("Stdlib glob + line iteration", r2)
        
        r3 = benchmark_subprocess_grep(test_dir)
        subprocess_times.append(r3['elapsed'])
        print_results("Subprocess grep CLI", r3)
    
    # Summary comparison
    if py_times and stdlib_times:
        avg_py = sum(py_times) / len(py_times)
        avg_stdlib = sum(stdlib_times) / len(stdlib_times)
        
        print(f"\n{'='*60}")
        print(f"  Grep COMPARISON SUMMARY")
        print('='*60)
        print(f"  Python rglob+read_text: {avg_py:.4f}s (avg)")
        print(f"  Stdlib glob+iteration:  {avg_stdlib:.4f}s (avg)")
        if avg_stdlib > 0:
            ratio = avg_py / avg_stdlib
            print(f"  Speedup factor: {ratio:.2f}x")
        
        subprocess_results = [t for t in subprocess_times if t > 0]
        if subprocess_results:
            avg_subprocess = sum(subprocess_results) / len(subprocess_results)
            print(f"\n  Subprocess grep CLI:    {avg_subprocess:.4f}s (avg)")
            if avg_py > 0 and avg_subprocess > 0:
                cli_ratio = avg_py / avg_subprocess
                print(f"  Speedup factor (vs CLI): {cli_ratio:.2f}x")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Profile AgentCascade file operations")
    parser.add_argument("--run-profiling", action="store_true", help="Actually run the benchmarks")
    args = parser.parse_args()
    
    main(run_profiling=args.run_profiling)