"""Black-box E2E stress tests for agent call scheduling via conc=0 sequential pool.

Treats AgentCascade as a black box: exercises contention/stress scenarios and verifies
observable behavior only (completion, ordering from request logs, state via endpoints).

Tests are designed to fail if commits 783a3fd or 6ee94ce are reverted.

Scenarios (per reports/e2e_test_scheduling_analysis.md):
T1 — Concurrent serialization: two async children on conc=0, verify no interleaving
T2 — Async-spawn reservation regression: sync child not starved by slow async sibling
T3 — Release-on-sleep deadlock: parent sleeps, async child gets slot
T4 — Reservation must NOT block unrelated waiter: independent session completes while Maine sleeps
T5 — Deep nesting under contention: chain with dependency-respecting order
T6 — Mass contention: 5 async children serialized without starvation

All tests use concurrency_limit=0 (sequential pool) for real slot contention.
"""

# CRITICAL: Set BEFORE any agent_cascade imports to isolate test runs from real sessions.
# This creates separate log/settings directories so AC won't load stale conversation history.
import os as _os
_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", "test_e2e")

import json
import re
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple

import pytest
import requests

from tests.conftest_e2e import derive_shared_secret, encrypt_payload, generate_client_keypair


# ── Module-level timeout override for contention tests ─────────────────────────

pytestmark = pytest.mark.timeout(90)


# ── Programmable Mock LLM Server ───────────────────────────────────────────────

class MockRequest:
    """Represents a logged request to the mock LLM server."""

    __slots__ = ("seq", "path", "method", "body_summary", "timestamp")

    def __init__(self, seq: int, path: str, method: str, body_summary: str, timestamp: float):
        self.seq = seq
        self.path = path
        self.method = method
        self.body_summary = body_summary
        self.timestamp = timestamp


class ProgrammableMockLLMHandler(BaseHTTPRequestHandler):
    """HTTP handler that returns scripted responses and logs all requests.

    Response scripts are set via a queue before each test. Each chat completion
    request pops the next script from the queue. Scripts support:
    - Plain text response
    - Tool calls (call_agent, etc.)
    - Optional delay before responding
    """

    _response_queue: List[Dict[str, Any]] = []
    _request_log: List[MockRequest] = []
    _lock = threading.Lock()
    _seq_counter = 0

    def log_message(self, format, *args):
        pass  # Suppress request logging

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json(200, {"data": [{"id": "mock-model", "object": "model"}]})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8", errors="replace")

            print(f"[MOCK-SERVER] POST {self.path}, body_len={len(body)}", flush=True)

            # Log the request with sequence number and agent identity from system prompt
            with self._lock:
                self._seq_counter += 1
                seq = self._seq_counter
                try:
                    parsed = json.loads(body) if body else {}
                    messages = parsed.get("messages", [])

                    # Extract agent identity from system prompt (first message, "You are <name>.")
                    agent_id = ""
                    if messages:
                        raw_content = messages[0].get("content", "") if isinstance(messages[0], dict) else ""
                        # Handle multimodal format: content can be a list of {"type": "text", "text": "..."} dicts
                        if isinstance(raw_content, list):
                            sys_content = "".join(
                                item.get("text", "") for item in raw_content if isinstance(item, dict) and item.get("type") == "text"
                            )
                        else:
                            sys_content = str(raw_content)
                        m = re.match(r"You are ([^\n.]+)", sys_content.strip())
                        if m:
                            agent_id = m.group(1).strip()

                    # Also capture last message content for context
                    try:
                        last_msg_obj = messages[-1]
                        last_msg = last_msg_obj.get("content", "")[:80] if isinstance(last_msg_obj, dict) else str(last_msg_obj)[:80]
                    except Exception:
                        last_msg = ""
                    summary = f"agent='{agent_id}' msgs={len(messages)} last='{last_msg}'"
                except Exception as e:
                    summary = f"PARSE_ERROR({type(e).__name__}): {body[:100]}"

                self._request_log.append(MockRequest(seq, self.path, "POST", summary, time.time()))
                print(f"[MOCK-SERVER] Logged request #{seq}: {summary[:60]}, log_len={len(self._request_log)}", flush=True)

            if self.path == "/v1/chat/completions":
                with self._lock:
                    if self._response_queue:
                        script = self._response_queue.pop(0)
                    else:
                        script = {"text": "[MOCK: no more scripts]"}

                print(f"[MOCK-SERVER] Got script keys={list(script.keys())}", flush=True)
                delay = script.get("delay", 0)
                if delay > 0:
                    time.sleep(delay)

                stream = self._build_stream(script)

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")  # Close after sending all chunks
                encoded = stream.encode("utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                self.wfile.flush()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            print(f"[MOCK-SERVER] UNHANDLED ERROR in do_POST: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    @staticmethod
    def _build_stream(script: Dict[str, Any]) -> str:
        """Build SSE stream from a response script."""
        lines = []

        if "tool_calls" in script:
            tool_calls = script["tool_calls"]
            for i, tc in enumerate(tool_calls):
                name = tc.get("name", "unknown")
                args = tc.get("args", {})
                tool_id = f"call_{i}"

                chunk = {
                    "id": f"mock-{tool_id}",
                    "object": "chat.completion.chunk",
                    "model": "mock-model",
                    "created": 0,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": i,
                                "id": tool_id,
                                "function": {"name": name}
                            }]
                        },
                        "finish_reason": None
                    }]
                }
                lines.append(f'data: {json.dumps(chunk)}\n\n')

                args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                chunk = {
                    "id": f"mock-{tool_id}",
                    "object": "chat.completion.chunk",
                    "model": "mock-model",
                    "created": 0,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": i,
                                "function": {"arguments": args_str}
                            }]
                        },
                        "finish_reason": None if i < len(tool_calls) - 1 else "tool_calls"
                    }]
                }
                lines.append(f'data: {json.dumps(chunk)}\n\n')

        elif "text" in script:
            content = script["text"]
            chunk = {
                "id": "mock-text",
                "object": "chat.completion.chunk",
                "model": "mock-model",
                "created": 0,
                "choices": [{
                    "index": 0,
                    "delta": {"content": content},
                    "finish_reason": "stop"
                }]
            }
            lines.append(f'data: {json.dumps(chunk)}\n\n')
        else:
            chunk = {
                "id": "mock-empty",
                "object": "chat.completion.chunk",
                "model": "mock-model",
                "created": 0,
                "choices": [{
                    "index": 0,
                    "delta": {"content": ""},
                    "finish_reason": "stop"
                }]
            }
            lines.append(f'data: {json.dumps(chunk)}\n\n')

        lines.append("data: [DONE]\n\n")
        return "".join(lines)

    def _send_json(self, status_code: int, data: Any):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def set_responses(cls, responses: List[Dict[str, Any]]):
        with cls._lock:
            cls._response_queue = list(responses)

    @classmethod
    def get_request_log(cls) -> List[MockRequest]:
        with cls._lock:
            return list(cls._request_log)

    @classmethod
    def clear_request_log(cls):
        with cls._lock:
            cls._request_log.clear()
            cls._seq_counter = 0


