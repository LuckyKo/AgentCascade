---
name: performance-profiling
description: Performance measurement and optimization including time/cprofile usage, memory profiling, bottleneck identification, and algorithm complexity analysis
source: manual
version: "1.0.0"
triggers:
  - "performance"
  - "profiling"
  - "benchmark"
  - "slow"
  - "bottleneck"
  - "optimize"
  - "memory usage"
  - "cpu profiling"
---

## Goal

Measure code performance systematically, identify bottlenecks through CPU and memory profiling, analyze algorithmic complexity, and apply targeted optimizations that yield real-world improvements.

## Procedure

### Step 1 — Timing and benchmarking

**Simple timing for quick checks:**
```python
import time

# Wall-clock timing (includes I/O, sleeps)
start = time.perf_counter()
result = expensive_function(data)
elapsed = time.perf_counter() - start
print(f"Took {elapsed:.3f}s")

# CPU-only timing (excludes I/O wait)
start_cpu = time.process_time()
result = compute_heavy_task(data)
cpu_time = time.process_time() - start_cpu
print(f"CPU time: {cpu_time:.3f}s")

# Benchmark with multiple runs for statistical significance
import statistics
times = []
for _ in range(10):
    t0 = time.perf_counter()
    result = function_to_benchmark(data)
    times.append(time.perf_counter() - t0)

print(f"Mean: {statistics.mean(times):.4f}s ± {statistics.stdev(times):.4f}s")
```

**Decorator for automatic timing:**
```python
import functools, time, logging

logger = logging.getLogger(__name__)

def timed(level=logging.DEBUG):
    """Decorator that logs execution time."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.log(level, f"{func.__name__} took {elapsed:.3f}s")
            return result
        return wrapper
    return decorator

@timed(logging.WARNING)  # Will log if slow
def process_batch(items):
    return [transform(item) for item in items]
```

### Step 2 — CPU profiling with cProfile

**Identify hot functions:**
```python
import cProfile, pstats, io

# Profile a function call
profiler = cProfile.Profile()
profiler.enable()

# ... code to profile ...
result = main_processing_pipeline(data)

profiler.disable()

# Print top 20 most time-consuming calls
stream = io.StringIO()
stats = pstats.Stats(profiler, stream=stream)
stats.sort_stats("cumulative")
stats.print_stats(20)
print(stream.getvalue())

# Sort by different metrics:
stats.sort_stats("tottime")   # Time spent in function itself (excluding subcalls)
stats.sort_stats("cumulative")  # Total time including subcalls
stats.sort_stats("calls")     # Number of calls
```

**Profile from command line:**
```bash
# Profile an entire script and save results
python -m cProfile -o profile.stats my_script.py

# Analyze the saved profile interactively
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumulative').print_stats(30)"
```

**Key metrics to interpret:**

| Metric | Meaning | Action if high |
|---|---|---|
| `tottime` | Time in this function only | Optimize the function's internal logic |
| `cumtime` | Total time including subcalls | Check which child functions are slow |
| `ncalls` | Call count | Consider caching/memoization if called repeatedly with same args |

### Step 3 — Memory profiling

**Track memory usage:**
```python
import tracemalloc, sys

# Start tracking allocations
tracemalloc.start()

# Run your code
result = process_large_dataset(data)

# Get snapshot and analyze
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics("lineno")[:10]

print("\nTop 10 memory consumers:")
for stat in top_stats:
    print(f"  {stat}")

# Compare two snapshots to find leaks
snapshot1 = tracemalloc.take_snapshot()
do_work_that_may_leak()
snapshot2 = tracemalloc.take_snapshot()

diffs = snapshot2.compare_to(snapshot1, "lineno")[:10]
print("\nMemory growth:")
for diff in diffs:
    print(f"  {diff}")
```

**Quick memory check:**
```python
import os

def get_memory_mb():
    """Get current process memory usage in MB."""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024  # KB → MB
    return None

# Or cross-platform approach
import psutil, os
process = psutil.Process(os.getpid())
mem_mb = process.memory_info().rss / (1024 * 1024)
```

