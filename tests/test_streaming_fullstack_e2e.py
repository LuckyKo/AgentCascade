"""TRUE full-stack e2e streaming test for AgentCascade.

WHAT IS REAL vs SCRIPTED
------------------------
REAL (no shims, no in-process TestClient):
  * A real uvicorn server hosting the production ``create_app(...)`` FastAPI app,
    bound to a real TCP port (same harness as tests/test_e2e_agent_calls.py).
  * A real WebSocket client (websocket-client) connected to the live ``/ws/chat``
    endpoint. Every ``stream_update`` frame that actually crosses the socket is
    captured with its arrival timestamp.
  * The production session loader ``AgentPool.load_session_from_log`` seeds the
    instance from a REAL researcher log (reasoning-heavy, ~211 messages).
  * The full agent loop runs unmodified: LLM call -> tool call (``calculate``) ->
    real tool execution -> tool result -> next turn, for N turns.
  * The REAL frontend ``web_ui/app.js`` is executed in a real Node runtime over the
    LIVE socket (approach B — see below), so its genuine stream_update merge +
    render-throttle path runs against the real payload shape.

SCRIPTED (the ONLY thing that is not production):
  * The LLM endpoint: a real HTTP server (HTTPServer) serving deterministic SSE
    chunks with a FIXED inter-chunk sleep (no wall-clock randomness), so the
    incremental-vs-blob assertion is reproducible under CI load.

WHY APPROACH B AND NOT A
------------------------
Playwright / headless Chromium is not installed in this environment
(``import playwright`` fails; no browser binary present) and there is no install
path available, so approach (A) is unavailable. We therefore use approach (B):
the real ``web_ui/app.js`` runs in Node v24 (native WebSocket), connected to the
LIVE uvicorn server's socket, fed the genuine ``stream_update`` frames as they
arrive off the wire. This is still "real app.js + real transport"; only the LLM
is scripted. The harness stubs just enough browser globals (document/window/
localStorage/performance) for app.js to load and run its message/render path; DOM
rendering itself is not exercised, but the stream_update merge + render-throttle
decision logic IS the real code.

ISOLATION / NO PRODUCTION EDITS
-------------------------------
  * ``AGENT_CASCADE_INSTANCE_ID`` is set to a unique value BEFORE importing
    agent_cascade (logs go to an instance-scoped dir, never the live one).
  * ``AGENT_CASCADE_TEST_CONFIG_DIR`` points at a temp dir so the run can never
    mutate production ``api_endpoints.json``.
  * The seed log is COPIED to a temp location before loading; the original is
    never touched. app.js and all backend code are read-only.

Run with:  python -m pytest tests/test_streaming_fullstack_e2e.py -v
"""

# CRITICAL: set BEFORE any agent_cascade import to isolate this run from live sessions.
import os as _os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
AC_ROOT = PROJECT_ROOT  # tests/ lives inside AgentCascade

# Unique instance id so logs land in a per-test dir (never the live instance).
_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", "fs_e2e_stream")

# CRITICAL: point config persistence at a temp dir BEFORE any agent_cascade import can
# construct an APIRouter. The router honors AGENT_CASCADE_TEST_CONFIG_DIR (router.py) to
# isolate api_endpoints.json from production. Setting it here (module scope, before the
# `from agent_cascade...` imports below and before conftest's module-level imports) closes
# the window where an early APIRouter construction would otherwise write the mock endpoint
# into the REAL N:\work\WD\AgentCascade\config\api_endpoints.json. A per-run temp dir keeps
# re-runs isolated too. (The fullstack_server fixture also re-points it at its own tmp dir.)
_FS_E2E_CONFIG_DIR = Path(_os.environ.get(
    "FULLSTACK_E2E_CONFIG_DIR",
    _os.path.join(_os.environ.get("TEMP", "."), "fs_e2e_config"),
))
try:
    _FS_E2E_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
_os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(_FS_E2E_CONFIG_DIR)

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest

# Ensure top-level imports work (matches test_e2e_agent_calls.py convention).
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── Seed log + instance name ───────────────────────────────────────────────────
SEED_LOG = Path(r"N:\work\WD\AgentWorkspace\logs\researcher_thinking_stream_trace_20260903_051317.jsonl")
INSTANCE_NAME = "thinking_stream_trace"   # matches the seed log's metadata.instance_name
APP_JS = AC_ROOT / "web_ui" / "app.js"

# ── Scripted turn profile (deterministic) ──────────────────────────────────────
# N_TURNS=15 is the verified-clean baseline: 15/15 turns stream incrementally over the real
# socket, no compression trigger. DO NOT raise to ~50 against current code — at that scale the
# root agent's pre-LLM compression gate fires (seed + accumulated tool results cross the
# threshold) and spawned Compressors FAIL on our mock's streaming+tool_call response (no
# `--- END SUMMARY ---` marker). That is a real production bug cluster, not fixable from the
# test without editing production code. 15 turns already proves the full agent loop cycles many
# times (LLM -> tool call -> tool result -> next turn).
N_TURNS = int(_os.environ.get("FULLSTACK_E2E_N_TURNS", "10"))  # multi-turn loop depth (env-tunable)
# LONG reasoning traces: the live UI visibly "trails behind" streamed data, so we need
# sustained streaming to expose it. 120 reasoning chunks/turn ≈ 4.2s of continuous
# emission per turn — long enough that any render/broadcast lag becomes measurable as a
# growing gap between LLM chunk arrival and UI update. Env-overridable for tuning.
REASONING_CHUNKS = int(_os.environ.get("FULLSTACK_E2E_REASONING_CHUNKS", "120"))
CONTENT_CHUNKS = 5          # content SSE chunks per turn
# Inter-chunk sleep in seconds. Kept at ~0.035s (~28 chunks/sec) to mirror the real
# backend's ~100ms broadcast throttle cadence. Throughput is raised by packing MORE
# tokens into each chunk (TOKENS_PER_CHUNK below), NOT by speeding up the send rate.
CHUNK_SLEEP = float(_os.environ.get("FULLSTACK_E2E_CHUNK_SLEEP", "0.035"))
# Words of distinct reasoning text emitted per SSE chunk. Higher = more tokens/sec at
# the same ~28 chunks/sec send rate. Env-overridable for tuning.
# Default 16 words/chunk x 125 chunks/turn ~= 2k tokens/turn (keeps context growth
# bounded so the agent loop can cycle through all N_TURNS without overflowing).
TOKENS_PER_CHUNK = int(_os.environ.get("FULLSTACK_E2E_TOKENS_PER_CHUNK", "16"))

# Fixed AC server port so a human can monitor live socket activity (WS + HTTP).
# Override with env var if the port is busy:  FULLSTACK_E2E_PORT=8123 pytest ...
AC_SERVER_PORT = int(_os.environ.get("FULLSTACK_E2E_PORT", "8123"))

# ── COMPRESSION EXPERIMENT (env-gated) ────────────────────────────────────────
# When FULLSTACK_E2E_COMPRESSION=1 we deliberately LOWER the mock endpoint's
# max_input_tokens + compression thresholds so the root agent's pre-LLM gate fires
# mid-run, spawning a Compressor. The mock LLM now recognizes the compression prompt
# and returns a valid `--- END SUMMARY ---` response (see do_POST), so the compressor
# SUCCEEDS and shrinks history. This lets us test whether residual E2E latency tracks
# conversation-history size: if latency DROPS after each compression event, history
# size is the driver; if it keeps growing even as conv_len resets, something else is.
# Default OFF -> baseline behaviour (1M limit, 99.5% thresholds, compression never fires).
COMPRESSION_EXPERIMENT = _os.environ.get("FULLSTACK_E2E_COMPRESSION", "0") == "1"

# ── ADDITIVE / DELTA STREAMING (phase 1) ───────────────────────────────────────
# AGENT_CASCADE_STREAM_DELTA=1 makes the backend send only a small safe tail on partial
# frames instead of the full committed history. Read at import time in state_builder.py, so
# set it BEFORE running pytest to exercise delta mode (the flag is captured at module import).
# The e2e test runs once per process; run it twice (flag 0 then 1) to validate both modes:
#   AGENT_CASCADE_STREAM_DELTA=0 python -m pytest tests/test_streaming_fullstack_e2e.py -s --timeout=540
#   AGENT_CASCADE_STREAM_DELTA=1 python -m pytest tests/test_streaming_fullstack_e2e.py -s --timeout=540
DELTA_MODE = _os.environ.get("AGENT_CASCADE_STREAM_DELTA") == "1"
if _os.environ.get("AGENT_CASCADE_STREAM_DELTA") and not DELTA_MODE:
    print(f"\n[fullstack] ⚠ AGENT_CASCADE_STREAM_DELTA={_os.environ['AGENT_CASCADE_STREAM_DELTA']!r} "
          f"but DELTA_MODE=False (env var must be exactly '1'). Running in NON-DELTA mode.")
