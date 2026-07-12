"""Verify ALL loop_samples from today's JSONL file don't trigger false positives at various chunk sizes.

The detector uses feed(chunk) to receive streaming text deltas. We simulate this by
splitting each sample into chunks of varying sizes and feeding them sequentially."""
from pathlib import Path
import json

# Resolve project root relative to this test file (tests/ → project_root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

from agent_cascade.inner_loop_detect import InnerLoopDetector

# Load all samples from the JSONL file
# Try relative to project first, then fallback to sibling workspace dir
SAMPLE_FILE = PROJECT_ROOT / "tests" / "loop_samples" / "samples_2026-07-07.jsonl"
if not SAMPLE_FILE.exists():
    # Sibling directory: AgentWorkspace/logs/loop_samples (same parent as project)
    SAMPLE_FILE = PROJECT_ROOT.parent / "AgentWorkspace" / "logs" / "loop_samples" / "samples_2026-07-07.jsonl"

with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
    samples = [json.loads(line) for line in f if line.strip()]

# Chunk sizes (characters per feed call) to test against
CHUNK_SIZES = [10, 30, 50, 100]


def test_loop_chunk_sizes():
    """Each sample should not trigger loop detection at any chunk size."""
    total_tests = len(samples) * len(CHUNK_SIZES)
    passed = 0
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
    print(f"  FAILED: {len(failures)}/{total_tests}")

    if failures:
        print(f"\nFailures ({len(failures)}):")
        for f in failures:
            print(f"  Sample #{f['sample']} ({f['instance']}) @ chunk_size={f['chunk_size']}: "
                  f"detected={f['detected_reason']} score={f['score']} chars_fed={f['chars_fed']} "
                  f"| original_reason={f['reason']}")
    else:
        print("\n✅ All samples clear — no false positives at any chunk size.")

    assert not failures, f"{len(failures)} sample(s) triggered false positives"


# Allow running as a standalone script too
if __name__ == "__main__":
    test_loop_chunk_sizes()