### Step 4 — Algorithm complexity analysis

**Big-O reference for common operations:**

| Operation | Python Implementation | Complexity |
|---|---|---|
| List append | `list.append(x)` | O(1) amortized |
| List lookup by index | `lst[i]` | O(1) |
| List search (contains) | `x in lst` | O(n) |
| Dict lookup/insert | `d[key]`, `d[key]=val` | O(1) average |
| Set membership | `x in set` | O(1) average |
| Sorted list binary search | `bisect.bisect(lst, x)` | O(log n) |
| Sorting | `sorted()`, `.sort()` | O(n log n) |
| String concatenation (loop) | `"a" + "b"` in loop | O(n²) — use `"".join()` instead |

**Common optimization patterns:**

```python
# ❌ O(n²): repeated list lookups
for item in large_list:
    if item in search_targets:  # O(n) per lookup
        process(item)

# ✅ O(n): convert to set for O(1) lookups
target_set = set(search_targets)
for item in large_list:
    if item in target_set:  # O(1) per lookup
        process(item)

# ❌ O(n²): building string by concatenation
result = ""
for word in words:
    result += word + " "

# ✅ O(n): use join
result = " ".join(words)

# ❌ O(n²): repeated dict lookups with key construction
counts = {}
for item in items:
    key = f"{item.type}_{item.category}"
    counts[key] = counts.get(key, 0) + 1

# ✅ Already O(1) per operation — but use defaultdict for cleaner code
from collections import defaultdict
counts = defaultdict(int)
for item in items:
    counts[f"{item.type}_{item.category}"] += 1
```

### Step 5 — Caching and memoization

```python
from functools import lru_cache, cache

# Simple memoization for pure functions (Python 3.9+)
@cache  # Unlimited cache, never expires
def fibonacci(n: int) -> int:
    if n < 2:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

# Bounded LRU cache (best for memory-constrained scenarios)
@lru_cache(maxsize=128)
def fetch_user_profile(user_id: int) -> dict:
    # Expensive DB/API call cached per user_id
    return db.query(f"SELECT * FROM users WHERE id={user_id}")

# Cache info and invalidation
print(fetch_user_profile.cache_info())
# CacheInfo(hits=42, misses=15, maxsize=128, currsize=15)

fetch_user_profile.cache_clear()  # Manual cache reset when data changes
```

### Step 6 — Bottleneck identification workflow

1. **Measure first** — Never optimize without profiling data
2. **Find the hot path** — cProfile shows where time is actually spent
3. **Check call frequency** — High `ncalls` suggests caching opportunity
4. **Verify algorithm complexity** — Is it O(n²) when O(n log n) would work?
5. **Test the optimization** — Benchmark before and after with same data size
6. **Check for regressions** — Ensure other paths didn't slow down

```python
# Before/after benchmark template
import time, statistics

def benchmark(func, data, runs=10):
    times = [time.perf_counter() - (t0 := time.perf_counter()) or 
             (lambda: (func(data), time.perf_counter() - t0))[1]() for _ in range(runs)]
    return {"mean": statistics.mean(times), "min": min(times), "max": max(times)}

# Compare implementations
results_before = benchmark(slow_version, large_dataset)
results_after  = benchmark(fast_version, large_dataset)
print(f"Speedup: {results_before['mean'] / results_after['mean']:.1f}x")
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| Benchmark runs | 10+ iterations | Averages out noise from GC, OS scheduling |
| `lru_cache maxsize` | 128–1024 depending on cardinality | Too small = no benefit; too large = memory waste |
| Profile sample rate | Default (cProfile) is fine for most cases | Only increase for microsecond-level precision needs |

## What NOT to do

- Do not optimize before measuring — premature optimization wastes time and introduces bugs
- Do not use `time.time()` for benchmarking — it has lower resolution than `time.perf_counter()`
- Do not cache non-pure functions without invalidation strategy — stale data is worse than slow data
- Do not profile in production without cleanup — profiling adds 10–20% overhead
- Do not assume the most complex-looking code is the bottleneck — often it's a simple loop