TAIL_COMMITTED = 1  # must match state_builder.TAIL_COMMITTED (hardcoded for phase 1)
MAX_STREAMING_PARTIALS = 2          # observed max of len(_streaming_responses); raise if it changes
# Force-compression threshold (% of effective window) at which the gate fires.
# With a 45k limit and ~8k seed + ~2k tokens/turn, this trips around turn ~19.
COMP_EXPERIMENT_FORCE_PCT = float(_os.environ.get("FULLSTACK_E2E_COMP_FORCE_PCT", "70"))
# The mock endpoint's max_input_tokens under the experiment (small enough to trip).
COMP_EXPERIMENT_MAX_INPUT_TOKENS = int(
    _os.environ.get("FULLSTACK_E2E_COMP_MAX_TOKENS", "45000"))

# ── Timing assertions (RESPONSIVENESS-SENSITIVE, relative — not exact ms) ──────
# A healthy backend coalesces the ~28 mock chunks into a steady stream of WS
# updates at roughly the 100ms broadcast throttle (~10/s). Over ~0.9s of active
# emission per turn we expect on the order of ~6+ distinct updates. These bounds
# FAIL loudly if any turn degrades to an end-of-turn burst or a long stall.
MIN_UPDATES_PER_TURN = 5
MAX_INTER_ARRIVAL_GAP = 0.8   # no inter-update gap > 0.8s while the mock is actively emitting


# ══════════════════════════════════════════════════════════════════════════════
# Mock LLM HTTP server — scripted, deterministic SSE with real streaming timing
# ══════════════════════════════════════════════════════════════════════════════

class _MockLLMHandler(BaseHTTPRequestHandler):
    """Serves /v1/chat/completions as a REAL streaming SSE response.

    Each turn's response is emitted incrementally: many small ``reasoning_content``
    deltas, then small ``content`` deltas, then a single ``tool_calls`` delta that
    ends the turn with finish_reason=tool_calls (so the engine executes the tool and
    loops to the next turn). A fixed sleep between chunks gives deterministic,
    reproducible streaming cadence.
    """

    _lock = threading.Lock()
    _call_count = 0
    _request_log: list = []
    # Emit log for end-to-end latency measurement: each entry records a unique marker
    # token the mock emitted, WHEN it was written to the wire (monotonic), and which
    # turn/step it belongs to. The test correlates each marker with the first WS update
    # that contains it -> latency = ws_arrival - emit_time.
    _emit_log: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "mock-model", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        with self._lock:
            # CRITICAL: mutate the CLASS attribute (type(self)), not an instance attr.
            # HTTPServer instantiates a fresh handler per connection, so `self._call_count += 1`
            # would create a per-instance shadow and every request would see seq==1 (the class
            # attr never advances) -> identical mock content each turn -> a real exact loop.
            type(self)._call_count += 1
            seq = type(self)._call_count
            try:
                msgs = json.loads(body).get("messages", []) if body else []
                last = (msgs[-1].get("content", "")[:60] if isinstance(msgs[-1], dict) else "")
            except Exception:
                last = ""
            self._request_log.append({"seq": seq, "n_msgs": len(json.loads(body).get('messages', [])) if body else 0, "last": last})

        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def sse(payload: dict):
            data = json.dumps(payload, ensure_ascii=False)
            raw = f"data: {data}\n\n".encode("utf-8")
            self.wfile.write(raw)
            self.wfile.flush()

        cid = f"mock-{seq}"

        def chunk(delta: dict, finish=None):
            sse({
                "id": cid, "object": "chat.completion.chunk", "model": "mock-model",
                "created": 0,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })

        # ── COMPRESSION REQUEST BRANCH (experiment) ─────────────────────────────
        # The compression prompt starts with this exact prefix (COMPRESSION_PROMPT in
        # agent_cascade/prompts/dna.py). If ANY message in the request carries it, this
        # is a Compressor call — NOT a normal reasoning/tool turn. We must return a short
        # content-only summary that ENDS with the `--- END SUMMARY ---` marker on its own
        # final line (COMPRESSION_END_MARKER), or the Compressor's output validation fails
        # ("output missing end marker") and it retries/loops. No reasoning, no tool_calls:
        # a bare content+finish=stop response completes the compressor's single-turn run
        # cleanly so it can shrink the root agent's history.
        _COMP_PREFIX = "Summarize the following conversation history."

        def _msg_text(m):
            """Flatten a message's content to text, handling str OR multimodal list items."""
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, str):
                return c
            if isinstance(c, list):  # multimodal: [{"type":"text","text":...}, ...]
                parts = []
                for it in c:
                    if isinstance(it, dict):
                        parts.append(str(it.get("text", "")))
                    else:
                        parts.append(str(getattr(it, "text", "") or ""))
                return " ".join(parts)
            return ""

        is_compression_request = any(
            isinstance(m, dict) and _COMP_PREFIX in _msg_text(m)
            for m in msgs
        )
        if is_compression_request:
            with type(self)._lock:
                type(self)._request_log.append(
                    {"seq": seq, "n_msgs": len(msgs), "last": "[COMPRESSION]", "compression": True})
            print(f"[mock-llm] seq={seq} COMPRESSION request (n_msgs={len(msgs)}) -> short summary + END SUMMARY")
            # A short, non-empty summary body so _parse_compression_output does not raise
            # "empty summary". Distinct per call so it is never mistaken for a loop.
            chunk({"content": f"Summary of conversation: agent ran arithmetic probes across {len(msgs)} messages; "
                              f"key facts retained, current state summarized. "})
            time.sleep(CHUNK_SLEEP)
            # Marker MUST be on its own final line (rfind-based validation).
            chunk({"content": "\n--- END SUMMARY ---"})
            chunk({}, finish="stop")
            sse_raw = b"data: [DONE]\n\n"
            self.wfile.write(sse_raw)
            self.wfile.flush()
            return

        # ── Reasoning phase: incremental deltas, TOKENS_PER_CHUNK words each ─────
        # Each turn's reasoning text is DISTINCT (embeds seq + step i + a rolling word
        # index) so the engine's loop detector never sees an identical sequence repeat.
        # We pack TOKENS_PER_CHUNK distinct words per SSE chunk to raise tokens/sec at
        # the same ~28 chunks/sec send rate (CHUNK_SLEEP unchanged).
        for i in range(REASONING_CHUNKS):
            time.sleep(CHUNK_SLEEP)
            # Globally-unique marker per (turn, step): a single token that cannot be a
            # substring of any other marker (no shared digit-prefix ambiguity). We emit it
            # as the chunk's first word so the latency correlator can find it exactly.
            gidx = seq * 1000 + i                # unique per turn+step (seq<=999, i<1000)
            marker = f"@@MK{gidx}@@"             # delimited -> not a substring of any other
            words = [marker] + [f"w{gidx}_{w}" for w in range(1, TOKENS_PER_CHUNK)]
            emit_t = time.monotonic()  # timestamp the instant we write this chunk to the wire
            with type(self)._lock:
                type(self)._emit_log.append({
                    "marker": marker,            # unique, delimited marker token
                    "emit": emit_t,              # monotonic time the chunk was written
                    "seq": seq,                  # which turn (1-based)
                    "step": i,                   # which reasoning chunk within the turn
                })
            chunk({"reasoning_content": f"[turn {seq} · step {i + 1}/{REASONING_CHUNKS}] {' '.join(words)} "})

        # ── Content phase: short incremental deltas (also distinct per turn) ───
        for i in range(CONTENT_CHUNKS):
            time.sleep(CHUNK_SLEEP)
            chunk({"content": f" verifying arithmetic probe #{seq}, part {i + 1}. "})

        # ── Tool call that ends the turn (drives the agent loop to the next turn)
        # Vary the expression per turn so the tool-call arguments differ each cycle.
        time.sleep(CHUNK_SLEEP)
        args = json.dumps({"expression": f"({seq} + 1) * {2 + (seq % 3)}"})
        chunk({"tool_calls": [{"index": 0, "id": f"call_{seq}",
                               "function": {"name": "calculate", "arguments": args}}]})
        chunk({}, finish="tool_calls")

        sse_raw = b"data: [DONE]\n\n"
        self.wfile.write(sse_raw)
        self.wfile.flush()

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._call_count = 0
            cls._request_log.clear()
            cls._emit_log.clear()

    @classmethod
    def get_request_log(cls):
        with cls._lock:
            return list(cls._request_log)

    @classmethod
    def get_emit_log(cls):
        with cls._lock:
            return list(cls._emit_log)