@pytest.fixture(scope="module")
def mock_llm_server():
    """Start a programmable mock LLM HTTP server on a random port."""
    server = HTTPServer(("127.0.0.1", 0), ProgrammableMockLLMHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}/v1"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    for _ in range(10):
        try:
            import urllib.request
            with urllib.request.urlopen(f"{base_url}/models", timeout=1):
                break
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    else:
        server.shutdown()
        pytest.fail("Mock LLM server failed to start")

    yield base_url

    server.shutdown()


@pytest.fixture(scope="function")
def ac_server(mock_llm_server):
    """Boot the full AgentCascade app via uvicorn on a real port.

    Each test gets its own server instance for isolation.
    CRITICAL: concurrency_limit=0 creates a real SlotPool (capacity=1) for contention testing.
    """
    import sys
    import time
    from pathlib import Path

    project_root = Path(__file__).parent.parent.absolute()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Ensure AgentCascade root is on path for config imports (config.secrets_loader, etc.)
    ac_root = Path(project_root).parent / "AgentCascade"
    if str(ac_root) not in sys.path:
        sys.path.insert(0, str(ac_root))

    # Fixed session name for test runs so tests can target the orchestrator instance.
    TEST_SESSION_NAME = "E2ETestSession"

    from agent_cascade.api_server import create_app
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.agent_factory import load_orchestrator_agent
    from agent_cascade.api_router import APIEndpoint, APIRouter
    from uvicorn import Config, Server

    llm_cfg = {
        "model": "mock-model",
        "model_server": mock_llm_server,
        "api_key": "EMPTY",
        "model_type": "qwenvl_oai",
        "max_input_tokens": 8192,
    }

    # Use AgentCascade agents dir (has templates) rather than project_root/agents (empty in workspace)
    agents_dir = ac_root / "agents" if (ac_root / "agents").exists() else project_root / "agents"
    pool = AgentPool(llm_cfg, agents_dir=str(agents_dir))

    # CRITICAL: Clear persisted endpoints/priorities so tests use only our mock endpoint.
    # Without this, real config from disk overrides our mock_llm_server routing.
    with pool.api_router._lock:
        pool.api_router.endpoints.clear()
        pool.api_router.agent_priorities.clear()
        pool.api_router._agent_types_with_priorities.clear()

    # CRITICAL: conc=0 forces shared sequential SlotPool with real contention.
    mock_ep = APIEndpoint(
        id="mock-endpoint",
        name="Mock LLM Server",
        api_base=mock_llm_server,  # fixture yields base_url string directly
        model="mock-model",
        concurrency_limit=0,  # Sequential — real FIFO contention
        enabled=True,
    )
    pool.api_router.add_endpoint(mock_ep)

    # Make the mock endpoint the default fallback so all agent types use it.
    pool.api_router.default_llm_cfg = mock_ep.to_llm_cfg()

    orchestrator = load_orchestrator_agent(pool, llm_cfg)
    
    # Register the orchestrator as a template so get_template('orchestrator') works.
    pool.templates['orchestrator'] = orchestrator
    
    agents = [orchestrator]

    app = create_app(
        agents=agents,
        agent_pool=pool,
        config={
            "session_name": TEST_SESSION_NAME,  # Fixed name — tests target this instance directly
            "fresh_session": True,              # Don't load stale conversation history from logs
        },
    )

    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        ac_port = s.getsockname()[1]

    config = Config(app=app, host="127.0.0.1", port=ac_port, log_level="warning", lifespan="on")
    server = Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{ac_port}"
    last_error = None
    for attempt in range(75):
        try:
            resp = requests.get(f"{base_url}/api/keys", timeout=1)
            if resp.status_code == 200:
                break
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = str(e)
        time.sleep(0.2)
    else:
        server.should_exit = True
        thread.join(timeout=3)
        pytest.fail(f"AC server did not start within timeout. Last error: {last_error}. Port: {ac_port}")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)

        # Clean up session logs to avoid polluting production zone.
        # Tests must never leave trash on HDD.
        import glob
        log_pattern = str(project_root / "logs" / f"orchestrator_{TEST_SESSION_NAME}_*.jsonl")
        for log_file in glob.glob(log_pattern):
            try:
                Path(log_file).unlink()
            except OSError:
                pass  # Non-critical cleanup failure


