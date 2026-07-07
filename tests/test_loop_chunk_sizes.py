"""Verify ALL loop_samples from today's JSONL file don't trigger false positives at various chunk sizes.

The detector uses feed(chunk) to receive streaming text deltas. We simulate this by
splitting each sample into chunks of varying sizes and feeding them sequentially."""
import sys
from pathlib import Path
import json
import importlib.util as _util

# The AgentCascade codebase lives in the extra_rw mount (N:\work\WD\AgentCascade_unified)
CASCADE_ROOT = Path(__file__).resolve().parent.parent
if not CASCADE_ROOT.exists():
    CASCADE_ROOT = Path(r"N:\work\WD\AgentCascade_unified")

# Load the detector module
_spec = _util.spec_from_file_location(
    "inner_loop_detect",
    CASCADE_ROOT / "agent_cascade" / "inner_loop_detect.py",
)
_mod = _util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

InnerLoopDetector = _mod.InnerLoopDetector

# Load all samples from the JSONL file
SAMPLE_FILE = CASCADE_ROOT / "loop_samples" / "samples_2026-07-07.jsonl"

samples = []
with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            samples.append(json.loads(line))

# Chunk sizes (characters per feed call) to test against
CHUNK_SIZES = [10, 30, 50, 100]
total_tests = len(samples) * len(CHUNK_SIZES)
passed = 0
failed = 0
failures = []

print(f"Testing {len(samples)} samples × {len(CHUNK_SIZES)} chunk sizes = {total_tests} checks")
print("=" * 70)

for idx, sample in enumerate(samples):
    text = sample["text"]
    instance = sample["instance_name"]
    reason = sample["reason"][:50]
    
    for chunk_size in CHUNK_SIZES:
        detector = InnerLoopDetector()
        
        # Split the full text into chunks and feed them sequentially
        pos = 0
        while pos < len(text):
            chunk = text[pos : pos + chunk_size]
            result = detector.feed(chunk)
            if result is not None:
                failed += 1
                failures.append({
                    "sample": idx + 1,
                    "instance": instance,
                    "chunk_size": chunk_size,
                    "reason": reason,
                    "detected_reason": result["reason"],
                    "score": result["score"],
                    "chars_fed": detector._chars_fed,
                })
                break  # Stop feeding this sample once a loop is detected
            pos += chunk_size
        else:
            passed += 1

print(f"\nResults:")
print(f"  PASSED: {passed}/{total_tests}")
print(f"  FAILED: {failed}/{total_tests}")

if failures:
    print(f"\nFailures ({len(failures)}):")
    for f in failures:
        print(f"  Sample #{f['sample']} ({f['instance']}) @ chunk_size={f['chunk_size']}: "
              f"detected={f['detected_reason']} score={f['score']} chars_fed={f['chars_fed']} "
              f"| original_reason={f['reason']}")
else:
    print("\n✅ All samples clear — no false positives at any chunk size.")

sys.exit(1 if failures else 0)