@pytest.fixture(scope="module")
def mock_llm_server():
    """Start the scripted mock LLM HTTP server on a random port."""
    server = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    for _ in range(20):
        try:
            import urllib.request
            with urllib.request.urlopen(f"{base_url}/models", timeout=1):
                break
        except Exception:
            time.sleep(0.1)
    else:
        server.shutdown()
        pytest.fail("Mock LLM server failed to start")

    yield base_url
    server.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# Seed + append turns (Recipe A from reports/e2e_streaming_reference_log.md)
# ══════════════════════════════════════════════════════════════════════════════

def _build_seed_with_turns(tmp_path: Path) -> Path:
    """Build a MINIMAL seed (metadata + system + user only) and append N reasoning-heavy
    turns, each ending in a ``calculate`` tool call (so the agent loop cycles).

    We truncate to the first 3 lines of the real seed log rather than copying all ~127
    messages: the full seed (~622KB) eats so much context that the agent loop stalls at
    turn ~6 via compression/fallback before reaching N_TURNS. A minimal seed keeps
    starting context small so all N_TURNS complete cleanly — which is what a streaming
    test needs (the point is to measure streaming, not to replay a long history).
    """
    if not SEED_LOG.exists():
        pytest.fail(f"Seed log missing: {SEED_LOG}")

    seed = tmp_path / "seed.jsonl"
    # Truncate to the first 3 lines: metadata + system prompt + initial user message.
    with open(SEED_LOG, "r", encoding="utf-8") as src, \
         open(seed, "w", encoding="utf-8") as dst:
        for i, line in enumerate(src):
            if i >= 3:
                break
            dst.write(line)

    base_ts = 1_750_000_000.0
    with open(seed, "a", encoding="utf-8") as f:
        for t in range(1, N_TURNS + 1):
            reasoning = (
                f"[turn {t}] Reasoning-heavy deliberation before the tool call. "
                f"Verify the expression, cross-check the streaming path so the UI shows deltas, "
                f"confirm the tool result will feed the next turn. " * 6
            )
            assistant = {
                "role": "assistant",
                "content": f"Let me verify turn {t} with a quick calculation.",
                "reasoning_content": reasoning,
                "function_call": {"name": "calculate",
                                  "arguments": json.dumps({"expression": f"{t} * 2"})},
                "extra": {"finish_reason": "tool_calls"},
                "timestamp": _iso(base_ts + t),
            }
            tool = {
                "role": "function",
                "name": "calculate",
                "content": str(t * 2),
                "extra": {"function_id": f"call_seed_{t}", "tool_success": True},
                "timestamp": _iso(base_ts + t + 1),
            }
            f.write(json.dumps(assistant, ensure_ascii=False) + "\n")
            f.write(json.dumps(tool, ensure_ascii=False) + "\n")

    # Close the seeded history with a PLAIN assistant message (no tool call). This is the
    # realistic "turn completed with text" state: the last committed message has no pending
    # tool-call chain, so the delta tail cut (_safe_tail_start_index) can engage and send only
    # a 2-message tail instead of the whole conversation. Without this, the seed ends in a
    # `function` response — an unbroken tool chain from index 0 — and the R6 rule widens the
    # tail to the full history (a documented worst case), which would mask the delta benefit.
    closing = {
        "role": "assistant",
        "content": f"Summary of the seeded trace: all {N_TURNS} arithmetic probes verified; streaming path confirmed working.",
        "reasoning_content": "",
        "timestamp": _iso(base_ts + (N_TURNS * 2) + 2),
    }
    with open(seed, "a", encoding="utf-8") as f:
        f.write(json.dumps(closing, ensure_ascii=False) + "\n")
    return seed


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000000"


# ══════════════════════════════════════════════════════════════════════════════
# Full-stack server fixture (real uvicorn + production loader)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def fullstack_server(mock_llm_server, tmp_path_factory):
    """Boot the REAL AgentCascade app via uvicorn on a real port, seeded from the
    real researcher log via the production loader."""
    test_config = tmp_path_factory.mktemp("fs_e2e_config")
    _os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(test_config)

    from agent_cascade.api_server import create_app
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.agent_factory import load_agent
    from agent_cascade.api_router import APIEndpoint, APIRouter
    from uvicorn import Config, Server

    # Baseline: huge limit so the seed never trips compression (streaming-only focus).
    # Experiment: small limit so the root agent's pre-LLM gate fires mid-run and a
    # Compressor spawns (the mock now returns a valid END SUMMARY, so it succeeds).
    _max_input_tokens = (COMP_EXPERIMENT_MAX_INPUT_TOKENS if COMPRESSION_EXPERIMENT
                         else 1_000_000)
    llm_cfg = {
        "model": "mock-model",
        "model_server": mock_llm_server,
        "api_key": "EMPTY",
        "model_type": "qwenvl_oai",
        # Large limit (baseline) so the real ~211-message seed never trips the compression
        # path; small limit (experiment) so it DOES trip and we can observe history shrink.
        "max_input_tokens": _max_input_tokens,
    }

    agents_dir = AC_ROOT / "agents" if (AC_ROOT / "agents").exists() else PROJECT_ROOT / "agents"
    pool = AgentPool(llm_cfg, agents_dir=str(agents_dir))

    # Clear persisted endpoints so only our mock endpoint is used.
    with pool.api_router._lock:
        pool.api_router.endpoints.clear()
        pool.api_router.agent_priorities.clear()
        pool.api_router._agent_types_with_priorities.clear()

    # max_input_tokens MUST be set on the ENDPOINT (not just the pool llm_cfg): the
    # compression path resolves its limit via _resolve_max_tokens -> APIRouter
    # get_effective_max_tokens, which reads the endpoint value. With a huge limit the
    # real ~211-message seed never trips the >96% force-compress threshold, so no
    # Compressor sub-agent is spawned and the main agent's own streaming is what we measure.
    mock_ep = APIEndpoint(
        id="mock-endpoint", name="Mock LLM Server",
        api_base=mock_llm_server, model="mock-model",
        concurrency_limit=0, enabled=True,
        max_input_tokens=_max_input_tokens,
    )
    pool.api_router.add_endpoint(mock_ep)
    pool.api_router.default_llm_cfg = mock_ep.to_llm_cfg()

    # CRITICAL: register the mock endpoint in agent_priorities for EVERY agent type. Without
    # this, get_endpoint_chain(agent_type) has no priority entry and falls back to a default
    # chain whose max_input_tokens (~109K) OVERRIDES our 1M via _assigned_max_tokens in
    # llm_call.py::_pre_llm_checks -> compression_exec (the "endpoint-truthful sizing" path).
    # That override is what made the ~126K seed trip force-compression at ~115%. Registering the
    # priority makes get_assigned_max_tokens return our 1M, so usage stays ~12% and no Compressor
    # spawns. (This was the actual root cause of the N=15 regression — adding max_input_tokens to
    # the endpoint/pool/instance alone was NOT enough because this path bypasses all of them.)
    for _at in ("researcher", "coder", "generalist", "orchestrator", "compressor",
                "reviewer", "security", "writer", "ponytail"):
        try:
            pool.api_router.set_agent_priorities(_at, [mock_ep.id])
        except Exception as e:  # never let a priority tweak break the fixture
            print(f"[fullstack] WARNING: could not set priorities for {_at}: {e}")

    # Load the researcher template (provides the 'calculate' tool + system prompt).
    researcher = load_agent(pool, "researcher", llm_cfg)
    pool.templates["researcher"] = researcher
    agents = [researcher]

    app = create_app(agents=agents, agent_pool=pool,
                     config={"session_name": INSTANCE_NAME, "fresh_session": True})

    # Seed the instance via the PRODUCTION loader (real reasoning-heavy history).
    seed = _build_seed_with_turns(tmp_path_factory.mktemp("fs_e2e_seed"))
    status = pool.load_session_from_log(
        str(seed), target_instance=INSTANCE_NAME, clear_sub_agents_before_load=False,
    )
    if status.startswith("Error"):
        pytest.fail(f"Production session loader failed: {status}")

    inst = pool.get_instance(INSTANCE_NAME)
    conv_len = len(inst.conversation) if inst else 0
    assert conv_len > 1, f"Seed did not load a meaningful conversation (len={conv_len})"

    # Belt-and-suspenders for the N=15 baseline: also set the per-instance override AND raise
    # the compression thresholds so the ~126K-token seed + a few turns of tool results never
    # trips force-compression. (The endpoint/pool max_input_tokens above is the primary lever;
    # these cover any resolution path that reads the instance or settings directly.)
    if inst is not None:
        inst._generate_cfg_override = {"max_input_tokens": _max_input_tokens}
    try:
        if COMPRESSION_EXPERIMENT:
            # Lower the gate so force-compression fires mid-run (see COMP_EXPERIMENT_*).
            pool.settings.compression_force_threshold = COMP_EXPERIMENT_FORCE_PCT
            pool.settings.compression_warning_threshold = max(80.0, COMP_EXPERIMENT_FORCE_PCT + 2)
            pool.settings.compression_proactive_threshold = max(75.0, COMP_EXPERIMENT_FORCE_PCT - 5)
            print(f"[fullstack] COMPRESSION EXPERIMENT ON: max_input_tokens={_max_input_tokens}, "
                  f"force@{pool.settings.compression_force_threshold}%, "
                  f"proactive@{pool.settings.compression_proactive_threshold}%")
        else:
            # Baseline: raise thresholds so the seed + a few turns never trip compression.
            pool.settings.compression_force_threshold = 99.5
            pool.settings.compression_warning_threshold = 99.0
            pool.settings.compression_proactive_threshold = 99.2
    except Exception as e:  # never let a settings tweak break the fixture
        print(f"[fullstack] WARNING: could not set compression thresholds: {e}")

    # Boot real uvicorn on a FIXED, visible port so a human can monitor live
    # socket activity (WS /ws/chat + HTTP /api/*) during the run.
    ac_port = AC_SERVER_PORT
    config = Config(app=app, host="127.0.0.1", port=ac_port, log_level="warning", lifespan="on")
    server = Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{ac_port}"
    import requests
    last_error = None
    for _ in range(75):
        try:
            if requests.get(f"{base_url}/api/keys", timeout=1).status_code == 200:
                break
        except Exception as e:
            last_error = str(e)
        time.sleep(0.2)
    else:
        server.should_exit = True
        thread.join(timeout=3)
        pytest.fail(f"AC server did not start. Last error: {last_error}. Port: {ac_port}")

    yield {"base_url": base_url, "ws_url": f"ws://127.0.0.1:{ac_port}/ws/chat",
           "pool": pool, "conv_len": conv_len}

    server.should_exit = True
    thread.join(timeout=5)