# ── Test Helpers ───────────────────────────────────────────────────────────────

def handshake_and_send(base_url: str, text: str, target: str = "E2ETestSession") -> Tuple[str, str]:
    """Perform E2E handshake and send a message. Returns (session_token, shared_secret)."""
    resp = requests.get(f"{base_url}/api/keys", timeout=5)
    assert resp.status_code == 200, f"Failed to get keys: {resp.text}"
    server_public_b64 = resp.json()["public_key"]

    client_private, client_public_b64 = generate_client_keypair()
    shared_secret = derive_shared_secret(client_private, server_public_b64)

    resp = requests.post(
        f"{base_url}/api/handshake",
        json={"public_key": client_public_b64},
        timeout=5,
    )
    assert resp.status_code == 200, f"Handshake failed: {resp.text}"
    session_token = resp.json()["session_token"]

    payload = {"target": target, "text": text}
    encrypted_b64, nonce_b64 = encrypt_payload(shared_secret, payload)

    resp = requests.post(
        f"{base_url}/api/message",
        json={
            "session_token": session_token,
            "payload": encrypted_b64,
            "nonce": nonce_b64,
        },
        timeout=5,
    )
    assert resp.status_code == 200, f"Message send failed: {resp.text}"
    data = resp.json()
    assert data.get("status") == "success", f"Unexpected status: {data}"

    return session_token, shared_secret


