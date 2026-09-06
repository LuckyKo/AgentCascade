"""E2E timing-sensitive streaming test for the "reasoning-in-history" regression.

WHAT THIS TEST COVERS (and its boundary)
----------------------------------------
This drives the REAL backend pipeline end-to-end with NO live LLM:

    ExecutionEngine.run()  [agent_cascade/engine/core.py]
        -> _call_llm_with_injection()   [mocked at this generator level only]
        -> engine yields (msgs, is_streaming_tick=True) per streaming tick
    run_agent_unified-style consumer loop  [mirrors agent_cascade/run_agent_unified.py L146-217]
        -> broadcast_stream_update()     [agent_cascade/api_integration_pkg/streaming.py]
        -> build_stream_update_from_pool()[agent_cascade/api_integration_pkg/state_builder.py]
        -> asyncio.run_coroutine_threadsafe(_put_stream_update, ...) onto pool._ws_send_queue

We capture every ``stream_update`` event that lands on the async send_queue with its
arrival timestamp and assert on TIMING (inter-arrival cadence), not just content:

  * incremental cadence: N >> 1 distinct stream_updates arrive spread over time (not a burst)
  * no long stall: while the mock LLM is actively emitting, no inter-broadcast gap exceeds a bound
  * reasoning surfaced incrementally: intermediate updates carry GROWING reasoning_content
    (partial reasoning visible before completion), not just the final full blob

The mock emits a pure-reasoning phase (many small ``reasoning_content`` deltas spaced ~50ms
apart) followed by a short content phase, against a pre-seeded conversation that ALREADY
contains a prior assistant message with non-trivial ``reasoning_content`` (the trigger
condition for the reported regression).

The ONLY thing mocked is the LLM generator (``ExecutionEngine._execute_llm_call``); every
other stage — engine.run(), the 0.1s _streaming_responses throttle, the broadcast throttle,
state serialization, and the async queue dispatch — runs UNMOCKED. This is exactly the path
the frontend consumes, so a PASS here means the backend hands growing reasoning to the WS
queue incrementally; any "no streaming / bursty" symptom would then live DOWNSTREAM of this
boundary (frontend render path), not in it.

TEST-ONLY: no production code is modified.

Run with:  python -m pytest tests/test_streaming_e2e_reasoning.py -v
"""

import asyncio
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

# Ensure top-level imports work (matches test_api_endpoints.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.llm.schema import Message, ASSISTANT  # noqa: E402

INSTANCE_NAME = "Maine"
AGENT_CLASS = "coder"

# Real example session log (same format the WS 'load_session' / REST resume command
# consumes). Used as a fallback fixture when present; the test otherwise builds an
# equivalent synthetic JSONL so it is self-contained and deterministic in CI.
EXAMPLE_SESSION_LOG = Path(
    r"N:\work\WD\AgentWorkspace\logs\researcher_stream-probe-analyst_20260903_093608.jsonl"
)

# ── Mock LLM timing profile (deterministic, fast) ─────────────────────────────
# Pure-reasoning phase: REASONING_DELTAS deltas spaced REASONING_GAP apart.
# The delta cadence (40ms) is FINER than the backend's 0.1s _streaming_responses
# throttle, so a healthy pipeline must coalesce to ~10 updates/s — that is exactly
# the responsiveness we assert on. A bursty/broken path would collapse this to a
# handful of end-of-turn events and fail hard.
REASONING_DELTAS = 45          # ~1.8s of reasoning at 40ms spacing
REASONING_GAP = 0.04           # 40ms between reasoning deltas (finer than the 0.1s throttle)
CONTENT_DELTAS = 4             # short content phase
CONTENT_GAP = 0.04

# ── Timing assertions (RESPONSIVENESS-SENSITIVE — intentionally tight) ────────
# A healthy backend emits ~one broadcast per 0.1s _streaming_responses refresh, so
# over a ~2s generation we expect on the order of ~15+ updates. These bounds are set
# to FAIL on any bursty/stalled behavior:
MIN_UPDATES_DURING_GENERATION = 12  # must see a steady stream, not a single end-of-turn burst
MAX_INTER_ARRIVAL_GAP = 0.6         # no inter-broadcast gap > 0.6s while the mock is actively emitting


# ---------------------------------------------------------------------------
# Mock LLM generator — replaces ONLY ExecutionEngine._execute_llm_call.
# Yields List[Message] (accumulated response), matching the real contract.
# ---------------------------------------------------------------------------

