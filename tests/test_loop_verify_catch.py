"""Verify that today's loop samples ACTUALLY trigger the detector (they should, since they were caught in production).

This confirms the detector is working — if these DON'T trigger, we've made the detector too loose."""
import sys
from pathlib import Path
import json
import importlib.util as _util

CASCADE_ROOT = Path(__file__).resolve().parent.parent

_spec = _util.spec_from_file_location(
    "inner_loop_detect",
    CASCADE_ROOT / "agent_cascade" / "inner_loop_detect.py",
)
_mod = _util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

InnerLoopDetector = _mod.InnerLoopDetector

SAMPLE_FILE = CASCADE_ROOT / "workspace" / "logs" / "loop_samples" / "samples_2026-07-07.jsonl"

samples = []
with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            samples.append(json.loads(line))

# Test with default settings (same as production)
print(f"Testing {len(samples)} samples against current detector (threshold=200, min_chars=4000)")
print("=" * 70)

caught = 0
missed = 0
results = []

for idx, sample in enumerate(samples):
    text = sample["text"]
    instance = sample["instance_name"]
    orig_reason = sample["reason"][:55]
    
    detector = InnerLoopDetector()
    # Feed the entire text at once (simulating one large chunk)
    result = detector.feed(text)
    
    if result is not None:
        caught += 1
        results.append({
            "sample": idx + 1,
            "instance": instance,
            "orig_reason": orig_reason,
            "detected_reason": result["reason"],
            "score": result["score"],
        })
    else:
        missed += 1

print(f"\nResults:")
print(f"  CAUGHT:   {caught}/{len(samples)}")
print(f"  MISSED:   {missed}/{len(samples)}")

if results:
    print(f"\nCaught samples ({len(results)}):")
    for r in results:
        print(f"  #{r['sample']:2d} {r['instance']:25s} | score={r['score']:<6.1f} "
              f"| detected={r['detected_reason'][:30]:<30s} | orig={r['orig_reason']}")

if missed:
    print(f"\n⚠️  {missed} samples were NOT caught — detector may be too loose!")
else:
    print("\n✅ All samples correctly triggered the detector.")

# Also test with chunked feeding (more realistic streaming scenario)
print("\n" + "=" * 70)
print("Now testing with CHUNKED feeding (50-char chunks, more realistic):")
caught_chunked = 0
missed_chunked = 0

for idx, sample in enumerate(samples):
    text = sample["text"]
    detector = InnerLoopDetector()
    
    pos = 0
    detected = False
    while pos < len(text):
        chunk = text[pos : pos + 50]
        result = detector.feed(chunk)
        if result is not None:
            caught_chunked += 1
            detected = True
            break
        pos += 50
    
    if not detected:
        missed_chunked += 1

print(f"  CAUGHT:   {caught_chunked}/{len(samples)}")
print(f"  MISSED:   {missed_chunked}/{len(samples)}")

if missed_chunked:
    print(f"\n⚠️  {missed_chunked} samples missed with chunked feeding!")
else:
    print("\n✅ All samples correctly triggered even with chunked feeding.")