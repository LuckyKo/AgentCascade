"""Regression tests for BUG_0007 — L2 compression dropping pre-marker raw history.

Root cause: _append_line() wrote to the cached file handle WITHOUT holding _write_lock,
while the rewrite paths (_sync_marker_single_write / _consolidate_markers_in_jsonl) hold
the lock, close the handle, and atomically replace the file. The unsynchronized append
raced the rewrite: it could hit a closed handle ("write to closed file") or race the
os.replace (WinError 5/32 on Windows), silently dropping messages and leaving the JSONL
in a shrunken/stale state.

Fix under test:
  1. _append_line() now acquires _write_lock, serializing appends with rewrites.
  2. _atomic_write_lines() retries os.replace on transient lock errors.

All tests are self-contained: no LLM, API server, or network required.
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict:
    return {"role": "assistant", "content": content}


def _marker(summary: str, kind: str = "l1") -> dict:
    if kind == "l2":
        header = "L2, 7 summaries consolidated"
    else:
        header = summary
    return {
        "role": "user",
        "content": (
            f"{COMPRESSION_MARKER} ({header}) ---\n"
            "<context_summary>\n"
            f"{summary}\n"
            "</context_summary>"
        ),
    }


@pytest.fixture
def tmp_log(tmp_path):
    log_file = tmp_path / "bug0007.jsonl"
    logger_inst = AgentInstanceLogger(
        agent_class="orchestrator",
        instance_name="Maine",
        log_dir=str(tmp_path),
        log_path=str(log_file),
    )
    return log_file, logger_inst


def _seed_full_history(log_path: Path, n_pre: int, n_mid: int):
    """Write a full-history file: [SYS][U0] + n_pre raw + L2 marker + n_mid raw."""
    lines = [json.dumps({"metadata": {"agent_class": "orchestrator"}})]
    lines.append(json.dumps(_user("system")))
    lines.append(json.dumps(_user("initial user")))
    for i in range(n_pre):
        lines.append(json.dumps(_user(f"pre {i}") if i % 2 == 0 else _assistant(f"pa {i}")))
    lines.append(json.dumps(_marker("L2 consolidated", kind="l2")))
    for i in range(n_mid):
        lines.append(json.dumps(_user(f"mid {i}") if i % 2 == 0 else _assistant(f"m {i}")))
    log_path.write_text("\n".join(lines) + "\n")


def _read_msgs(log_path: Path):
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "metadata" not in item and "event" not in item:
            out.append(item)
    return out


def _raw_contents(msgs):
    return [
        m["content"] for m in msgs
        if isinstance(m.get("content"), str) and not m["content"].startswith(COMPRESSION_MARKER)
    ]


# ──────────────────────────────────────────────
# 1. Full-history retention: trimmed pool state must NOT shrink a larger file
# ──────────────────────────────────────────────

def test_trimmed_pool_preserves_all_pre_marker_raw(tmp_log):
    """reset_history(rewrite=True) with a TRIMMED pool against a LARGER full-history file
    must retain every pre-marker raw message already on disk (design §5.2)."""
    log_file, lg = tmp_log
    _seed_full_history(log_file, n_pre=116, n_mid=50)

    before = _read_msgs(log_file)
    raw_before = _raw_contents(before)
    # system + initial user are also non-marker "raw" lines, so total = 2 + n_pre + n_mid.
    assert len(raw_before) == 2 + 116 + 50

    # Trimmed pool working set (what get_conversation returns after compression):
    # [SYS][U0] + L2 + a fresh newest marker + small tail. Does NOT contain the raw.
    l2 = _marker("L2 consolidated", kind="l2")
    fresh = _marker("FRESH L1", kind="l1")
    pool = [_user("system"), _user("initial user"), l2, fresh,
            _user("tail 0"), _assistant("t 0")]

    ok = lg.reset_history(pool, rewrite=True)
    assert ok is True

    after = _read_msgs(log_file)
    raw_after = _raw_contents(after)
    # Every pre-existing raw message must survive the rewrite.
    lost = [c for c in raw_before if c not in raw_after]
    assert lost == [], f"BUG_0007 regression: {len(lost)} raw messages dropped, e.g. {lost[:3]}"


# ──────────────────────────────────────────────
# 2. Idempotency: marker already present = no-op (file unchanged)
# ──────────────────────────────────────────────

def test_marker_already_present_is_noop(tmp_log):
    """If the pool's newest marker is already byte-identical in the file, the rewrite
    must be a no-op — file content and length stay identical."""
    log_file, lg = tmp_log
    _seed_full_history(log_file, n_pre=20, n_mid=30)

    before = _read_msgs(log_file)
    before_bytes = [json.dumps(m, sort_keys=True) for m in before]

    # Pool whose newest marker (L2) is already present verbatim in the file.
    l2 = _marker("L2 consolidated", kind="l2")
    pool = [_user("system"), _user("initial user"), l2, _user("tail")]

    ok = lg.reset_history(pool, rewrite=True)
    assert ok is True

    after = _read_msgs(log_file)
    after_bytes = [json.dumps(m, sort_keys=True) for m in after]
    assert before_bytes == after_bytes, "no-op violated: file changed when marker already present"


# ──────────────────────────────────────────────
# 3. Concurrency: appends + rewrites never lose messages (the actual BUG_0007 race)
# ──────────────────────────────────────────────

def test_concurrent_append_and_rewrite_no_loss(tmp_log):
    """BUG_0007 root cause: _append_line raced the locked rewrite path, dropping messages.
    After the fix (append under _write_lock + os.replace retry), concurrent appends and
    rewrites must lose ZERO messages."""
    log_file, lg = tmp_log
    _seed_full_history(log_file, n_pre=50, n_mid=30)

    # Pre-open the handle as a live agent would have.
    lg._ensure_file()

    errors = []

    def appender():
        for i in range(40):
            try:
                lg.log_message(_assistant(f"live append {i}"))
            except Exception as e:  # pragma: no cover - should not happen after fix
                errors.append(str(e))

    def compressor():
        l2 = _marker("L2 consolidated", kind="l2")
        fresh = _marker("FRESH L1", kind="l1")
        pool = [_user("system"), _user("initial user"), l2, fresh]
        for _ in range(5):
            lg.reset_history(pool, rewrite=True)

    t1 = threading.Thread(target=appender)
    t2 = threading.Thread(target=compressor)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"append raised (handle race not closed): {errors[:3]}"

    after = _read_msgs(log_file)
    survived = [m for m in after if isinstance(m.get("content"), str)
                and m["content"].startswith("live append")]
    assert len(survived) == 40, (
        f"BUG_0007 regression: {40 - len(survived)} of 40 concurrent appends lost "
        f"(append/rewrite race)"
    )


# ──────────────────────────────────────────────
# 4. End-to-end L2 consolidation sequence retains pre-marker raw
# ──────────────────────────────────────────────

def test_consolidation_sequence_retains_pre_marker_raw(tmp_log):
    """Full L2 consolidation flow: consolidation rewrite followed by the handler's
    reset_history sync. The pre-marker raw count must NOT decrease."""
    log_file, lg = tmp_log
    # File with 2 markers (consolidation threshold is >=3 for real runs; here we test
    # the filter directly preserves raw through a consolidation-style rewrite).
    _seed_full_history(log_file, n_pre=100, n_mid=40)

    before = _read_msgs(log_file)
    pre_raw_before = _raw_contents(before)

    l2 = _marker("L2 consolidated", kind="l2")
    # Simulate consolidation: new L2 replaces first marker; pool state passed in.
    from agent_cascade.compression.helpers import filter_jsonl_for_consolidation
    result_msgs, removed = filter_jsonl_for_consolidation(before, lg._format_message(l2), -1)

    # Raw retention invariant: no raw message is dropped by consolidation filtering.
    post_raw = _raw_contents(result_msgs)
    lost = [c for c in pre_raw_before if c not in post_raw]
    assert lost == [], f"consolidation dropped {len(lost)} raw messages"