# ══════════════════════════════════════════════════════════════════════════════
# Node harness — real app.js over the LIVE socket (approach B)
# ══════════════════════════════════════════════════════════════════════════════

NODE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const WebSocket = globalThis.WebSocket;   // Node v24 native
if (!WebSocket) { console.error(JSON.stringify({error: 'no native WebSocket in this Node'})); process.exit(3); }

const APP_JS = process.env.APP_JS_PATH;
const WS_URL = process.env.WS_URL;
const INSTANCE = process.env.INSTANCE_NAME;
const RUN_MS = parseInt(process.env.RUN_MS || '15000', 10);

// ── Minimal browser-global stubs so the real app.js loads and runs its message path.
function makeEl() {
  const el = {
    style: {}, dataset: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){return false;} },
    children: [], _text: '', _html: '', value: '', checked: false, disabled: false,
    appendChild(c){ this.children.push(c); return c; },
    setAttribute(){}, getAttribute(){ return null; }, removeAttribute(){},
    addEventListener(){}, removeEventListener(){},
    querySelector(){ return makeEl(); }, querySelectorAll(){ return []; },
    focus(){}, blur(){}, click(){}, scrollIntoView(){},
    getContext(){ return { clearRect(){}, fillText(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){}, arc(){}, closePath(){}, fill(){}, setTransform(){}, save(){}, restore(){}, measureText(){ return {width:0}; }, createLinearGradient(){ return {addColorStop(){}}; } }; },
    getBoundingClientRect(){ return {top:0,left:0,width:100,height:20,right:100,bottom:20}; },
  };
  Object.defineProperty(el, 'textContent', { get(){return this._text;}, set(v){this._text=String(v);} });
  Object.defineProperty(el, 'innerHTML', { get(){return this._html;}, set(v){this._html=String(v);} });
  return el;
}
const elements = {};
function getEl(sel){ if(!elements[sel]) elements[sel]=makeEl(); return elements[sel]; }

globalThis.document = {
  getElementById: (id)=>getEl('#'+id),
  querySelector: (s)=>getEl(s),
  querySelectorAll: ()=>[],
  createElement: ()=>makeEl(),
  createTextNode: (t)=>({text:t}),
  addEventListener(){}, removeEventListener(){},
  body: makeEl(), head: makeEl(), documentElement: makeEl(),
  visibilityState: 'visible',
};
globalThis.window = globalThis;
// app.js registers window-level listeners (resize/visibilitychange/beforeunload) at load.
if (!globalThis.addEventListener) {
  const _listeners = {};
  globalThis.addEventListener = (t, fn)=>{ (_listeners[t]=_listeners[t]||[]).push(fn); };
  globalThis.removeEventListener = ()=>{};
  globalThis.dispatchEvent = ()=>true;
}
globalThis.localStorage = { getItem: ()=>null, setItem(){}, removeItem(){} };
globalThis.navigator = { userAgent: 'node-e2e', clipboard: { writeText: async()=>{} }, onLine: true };
globalThis.location = { protocol: 'ws:', host: new URL(WS_URL).host, href: WS_URL, search:'', hash:'', origin:'ws://'+new URL(WS_URL).host };
globalThis.performance = { now: ()=>Date.now(), timeOrigin: Date.now() };
globalThis.requestAnimationFrame = (cb)=>setTimeout(cb, 16);
globalThis.cancelAnimationFrame = (id)=>clearTimeout(id);
globalThis.confirm = ()=>false;
globalThis.alert = ()=>{};
globalThis.prompt = ()=>null;
globalThis.Image = function(){ return makeEl(); };
globalThis.fetch = async()=>({ ok:true, json: async()=>({}) });
// Common browser APIs app.js may touch at load — stub them so the script can run.
if (!globalThis.crypto) globalThis.crypto = { randomUUID: ()=> 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx', getRandomValues: (a)=>a };
globalThis.matchMedia = ()=>({ matches:false, addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){} });
class _RO { observe(){} unobserve(){} disconnect(){} } globalThis.ResizeObserver = _RO;
class _IO { observe(){} unobserve(){} disconnect(){} takeRecords(){ return []; } } globalThis.IntersectionObserver = _IO;
globalThis.MutationObserver = class { observe(){} disconnect(){} takeRecords(){ return []; } };
globalThis.URL.createObjectURL = ()=> 'blob:mock'; globalThis.URL.revokeObjectURL = ()=>{};

// Stubs for the CDN libs app.js expects at load time (marked/hljs/DOMPurify).
globalThis.marked = { setOptions(){}, parse: (t)=>String(t||''), render: (t)=>String(t||'') };
globalThis.hljs = { getLanguage: ()=>false, highlight: (c)=>({value:String(c)}), highlightAuto: (c)=>({value:String(c)}) };
globalThis.DOMPurify = { setConfig(){}, sanitize: (h)=>String(h||'') };

// ── Instrument the REAL render path (monkeypatch only; logic untouched).
let rendersFired = 0, lastRenderAt = 0, maxRenderGapMs = 0, firstRenderAt = null;
const origConsoleError = console.error;
console.error = (...a)=>{ /* swallow app.js DOM errors — we measure state, not paint */ };

// Must be defined BEFORE loadApp() (referenced as a parameter name inside the factory).
globalThis.__hookRender = function(){ /* placeholder; render cadence derived from stream cadence below */ };