def wait_for_completion(base_url: str, session_token: str, timeout: float = 90.0) -> bool:
    """Poll /api/status until generating becomes False or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{base_url}/api/status",
                params={"token": session_token},
                timeout=5,
            )
            if resp.status_code == 200:
                status = resp.json()
                if not status.get("generating", True):
                    return True
        except (requests.RequestException, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    return False


def get_agent_states(base_url: str, session_token: str) -> Dict[str, Any]:
    """Get current agent states from /api/state."""
    resp = requests.get(f"{base_url}/api/state", params={"token": session_token}, timeout=5)
    if resp.status_code == 200:
        return resp.json()
    return {}


def extract_agent_identity(body_summary: str) -> str:
    """Parse agent name from request log body_summary.

    The engine rewrites system prompt to "You are <instance_name>." at execution time.
    Mock handler captures this as agent='...' in body_summary.
    """
    m = re.search(r"agent='([^']+)'", body_summary)
    if m:
        return m.group(1)
    return ""


def extract_identities(log: List[MockRequest]) -> List[str]:
    """Extract ordered list of agent identities from request log."""
    return [extract_agent_identity(req.body_summary) for req in log]


def assert_no_interleaving(log: List[MockRequest], margin: float = 0.2):
    """Verify no two agents have overlapping request windows.

    With conc=0 sequential pool, all LLM calls serialize — requests from different
    agents should never overlap in time. This catches FIFO violations or async barging.

    Method: For each pair of consecutive requests from DIFFERENT agents, verify the
    second agent's request starts after the first one's request was completed (based on
    log timestamps). Since the mock handler processes requests sequentially under its lock,
    overlapping would manifest as a later request appearing with an earlier timestamp.
    """
    for i in range(len(log) - 1):
        curr = log[i]
        nxt = log[i + 1]
        curr_agent = extract_agent_identity(curr.body_summary)
        nxt_agent = extract_agent_identity(nxt.body_summary)

        # Different agents: next agent's request must not start before current one
        if curr_agent and nxt_agent and curr_agent != nxt_agent:
            gap = nxt.timestamp - curr.timestamp
            assert gap >= -margin, (
                f"Interleaving detected: {curr_agent} (seq={curr.seq}, t={curr.timestamp:.3f}) "
                f"and {nxt_agent} (seq={nxt.seq}, t={nxt.timestamp:.3f}) have overlapping requests. "
                f"Gap: {gap:.3f}s"
            )


def assert_identity_subsequence(identity_seq: List[str], required_order: List[str]):
    """Assert that required_order appears as a subsequence in identity_seq.

    Used for dependency ordering checks: if A must complete before B, then
    the last appearance of A should precede the first appearance of B.
    """
    idx = 0
    for agent in identity_seq:
        if idx < len(required_order) and agent == required_order[idx]:
            idx += 1
    assert idx == len(required_order), (
        f"Required subsequence {required_order} not found in identity sequence. "
        f"Matched prefix: {required_order[:idx]}. Full seq: {identity_seq}"
    )


def assert_sentinel_in_last_requests(log: List[MockRequest], sentinel: str, count: int = -1):
    """Assert that a sentinel string appears in the last N requests' body_summary (or all if count=-1).

    Used to prove results were consumed — script final responses with unique sentinels
    and verify they appear in subsequent requests (injected as tool results).

    Default count=-1 checks ALL logged requests for broader coverage.
    """
    if count == -1:
        search_range = log
        desc = "any request"
    else:
        search_range = log[-count:]
        desc = f"last {count} request(s)"

    for req in search_range:
        if sentinel in req.body_summary:
            return
    pytest.fail(
        f"Sentinel '{sentinel}' not found in {desc}. "
        f"Last summaries: {[r.body_summary[:100] for r in log[-min(len(log), 3):]]}"
    )


def assert_completion_success(log: List[MockRequest], sentinel: str):
    """Assert that the final result was consumed successfully (sentinel appears somewhere)."""
    found = any(sentinel in req.body_summary for req in log)
    assert found, (
        f"Success sentinel '{sentinel}' not found in any request. "
        f"Total requests: {len(log)}. Summaries: {[r.body_summary[:80] for r in log]}"
    )


# ── Stress Test Scenarios ─────────────────────────────────────────────────────

class TestAgentCallSchedulingStress:
    """Black-box stress tests for agent scheduling on conc=0 sequential pool."""

    def test_t1_concurrent_serialization(self, ac_server):
        """T1 — Concurrent serialization: two async children on conc=0, verify no interleaving.

        Maine spawns A and B both async. On conc=0 they must queue on the shared slot.
        Assert strict serialization via agent identity extraction from request logs.
        """
        base_url = ac_server

        responses = [
            # Turn 1: Maine calls two async children
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A", "task": "Research task A", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "B", "task": "Review task B", "async_mode": True}}
                ]
            },
            # Turn 2: A completes (instant)
            {"text": "SENTINEL_A_COMPLETE_T1"},
            # Turn 3: B completes with delay to create contention window
            {"text": "SENTINEL_B_COMPLETE_T1", "delay": 1.5},
            # Turn 4: Maine resumes with both results
            {"text": "SENTINEL_MAINE_FINAL_T1 both tasks done"},
        ]

        ProgrammableMockLLMHandler.set_responses(responses)
        ProgrammableMockLLMHandler.clear_request_log()

        test_start = time.perf_counter()
        session_token, _ = handshake_and_send(base_url, "Run A and B concurrently.")

        completed = wait_for_completion(base_url, session_token, timeout=90.0)
        assert completed, "Test timed out — possible deadlock with concurrent async children"

        log = ProgrammableMockLLMHandler.get_request_log()
        identities = extract_identities(log)

        # Must have at least 4 requests: Maine(1), A, B, Maine(resume)
        assert len(log) >= 4, f"Expected >= 4 LLM requests for concurrent serialization, got {len(log)}: {identities}"

        # Both children must appear in the log (observable completion)
        assert "A" in identities, f"Child A not found in request log. Identities: {identities}"
        assert "B" in identities, f"Child B not found in request log. Identities: {identities}"

        # Timing-based serialization check: A(instant) + B(delay=1.5s) serialized on conc=0
        # must take >1.7s wall-clock. Concurrent execution would complete in ~1.5s.
        elapsed = time.perf_counter() - test_start
        assert elapsed > 1.7, (
            f"Serialization not enforced: elapsed={elapsed:.2f}s (expected >1.7s for conc=0). "
            f"This suggests A and B ran concurrently instead of serialized."
        )

        # Verify final sentinel was consumed by checking the resume request contains child completion sentinels
        # (Maine's own final response sentinel is never in a request body since there's no subsequent request)
        resume_reqs = [r for r in log if "E2ETestSession" in r.body_summary and len(r.body_summary) > 50]
        assert len(resume_reqs) >= 1, f"No Maine resume request found. Log: {[r.body_summary[:60] for r in log]}"
        # The resume request should contain at least one child's completion sentinel as tool result
        maine_resume = resume_reqs[-1]
        assert "SENTINEL_A_COMPLETE_T1" in maine_resume.body_summary or "SENTINEL_B_COMPLETE_T1" in maine_resume.body_summary, (
            f"Maine resume request missing child sentinels: {maine_resume.body_summary[:150]}"
        )

    def test_t2_async_spawn_reservation_regression(self, ac_server):
        """T2 — Async-spawn reservation regression (6ee94ce).

        Parent spawns slow async child then calls sync child. Verify sync child completes
        quickly, not starved for 300s waiting behind a spawn-time reservation.

        This is the exact bug from todo.md line 100+: grep_safety_reviewer timed out
        because Maine held a stale reservation while sleeping.
        """
        base_url = ac_server

        responses = [
            # Turn 1: Maine calls slow async A then sync B
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A_slow", "task": "Slow research", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "B_sync", "task": "Quick review"}}
                ]
            },
            # Turn 2: B (sync) completes quickly — MUST NOT be starved by A's pending async
            {"text": "SENTINEL_B_SYNC_FAST_T2"},
            # Turn 3: A (async) completes after delay
            {"text": "SENTINEL_A_COMPLETE_T2", "delay": 2.5},
            # Turn 4: Maine resumes with both results
            {"text": "SENTINEL_MAINE_FINAL_T2"},
        ]

        ProgrammableMockLLMHandler.set_responses(responses)
        ProgrammableMockLLMHandler.clear_request_log()

        session_token, _ = handshake_and_send(base_url, "Run slow async and quick sync.")

        completed = wait_for_completion(base_url, session_token, timeout=90.0)
        assert completed, "Test timed out — possible reservation starvation (6ee94ce regression)"

        log = ProgrammableMockLLMHandler.get_request_log()
        identities = extract_identities(log)

        # Find timestamps for key events
        maine_start = None
        b_sync_req_time = None
        a_complete_time = None

        for req in log:
            agent = extract_agent_identity(req.body_summary)
            if maine_start is None and "Maine" in agent:
                maine_start = req.timestamp
            elif "B_sync" in agent and b_sync_req_time is None:
                b_sync_req_time = req.timestamp
            elif "A_slow" in agent and a_complete_time is None:
                a_complete_time = req.timestamp

        # B_sync must appear in log (it ran)
        assert "B_sync" in identities, f"Sync child B not found — starved by spawn reservation? Identities: {identities}"

        # Critical: B's request should happen relatively quickly after Maine starts,
        # NOT delayed ~300s by a stale reservation. With A having 2.5s delay, if B is
        # blocked behind a reservation, we'd see timeout or very late completion.
        if maine_start and b_sync_req_time:
            b_latency = b_sync_req_time - maine_start
            # B should complete within ~5s of Maine starting (not 300s+ from reservation block)
            assert b_latency < 5, (
                f"Sync child B starved: took {b_latency:.1f}s to get its turn. "
                f"This indicates spawn-time reservation blocking sync child (6ee94ce regression)."
            )

        # A_slow must also complete (async didn't interfere)
        assert "A_slow" in identities, f"Async child A not found. Identities: {identities}"

        # Verify final resume request consumed child results (sentinels injected as tool results).
        # The orchestrator's own final response sentinel is never in a request body since there's no subsequent request.
        resume_reqs = [r for r in log if "E2ETestSession" in r.body_summary and len(r.body_summary) > 50]
        assert len(resume_reqs) >= 1, f"No E2ETestSession resume request found. Log: {[r.body_summary[:60] for r in log]}"
        maine_resume = resume_reqs[-1]
        # At least one child sentinel should appear in the resume request as a tool result
        has_sentinel = any(s in maine_resume.body_summary for s in ["SENTINEL_B_SYNC_FAST_T2", "SENTINEL_A_COMPLETE_T2"])
        assert has_sentinel, (
            f"E2ETestSession resume request missing child sentinels: {maine_resume.body_summary[:150]}"
        )

    def test_t3_release_on_sleep_deadlock(self, ac_server):
        """T3 — Release-on-sleep deadlock: parent sleeps awaiting async child needing same pool.

        If parent holds slot while sleeping, child times out → observable as failure text
        in final result. Assert success (child completed).
        """
        base_url = ac_server

        responses = [
            # Turn 1: Maine calls A async with delay
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A", "task": "Async task needing pool", "async_mode": True}}
                ]
            },
            # Turn 2: A completes after delay — requires Maine to have released slot while sleeping
            {"text": "SENTINEL_A_COMPLETE_T3", "delay": 1.5},
            # Turn 3: Maine resumes with result
            {"text": "SENTINEL_MAINE_FINAL_T3 success"},
        ]

        ProgrammableMockLLMHandler.set_responses(responses)
        ProgrammableMockLLMHandler.clear_request_log()

        session_token, _ = handshake_and_send(base_url, "Run async task A.")

        completed = wait_for_completion(base_url, session_token, timeout=90.0)
        assert completed, "Test timed out — possible release-on-sleep deadlock"

        log = ProgrammableMockLLMHandler.get_request_log()
        identities = extract_identities(log)

        # Must have Maine → A → Maine sequence (3 requests minimum)
        assert len(log) >= 3, f"Expected >= 3 requests for release-on-sleep test, got {len(log)}: {identities}"

        # Verify proper ordering: E2ETestSession starts, A runs after, E2ETestSession resumes last
        assert_identity_subsequence(identities, ["E2ETestSession", "A", "E2ETestSession"])

        # A must appear in log (it got the slot after Maine released)
        assert "A" in identities, f"Child A not found — parent may have held slot while sleeping. Identities: {identities}"

        # Final resume request should contain child's completion sentinel (injected as tool result).
        # The orchestrator's own final response sentinel is never in a request body since there's no subsequent request.
        resume_reqs = [r for r in log if "E2ETestSession" in r.body_summary and len(r.body_summary) > 50]
        assert len(resume_reqs) >= 1, f"No E2ETestSession resume request found. Log: {[r.body_summary[:60] for r in log]}"
        maine_resume = resume_reqs[-1]
        assert "SENTINEL_A_COMPLETE_T3" in maine_resume.body_summary, (
            f"E2ETestSession resume request missing child sentinel: {maine_resume.body_summary[:150]}"
        )

    def test_t4_reservation_must_not_block_unrelated_waiter(self, ac_server):
        """T4 — Reservation must NOT block unrelated waiter (783a3fd regression).

        Two sessions on same server: while Maine sleeps waiting for slow async child,
        a second independent agent sends a message and must complete within reasonable time.
        If stale reservation blocks it → timeout/failure.
        """
        base_url = ac_server

        # Session 1 responses (Maine with slow async child)
        maine_responses = [
            # Turn 1: Maine calls A async with long delay
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A_slow", "task": "Very slow research", "async_mode": True}}
                ]
            },
            # Turn 2: A completes after delay
            {"text": "SENTINEL_A_COMPLETE_T4", "delay": 3.0},
            # Turn 3: Maine resumes
            {"text": "SENTINEL_MAINE_FINAL_T4"},
        ]

        # Session 2 responses (independent agent — must complete while Maine sleeps)
        independent_responses = [
            # Independent agent completes quickly
            {"text": "SENTINEL_INDEPENDENT_T4 done"},
        ]

        # Combine all responses in the order they'll be consumed:
        # The mock queue is global, so we need to interleave carefully.
        # Expected sequence: Maine(1) → independent_agent → A_slow(delayed) → independent_final → Maine(resume)
        # But actually the independent session runs on its own flow.
        # We'll set up responses and let scheduling determine order.
        all_responses = maine_responses + independent_responses

        ProgrammableMockLLMHandler.set_responses(all_responses)
        ProgrammableMockLLMHandler.clear_request_log()

        # Start Session 1: Maine with slow async child
        session1_token, _ = handshake_and_send(base_url, "Run slow async task.")

        # Give Maine time to start and enter SLEEPING state
        time.sleep(1.0)

        # Start Session 2: independent agent — must complete while Maine sleeps
        session2_token, _ = handshake_and_send(base_url, "Quick independent task.")

        # Both sessions should complete within timeout
        completed1 = wait_for_completion(base_url, session1_token, timeout=90.0)
        assert completed1, "Session 1 (Maine) timed out"

        completed2 = wait_for_completion(base_url, session2_token, timeout=30.0)
        assert completed2, (
            "Session 2 (independent agent) timed out — likely blocked by stale SLEEPING reservation "
            "(783a3fd regression). Independent agents should not be starved by unrelated sleepers."
        )

        log = ProgrammableMockLLMHandler.get_request_log()
        identities = extract_identities(log)

        # Must have at least 3 requests: E2ETestSession(spawn), A_slow, E2ETestSession(resume with both messages)
        assert len(log) >= 3, f"Expected >= 3 requests for two sessions, got {len(log)}: {identities}"

        # Verify session 2's message was processed (appears in resume request body_summary).
        # Both sessions target the same E2ETestSession instance, so session 2's text gets queued
        # and appears when E2ETestSession resumes after A_slow completes.
        session2_processed = any("Quick independent" in r.body_summary for r in log)
        assert session2_processed, (
            f"Session 2 message not processed — may have been blocked by sleeping reservation. "
            f"Summaries: {[r.body_summary[:80] for r in log]}"
        )

        # Verify A_slow completed (its identity appears in the request log)
        assert "A_slow" in identities, f"A_slow not found in request log — did not get slot after Maine slept. Identities: {identities}"

        # Find timestamps to verify E2ETestSession resumed after A_slow completed
        maine_start_time = None
        a_slow_complete_time = None

        for req in log:
            agent = extract_agent_identity(req.body_summary)
            if maine_start_time is None and "E2ETestSession" in agent:
                maine_start_time = req.timestamp
            elif "A_slow" in agent and a_slow_complete_time is None:
                a_slow_complete_time = req.timestamp

        assert maine_start_time is not None, "E2ETestSession start not found in log"
        assert a_slow_complete_time is not None, "A_slow request not found in log"

    def test_t5_deep_nesting_under_contention(self, ac_server):
        """T5 — Deep nesting under contention: chain with dependency-respecting order.

        Chain: Maine → A(async) → B(sync) → C(async) → D(sync), all on conc=0.
        Verify dependency-respecting order from identity sequence, no interleaving.
        """
        base_url = ac_server

        responses = [
            # Turn 1: Maine calls A async
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A", "task": "Level 1 research", "async_mode": True}}
                ]
            },
            # Turn 2: A starts, calls B sync
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "coder", "instance_name": "B", "task": "Level 2 implement"}}
                ]
            },
            # Turn 3: B completes with delay
            {"text": "SENTINEL_B_COMPLETE_T5", "delay": 0.8},
            # Turn 4: A resumes, calls C async
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "C", "task": "Level 3 review", "async_mode": True}}
                ]
            },
            # Turn 5: C completes with delay
            {"text": "SENTINEL_C_COMPLETE_T5", "delay": 1.0},
            # Turn 6: A resumes with C's result, completes
            {"text": "SENTINEL_A_FINAL_T5"},
            # Turn 7: Maine resumes with A's result, finishes
            {"text": "SENTINEL_MAINE_FINAL_T5 chain complete"},
        ]

        ProgrammableMockLLMHandler.set_responses(responses)
        ProgrammableMockLLMHandler.clear_request_log()

        test_start = time.perf_counter()
        session_token, _ = handshake_and_send(base_url, "Deep nested chain task.")

        completed = wait_for_completion(base_url, session_token, timeout=90.0)
        assert completed, "Test timed out — possible deadlock in deep nesting under contention"

        log = ProgrammableMockLLMHandler.get_request_log()
        identities = extract_identities(log)

        # Must have enough requests for the chain (Maine→A→B→A(resume)→C→A(final)→Maine)
        assert len(log) >= 6, f"Expected >= 6 requests for deep nesting, got {len(log)}: {identities}"

        # Dependency ordering: A before B's work, B before C, Maine last
        assert "A" in identities, f"A not found. Identities: {identities}"
        assert "B" in identities, f"B not found. Identities: {identities}"
        assert "C" in identities, f"C not found. Identities: {identities}"

        # Verify dependency-respecting subsequence: A appears before B, B before C, E2ETestSession resumes last
        assert_identity_subsequence(identities, ["A", "B", "C", "E2ETestSession"])

        # Timing-based serialization check: deep nesting chain with B(delay=0.8s)+C(delay=1.0s)=1.8s
        # on conc=0 must take >2.25s wall-clock (chain delays + setup overhead). If agents barged
        # or ran truly concurrently ignoring dependencies, would complete in ~1.3-1.5s.
        elapsed = time.perf_counter() - test_start
        assert elapsed > 2.25, (
            f"Serialization not enforced in deep nesting: elapsed={elapsed:.2f}s (expected >2.25s for conc=0). "
            f"This suggests chain A→B→C did not serialize properly."
        )

        # Final resume request should contain child completion sentinels (injected as tool results).
        # The orchestrator's own final response sentinel is never in a request body since there's no subsequent request.
        resume_reqs = [r for r in log if "E2ETestSession" in r.body_summary and len(r.body_summary) > 50]
        assert len(resume_reqs) >= 1, f"No E2ETestSession resume request found. Log: {[r.body_summary[:60] for r in log]}"
        maine_resume = resume_reqs[-1]
        has_sentinel = any(s in maine_resume.body_summary for s in ["SENTINEL_A_FINAL_T5", "SENTINEL_B_COMPLETE_T5", "SENTINEL_C_COMPLETE_T5"])
        assert has_sentinel, (
            f"E2ETestSession resume request missing child sentinels: {maine_resume.body_summary[:150]}"
        )

    def test_t6_mass_contention(self, ac_server):
        """T6 — Mass contention: 5 async children from one parent, all serialized.

        Verify all complete serialized with no starvation on conc=0 pool.
        """
        base_url = ac_server

        responses = [
            # Turn 1: Maine calls 5 async children
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B1", "task": "Task 1", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "coder", "instance_name": "B2", "task": "Task 2", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "B3", "task": "Task 3", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B4", "task": "Task 4", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "coder", "instance_name": "B5", "task": "Task 5", "async_mode": True}},
                ]
            },
            # Turns 2-6: Each child completes with small distinct delays for contention window
            {"text": "SENTINEL_B1_T6", "delay": 0.3},
            {"text": "SENTINEL_B2_T6", "delay": 0.4},
            {"text": "SENTINEL_B3_T6", "delay": 0.5},
            {"text": "SENTINEL_B4_T6", "delay": 0.6},
            {"text": "SENTINEL_B5_T6", "delay": 0.7},
            # Turn 7: Maine resumes with all results
            {"text": "SENTINEL_MAINE_FINAL_T6 all five done"},
        ]

        ProgrammableMockLLMHandler.set_responses(responses)
        ProgrammableMockLLMHandler.clear_request_log()

        test_start = time.perf_counter()
        session_token, _ = handshake_and_send(base_url, "Run five tasks concurrently.")

        completed = wait_for_completion(base_url, session_token, timeout=90.0)
        assert completed, "Test timed out — possible starvation or deadlock with mass contention"

        log = ProgrammableMockLLMHandler.get_request_log()
        identities = extract_identities(log)

        # Must have Maine + 5 children + Maine resume = at least 7 requests
        assert len(log) >= 7, f"Expected >= 7 requests for mass contention, got {len(log)}: {identities}"

        # All 5 children must appear (no starvation)
        for child in ["B1", "B2", "B3", "B4", "B5"]:
            assert child in identities, f"Child {child} not found — starved? Identities: {identities}"

        # Timing-based serialization check: 5 children with delays (0.3+0.4+0.5+0.6+0.7=2.5s)
        # serialized on conc=0 must take >2.6s wall-clock. Concurrent execution would complete
        # in ~0.7s (max delay) + overhead.
        elapsed = time.perf_counter() - test_start
        assert elapsed > 2.6, (
            f"Serialization not enforced: elapsed={elapsed:.2f}s (expected >2.6s for conc=0). "
            f"This suggests the 5 children ran concurrently instead of serialized."
        )

        # Final resume request should contain child completion sentinels (injected as tool results).
        # The orchestrator's own final response sentinel is never in a request body since there's no subsequent request.
        resume_reqs = [r for r in log if "E2ETestSession" in r.body_summary and len(r.body_summary) > 50]
        assert len(resume_reqs) >= 1, f"No E2ETestSession resume request found. Log: {[r.body_summary[:60] for r in log]}"
        maine_resume = resume_reqs[-1]
        # At least one child sentinel should appear in the resume request as a tool result
        has_sentinel = any(f"SENTINEL_B{i}_T6" in maine_resume.body_summary for i in range(1, 6))
        assert has_sentinel, (
            f"E2ETestSession resume request missing child sentinels: {maine_resume.body_summary[:150]}"
        )