def _make_mock_execute_llm_call(self, instance, template, messages, active_functions):
    """Generator standing in for the real LLM stream.

    Emits a pure-reasoning phase (content empty, reasoning grows) then a short
    content phase, sleeping between deltas to simulate real token cadence. Each
    yield is the ACCUMULATED response (a single assistant Message), exactly as
    ``_execute_llm_call`` / oai.py ``delta_stream`` yields.
    """
    reasoning_parts = []
    content_parts = []

    # Pure-reasoning phase: many small reasoning_content deltas, content empty.
    for i in range(REASONING_DELTAS):
        time.sleep(REASONING_GAP)
        reasoning_parts.append(f"thought_{i:02d}_")
        yield [
            _msg(role=ASSISTANT, content="", reasoning_content="".join(reasoning_parts))
        ]

    # Short content phase: reasoning frozen, content grows.
    final_reasoning = "".join(reasoning_parts)
    for i in range(CONTENT_DELTAS):
        time.sleep(CONTENT_GAP)
        content_parts.append(f" answer_{i}")
        yield [
            _msg(role=ASSISTANT, content="".join(content_parts),
                 reasoning_content=final_reasoning)
        ]


# ---------------------------------------------------------------------------
# Burst / sparse-arrival mock profiles.
# These model the REAL-WORLD condition (Qwen endpoints): the LLM does NOT emit one
# token at a time — it delivers in bursts or sparsely. We measure whether the backend
# pipeline degrades to "no streaming / burst-at-end" under those arrival patterns.
# Each is a standalone generator with the SAME signature as _execute_llm_call and yields
# ACCUMULATED List[Message], exactly like the real contract.
# ---------------------------------------------------------------------------

# Total synthetic reasoning length shared by the burst profiles (kept identical so the
# scenarios differ ONLY in arrival cadence, not payload size).
_BURST_REASONING_TOTAL = 45 * len("thought_00_")   # == REASONING_DELTAS deltas' worth
_BURST_CONTENT = " answer" * 4

