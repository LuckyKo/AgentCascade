"""Tests for the JSONL silent-destruction fix.

Covers the three production changes:
  1. rewrite_log_with_history() shrink guard — refuses to write a drastically
     smaller history over a larger tracked working set (the original data-loss bug),
     with allow_shrink=True as the explicit override.
  2. _atomic_write_lines() — temp file + os.replace(); on failure the previous file
     is left intact and no stray temp files remain.
  3. Concurrency — many threads rewriting/appending simultaneously never corrupt the
     JSONL (every line stays valid) and leave no temp-file litter.

All tests are self-contained: no LLM, API server, or network required.
"""

import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict:
    return {"role": "assistant", "content": content}


@pytest.fixture
def tmp_log(tmp_path):
    """Return (log_path, AgentInstanceLogger) for a fresh temp log file."""
    log_file = tmp_path / "test_logger.jsonl"
    logger_inst = AgentInstanceLogger(
        agent_class="coder",
        instance_name="test_worker",
        log_dir=str(tmp_path),
        log_path=str(log_file),
    )
    return log_file, logger_inst


def _seed_file(log_path: Path, n_msgs: int):
    """Write a metadata header + n message lines to the log file."""
    lines = [json.dumps({"metadata": {"agent_class": "coder"}})]
    for i in range(n_msgs):
        m = _user(f"original {i}") if i % 2 == 0 else _assistant(f"reply {i}")
        lines.append(json.dumps(m))
    log_path.write_text("\n".join(lines) + "\n")


def _set_tracked(logger_inst, n_msgs: int):
    """Set the in-memory tracked working set to n formatted message dicts.

    This mirrors what _sync_marker_single_write / _consolidate_markers_in_jsonl do
    after compression: data["history"] becomes the trimmed pool working set, which is
    the shrink-guard baseline (a count-based delta-sync cursor).
    """
    tracked = []
    for i in range(n_msgs):
        m = _user(f"tracked {i}") if i % 2 == 0 else _assistant(f"tracked reply {i}")
        tracked.append(logger_inst._format_message(m))
    logger_inst.data["history"] = tracked
    logger_inst._file_history_synced = True


def _count_messages(log_path: Path) -> int:
    """Count non-metadata, non-event message lines in the log file."""
    count = 0
    if not os.path.exists(log_path):
        return 0
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            count = -1  # signal corruption
            break
        if isinstance(item, dict) and "metadata" not in item and "event" not in item:
            count += 1
    return count


def _all_lines_valid_jsonl(log_path: Path):
    """Return (ok, bad_line_index). ok is False if any non-empty line is invalid JSON."""
    for idx, line in enumerate(log_path.read_text().splitlines()):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False, idx
    return True, None


def _temp_files(dir_path: Path):
    """List any *.tmp files left in a directory."""
    return [p.name for p in dir_path.iterdir() if p.name.endswith(".tmp")]


# ──────────────────────────────────────────────
# 1. Shrink guard
# ──────────────────────────────────────────────

class TestShrinkGuard:
    """rewrite_log_with_history must refuse a drastic shrink of the tracked working set."""

    def test_refuses_drastic_shrink(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 10)
        _set_tracked(logger_inst, 10)          # tracked working set = 10

        result = logger_inst.rewrite_log_with_history([_user("tiny")])  # incoming = 1 (< 5)

        assert result is False, "shrink guard must refuse a drastic shrink"
        # File must be untouched (still the original 10 messages).
        assert _count_messages(log_path) == 10
        # Tracked working set must be unchanged.
        assert len(logger_inst.data["history"]) == 10

    def test_allows_same_size(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 8)
        _set_tracked(logger_inst, 8)           # tracked = 8

        result = logger_inst.rewrite_log_with_history([_user(f"same {i}") for i in range(8)])

        assert result is True, "same-size rewrite must be allowed"
        assert _count_messages(log_path) == 8
        assert len(logger_inst.data["history"]) == 8

    def test_allows_additive_growth(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 6)
        _set_tracked(logger_inst, 6)           # tracked = 6

        result = logger_inst.rewrite_log_with_history([_user(f"growth {i}") for i in range(12)])

        assert result is True, "additive growth must be allowed"
        assert _count_messages(log_path) == 12
        assert len(logger_inst.data["history"]) == 12

    def test_allows_shrink_when_override(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 10)
        _set_tracked(logger_inst, 10)          # tracked = 10

        result = logger_inst.rewrite_log_with_history([_user("kept")], allow_shrink=True)

        assert result is True, "allow_shrink=True must permit an intentional shrink"
        assert _count_messages(log_path) == 1
        assert len(logger_inst.data["history"]) == 1

    def test_boundary_at_half_is_allowed(self, tmp_log):
        """new_count == prev_tracked * 0.5 is NOT < half → allowed (boundary)."""
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 10)
        _set_tracked(logger_inst, 10)          # tracked = 10, half = 5

        result = logger_inst.rewrite_log_with_history([_user(f"edge {i}") for i in range(5)])

        assert result is True, "exactly-half incoming history must be allowed (not a drastic shrink)"
        assert _count_messages(log_path) == 5