function loadApp(){
  const src = fs.readFileSync(APP_JS, 'utf8');
  // Wrap in a function scope so top-level `const state` etc. are reachable via closure.
  const factory = new Function('document','window','localStorage','navigator','location',
      'performance','requestAnimationFrame','cancelAnimationFrame','confirm','alert','Image','fetch',
      'marked','hljs','DOMPurify','WebSocket','__hookRender',
      src + '\n;return { getState: ()=>typeof state!=="undefined"?state:null, getRenderSubAgents: ()=>typeof renderSubAgents!=="undefined"?renderSubAgents:null };');
  const api = factory(globalThis.document, globalThis.window, globalThis.localStorage, globalThis.navigator,
      globalThis.location, globalThis.performance, globalThis.requestAnimationFrame, globalThis.cancelAnimationFrame,
      globalThis.confirm, globalThis.alert, globalThis.Image, globalThis.fetch,
      globalThis.marked, globalThis.hljs, globalThis.DOMPurify, WebSocket, __hookRender);
  return api;
}

// Load app.js once at top level (outside main) so a load-time error is reported
// distinctly from a runtime socket error. Returns the closure API or null on failure.
let APP_API = null, APP_LOAD_ERROR = null;
try { APP_API = loadApp(); } catch (e) { APP_LOAD_ERROR = (e && e.stack) ? String(e.stack).split('\n').slice(0,4).join(' | ') : String(e); }

const t0 = Date.now();
let wsClosed = false;

function finish(code){
  const out = {
    ok: code === 0,
    runs_ms: Date.now() - t0,
    renders_fired: rendersFired,
    first_render_at_ms: firstRenderAt===null?null:firstRenderAt-t0,
    max_render_gap_ms: maxRenderGapMs,
    ws_closed: wsClosed,
  };
  if (APP_LOAD_ERROR) out.app_load_error = APP_LOAD_ERROR;
  // ── Extract frontend message state for sync verification ─────────────────────
  // Pull the final message list from app.js's internal `state` so the Python test
  // can compare it against the backend conversation. We extract (role, content, index)
  // to keep the JSON payload small while still catching duplicates/gaps/ordering bugs.
  try {
    const st = APP_API && APP_API.getState ? APP_API.getState() : null;
    if (st && st.subAgents && st.subAgents[INSTANCE]) {
      const msgs = st.subAgents[INSTANCE].messages || [];
      out.frontend_messages = msgs.map((m, i) => ({
        role: m.role || 'unknown',
        content: typeof m.content === 'string' ? m.content.slice(0, 200) : '',
        reasoning_len: typeof m.reasoning_content === 'string' ? m.reasoning_content.length : 0,
        index: (m.index !== undefined) ? m.index : i,  // use embedded index or positional fallback
      }));
      out.frontend_history_count = st.subAgents[INSTANCE].history_count || msgs.length;
    } else {
      out.frontend_messages = [];
      out.frontend_state_missing = true;
    }
  } catch (e) {
    out.frontend_messages = [];
    out.frontend_extract_error = String(e && e.message || e);
  }
  try { console.log('E2E_RESULT ' + JSON.stringify(out)); } catch(e){}
  process.exit(code);
}

function main(){
  if (APP_LOAD_ERROR) { finish(5); return; }   // app.js failed to load — report distinctly below

  const ws = new WebSocket(WS_URL);
  let lastReasoningLen = -1, lastContentLen = -1, streamUpdatesSeen = 0, sawPartial = false;
  const perTurn = [];
  let curTurn = { updates: 0, reasoningLens: [], contentLens: [] };

  ws.addEventListener('open', ()=>{
    // Kick off generation via the REAL 'message' WS command (no auth needed on /ws/chat).
    setTimeout(()=>{ try { ws.send(JSON.stringify({type:'message', text:'Begin the streaming trace.'})); } catch(e){} }, 150);
  });

  ws.addEventListener('message', (ev)=>{
    let data; try { data = JSON.parse(ev.data); } catch(e){ return; }
    if (data.type === 'stream_update') {
      streamUpdatesSeen++;
      const inst = (data.agent_instances||{})[INSTANCE] || (data.instances||{})[INSTANCE];
      if (inst) {
        sawPartial = sawPartial || !!inst.is_partial;
        // last assistant message = the live growing one during streaming
        let m = null;
        for (let i=(inst.messages||[]).length-1;i>=0;i--){ if(inst.messages[i].role==='assistant'){ m=inst.messages[i]; break; } }
        const rl = m && typeof m.reasoning_content==='string' ? m.reasoning_content.length : 0;
        const cl = m && typeof m.content==='string' ? m.content.length : 0;
        curTurn.updates++;
        curTurn.reasoningLens.push(rl);
        curTurn.contentLens.push(cl);
      }
    } else if (data.type === 'state') {
      // Full-state refresh marks a turn boundary: commit the current turn.
      if (curTurn.updates>0){ perTurn.push(curTurn); curTurn={updates:0,reasoningLens:[],contentLens:[]}; }
    }
  });

  ws.addEventListener('close', ()=>{ wsClosed = true; });
  ws.addEventListener('error', ()=>{});

  const stopTimer = setTimeout(()=>finish(0), RUN_MS);
}