def _full_reasoning(n_chars=_BURST_REASONING_TOTAL):
    """Deterministic reasoning string of exactly n_chars (repeated unit)."""
    unit = "thought_"
    return (unit * ((n_chars // len(unit)) + 1))[:n_chars]


def _mock_all_burst(self, instance, template, messages, active_functions):
    """ALL-BURST: the ENTIRE reasoning+content response arrives as a SINGLE yield at the
    very end — no intermediate yields. The extreme burst case."""
    time.sleep(1.0)  # simulate the model 'thinking' with nothing on the wire
    yield [
        _msg(role=ASSISTANT, content=_BURST_CONTENT, reasoning_content=_full_reasoning())
    ]


def _mock_chunked(self, instance, template, messages, active_functions):
    """SLOW-BURST / CHUNKED: a few LARGE batches (each a big accumulated chunk) spaced ~0.6s
    apart — mimics 'server sends a batch every second' rather than per-token."""
    n_batches = 4
    reasoning_parts = []
    for i in range(n_batches):
        time.sleep(0.6)
        # Append a big slice of the total reasoning each batch (accumulated).
        reasoning_parts.append(_full_reasoning(_BURST_REASONING_TOTAL // n_batches))
        yield [
            _msg(role=ASSISTANT, content="", reasoning_content="".join(reasoning_parts))
        ]
    # Final chunk carries the content.
    yield [
        _msg(role=ASSISTANT, content=_BURST_CONTENT, reasoning_content=_full_reasoning())
    ]


def _mock_single_reasoning_blob(self, instance, template, messages, active_functions):
    """SINGLE-YIELD-REASONING-THEN-CONTENT: one BIG reasoning blob yielded at once (the
    reported trigger — 'reasoning arrives as one blob'), then a short content phase."""
    time.sleep(0.4)
    full_reasoning = _full_reasoning()
    # One big reasoning blob, no intermediate reasoning yields.
    yield [_msg(role=ASSISTANT, content="", reasoning_content=full_reasoning)]
    # Then content grows in a couple of small deltas (reasoning frozen).
    for i in range(2):
        time.sleep(0.4)
        yield [
            _msg(role=ASSISTANT, content=" answer" * (i + 1), reasoning_content=full_reasoning)
        ]


def _msg(**kwargs):
    return Message(**kwargs)


# ---------------------------------------------------------------------------
# Session-log fixture — build a real-format JSONL so the test can drive the
# PRODUCTION session loader (AgentPool.load_session_from_log), the same path the
# WS 'load_session' command and REST resume route use. Self-contained & deterministic.
# ---------------------------------------------------------------------------

def _build_synthetic_session_log(dest: Path) -> Path:
    """Write a minimal but format-faithful agent session JSONL to ``dest``.

    Mirrors the real log layout consumed by ``load_session_from_log``:
      line 1: {"metadata": {...}}            (agent_class, instance_name, current_log_path)
      then:   system / user / assistant(with reasoning_content) messages
              a compression marker ([COMPRESSION] + <context_summary>) and a tail,
    so the loader's working-set logic ``[SYS][U0][markers][tail]`` is exercised.
    """
    # Use a 'researcher' class (matches the real example log) and keep the history
    # deliberately SMALL so engine.run() does NOT trigger the real compression path
    # (which would spawn a Compressor agent and break this streaming-focused test).
    meta = {
        "agent_class": "researcher",
        "instance_name": INSTANCE_NAME,
        "start_timestamp": "2026-09-03T09:00:00.000000",
        "last_update": "2026-09-03T09:10:00.000000",
        "current_log_path": str(dest),
        "working_dir": str(PROJECT_ROOT),
        "supervisor": "Maine",
    }
    lines = [json.dumps({"metadata": meta})]

    def _line(role, content, **extra):
        d = {"role": role, "content": content}
        d.update(extra)
        d["timestamp"] = "2026-09-03T09:00:01.000000"
        lines.append(json.dumps(d))

    _line("system", f"You are {INSTANCE_NAME}. Senior software engineer.")
    _line("user", "Explain how the streaming pipeline works.")
    # Prior assistant turn WITH non-trivial reasoning_content — the trigger condition.
    _line(
        "assistant",
        "(prior answer)",
        reasoning_content=(
            "Let me think carefully about the pipeline: the LLM emits deltas, the engine "
            "forwards them, and the broadcast loop pushes them to the UI. " * 3
        ),
    )
    # A compression marker so the working-set builder takes the [SYS][U0][markers][tail] branch.
    _line(
        "user",
        "--- CONTEXT COMPRESSED (70% of history summarized) ---\n<context_summary>\n"
        "- Prior investigation of the streaming pipeline.\n- Confirmed backend path healthy.\n"
        "</context_summary>",
    )
    _line("user", "[COMPRESSION] forced compression complete. 12 messages summarized.")
    # Tail after the last marker (recent turns).
    _line("assistant", "(tail answer)", reasoning_content="tail reasoning about next steps.")
    _line("user", "Now continue with the streaming test.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def _resolve_session_log(tmp_path: Path) -> Path:
    """Resolve a session log to feed the PRODUCTION loader.

    PREFER THE REAL EXAMPLE LOG (EXAMPLE_SESSION_LOG) so we STRESS the pipeline with a
    genuinely large, reasoning-heavy history (~26k tokens / ~200 summarized messages) —
    that is the condition under which the "no-streaming / bursty" regression appears. A
    small synthetic session would NOT reproduce it (it passed cleanly), so we load the real
    thing. The log is copied into tmp_path so the loader can rewrite it without touching
    the original. Falls back to a synthetic log if the example is absent (CI).

    NOTE: the heavy context WOULD trigger the real compression path in engine.run(); the
    fixture suppresses that via instance._generate_cfg_override['max_input_tokens'] (see
    streaming_harness) so the test stays focused on streaming while keeping the load.
    """
    if EXAMPLE_SESSION_LOG.exists():
        target = tmp_path / "session" / EXAMPLE_SESSION_LOG.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_SESSION_LOG, target)
        return target
    return _build_synthetic_session_log(tmp_path / "session" / f"researcher_{INSTANCE_NAME}.jsonl")


# ---------------------------------------------------------------------------
# Payload extraction — pull the live (partial) assistant message out of a
# stream_update event and measure its reasoning/content length.
# ---------------------------------------------------------------------------

def _extract_live_assistant(event):
    """Return the last assistant message dict from an instance payload, or None.

    During streaming, ``_serialize_instance`` appends the partial LLM content
    (from ``instance._streaming_responses``) AFTER the committed conversation,
    so the LAST assistant entry in ``messages`` is the live growing one.
    """
    if not isinstance(event, dict):
        return None
    instances = event.get("instances") or event.get("agent_instances") or {}
    inst = instances.get(INSTANCE_NAME)
    if not isinstance(inst, dict):
        return None
    msgs = inst.get("messages")
    if not msgs:
        return None
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == ASSISTANT:
            return m
    return None


def _reasoning_len(m):
    if not m:
        return 0
    r = m.get("reasoning_content") or ""
    return len(r) if isinstance(r, str) else 0


def _content_len(m):
    if not m:
        return 0
    c = m.get("content") or ""
    return len(c) if isinstance(c, str) else 0


def _measure_events(events, diag, label):
    """Extract per-update arrival timestamps + reasoning/content lengths from the raw
    (arrival_time, event_dict) list produced by _drive_pipeline, and compute a measured
    summary. Shared by ALL scenarios (incremental + burst) so we never duplicate this logic.

    Returns a dict with:
      n_updates, max_gap, reasoning_lens, content_lens, distinct_reasoning,
      first/last reasoning+content, summary (human-readable string), stream_events count.
    """
    stream_events = [ev for (_t, ev) in events if isinstance(ev, dict) and ev.get("type") == "stream_update"]

    arrivals = []  # (arrival_time, reasoning_len, content_len)
    for arrival, ev in events:
        if not (isinstance(ev, dict) and ev.get("type") == "stream_update"):
            continue
        m = _extract_live_assistant(ev)
        arrivals.append((arrival, _reasoning_len(m), _content_len(m)))

    gaps = [arrivals[i + 1][0] - arrivals[i][0] for i in range(len(arrivals) - 1)]
    max_gap = max(gaps) if gaps else float("inf")

    reasoning_lens = [r for (_t, r, _c) in arrivals]
    content_lens = [c for (_t, _r, c) in arrivals]
    distinct_reasoning = len(set(reasoning_lens))

    summary = (
        f"\n[streaming_e2e_reasoning:{label}] measured against CURRENT backend code\n"
        f"  stream_update events on send_queue : {len(stream_events)}\n"
        f"  num updates during generation      : {len(arrivals)}\n"
        f"  max inter-arrival gap              : "
        f"{('inf (no gaps)' if not gaps else f'{max_gap:.3f}s')}\n"
        f"  reasoning_content lengths (first/last): "
        f"{(reasoning_lens[0] if reasoning_lens else 'n/a')}"
        f" -> {(reasoning_lens[-1] if reasoning_lens else 'n/a')}\n"
        f"  content lengths (first/last)         : "
        f"{(content_lens[0] if content_lens else 'n/a')}"
        f" -> {(content_lens[-1] if content_lens else 'n/a')}\n"
        f"  distinct reasoning lengths seen      : {distinct_reasoning}\n"
        f"  engine.run() ticks consumed          : {diag['ticks']} "
        f"(streaming_ticks={diag['streaming_ticks']})\n"
        f"  _streaming_responses reasoning lens  : {diag['sr_lens'][:5]}...{diag['sr_lens'][-3:]}\n"
    )

    return {
        "label": label,
        "stream_events": len(stream_events),
        "arrivals": arrivals,
        "n_updates": len(arrivals),
        "max_gap": max_gap,
        "reasoning_lens": reasoning_lens,
        "content_lens": content_lens,
        "distinct_reasoning": distinct_reasoning,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Pipeline driver — mirrors run_agent_unified.py L146-217.
# ---------------------------------------------------------------------------

def _drive_pipeline(pool, engine, instance):
    """Run the real engine.run() in a background thread and capture every
    stream_update event that lands on the async send_queue with its timestamp.

    Returns (events, gen_error) where events is a list of (arrival_monotonic, event_dict).
    """
    from agent_cascade.api_integration_pkg.streaming import broadcast_stream_update

    send_queue = asyncio.Queue()
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    # Wire the pool EXACTLY as run_agent_unified.py L100-101 does.
    pool._ws_send_queue = send_queue
    pool._ws_loop = loop
    pool.stopped = False

    events = []           # list of (arrival_time, event_dict)
    ev_lock = threading.Lock()
    gen_error = {}        # {"exc": ...} if the generator raised
    done = asyncio.Event()  # set by the consumer when engine.run() is exhausted
    diag = {"ticks": 0, "streaming_ticks": 0, "sr_lens": []}

    def _consumer():
        """Consume engine.run() and broadcast per tick — mirrors run_agent_unified."""
        try:
            last_send = 0.0
            exec_state = {"last_resp_len": 0}
            tick_num = 0
            for turn_output_raw in engine.run(instance):
                if isinstance(turn_output_raw, tuple) and len(turn_output_raw) == 2:
                    turn_output, is_streaming_tick = turn_output_raw
                else:
                    turn_output, is_streaming_tick = turn_output_raw, False

                diag["ticks"] += 1
                if is_streaming_tick:
                    diag["streaming_ticks"] += 1
                with instance._compression_lock:
                    sr = list(instance._streaming_responses)
                diag["sr_lens"].append(sum(len(m.get("reasoning_content") or "") for m in sr))

                now = time.monotonic()
                last_send, exec_state["last_resp_len"] = broadcast_stream_update(
                    pool=pool,
                    instance_name=INSTANCE_NAME,
                    turn_output=turn_output,
                    is_streaming_tick=is_streaming_tick,
                    tick_num=tick_num,
                    now_sec=now,
                    last_send=last_send,
                    last_resp_len=exec_state["last_resp_len"],
                )
                tick_num += 1
        except Exception as e:  # surface generator errors to the test
            import traceback
            gen_error["exc"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        finally:
            loop.call_soon_threadsafe(done.set)

    consumer_thread = threading.Thread(target=_consumer, daemon=True)
    consumer_thread.start()

    # Drain the send_queue in the event loop until the consumer finishes. The
    # loop runs on its own thread (started above), so we drive this as a task
    # rather than run_until_complete (which would clash with the running loop).
    async def _drain():
        while True:
            try:
                item = send_queue.get_nowait()
            except asyncio.QueueEmpty:
                if done.is_set():
                    # Final drain to pick up anything queued just before exit.
                    await asyncio.sleep(0.05)
                    try:
                        item = send_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                else:
                    await asyncio.sleep(0.01)
                    continue
            arrival = time.monotonic()
            with ev_lock:
                events.append((arrival, item))

    drain_future = asyncio.run_coroutine_threadsafe(_drain(), loop)
    try:
        drain_future.result(timeout=30.0)
    finally:
        consumer_thread.join(timeout=5.0)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2.0)
        try:
            loop.close()
        except Exception:
            pass

    return events, gen_error, diag


# ---------------------------------------------------------------------------
# Harness fixture — real AgentPool + instance, minimal (no live router needed).
# ---------------------------------------------------------------------------

@pytest.fixture
def streaming_harness(tmp_path):
    """Build a real AgentPool, then LOAD a genuine session via the PRODUCTION loader
    (AgentPool.load_session_from_log — same path as the WS 'load_session' command / REST
    resume). The loaded conversation contains a prior assistant message with non-trivial
    reasoning_content (the regression trigger condition)."""
    import agent_cascade.agent_pool as ap_mod
    from agent_cascade.agent_instance import AgentInstance
    from agent_cascade.llm.schema import Message, USER, ASSISTANT

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(cfg_dir)

    llm_cfg = {
        "model": "mock",
        "api_base": "http://127.0.0.1:9/v1",
        "model_server": "http://127.0.0.1:9/v1",
        "api_key": "EMPTY",
    }

    try:
        pool = ap_mod.AgentPool(llm_cfg, agents_dir=str(cfg_dir))
    except Exception as e:
        pytest.skip(f"Could not construct a real AgentPool in this environment: {e}")

    # No live router needed: we mock the LLM generator and there is no slot to
    # acquire (a minimal pool has no _acquire_slot, so run() skips slot logic).
    if getattr(pool, "api_router", None) is not None:
        try:
            pool.api_router = None
        except Exception:
            pass

    # Register a real 'coder' template so _call_llm_with_injection finds one and
    # the retry wrapper (which yields None per chunk + populates _streaming_responses)
    # runs cleanly. The LLM generator itself is mocked, so this template's llm is
    # never actually called — it just needs to exist with a .llm attribute.
    from agent_cascade.agents.assistant import Assistant

    def _register_template(agent_class: str):
        t = Assistant(llm=dict(llm_cfg), name=agent_class, description="test streaming template")
        pool.templates[agent_class] = t
        pool.templates[agent_class.lower()] = t

    # Register templates for the agent classes this test may load (researcher is the
    # synthetic log's class; also register AGENT_CLASS as a safety net). The LLM
    # generator is mocked, so these templates' llm is never actually called — they just
    # need to exist with a .llm attribute for _call_llm_with_injection.
    _register_template(AGENT_CLASS)
    _register_template("researcher")

    # ── Load a REAL session via the PRODUCTION loader ────────────────────────
    # This is the same path the WS 'load_session' command and REST resume route use
    # (ws_handlers.handle_load_session / api_server → AgentPool.load_session_from_log).
    # It parses the JSONL, builds the working set [SYS][U0][markers][tail], and creates
    # a fresh AgentInstance — so the streaming test starts from genuine loaded history
    # (including a prior assistant message with reasoning_content = the trigger condition).
    session_log = _resolve_session_log(tmp_path)
    status = pool.load_session_from_log(
        str(session_log),
        target_instance=INSTANCE_NAME,
        clear_sub_agents_before_load=False,  # fresh pool: nothing to dismiss
    )
    assert not status.startswith("Error"), f"load_session_from_log failed: {status}"

    instance = pool.get_instance(INSTANCE_NAME)
    assert instance is not None, (
        f"No instance '{INSTANCE_NAME}' in pool after load (status={status!r}, "
        f"log={session_log})"
    )
    # The loaded conversation must be non-trivial and contain the trigger condition:
    # a prior assistant message with non-trivial reasoning_content.
    assert len(instance.conversation) >= 2, (
        f"Loaded conversation too small ({len(instance.conversation)} msgs) — "
        f"loader may have dropped history. status={status!r}"
    )
    has_prior_reasoning = any(
        getattr(m, "role", None) == ASSISTANT and len(getattr(m, "reasoning_content", "") or "") > 20
        for m in instance.conversation
    )
    assert has_prior_reasoning, (
        f"Loaded conversation has no prior assistant message with reasoning_content — "
        f"the regression trigger condition is not present. status={status!r}"
    )

    # ── Suppress the real compression path while KEEPING the heavy context ──────
    # The loaded history is large (~26k tokens). In engine.run() that would push usage_pct
    # past compression_force_threshold and spawn a Compressor agent (out of scope + slow +
    # non-deterministic for this streaming test). We give the instance a HUGE max_input_tokens
    # via _generate_cfg_override — the Step-1 ABSOLUTE-PRIORITY override in
    # _resolve_max_tokens() (api_integration_pkg/tokens.py) — so usage_pct stays ~tiny and
    # compression never fires, yet the heavy reasoning-heavy context is still loaded. This
    # simulates a high-limit model endpoint: we stress the streaming pipeline with a big
    # history without tripping compression.
    instance._generate_cfg_override = {"max_input_tokens": 1_000_000}

    # Ensure the pool exposes an execution state lock (run_agent_unified uses it).
    if getattr(pool, "_execution", None) is None:
        from unittest.mock import MagicMock
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()

    yield {"pool": pool, "instance": instance}

    try:
        if hasattr(pool, "stop"):
            pool.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def _async_loop_works():
    """Guard: skip cleanly if the environment can't spin up an event loop."""
    try:
        async def _noop():
            return 42
        return asyncio.run(_noop()) == 42
    except Exception as e:
        pytest.skip(f"Cannot start an asyncio event loop in this environment: {e}")


def test_streaming_e2e_reasoning_incremental(streaming_harness):
    """Drive the real engine->broadcast->send_queue pipeline with a reasoning-heavy
    mock LLM and assert that stream_updates arrive INCREMENTALLY (timing) with
    GROWING reasoning_content — not as a single end-of-turn burst."""
    _async_loop_works()

    from agent_cascade.engine.core import ExecutionEngine
    from agent_cascade.llm.schema import ASSISTANT  # noqa: F401 (used in mock)

    pool = streaming_harness["pool"]
    instance = streaming_harness["instance"]
    engine = ExecutionEngine(pool)

    # Mock ONLY the LLM generator. Everything downstream is real.
    monkeypatched = False
    original = ExecutionEngine._execute_llm_call
    try:
        ExecutionEngine._execute_llm_call = _make_mock_execute_llm_call
        monkeypatched = True

        events, gen_error, diag = _drive_pipeline(pool, engine, instance)
    finally:
        if monkeypatched:
            ExecutionEngine._execute_llm_call = original

    # Surface any generator error as a clear failure.
    assert not gen_error, f"engine.run() raised during the pipeline: {gen_error.get('exc')}"

    # ── Collect timing + reasoning-length samples from stream_update events ──
    stream_events = [ev for (_t, ev) in events if isinstance(ev, dict) and ev.get("type") == "stream_update"]
    assert stream_events, (
        f"NO stream_update events reached the send_queue (got {len(events)} total events: "
        f"{[ev.get('type') if isinstance(ev, dict) else type(ev).__name__ for _t, ev in events][:10]}). "
        "The broadcast path never fired — check pool._ws_send_queue/_ws_loop wiring."
    )

    arrivals = []            # (arrival_time, reasoning_len, content_len)
    for arrival, ev in events:
        if not (isinstance(ev, dict) and ev.get("type") == "stream_update"):
            continue
        m = _extract_live_assistant(ev)
        arrivals.append((arrival, _reasoning_len(m), _content_len(m)))

    assert arrivals, "No stream_update events carried a parseable instance payload."

    # ── Measure inter-arrival gaps (only while the mock is actively emitting) ──
    gen_wall = REASONING_DELTAS * REASONING_GAP + CONTENT_DELTAS * CONTENT_GAP  # ~2.05s
    gaps = [arrivals[i + 1][0] - arrivals[i][0] for i in range(len(arrivals) - 1)]
    max_gap = max(gaps) if gaps else float("inf")

    reasoning_lens = [r for (_t, r, _c) in arrivals]
    content_lens = [c for (_t, _r, c) in arrivals]

    # Diagnostic dump (visible with -v / on failure).
    summary = (
        f"\n[streaming_e2e_reasoning] measured against CURRENT backend code\n"
        f"  stream_update events on send_queue : {len(stream_events)}\n"
        f"  reasoning deltas emitted by mock   : {REASONING_DELTAS} @ {REASONING_GAP}s (~{gen_wall:.2f}s)\n"
        f"  num updates during generation      : {len(arrivals)}\n"
        f"  max inter-arrival gap              : {max_gap:.3f}s\n"
        f"  reasoning_content lengths (first/last): {reasoning_lens[0]} -> {reasoning_lens[-1]}\n"
        f"  content lengths (first/last)         : {content_lens[0]} -> {content_lens[-1]}\n"
        f"  distinct reasoning lengths seen      : {len(set(reasoning_lens))}\n"
        f"  engine.run() ticks consumed          : {diag['ticks']} "
        f"(streaming_ticks={diag['streaming_ticks']})\n"
        f"  _streaming_responses reasoning lens  : {diag['sr_lens'][:5]}...{diag['sr_lens'][-3:]}\n"
    )
    print(summary)  # always show measured numbers (visible with -s, on pass and fail)

    # ── ASSERTION 1: incremental cadence — many updates, not a single burst ──
    assert len(arrivals) >= MIN_UPDATES_DURING_GENERATION, (
        f"Only {len(arrivals)} stream_updates arrived during generation "
        f"(need >= {MIN_UPDATES_DURING_GENERATION}). This is the 'burst at turn end' symptom.\n{summary}"
    )

    # ── ASSERTION 2: no long stall while the mock is actively emitting ──
    assert max_gap < MAX_INTER_ARRIVAL_GAP, (
        f"Max inter-broadcast gap was {max_gap:.3f}s (bound {MAX_INTER_ARRIVAL_GAP}s) while the "
        f"mock LLM was actively emitting — a stall in the backend path.\n{summary}"
    )

    # ── ASSERTION 3: reasoning surfaced incrementally (growing, not final-blob-only) ──
    distinct_reasoning = len(set(reasoning_lens))
    assert distinct_reasoning >= MIN_UPDATES_DURING_GENERATION, (
        f"reasoning_content only took {distinct_reasoning} distinct values across "
        f"{len(arrivals)} updates — partial reasoning was NOT surfaced incrementally "
        f"(it arrived as a single blob). lengths={reasoning_lens}\n{summary}"
    )

    # Reasoning must have GROWN over the generation window (monotonic increase somewhere).
    assert max(reasoning_lens) > min(reasoning_lens), (
        f"reasoning_content never grew during streaming: {reasoning_lens}\n{summary}"
    )

    # ── ASSERTION 4: content phase also streamed (the final answer is present) ──
    assert max(content_lens) > 0, (
        f"No content was ever surfaced in a stream_update: {content_lens}\n{summary}"
    )


# ---------------------------------------------------------------------------
# Burst / sparse-arrival scenarios.
#
# These reuse the SAME real pipeline + harness as the incremental test; only the mocked
# LLM generator differs. For each we MEASURE what the current backend actually does and
# LOCK IN that behavior as the baseline (assert on observed values), so any future
# regression is caught. If a scenario shows ZERO intermediate updates (pure end-of-turn
# burst), the assertion makes that explicit and loud — that is a valid, important finding.
# ---------------------------------------------------------------------------

def _run_scenario(streaming_harness, mock_fn, label):
    """Mock ONLY the LLM generator with ``mock_fn``, drive the real pipeline, and return
    the measured summary dict. Shared by every scenario test (no duplication)."""
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = streaming_harness["pool"]
    instance = streaming_harness["instance"]
    engine = ExecutionEngine(pool)

    original = ExecutionEngine._execute_llm_call
    try:
        ExecutionEngine._execute_llm_call = mock_fn
        events, gen_error, diag = _drive_pipeline(pool, engine, instance)
    finally:
        ExecutionEngine._execute_llm_call = original

    assert not gen_error, f"engine.run() raised during the pipeline [{label}]: {gen_error.get('exc')}"
    return _measure_events(events, diag, label)


def test_streaming_e2e_all_burst(streaming_harness):
    """ALL-BURST: entire response arrives as ONE yield at the end.

    Documents + locks in how the backend handles the extreme burst. OBSERVED current behavior
    (measured 2026-09-03): the pipeline emits a SMALL FIXED NUMBER of end-of-turn ticks
    (~2 — one for the streaming tick, one for the turn-end commit), ALL carrying the FULL
    blob (reasoning 495, content 28) with ZERO intermediate values. So the user sees NOTHING
    until the very end, then a burst of identical updates. We lock that in: it must NOT be
    incremental (distinct reasoning stays 1) and it must stay a small count (not grow into a
    real stream). If the backend ever starts synthesizing intermediate ticks here, this fails
    loudly so we re-baseline."""
    m = _run_scenario(streaming_harness, _mock_all_burst, "all-burst")
    print(m["summary"])

    # OBSERVED: a small fixed number of end-of-turn updates (measured: 2). Lock in the ceiling.
    assert m["n_updates"] <= 3, (
        f"ALL-BURST produced {m['n_updates']} updates — far more than the ~2 observed "
        f"end-of-turn ticks. The backend is now synthesizing intermediate ticks for a "
        f"single-yield response. Re-baseline.\n{m['summary']}"
    )
    # The final answer MUST still arrive (no data loss). If n_updates==0, WORST case — loud.
    assert m["n_updates"] >= 1, (
        f"ALL-BURST produced ZERO stream_updates — the user gets NOTHING (not even the final "
        f"answer). This is a severe backend bug.\n{m['summary']}"
    )
    # The extreme burst must NOT be surfaced incrementally: every update carries the same
    # full blob → distinct reasoning stays 1. This is the key 'no streaming' signature.
    assert m["distinct_reasoning"] == 1, (
        f"ALL-BURST showed {m['distinct_reasoning']} distinct reasoning values — the backend "
        f"is now splitting a single-yield response into increments it doesn't have. "
        f"Re-baseline.\n{m['summary']}"
    )
    assert max(m["reasoning_lens"]) > 0 and max(m["content_lens"]) > 0, (
        f"ALL-BURST end-of-turn update lost reasoning/content. "
        f"reasoning={m['reasoning_lens']} content={m['content_lens']}\n{m['summary']}"
    )


def test_streaming_e2e_chunked(streaming_harness):
    """SLOW-BURST / CHUNKED: a few large batches spaced ~0.6s apart.

    Measures whether reasoning is surfaced incrementally BETWEEN batches (it should be —
    each batch is an accumulated yield, so the pipeline sees distinct growing values)."""
    m = _run_scenario(streaming_harness, _mock_chunked, "chunked")
    print(m["summary"])

    # Each of the 4 reasoning batches + 1 content batch is a distinct accumulated yield, so
    # the backend should surface several updates with GROWING reasoning (not one blob).
    assert m["n_updates"] >= 3, (
        f"CHUNKED produced only {m['n_updates']} updates — batches are being collapsed into "
        f"a single end-of-turn event.\n{m['summary']}"
    )
    # Reasoning must be surfaced incrementally across the batches (more than one distinct value).
    assert m["distinct_reasoning"] >= 3, (
        f"CHUNKED reasoning only took {m['distinct_reasoning']} distinct values — partial "
        f"reasoning is NOT surfaced between batches. lengths={m['reasoning_lens']}\n{m['summary']}"
    )
    assert max(m["reasoning_lens"]) > min(m["reasoning_lens"]), (
        f"CHUNKED reasoning never grew across batches: {m['reasoning_lens']}\n{m['summary']}"
    )
    # Content arrives in the final batch.
    assert max(m["content_lens"]) > 0, (
        f"CHUNKED content was never surfaced: {m['content_lens']}\n{m['summary']}"
    )


def test_streaming_e2e_single_reasoning_blob(streaming_harness):
    """SINGLE-YIELD-REASONING-THEN-CONTENT: one big reasoning blob at once, then content.

    The reported trigger ('reasoning arrives as one blob'). Documents whether partial
    reasoning EVER surfaces (it should NOT — it's a single yield) and that the final answer
    still arrives."""
    m = _run_scenario(streaming_harness, _mock_single_reasoning_blob, "single-reasoning-blob")
    print(m["summary"])

    # The whole reasoning is ONE yield → the backend can only surface it as a single value.
    # Lock in that partial reasoning does NOT appear incrementally (it's a blob by input).
    assert m["distinct_reasoning"] <= 1, (
        f"SINGLE-REASONING-BLOB showed {m['distinct_reasoning']} distinct reasoning values — "
        f"the backend is splitting a single-yield blob into increments it doesn't have. "
        f"Re-baseline.\n{m['summary']}"
    )
    # The final answer MUST still arrive (reasoning blob + content present at turn end).
    assert m["n_updates"] >= 1, (
        f"SINGLE-REASONING-BLOB produced ZERO stream_updates — user gets nothing. Severe bug."
        f"\n{m['summary']}"
    )
    assert max(m["reasoning_lens"]) > 0, (
        f"SINGLE-REASONING-BLOB reasoning was lost: {m['reasoning_lens']}\n{m['summary']}"
    )
    assert max(m["content_lens"]) > 0, (
        f"SINGLE-REASONING-BLOB content was never surfaced: {m['content_lens']}\n{m['summary']}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
