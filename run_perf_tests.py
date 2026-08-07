#!/usr/bin/env python3
"""Run the existing TestFeedPerformance tests manually (no pytest/conftest needed)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import settings first
import importlib.util
_settings_spec = importlib.util.spec_from_file_location(
    "settings",
    str(PROJECT_ROOT / "agent_cascade" / "settings.py"),
)
_settings_mod = importlib.util.module_from_spec(_settings_spec)
sys.modules["agent_cascade.settings"] = _settings_mod
_settings_spec.loader.exec_module(_settings_mod)

# Import two_phase_loop_detect
_tp_spec = importlib.util.spec_from_file_location(
    "agent_cascade.two_phase_loop_detect",
    str(PROJECT_ROOT / "agent_cascade" / "two_phase_loop_detect.py"),
)
_tp_mod = importlib.util.module_from_spec(_tp_spec)
sys.modules["agent_cascade.two_phase_loop_detect"] = _tp_mod
_tp_mod.__package__ = "agent_cascade"
_tp_spec.loader.exec_module(_tp_mod)

# Import detector
_spec = importlib.util.spec_from_file_location(
    "inner_loop_detect",
    str(PROJECT_ROOT / "agent_cascade" / "inner_loop_detect.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
InnerLoopDetector = _mod.InnerLoopDetector

import time

def make_detector(**kwargs):
    defaults = dict(
        char_run_limit=130,
        min_chars=500,
        max_chars=40000,
    )
    defaults.update(kwargs)
    det = InnerLoopDetector(**defaults)
    det._two_phase_detector.two_phase_enabled = True
    return det

print("=== TestFeedPerformance.test_feed_latency_large_text ===")
det = make_detector()
chunks = []
for i in range(400):
    chunks.append(
        f"In section {i} the analysis reveals that component "
        f"alpha-{i % 20} interacts with module beta-{i // 10} "
        f"in a non-trivial manner. "
    )

start = time.perf_counter()
for chunk in chunks:
    det.feed(chunk)
elapsed_ms = (time.perf_counter() - start) * 1000

total_chars = sum(len(c) for c in chunks)
print(f"Time: {elapsed_ms:.1f}ms over {len(chunks)} chunks (~{total_chars:,} chars)")
if elapsed_ms < 2000:
    print("PASS (< 2000ms)")
else:
    print("FAIL (>= 2000ms) - suggests O(n) operations")

print()
print("=== TestFeedPerformance.test_feed_latency_default_params ===")
det = InnerLoopDetector()
det._two_phase_detector.two_phase_enabled = True

chunk_size = 80
total_chars_needed = det.min_chars + 2000

start = time.perf_counter()
fed = 0
i = 0
while fed < total_chars_needed:
    chunk = f"Word{i} has properties that are interesting for analysis. "
    det.feed(chunk)
    fed += len(chunk)
    i += 1

elapsed_ms = (time.perf_counter() - start) * 1000
print(f"Time: {elapsed_ms:.1f}ms over {i} chunks (~{fed:,} chars)")
if elapsed_ms < 2000:
    print("PASS (< 2000ms)")
else:
    print("FAIL (>= 2000ms) - performance regression detected")