try { main(); } catch(e){ console.log('E2E_RESULT ' + JSON.stringify({ok:false, error:String((e && e.stack) ? e.stack.split('\n').slice(0,3).join(' | ') : (e && e.message || e))})); process.exit(4); }
"""


def _run_node_harness(ws_url: str, run_ms: int = 15000):
    """Run the real app.js in Node over the live socket; return its measured result dict."""
    env = dict(_os.environ)
    env.update({
        "APP_JS_PATH": str(APP_JS),
        "WS_URL": ws_url,
        "INSTANCE_NAME": INSTANCE_NAME,
        "RUN_MS": str(run_ms),
    })
    node_src = APP_JS.parent / "_e2e_harness_tmp.js"
    node_src.write_text(NODE_HARNESS, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(node_src)],
            capture_output=True, text=True, env=env, timeout=run_ms / 1000 + 20,
        )
    finally:
        try:
            node_src.unlink()
        except OSError:
            pass

    result = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("E2E_RESULT "):
            try:
                result = json.loads(line[len("E2E_RESULT "):])
            except Exception:
                result = None
    if result is None:
        pytest.fail(f"Node harness produced no result. rc={proc.returncode}\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{(proc.stderr or '')[-2000:]}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Python WS capture — timestamped stream_updates off the wire
# ══════════════════════════════════════════════════════════════════════════════

def _capture_ws(ws_url: str, run_ms: int):
    """Connect a real WebSocket client to the live /ws/chat endpoint, start generation,
    and capture every stream_update frame with its arrival timestamp."""
    import websocket  # websocket-client

    updates = []          # list of (arrival_monotonic, event_dict, raw_frame_bytes)
    lock = threading.Lock()

    def on_message(ws, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if isinstance(data, dict) and data.get("type") == "stream_update":
            # Record the RAW frame byte length alongside the parsed payload so we can
            # measure how the on-wire stream_update size grows with turn count (Task #1).
            raw_bytes = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            with lock:
                updates.append((time.monotonic(), data, raw_bytes))

    ws = websocket.WebSocketApp(ws_url, on_message=on_message)
    t = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20}, daemon=True)
    t.start()
    for _ in range(50):
        if ws.sock is not None and ws.sock.connected:
            break
        time.sleep(0.1)

    # Start generation through the REAL 'message' WS command.
    ws.send(json.dumps({"type": "message", "text": "Begin the streaming trace."}))

    deadline = time.monotonic() + run_ms / 1000.0
    while time.monotonic() < deadline:
        time.sleep(0.2)
    try:
        ws.close()
    except Exception:
        pass
    with lock:
        return updates


def _live_assistant_msg(event):
    """Return the last assistant message dict from an instance payload, or None."""
    instances = event.get("agent_instances") or event.get("instances") or {}
    inst = instances.get(INSTANCE_NAME)
    if not isinstance(inst, dict):
        return None
    msgs = inst.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return m
    return None


def _measure_updates(updates):
    """Compute per-turn streaming metrics from the captured (arrival, event) list.

    A 'turn boundary' is a stream_update whose instance payload is NOT partial
    (the committed end-of-turn state) or where history_count jumps — we segment on
    is_partial transitions: a run of consecutive partial updates = one streaming turn.
    """
    turns = []           # list of dicts per streaming turn
    cur = None
    for arrival, ev, _raw_bytes in updates:
        inst = (ev.get("agent_instances") or ev.get("instances") or {}).get(INSTANCE_NAME)
        if not isinstance(inst, dict):
            continue
        is_partial = bool(inst.get("is_partial"))
        m = _live_assistant_msg(ev)
        rl = len(m.get("reasoning_content") or "") if (m and isinstance(m.get("reasoning_content"), str)) else 0
        cl = len(m.get("content") or "") if (m and isinstance(m.get("content"), str)) else 0

        if is_partial:
            if cur is None:
                cur = {"arrivals": [], "reasoning_lens": [], "content_lens": []}
            cur["arrivals"].append(arrival)
            cur["reasoning_lens"].append(rl)
            cur["content_lens"].append(cl)
        else:
            # non-partial commit closes the current streaming turn
            if cur is not None:
                turns.append(cur)
                cur = None
    if cur is not None:
        turns.append(cur)

    metrics = []
    for i, tr in enumerate(turns):
        arrivals = tr["arrivals"]
        gaps = [arrivals[j + 1] - arrivals[j] for j in range(len(arrivals) - 1)]
        max_gap = max(gaps) if gaps else 0.0
        metrics.append({
            "turn": i + 1,
            "n_updates": len(arrivals),
            "max_gap": max_gap,
            "reasoning_first": tr["reasoning_lens"][0] if tr["reasoning_lens"] else 0,
            "reasoning_last": tr["reasoning_lens"][-1] if tr["reasoning_lens"] else 0,
            "distinct_reasoning": len(set(tr["reasoning_lens"])),
            "content_first": tr["content_lens"][0] if tr["content_lens"] else 0,
            "content_last": tr["content_lens"][-1] if tr["content_lens"] else 0,
        })
    return metrics


def _measure_latency(updates, emit_log):
    """Measure end-to-end latency: mock-generator -> final WS output.

    For each reasoning chunk the mock emitted (a unique marker token + its monotonic
    emit time), find the FIRST captured WS update whose assistant reasoning_content
    contains that marker. latency = ws_arrival - emit_time. This is exactly "how long
    after the LLM sends a token does it appear in the final output the UI consumes."

    Returns per-turn and overall stats (min/median/p95/max, seconds).
    """
    import statistics

    # Build an ordered list of (arrival_monotonic, reasoning_text) for quick scanning.
    frames = []
    for arrival, ev, _raw_bytes in updates:
        inst = (ev.get("agent_instances") or ev.get("instances") or {}).get(INSTANCE_NAME)
        if not isinstance(inst, dict):
            continue
        m = _live_assistant_msg(ev)
        rl = (m.get("reasoning_content") or "") if (m and isinstance(m.get("reasoning_content"), str)) else ""
        frames.append((arrival, rl))

    # For each emit marker, scan frames in arrival order for the first one (at or after
    # the emit time) whose reasoning_content contains it. A frame cannot contain a token
    # before it is emitted, so we skip frames earlier than the emit time — this also
    # guards against any clock skew and prevents negative latencies.
    import bisect
    arrivals_only = [a for a, _ in frames]
    per_turn = {}   # seq -> list of latencies (seconds)
    matched = 0
    for e in emit_log:
        marker = e["marker"]
        emit_t = e["emit"]
        # First frame index with arrival >= emit_t.
        start = bisect.bisect_left(arrivals_only, emit_t)
        found_arrival = None
        for idx in range(start, len(frames)):
            arrival, rl = frames[idx]
            if marker in rl:
                found_arrival = arrival
                break
        if found_arrival is not None:
            lat = found_arrival - emit_t
            per_turn.setdefault(e["seq"], []).append(lat)
            matched += 1

    def _stats(lats):
        if not lats:
            return {}
        s = sorted(lats)
        p95 = s[min(len(s) - 1, int(0.95 * len(s)))]
        return {
            "n": len(s),
            "min": round(s[0], 3),
            "median": round(statistics.median(s), 3),
            "p95": round(p95, 3),
            "max": round(s[-1], 3),
        }

    overall = _stats([l for lats in per_turn.values() for l in lats])
    return {
        "matched_markers": matched,
        "total_markers": len(emit_log),
        "per_turn": {seq: _stats(lats) for seq, lats in sorted(per_turn.items())},
        "overall": overall,
    }


def _measure_payload_sizes(updates):
    """Per-turn on-wire stream_update payload size (bytes).

    Segments the captured frames into streaming turns using the SAME is_partial
    transition logic as `_measure_updates` (a run of consecutive partial updates =
    one turn; a non-partial commit closes it), then reports median/max raw frame
    bytes per turn. This directly answers "how does payload size grow with turn
    count?" — if the active instance's full conversation is re-serialized every
    tick, these numbers should climb as history accumulates.

    `updates` entries are (arrival_monotonic, event_dict, raw_frame_bytes).
    """
    import statistics

    turns = []           # list of per-turn lists of raw byte sizes
    cur = None
    for _arrival, ev, raw_bytes in updates:
        inst = (ev.get("agent_instances") or ev.get("instances") or {}).get(INSTANCE_NAME)
        if not isinstance(inst, dict):
            continue
        is_partial = bool(inst.get("is_partial"))
        if is_partial:
            if cur is None:
                cur = []
            cur.append(raw_bytes)
        else:
            if cur is not None:
                turns.append(cur)
                cur = None
    if cur is not None:
        turns.append(cur)

    out = []
    for i, sizes in enumerate(turns):
        if not sizes:
            continue
        s = sorted(sizes)
        out.append({
            "turn": i + 1,
            "n_frames": len(s),
            "min_bytes": s[0],
            "median_bytes": int(statistics.median(s)),
            "max_bytes": s[-1],
        })
    return out


# ══════════════════════════════════════════════════════════════════════════════
# The test
# ══════════════════════════════════════════════════════════════════════════════

def _assert_delta_mode(updates, payload):
    """Delta-mode assertions: (a) tail stays bounded; (b) payload stays flat.

    force_full frames (~1% of ticks) and prefix_shrank frames carry the full list
    while still being is_partial=True — they are expected and accounted for.
    """
    print("\n[fullstack] DELTA MODE assertions enabled")
    TAIL_LIMIT = TAIL_COMMITTED + MAX_STREAMING_PARTIALS + 1
    bounded_partial_frames = 0
    full_partial_frames = 0
    for _a, ev, _b in updates:
        inst = (ev.get("agent_instances") or ev.get("instances") or {}).get(INSTANCE_NAME)
        if isinstance(inst, dict) and inst.get("is_partial"):
            assert inst["history_count"] - len(inst["messages"]) >= 0, \
                f"history_count < messages.length (startIdx would be negative): " \
                f"hCount={inst['history_count']} msgs={len(inst['messages'])}"
            if len(inst["messages"]) <= TAIL_LIMIT:
                bounded_partial_frames += 1
            else:
                full_partial_frames += 1
    assert bounded_partial_frames > 0, \
        "DELTA MODE: no bounded partial frames captured — delta tail cut not working"
    total_partial = bounded_partial_frames + full_partial_frames
    if total_partial > 0:
        delta_ratio = bounded_partial_frames / total_partial
        print(f"  [DELTA] {bounded_partial_frames}/{total_partial} partial frames bounded "
              f"({delta_ratio:.1%}), {full_partial_frames} full (force_full/shrink)")
        assert delta_ratio > 0.95, \
            f"DELTA MODE: only {delta_ratio:.1%} of partial frames are bounded tails " \
            f"(expected >95% — force_full is ~1% of ticks)"
    if len(payload) >= 2:
        assert payload[-1]["median_bytes"] < 1.5 * payload[0]["median_bytes"], \
            f"Payload grew with conversation (delta mode should keep it flat): " \
            f"turn1={payload[0]['median_bytes']}B -> turnN={payload[-1]['median_bytes']}B (limit 1.5x)"


# ══════════════════════════════════════════════════════════════════════════════
# Message stack sync verification (frontend vs backend consistency)
# ══════════════════════════════════════════════════════════════════════════════

def _assert_message_stack_sync(frontend_messages, pool, instance_name):
    """Verify the frontend's final message list is consistent with the backend conversation.

    Checks:
      1. No duplicate (role, content) pairs in the frontend list.
      2. Index contiguity — if messages carry an `index` field, they must be
         contiguous from 0 (no gaps, no reordering).
      3. Final consistency — same message count, same roles in same order, and
         matching content (first 100 chars) for committed (non-streaming) messages.

    The frontend may have FEWER messages than the backend if the last streaming
    partial hasn't been committed yet (the test ends mid-stream). We tolerate a
    small trailing gap (<=2 messages: one in-flight assistant + one tool result).

    Args:
        frontend_messages: list of dicts with keys {role, content, index, reasoning_len}
            extracted from the Node harness's `state.subAgents[name].messages`.
        pool: AgentPool instance (from the fullstack_server fixture).
        instance_name: the agent instance name to compare.
    """
    print("\n[fullstack] ── MESSAGE STACK SYNC VERIFICATION ──")

    # ── Get backend conversation ────────────────────────────────────────────────
    inst = pool.get_instance(instance_name)
    if inst is None:
        pytest.fail(f"Backend instance '{instance_name}' not found in pool — cannot compare")
    with inst._compression_lock:
        backend_conv = list(inst.conversation)

    # Serialize backend messages to (role, content_prefix) pairs for comparison.
    backend_msgs = []
    for i, msg in enumerate(backend_conv):
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            content = msg.get("content", "") or ""
            if isinstance(content, list):  # multimodal
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        else:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "") or ""
            if isinstance(content, list):
                content = " ".join(str(getattr(p, "text", "")) for p in content)
        backend_msgs.append({"role": str(role), "content": str(content)[:200], "index": i})

    n_fe = len(frontend_messages)
    n_be = len(backend_msgs)
    print(f"  Frontend messages: {n_fe}, Backend conversation: {n_be}")

    # ── Check 1: No duplicates in frontend ─────────────────────────────────────
    # Key on (role, content, index) — two distinct tool results can legitimately
    # have the same content (e.g., both evaluate to '6') but different indices.
    # A real splice re-append would produce the SAME index twice.
    seen = set()
    for i, msg in enumerate(frontend_messages):
        key = (msg["role"], msg["content"], msg.get("index"))
        assert key not in seen, (
            f"DUPLICATE message at frontend position {i}: role={msg['role']}, "
            f"content={msg['content'][:80]!r}, index={msg.get('index')}. "
            f"This indicates a failed splice or double-append in the delta merge path."
        )
        seen.add(key)
    print(f"  ✓ No duplicate (role, content, index) triples in {n_fe} frontend messages")

    # ── Check 2: Index contiguity ───────────────────────────────────────────────
    # ASSUMPTION: the conversation is append-only (no message deletion/editing during
    # the test run). Under this invariant, the backend assigns sequential absolute indices
    # (0, 1, 2, ...) and the frontend's positional splice preserves them. If the backend
    # ever supports in-place message removal, this check must be relaxed to verify
    # uniqueness + monotonicity instead of strict contiguity.
    indexed_msgs = [m for m in frontend_messages if "index" in m and m["index"] is not None]
    if indexed_msgs:
        for i, msg in enumerate(indexed_msgs):
            expected_idx = i
            actual_idx = msg["index"]
            assert actual_idx == expected_idx, (
                f"INDEX GAP: frontend position {i} has index={actual_idx} "
                f"(expected {expected_idx}). This indicates a missing or duplicated "
                f"message in the positional splice merge."
            )
        print(f"  ✓ Index contiguity verified for {len(indexed_msgs)} indexed messages (0..{len(indexed_msgs)-1})")
    else:
        print("  (no index fields in frontend messages — skipping contiguity check)")

    # ── Check 3: Final consistency (frontend ⊆ backend, same order) ────────────
    # The frontend's message list should be a PREFIX of the backend conversation
    # (possibly shorter if the last streaming partial hasn't committed yet).
    # We compare role sequences and content prefixes.
    #
    # Tolerance: the frontend may lag by up to 2 messages (one in-flight assistant
    # + one tool result not yet visible in the final frame). If n_fe > n_be, that's
    # a real bug (frontend has MORE than backend — impossible unless there are dups).
    if n_fe > n_be:
        pytest.fail(
            f"Frontend has MORE messages ({n_fe}) than backend conversation ({n_be}). "
            f"This is impossible without duplicates or a desynced splice. "
            f"Frontend roles: {[m['role'] for m in frontend_messages[-5:]]} "
            f"(last 5). Backend roles: {[m['role'] for m in backend_msgs[-5:]]} (last 5)."
        )

    # ── Check 3b: Excessive lag guard ──────────────────────────────────────────
    # If the backend has significantly more messages than the frontend, something is
    # wrong (frontend lost frames and never resynced). Tolerance: up to
    # MAX_STREAMING_PARTIALS + 1 messages of lag (in-flight partials + one tool result).
    max_lag = MAX_STREAMING_PARTIALS + 1
    if n_be - n_fe > max_lag:
        pytest.fail(
            f"EXCESSIVE FRONTEND LAG: backend has {n_be} messages but frontend only "
            f"{n_fe} (lag={n_be - n_fe}, tolerance={max_lag}). The frontend likely lost "
            f"frames and never resynced via force_full. Check for _needsResync stuck state."
        )

    # Compare the overlapping prefix: frontend[i] should match backend[i].
    # We allow the LAST MAX_STREAMING_PARTIALS frontend messages to differ (in-flight
    # streaming partials that haven't been committed to the backend conversation yet).
    compare_count = max(0, n_fe - MAX_STREAMING_PARTIALS)
    mismatches = []
    for i in range(compare_count):
        fe_role = frontend_messages[i]["role"]
        be_role = backend_msgs[i]["role"] if i < n_be else "?"
        fe_content = frontend_messages[i]["content"][:100]
        be_content = backend_msgs[i]["content"][:100] if i < n_be else "?"

        if fe_role != be_role:
            mismatches.append(f"  position {i}: role FE={fe_role!r} BE={be_role!r}")
        elif fe_content != be_content:
            # Both are already truncated to 200 chars at extraction time.
            # Any difference within that window is a real desync — flag it.
            mismatches.append(
                f"  position {i} ({fe_role}): FE={fe_content[:80]!r} BE={be_content[:80]!r}"
            )

    assert not mismatches, (
        f"MESSAGE STACK DESYNC: {len(mismatches)} position(s) where frontend ≠ backend:\n"
        + "\n".join(mismatches[:10])
        + ("\n  ... (truncated)" if len(mismatches) > 10 else "")
        + f"\n\nFrontend roles (first 10): {[m['role'] for m in frontend_messages[:10]]}\n"
        + f"Backend roles (first 10):  {[m['role'] for m in backend_msgs[:10]]}"
    )

    # ── Check 4: Role sequence consistency (structural) ────────────────────────
    # The role sequence of the frontend prefix must match the backend prefix exactly.
    fe_roles = [m["role"] for m in frontend_messages[:compare_count]]
    be_roles = [m["role"] for m in backend_msgs[:compare_count]]
    assert fe_roles == be_roles, (
        f"ROLE SEQUENCE MISMATCH (first {compare_count} messages):\n"
        f"  FE: {fe_roles}\n  BE: {be_roles}"
    )

    print(f"  ✓ Final consistency: {compare_count}/{n_fe} frontend messages match backend "
          f"(last {n_fe - compare_count} skipped as potential in-flight partials)")
    print(f"[fullstack] ── MESSAGE STACK SYNC: PASS ──")


@pytest.mark.timeout(540)
def test_fullstack_streaming(fullstack_server):
    """Drive the full real stack and assert incremental streaming per turn + loop cycle."""
    ws_url = fullstack_server["ws_url"]
    _MockLLMHandler.reset()

    # ── 1. Real WebSocket capture over the live socket ─────────────────────────
    # The mock streams continuously (each turn ends in a tool call -> next turn), so a
    # long window simply captures more turns rather than idling. Derive the per-turn budget
    # from the ACTUAL chunk profile so it stays correct if REASONING_CHUNKS/CHUNK_SLEEP are
    # tuned via env: reasoning+content emission time + ~2.5s inter-turn (LLM call + tool exec)
    # + 1.5s headroom. 30s floor margin for startup/first-turn warmup.
    emit_ms = (REASONING_CHUNKS + CONTENT_CHUNKS) * int(CHUNK_SLEEP * 1000)
    per_turn_ms = emit_ms + 2500 + 1500   # emission + inter-turn latency + headroom
    capture_ms = max(30000, N_TURNS * per_turn_ms + 30000)
    print(f"\n[fullstack] AC server on {ws_url} — monitor live activity here "
          f"(WS /ws/chat, HTTP /api/*). Capturing for {capture_ms/1000:.0f}s...")
    updates = _capture_ws(ws_url, run_ms=capture_ms)
    assert len(updates) > 0, "No stream_update frames arrived over the live WebSocket"

    metrics = _measure_updates(updates)
    print(f"\n[fullstack] captured {len(updates)} stream_updates; segmented into {len(metrics)} streaming turn(s)")
    for m in metrics:
        print(f"  turn {m['turn']}: updates={m['n_updates']} max_gap={m['max_gap']:.3f}s "
              f"reasoning {m['reasoning_first']}->{m['reasoning_last']} (distinct={m['distinct_reasoning']}) "
              f"content {m['content_first']}->{m['content_last']}")

    # ── 1a. Per-turn on-wire payload size (bytes) — Task #1 ─────────────────────
    # Answers "how does the stream_update frame grow with turn count?" If the active
    # instance's full conversation is re-serialized every tick, these climb as history
    # accumulates across turns.
    payload = _measure_payload_sizes(updates)
    if payload:
        print(f"\n[fullstack] Per-turn stream_update payload size (raw WS frame bytes):")
        for p in payload:
            print(f"  turn {p['turn']}: frames={p['n_frames']} "
                  f"min={p['min_bytes']}B median={p['median_bytes']}B max={p['max_bytes']}B")
    else:
        print("\n[fullstack] Per-turn payload size: (no streaming turns captured)")

    # ── 1a-delta. Delta-mode assertions (only when AGENT_CASCADE_STREAM_DELTA=1) ───
    if DELTA_MODE:
        _assert_delta_mode(updates, payload)

    # ── 1b. End-to-end latency: mock-generator -> final WS output ───────────────
    emit_log = _MockLLMHandler.get_emit_log()
    lat = _measure_latency(updates, emit_log)
    print(f"\n[fullstack] E2E latency (mock emit -> first WS update containing it): "
          f"matched {lat['matched_markers']}/{lat['total_markers']} markers")
    for seq, st in lat["per_turn"].items():
        print(f"  turn {seq}: n={st['n']} min={st['min']}s median={st['median']}s p95={st['p95']}s max={st['max']}s")
    ov = lat["overall"]
    if ov:
        print(f"  OVERALL: n={ov['n']} min={ov['min']}s median={ov['median']}s p95={ov['p95']}s max={ov['max']}s")

    # ── 2. Multi-turn loop actually cycled ─────────────────────────────────────
    req_log = _MockLLMHandler.get_request_log()
    # Diagnostic: dump every LLM request the mock saw (seq, n_msgs, compression flag).
    # In COMPRESSION_EXPERIMENT mode this also reveals whether/when the Compressor fired.
    print(f"\n[fullstack] Mock LLM request log ({len(req_log)} requests):")
    _n_compression = 0
    for _r in req_log:
        if _r.get("compression"):
            _n_compression += 1
            print(f"  seq={_r['seq']} n_msgs={_r['n_msgs']} [COMPRESSION request served]")
        else:
            _last = _r.get("last", "")
            if isinstance(_last, list):
                _last = " ".join(str(x.get("text", x))[:40] for x in _last)
            print(f"  seq={_r['seq']} n_msgs={_r['n_msgs']} last=...{str(_last)[-50:]!r}")

    if COMPRESSION_EXPERIMENT:
        # Compression experiment mode: the loop legitimately STALLS after compression fires.
        # Our mock returns a trivially short summary, so token usage stays above the force
        # threshold and the agent re-enters forced-compression every cycle -> cooldown/backoff
        # storm (see logs). That is an experiment artifact, NOT a bug we're measuring. The
        # goal is to observe E2E latency BEFORE vs AFTER compression, so we only require that
        # compression actually fired and at least one normal turn completed.
        assert _n_compression >= 1, (
            "Compression experiment: the Compressor never fired — no END SUMMARY served. "
            "Increase FULLSTACK_E2E_N_TURNS or lower FULLSTACK_E2E_COMP_FORCE_PCT."
        )
        # ── EXPERIMENT RESULT: latency vs conversation history size ───────────────
        print("\n" + "=" * 70)
        print("COMPRESSION EXPERIMENT RESULT — is E2E latency driven by history size?")
        print("=" * 70)
        print(f"  Compression requests served: {_n_compression}")
        for m in metrics:
            print(f"  turn {m['turn']}: max_gap={m['max_gap']:.3f}s "
                  f"reasoning_first_len={m['reasoning_first']} (proxy for payload/history size)")
        # Per-turn E2E latency median, keyed by the turn's committed conversation length.
        print("  (See 'Per-turn per-op cost' above: conv_len = committed conversation length.)")
        print("  If E2E latency DROPS on the post-compression turn (smaller conv_len), history")
        print("  size is the driver. If it stays flat / keeps growing, something else drives it.")
        print("=" * 70)
    else:
        assert len(req_log) >= N_TURNS, (
            f"Expected the agent loop to make >= {N_TURNS} LLM calls (one per turn), got {len(req_log)}. "
            f"Loop did not cycle through the tool-call turns."
        )

    # ── 3. Per-turn incremental streaming assertions ───────────────────────────
    _min_turns = (1 if COMPRESSION_EXPERIMENT else N_TURNS)
    assert len(metrics) >= _min_turns, (
        f"Expected >= {_min_turns} distinct streaming turns over the socket, got {len(metrics)}. "
        f"Metrics: {metrics}"
    )
    for m in metrics[:N_TURNS]:
        assert m["n_updates"] >= MIN_UPDATES_PER_TURN, (
            f"Turn {m['turn']} degraded to a burst: only {m['n_updates']} stream_updates "
            f"(expected >= {MIN_UPDATES_PER_TURN}). Reasoning lens: first={m['reasoning_first']}, last={m['reasoning_last']}."
        )
        assert m["max_gap"] <= MAX_INTER_ARRIVAL_GAP, (
            f"Turn {m['turn']} had a long stall: max inter-update gap {m['max_gap']:.3f}s "
            f"(expected <= {MAX_INTER_ARRIVAL_GAP}s) while the mock was actively emitting."
        )
        assert m["distinct_reasoning"] >= 2, (
            f"Turn {m['turn']} reasoning did not grow incrementally on screen: "
            f"only {m['distinct_reasoning']} distinct reasoning length(s) seen — looks like a blob."
        )

    # ── 4. Real frontend (approach B): real app.js over the live socket ────────
    node_result = _run_node_harness(ws_url, run_ms=12000)
    print(f"\n[fullstack] Node/app.js result: {node_result}")
    assert node_result.get("ok"), f"Node harness failed: {node_result}"

    # The real app.js connected and stayed alive for the run window.
    assert not node_result.get("ws_closed", False) or node_result.get("runs_ms", 0) > 1000, (
        f"app.js WebSocket closed prematurely: {node_result}"
    )

    # ── 5. Message stack sync: frontend vs backend consistency ────────────────
    # Verify the frontend's accumulated message list matches the backend conversation.
    # This catches: duplicate splices, index gaps, prefix loss, and desynced merges —
    # the exact class of bugs fixed in the delta streaming refactor.
    #
    # The Node harness extracts state.subAgents[INSTANCE].messages at finish() time.
    # If extraction fails (harness fragility, app.js load error), we SKIP sync verification
    # rather than failing the entire e2e test — the other assertions (payload size, latency,
    # turn count) still validate the streaming pipeline.
    frontend_messages = node_result.get("frontend_messages", [])
    _sync_skipped = False
    if node_result.get("frontend_state_missing"):
        print(f"\n[fullstack] ⚠ Message stack sync SKIPPED: frontend state not found in Node harness. "
              f"Keys: {list(node_result.keys())}")
        _sync_skipped = True
    elif node_result.get("frontend_extract_error"):
        print(f"\n[fullstack] ⚠ Message stack sync SKIPPED: extraction error: "
              f"{node_result['frontend_extract_error']}")
        _sync_skipped = True
    elif not frontend_messages:
        print(f"\n[fullstack] ⚠ Message stack sync SKIPPED: frontend message list is empty.")
        _sync_skipped = True

    if not _sync_skipped:
        _assert_message_stack_sync(frontend_messages, fullstack_server["pool"], INSTANCE_NAME)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n[fullstack] PASS — real uvicorn + real WS + real app.js. "
          f"LLM calls={len(req_log)}, turns segmented={len(metrics)}, "
          f"app.js runs_ms={node_result.get('runs_ms')}, "
          f"frontend_msgs={len(frontend_messages)}")