# ──────────────────────────────────────────────
# 2. Atomic write
# ──────────────────────────────────────────────

class TestAtomicWrite:
    """_atomic_write_lines must replace atomically and never litter temp files."""

    def test_success_replaces_content(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 3)

        new_lines = [json.dumps({"metadata": {"agent_class": "coder"}}) + "\n",
                     json.dumps(_user("fresh a")) + "\n",
                     json.dumps(_assistant("fresh b")) + "\n"]
        assert logger_inst._atomic_write_lines(new_lines) is True
        assert _count_messages(log_path) == 2
        ok, bad = _all_lines_valid_jsonl(log_path)
        assert ok, f"corrupt line at {bad}"

    def test_no_temp_leftover_on_success(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 2)
        logger_inst._atomic_write_lines([json.dumps(_user("x")) + "\n"])
        assert _temp_files(log_path.parent) == [], "no .tmp files may remain after success"

    def test_failure_leaves_previous_intact(self, tmp_log):
        """If the write fails (target dir not writable), the prior file must survive."""
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 4)
        original_text = log_path.read_text()

        # Point the logger at a non-writable path to force os.replace/open failure.
        unwritable_dir = log_path.parent / "no_such_dir"
        logger_inst.log_path = str(unwritable_dir / "impossible.jsonl")

        result = logger_inst._atomic_write_lines([json.dumps(_user("x")) + "\n"])

        assert result is False, "write to a non-existent directory must fail"
        # The ORIGINAL file is untouched.
        assert log_path.read_text() == original_text
        assert _count_messages(log_path) == 4
        # No temp litter in the real dir.
        assert _temp_files(log_path.parent) == []


# ──────────────────────────────────────────────
# 3. Concurrency
# ──────────────────────────────────────────────

class TestConcurrentWrites:
    """Many threads hammering the same log file must never corrupt it."""

    def test_concurrent_rewrites_keep_valid_jsonl(self, tmp_log):
        log_path, logger_inst = tmp_log
        _seed_file(log_path, 5)
        _set_tracked(logger_inst, 5)

        n_threads = 20
        errors = []

        def worker(tid):
            try:
                # Each thread rewrites with its own distinct history (same size → guard allows).
                hist = [_user(f"t{tid}-m{i}") for i in range(5)]
                logger_inst.rewrite_log_with_history(hist)
            except Exception as e:  # pragma: no cover - defensive
                errors.append((tid, repr(e)))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"threads raised: {errors}"
        ok, bad = _all_lines_valid_jsonl(log_path)
        assert ok, f"JSONL corrupted at line {bad} after concurrent rewrites"
        # Exactly one metadata header + 5 messages remain (last writer wins, atomically).
        assert _count_messages(log_path) == 5
        assert _temp_files(log_path.parent) == [], "no temp files may litter after concurrency"

    def test_concurrent_appends_no_corruption(self, tmp_log):
        log_path, logger_inst = tmp_log
        # Ensure the file exists with a metadata header before concurrent appends.
        _seed_file(log_path, 1)
        logger_inst._ensure_file()

        n_threads, per_thread = 8, 25
        errors = []

        def worker(tid):
            try:
                for i in range(per_thread):
                    logger_inst.log_message(_user(f"t{tid}-a{i}"))
            except Exception as e:  # pragma: no cover - defensive
                errors.append((tid, repr(e)))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"threads raised: {errors}"
        ok, bad = _all_lines_valid_jsonl(log_path)
        assert ok, f"JSONL corrupted at line {bad} after concurrent appends"
        total = n_threads * per_thread
        assert _count_messages(log_path) == 1 + total
