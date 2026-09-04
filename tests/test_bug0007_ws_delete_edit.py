"""Regression tests for BUG_0007 — UI delete/edit rewriting the agent log from the TRIMMED
pool working set, dropping all pre-marker raw history.

Root cause: handle_delete_messages / handle_edit_message read inst.conversation (the trimmed
pool ~144 msgs), popped/edited there, then rewrite_log_with_history(...) overwrote the ENTIRE
on-disk JSONL (full history ~1528) with the small list — destroying pre-marker raw. allow_shrink=True
in the delete path even disabled the shrink guard that would have caught it.

Fix under test:
  - Both handlers now operate on the FULL on-disk history (logger.get_full_history()), mapping
    the UI's displayed indices to file messages by identity (timestamp + content).
  - The delete path no longer passes allow_shrink=True — the shrink guard stays armed so a
    normal delete can never collapse a full-history file.

All tests are self-contained: no LLM, API server, or network required.
"""

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
from agent_cascade.ws_handlers import WsMessageHandler


# ──────────────────────────────────────────────
# Helpers / fakes
# ──────────────────────────────────────────────

def _user(content: str, ts: str = None) -> dict:
    m = {"role": "user", "content": content}
    if ts:
        m["timestamp"] = ts
    return m


def _assistant(content: str, ts: str = None) -> dict:
    m = {"role": "assistant", "content": content}
    if ts:
        m["timestamp"] = ts
    return m


def _marker(summary: str, ts: str, kind: str = "l1") -> dict:
    header = f"L2, 7 summaries consolidated" if kind == "l2" else summary
    return {
        "role": "user",
        "content": (f"{COMPRESSION_MARKER} ({header}) ---\n<context_summary>\n{summary}\n</context_summary>"),
        "timestamp": ts,
    }


def _ts(i: int) -> str:
    # Unique, ordered timestamps so identity keys are distinct per message.
    return f"2026-09-01T08:{i // 60:02d}:{i % 60:02d}.000000"


class _FakeInstance:
    """Minimal stand-in for an AgentPool instance: trimmed conversation + rebuild hook."""

    def __init__(self, conversation):
        self._compression_lock = threading.Lock()
        self.conversation = list(conversation)
        self.rebuilt = None

    def rebuild_conversation(self, new_messages):
        with self._compression_lock:
            self.conversation = list(new_messages)
        self.rebuilt = list(new_messages)


class _FakePool:
    def __init__(self, instance, logger_inst):
        self._inst = instance
        self._logger = logger_inst
        # Pre-populate so handlers can write instance_state[name]['messages'] (mirrors real pool).
        self.instance_state = {"Maine": {"messages": []}}

    def get_instance(self, name):
        return self._inst

    def get_logger(self, name, agent_class):
        return self._logger


def _make_handler(tmp_path: Path) -> tuple:
    """Build a WsMessageHandler wired to a real logger + fake pool/instance.

    Returns (handler, logger_inst, fake_instance, log_file).
    The on-disk file holds FULL history; the instance.conversation is the TRIMMED pool.
    """
    log_file = tmp_path / "bug0007_ws.jsonl"

    # ── Full on-disk history: [SYS][U0] + 100 pre-marker raw + L2 marker + 40 mid + tail ──
    full = []
    full.append(_user("system", _ts(0)))
    full.append(_user("initial user", _ts(1)))
    for i in range(100):
        full.append(_user(f"pre {i}", _ts(2 + i)) if i % 2 == 0 else _assistant(f"pa {i}", _ts(2 + i)))
    l2 = _marker("L2 consolidated", _ts(102), kind="l2")
    full.append(l2)
    for i in range(40):
        full.append(_user(f"mid {i}", _ts(103 + i)) if i % 2 == 0 else _assistant(f"m {i}", _ts(103 + i)))

    lines = [json.dumps({"metadata": {"agent_class": "orchestrator"}})]
    for m in full:
        lines.append(json.dumps(m))
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    logger_inst = AgentInstanceLogger(
        agent_class="orchestrator", instance_name="Maine",
        log_dir=str(tmp_path), log_path=str(log_file),
    )
    # Mirror what load_session_from_log leaves: data["history"] = full tracked set.
    logger_inst.load_history_from_file()

    # ── Trimmed pool working set (what the UI displays): [SYS][U0][L2][last few tail] ──
    trimmed = [full[0], full[1], l2, full[-3], full[-2], full[-1]]

    fake_inst = _FakeInstance(trimmed)
    fake_pool = _FakePool(fake_inst, logger_inst)

    async def _noop_broadcast(*a, **k):
        return None

    handler = WsMessageHandler(
        session={"session_name": "Maine"},
        agent_pool=fake_pool,
        agents=[],
        send_queue=None,
        broadcast_fn=_noop_broadcast,
        build_state_fn=lambda *a, **k: {},
        start_gen_fn=None,
        session_lock=threading.Lock(),
        app=None,
    )
    return handler, logger_inst, fake_inst, log_file


def _read_msgs(log_file: Path):
    out = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
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
    return [m["content"] for m in msgs
            if isinstance(m.get("content"), str) and not m["content"].startswith(COMPRESSION_MARKER)]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ──────────────────────────────────────────────
# (a) Delete one displayed message on a full-history file → pre-marker raw survive
# ──────────────────────────────────────────────

def test_delete_one_preserves_pre_marker_raw(tmp_path):
    handler, lg, fake_inst, log_file = _make_handler(tmp_path)

    before = _read_msgs(log_file)
    raw_before = set(_raw_contents(before))
    # Pre-marker raw = every non-marker message that sits BEFORE the L2 marker on disk.
    l2_idx = next(i for i, m in enumerate(before)
                  if isinstance(m.get("content"), str) and m["content"].startswith(COMPRESSION_MARKER))
    pre_marker_raw = set(_raw_contents(before[:l2_idx]))
    assert pre_marker_raw, "seed sanity: pre-marker raw present on disk"

    # The displayed (trimmed) pool has 6 messages. Delete the LAST one (index 5 = full[-1]).
    trimmed = fake_inst.conversation
    target_content = trimmed[5]["content"]
    data = {"indices": [5], "instance_name": "Maine"}

    _run(handler.handle_delete_messages(data))

    after = _read_msgs(log_file)
    raw_after = set(_raw_contents(after))

    # 1. ALL pre-marker raw must survive the delete (the actual BUG_0007 invariant).
    lost_pre = pre_marker_raw - raw_after
    assert lost_pre == set(), f"BUG_0007: delete dropped {len(lost_pre)} pre-marker raw, e.g. {list(lost_pre)[:5]}"

    # 2. The targeted message was actually removed (exactly one fewer message overall).
    assert len(after) == len(before) - 1, f"expected {len(before)-1} msgs after deleting 1, got {len(after)}"
    assert target_content not in raw_after, "targeted message should be removed"

    # 3. Pool working set stayed trimmed and dropped the same message by identity.
    assert len(fake_inst.rebuilt) == len(trimmed) - 1
    assert all(m["content"] != target_content for m in fake_inst.rebuilt)


# ──────────────────────────────────────────────
# (b) Shrink guard aborts a rewrite that would drop far more than requested
# ──────────────────────────────────────────────

def test_shrink_guard_aborts_massive_drop(tmp_path):
    handler, lg, fake_inst, log_file = _make_handler(tmp_path)

    before_count = len(_read_msgs(log_file))
    # data["history"] was loaded as the full set, so a rewrite that shrinks it dramatically
    # (as the old buggy code did: trimmed pool over full file) must be REFUSED.
    tiny = [lg._format_message(fake_inst.conversation[0])]  # 1 message

    ok = lg.rewrite_log_with_history(tiny, caller="ws_delete_test")  # allow_shrink NOT set
    assert ok is False, "shrink guard should refuse a massive shrink without allow_shrink=True"

    after_count = len(_read_msgs(log_file))
    assert after_count == before_count, "file must be unchanged when the shrink guard aborts"


# ──────────────────────────────────────────────
# (c) Edit path preserves pre-marker raw
# ──────────────────────────────────────────────

def test_edit_preserves_pre_marker_raw(tmp_path):
    handler, lg, fake_inst, log_file = _make_handler(tmp_path)

    before = _read_msgs(log_file)
    raw_before = set(_raw_contents(before))
    l2_idx = next(i for i, m in enumerate(before)
                  if isinstance(m.get("content"), str) and m["content"].startswith(COMPRESSION_MARKER))
    pre_marker_raw = set(_raw_contents(before[:l2_idx]))

    # Edit the LAST displayed message (index 5) to new content.
    trimmed = fake_inst.conversation
    target_content = trimmed[5]["content"]
    data = {"index": 5, "content": "EDITED CONTENT", "instance_name": "Maine"}

    _run(handler.handle_edit_message(data))

    after = _read_msgs(log_file)
    raw_after = set(_raw_contents(after))

    # 1. ALL pre-marker raw survive the edit (BUG_0007 invariant).
    lost_pre = pre_marker_raw - raw_after
    assert lost_pre == set(), f"BUG_0007: edit dropped {len(lost_pre)} pre-marker raw, e.g. {list(lost_pre)[:5]}"

    # 2. Message count unchanged (edit is in-place, not a delete).
    assert len(after) == len(before), f"edit should not change message count: {len(before)} -> {len(after)}"

    # 3. The edit was actually applied to the on-disk copy of that message.
    assert "EDITED CONTENT" in raw_after, "edited content should be present on disk"
    assert target_content not in raw_after or "EDITED CONTENT" in raw_after


# ──────────────────────────────────────────────
# (d) Fresh/uncompressed session: pool == file, delete still works via fallback
# ──────────────────────────────────────────────

def test_delete_on_fresh_session_no_file(tmp_path):
    """When the on-disk file is empty/missing (fresh session), delete falls back to the pool
    view and still removes the targeted message without error."""
    log_file = tmp_path / "fresh.jsonl"
    logger_inst = AgentInstanceLogger(
        agent_class="orchestrator", instance_name="Maine",
        log_dir=str(tmp_path), log_path=str(log_file),
    )
    # No file written yet.
    conv = [_user("system", _ts(0)), _user("hello", _ts(1)), _assistant("hi", _ts(2))]
    fake_inst = _FakeInstance(conv)
    fake_pool = _FakePool(fake_inst, logger_inst)

    async def _noop(*a, **k):
        return None

    handler = WsMessageHandler(
        session={"session_name": "Maine"}, agent_pool=fake_pool, agents=[], send_queue=None,
        broadcast_fn=_noop, build_state_fn=lambda *a, **k: {}, start_gen_fn=None,
        session_lock=threading.Lock(), app=None,
    )

    _run(handler.handle_delete_messages({"indices": [1], "instance_name": "Maine"}))

    rebuilt = fake_inst.rebuilt
    assert len(rebuilt) == 2
    assert all(m["content"] != "hello" for m in rebuilt), "targeted message should be removed from pool"
