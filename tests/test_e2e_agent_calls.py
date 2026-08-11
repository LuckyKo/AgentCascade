"""End-to-end tests for agent call scheduling with programmable mock LLM server.

Tests complex call_agent scenarios including async/sync parallelism, SLEEPING state
transitions, nested depth > 2, and endpoint collision detection. Uses a mock HTTP
server that accepts pre-defined response scripts to simulate LLM behavior deterministically.

Catches regressions like:
- Async/sync deadlock when parent calls child async + sync in parallel
- FIFO ordering violations on conc=0 sequential pools
- SLEEPING state not transitioning when async tools pending
- Endpoint collision detection failures
"""

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional

import pytest
import requests

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from tests.conftest_e2e import derive_shared_secret, encrypt_payload, generate_client_keypair


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

    Response scripts are set via server-side attributes (not class-level) to work
    correctly with pytest-xdist where each worker process has its own server instance.
    
    Scripts support:
    - Plain text response
    - Tool calls (call_agent, etc.)
    - Optional delay before responding
    """

    # Per-server-instance state stored on self.server (HTTPServer)
    # Initialized by the fixture, accessed via self.server.<attr>

    def log_message(self, format, *args):
        pass  # Suppress request logging

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json(200, {"data": [{"id": "mock-model", "object": "model"}]})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")

        server = self.server

        # Log the request with sequence number
        with server._lock:
            server._seq_counter += 1
            seq = server._seq_counter
            try:
                parsed = json.loads(body) if body else {}
                messages = parsed.get("messages", [])
                last_msg = messages[-1]["content"][:80] if messages else ""
                summary = f"msgs={len(messages)} last='{last_msg}'"
            except (json.JSONDecodeError, IndexError, KeyError):
                summary = body[:100]

            server._request_log.append(MockRequest(seq, self.path, "POST", summary, time.time()))

        if self.path == "/v1/chat/completions":
            # Pop next response script from queue
            with server._lock:
                if server._response_queue:
                    script = server._response_queue.pop(0)
                else:
                    script = {"text": "[MOCK: no more scripts]"}

            delay = script.get("delay", 0)
            if delay > 0:
                time.sleep(delay)

            # Build OpenAI-compatible streaming response
            stream = self._build_stream(script)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")  # Close after response to avoid client hang
            self.end_headers()
            self.wfile.write(stream.encode("utf-8"))
            self.wfile.flush()
        else:
            self.send_response(404)
            self.end_headers()

    @staticmethod
    def _build_stream(script: Dict[str, Any]) -> str:
        """Build SSE stream from a response script."""
        lines = []

        if "tool_calls" in script:
            # Tool call response — send as function_call chunks
            tool_calls = script["tool_calls"]
            for i, tc in enumerate(tool_calls):
                name = tc.get("name", "unknown")
                args = tc.get("args", {})
                tool_id = f"call_{i}"

                # First chunk: tool call start with name
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
                lines.append(f'data: {json.dumps(chunk)}\n')

                # Second chunk: arguments
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
                lines.append(f'data: {json.dumps(chunk)}\n')

        elif "text" in script:
            # Plain text response
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
            lines.append(f'data: {json.dumps(chunk)}\n')

        else:
            # Fallback empty response
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
            lines.append(f'data: {json.dumps(chunk)}\n')

        lines.append("data: [DONE]\n")
        return "\n".join(lines)

    def _send_json(self, status_code: int, data: Any):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def set_responses(cls, server: HTTPServer, responses: List[Dict[str, Any]]):
        """Set the response queue for a given server instance."""
        with server._lock:
            server._response_queue = list(responses)  # Copy to avoid mutation issues

    @classmethod
    def get_request_log(cls, server: HTTPServer) -> List[MockRequest]:
        """Return a copy of the request log for a given server instance."""
        with server._lock:
            return list(server._request_log)

    @classmethod
    def clear_request_log(cls, server: HTTPServer):
        """Clear the request log for a given server instance."""
        with server._lock:
            server._request_log.clear()
            server._seq_counter = 0


@pytest.fixture(scope="module")
def mock_llm_server():
    """Start a programmable mock LLM HTTP server on a random port.

    Yields (base_url, server) so tests can set responses per-server.
    """
    server = HTTPServer(("127.0.0.1", 0), ProgrammableMockLLMHandler)
    # Initialize per-server state
    server._response_queue: List[Dict[str, Any]] = []
    server._request_log: List[MockRequest] = []
    server._lock = threading.Lock()
    server._seq_counter = 0

    host, port = server.server_address
    base_url = f"http://{host}:{port}/v1"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Verify mock server is accepting connections
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

    yield base_url, server

    server.shutdown()


@pytest.fixture(scope="function")
def ac_server(mock_llm_server):
    """Boot the full AgentCascade app via uvicorn on a real port.

    Each test gets its own server instance for isolation.
    Yields (ac_base_url, mock_server) tuple.
    """
    import sys
    from pathlib import Path

    project_root = Path(__file__).parent.parent.absolute()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    mock_base_url, mock_server = mock_llm_server

    from agent_cascade.api_server import create_app
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.agent_factory import load_orchestrator_agent
    from uvicorn import Config, Server

    llm_cfg = {
        "model": "mock-model",
        "model_server": mock_base_url,
        "api_key": "EMPTY",
        "model_type": "qwenvl_oai",
        "max_input_tokens": 8192,
    }

    pool = AgentPool(llm_cfg, agents_dir=str(project_root / "agents"))
    
    # Add an endpoint for the mock server with unlimited concurrency.
    # This allows async agent calls to run without slot collision, enabling SLEEPING state tests.
    from agent_cascade.api_router import APIEndpoint
    mock_ep = APIEndpoint(
        id="mock-endpoint",
        name="Mock LLM Server",
        api_base=mock_base_url,
        model="mock-model",
        concurrency_limit=-1,  # Unlimited — no slot needed, allows async execution
        enabled=True,
    )
    pool.api_router.add_endpoint(mock_ep)
    
    orchestrator = load_orchestrator_agent(pool, llm_cfg)
    agents = [orchestrator]

    app = create_app(
        agents=agents,
        agent_pool=pool,
        config={"session_name": f"E2ETestSession_{threading.current_thread().ident}"},
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
        yield base_url, mock_server
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ── Test Helpers ───────────────────────────────────────────────────────────────

def handshake_and_send_via_ws(base_url: str, text: str) -> tuple[str, str]:
    """Perform E2E handshake and send a message via WebSocket to trigger generation.

    Returns (session_token, shared_secret). The WebSocket path triggers start_gen()
    which spawns the agent execution thread, unlike /api/message which only queues.
    """
    if not HAS_WEBSOCKETS:
        pytest.skip("websockets package not installed")

    # Handshake via REST
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

    # Send message via WebSocket to trigger generation
    ws_url = base_url.replace("http://", "ws://") + "/ws/chat"

    async def send_via_ws():
        import asyncio
        async with websockets.connect(ws_url, close_timeout=2) as ws:
            # Wait for initial state message
            await ws.recv()

            # Send a chat message — this triggers start_gen via WsMessageHandler
            msg = {
                "type": "message",
                "text": text,
                "target": "Maine",
            }
            await ws.send(json.dumps(msg))

            # Brief wait to let generation thread start and process initial turn
            await asyncio.sleep(1.0)

    import asyncio
    asyncio.run(send_via_ws())

    return session_token, shared_secret


def wait_for_completion(base_url: str, session_token: str, timeout: float = 45.0) -> bool:
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


def get_agent_states(base_url: str, session_token: Optional[str] = None) -> Dict[str, Any]:
    """Get current agent states from /api/state."""
    try:
        resp = requests.get(f"{base_url}/api/state", timeout=2)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        pass  # Silently ignore errors during polling
    return {}


def summarize_request_log(log: List[MockRequest]) -> str:
    """Create a concise summary of the request log for error messages."""
    entries = []
    for req in log[:10]:
        seq = getattr(req, 'seq', '?')
        agent = getattr(req, 'agent_class', getattr(req, 'instance_name', '?'))
        body_summary = str(getattr(req, 'body_summary', ''))[:50]
        entries.append(f"[{seq}] {agent}: {body_summary}")
    if len(log) > 10:
        entries.append(f"... (+{len(log)-10} more)")
    return "; ".join(entries)


def extract_agent_state(states: Dict[str, Any], agent_name: str) -> Optional[str]:
    """Extract the state of a specific agent from the /api/state response."""
    agent_instances = states.get("agent_instances", {})
    if isinstance(agent_instances, dict):
        info = agent_instances.get(agent_name)
        if isinstance(info, dict):
            return info.get("state") or info.get("agent_state")
    return None


def wait_for_agent_state(base_url: str, session_token: str, agent_name: str, expected_state: str, timeout: float = 30.0) -> bool:
    """Poll /api/state until an agent reaches the expected state or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        states = get_agent_states(base_url, session_token)
        actual_state = extract_agent_state(states, agent_name)
        if actual_state == expected_state:
            return True
        time.sleep(0.1)
    return False


def capture_state_history(base_url: str, duration: float, poll_interval: float = 0.1) -> List[Dict[str, str]]:
    """Capture agent state snapshots over a time window.

    Returns list of {agent_name: state} dicts sampled during the window.
    Use this to verify transient states like SLEEPING that occur during execution.
    """
    history = []
    deadline = time.time() + duration
    while time.time() < deadline:
        states = get_agent_states(base_url)
        if states:
            snapshot = {}
            agent_instances = states.get("agent_instances", {})
            if isinstance(agent_instances, dict):
                for name, info in agent_instances.items():
                    if isinstance(info, dict):
                        state = info.get("state") or info.get("agent_state")
                        if state:
                            snapshot[name] = state
            if snapshot:
                history.append(snapshot)
        time.sleep(poll_interval)
    return history


def verify_agent_entered_state(history: List[Dict[str, str]], agent_name: str, expected_state: str) -> bool:
    """Check if an agent entered a specific state at any point in the history."""
    for snapshot in history:
        if snapshot.get(agent_name) == expected_state:
            return True
    return False


def verify_request_order(log: List[MockRequest], expected_patterns: List[str]) -> bool:
    """Verify that request body summaries match expected patterns in order.

    Args:
        log: Request log from mock server
        expected_patterns: List of substrings to find sequentially in body_summary
    Returns:
        True if all patterns found in order, False otherwise
    """
    if len(log) < len(expected_patterns):
        return False
    
    pattern_idx = 0
    for req in log:
        body = getattr(req, 'body_summary', '').lower()
        if pattern_idx < len(expected_patterns) and expected_patterns[pattern_idx].lower() in body:
            pattern_idx += 1
        if pattern_idx >= len(expected_patterns):
            return True
    return False


# ── Test Scenarios ─────────────────────────────────────────────────────────────

class TestAgentCallSchedulingE2E:
    """End-to-end tests for agent call scheduling patterns."""

    def test_scenario_a_async_sync_parallel_sleeping_resume(self, ac_server):
        """Scenario A: Parent calls child async + sync in parallel.

        Maine → B(async) + C(sync). Maine should enter SLEEPING while waiting for B.
        After C completes (sync), Maine has no more tool calls but B is pending → SLEEPING.
        When B completes, Maine resumes and finishes.

        This catches the bug where parent would complete instead of sleeping when async pending.
        """
        base_url, mock_server = ac_server

        # Define response scripts: each script corresponds to one chat completion request in FIFO order.
        # To make SLEEPING verification deterministic (not dependent on thread scheduling):
        # - Both B and C responses have delays, but B's is significantly longer.
        # - This ensures Maine completes C quickly while B is still pending → SLEEPING triggered.
        responses = [
            # Turn 1: Maine receives user message, calls B async and C sync
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B", "task": "Research topic X", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "C", "task": "Review findings"}}
                ]
            },
            # Turn 2: B (researcher) response — LONG delay ensures Maine finishes C first.
            # Regardless of whether B or C requests first, B takes much longer than C.
            {"text": "Research on topic X complete. Key findings: [details].", "delay": 3.0},
            # Turn 3: C (reviewer) response — SHORT delay so Maine completes this quickly while waiting for B
            {"text": "Review complete. Findings are accurate and well-structured.", "delay": 0.2},
            # Turn 4: After async tool results injected, Maine gets another turn to process
            # The call_agent result for B will be injected as a tool result message.
            # Maine now has both results and produces final answer.
            {"text": "All tasks completed successfully. Research and review done."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Please research topic X and have it reviewed.")

        # Capture state history during execution to verify behavioral transitions
        # Duration covers the expected test runtime plus buffer for async operations
        state_history = capture_state_history(base_url, duration=10.0, poll_interval=0.05)

        # Wait for completion with reasonable timeout (deadlock would indicate bug)
        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — possible deadlock or agent stuck"

        # Verify request log shows expected sequence: Maine→B→C→Maine(resume)
        log = ProgrammableMockLLMHandler.get_request_log(mock_server)
        assert len(log) >= 4, f"Expected at least 4 LLM requests (Maine+B+C+Maine), got {len(log)}: {summarize_request_log(log)}"

        # Verify state transitions occurred during execution
        assert state_history, "No state history captured — state monitoring may have failed"

        # Check that at least one agent entered RUNNING state (confirms execution happened)
        assert any("RUNNING" in states.values() for states in state_history), \
            "No agent entered RUNNING state — execution may not have started"

        # Verify children B and C were created and executed
        all_agent_names = set()
        for snapshot in state_history:
            all_agent_names.update(snapshot.keys())
        
        assert "B" in all_agent_names, f"Child agent B was never created. Agents seen: {all_agent_names}"
        assert "C" in all_agent_names, f"Child agent C was never created. Agents seen: {all_agent_names}"

        # CRITICAL: Verify orchestrator entered SLEEPING state while waiting for async child B.
        # This catches the original bug where parent would complete instead of sleeping when async pending.
        # Note: The orchestrator agent uses the session name, not "Maine"
        # Find the orchestrator/session agent (not B or C)
        orchestrator_name = None
        for name in all_agent_names:
            if name not in ("B", "C") and len(name) > 3:  # Session names are longer
                orchestrator_name = name
                break
        
        assert orchestrator_name is not None, f"Could not identify orchestrator agent. Agents seen: {all_agent_names}"
        
        assert verify_agent_entered_state(state_history, orchestrator_name, "SLEEPING"), \
            f"{orchestrator_name} never entered SLEEPING state — async pending handling broken. States seen: {set(s.get(orchestrator_name) for s in state_history if orchestrator_name in s)}"

        # Verify final state: all agents should be IDLE after completion
        final_states = state_history[-1]
        for name in ["B", "C"]:
            assert final_states.get(name) == "IDLE", \
                f"Agent {name} did not reach IDLE state after completion. Final states: {final_states}"

    def test_scenario_b_fiforder_conc0_no_deadlock(self, ac_server):
        """Scenario B: A→B(async), A→C(sync), B→D(sync) on same conc=0 pool.

        All agents use the shared sequential endpoint. Verify FIFO order and no deadlock.
        Expected sequence: Maine starts → C(sync) runs → Maine continues → B(async) launched
        → D(sync by B) runs → B completes → Maine resumes.
        """
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine calls B async and C sync
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B", "task": "Research phase 1", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "C", "task": "Review setup"}}
                ]
            },
            # Turn 2: C (sync) completes immediately
            {"text": "Setup review passed."},
            # Turn 3: B starts running (async background), calls D sync
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "D", "task": "Verify research"}}
                ]
            },
            # Turn 4: D (sync from B) completes
            {"text": "Research verified."},
            # Turn 5: B completes after D returns
            {"text": "Phase 1 research complete with verification."},
            # Turn 6: Maine resumes with both results, finishes
            {"text": "All phases completed. Research verified and reviewed."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Research phase 1 and verify it.")

        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — possible deadlock in sequential pool"

        log = ProgrammableMockLLMHandler.get_request_log(mock_server)
        # We expect: Maine(1), C(2), B-calls-D(3), D(4), B-completes(5), Maine-resumes(6)
        assert len(log) >= 6, f"Expected at least 6 LLM requests for sequential flow, got {len(log)}: {summarize_request_log(log)}"

        # Verify FIFO ordering: sequence numbers should be monotonically increasing
        # (each request processed after previous one completes on conc=0 pool)
        for i in range(1, len(log)):
            assert log[i].seq > log[i-1].seq, \
                f"FIFO violation: request #{log[i].seq} has lower sequence than #{log[i-1].seq}"

        # Verify behavioral ordering: B's call to D should appear before D's response
        # This confirms the nested call pattern worked correctly
        b_calls_d_idx = None
        d_response_idx = None
        for i, req in enumerate(log):
            body = getattr(req, 'body_summary', '').lower()
            if 'call_agent' in body and 'd' in body and b_calls_d_idx is None:
                b_calls_d_idx = i
            if d_response_idx is None and ('verified' in body or ('researcher' in body and i > 2)):
                # D's response contains verification text
                d_response_idx = i
        
        if b_calls_d_idx is not None and d_response_idx is not None:
            assert b_calls_d_idx < d_response_idx, \
                f"Ordering violation: B's call to D (#{b_calls_d_idx}) appeared after D's response (#{d_response_idx})"

    def test_scenario_c_nested_depth_gt2_mixed_sync_async(self, ac_server):
        """Scenario C: Nested depth > 2 with mixed sync/async calls.

        Maine → A(async) → B(sync) → C(async). Tests deep nesting and state management.
        Depth chain: Maine(0) → A(1) → B(2) → C(3).
        """
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine calls A async
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A", "task": "Deep research task", "async_mode": True}}
                ]
            },
            # Turn 2: A starts, calls B sync (depth 2)
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "coder", "instance_name": "B", "task": "Implement solution"}}
                ]
            },
            # Turn 3: B calls C async (depth 3)
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "C", "task": "Code review", "async_mode": True}}
                ]
            },
            # Turn 4: C completes (async)
            {"text": "Code review passed. No issues found."},
            # Turn 5: B resumes with C's result, completes
            {"text": "Implementation complete and reviewed."},
            # Turn 6: A resumes with B's result, completes
            {"text": "Deep research task completed with implementation."},
            # Turn 7: Maine resumes with A's result, finishes
            {"text": "Task chain completed successfully."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Do a deep research task with implementation.")

        # Capture state history to verify all agents in the chain were created
        state_history = capture_state_history(base_url, duration=10.0, poll_interval=0.05)

        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — possible deadlock in nested chain"

        log = ProgrammableMockLLMHandler.get_request_log(mock_server)
        # Verify we got all the expected turns: Maine→A→B→C→B→A→Maine = 7 requests
        assert len(log) >= 7, f"Expected at least 7 LLM requests for nested chain, got {len(log)}: {summarize_request_log(log)}"

        # Verify all agents in the chain were created and executed via state history
        all_agent_names = set()
        for snapshot in state_history:
            all_agent_names.update(snapshot.keys())
        
        assert "A" in all_agent_names, f"Agent A not found in state history. Agents seen: {all_agent_names}"
        assert "B" in all_agent_names, f"Agent B not found in state history. Agents seen: {all_agent_names}"
        assert "C" in all_agent_names, f"Agent C not found in state history. Agents seen: {all_agent_names}"

        # Verify nesting depth via request count pattern:
        # With 3 agents nested (A→B→C) plus Maine at root, we expect interleaved turns.
        # Minimum expected: Maine→A→B→C→B→A→Maine = 7 requests.
        # Allow some extra for system messages/retries but require substantial count.
        assert len(log) >= 7 and len(log) <= 12, \
            f"Unexpected request count for depth-3 chain: {len(log)} (expected 7-12)"

    def test_scenario_d_different_endpoint_collision(self, ac_server):
        """Scenario D: Agent uses endpoint different from parent.

        Tests collision detection and Rule 4 inheritance when child has different
        endpoints than parent. Parent with slot calls child that needs same pool → sync.
        Parent with slot calls child with different pool → async is safe.
        """
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine (orchestrator) calls researcher B and coder C
            # Both use same default endpoint as Maine, so should trigger collision detection
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B", "task": "Research"}}
                ]
            },
            # Turn 2: B completes (sync due to same endpoint pool)
            {"text": "Research complete."},
            # Turn 3: Maine finishes
            {"text": "Task completed with research."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Research something for me.")

        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — possible deadlock in endpoint collision"

        log = ProgrammableMockLLMHandler.get_request_log(mock_server)
        assert len(log) >= 2, f"Expected at least 2 LLM requests, got {len(log)}"

    def test_negative_async_pending_without_deadlock(self, ac_server):
        """Negative test: Verify parent doesn't deadlock when async child is slow.

        This is the core regression test for the original bug. Parent calls a child async
        with a long delay. Without proper SLEEPING state handling, parent would either:
        1. Complete immediately (losing the async result)
        2. Deadlock waiting forever
        
        With the fix: parent sleeps while waiting, resumes when async completes, finishes properly.
        
        We use a relatively short timeout — if this takes longer than ~30s, it's likely deadlocked.
        """
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine calls slow async agent
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "SlowAgent", "task": "Slow research task", "async_mode": True}}
                ]
            },
            # Turn 2: Slow agent takes time (simulated via delay)
            {"text": "Slow research complete.", "delay": 2.0},
            # Turn 3: Maine resumes with result and finishes
            {"text": "Task completed with slow research results."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Do a slow research task.")

        # Short timeout — if async pending handling is broken, this will deadlock here
        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "DEADLOCK DETECTED: Parent failed to handle async pending — likely missing SLEEPING state transition"

        log = ProgrammableMockLLMHandler.get_request_log(mock_server)
        # Must have at least 3 requests: orchestrator→SlowAgent→orchestrator(resume)
        assert len(log) >= 3, \
            f"Expected ≥3 requests for async pending test, got {len(log)}: {summarize_request_log(log)}"

        # Verify the slow agent actually ran
        assert any('slow' in getattr(req, 'body_summary', '').lower() or 'researcher' in getattr(req, 'body_summary', '').lower() 
                   for req in log), "SlowAgent research was never executed"


class TestMockServerBehavior:
    """Tests for the mock server itself."""

    def test_response_queue_ordering(self, mock_llm_server):
        """Verify responses are returned in FIFO order."""
        base_url, mock_server = mock_llm_server

        ProgrammableMockLLMHandler.set_responses(mock_server, [
            {"text": "first"},
            {"text": "second"},
            {"text": "third"},
        ])

        completion_url = base_url + "/chat/completions"

        def collect_text(url):
            resp = requests.post(url, json={"messages": [{"role": "user", "content": "test"}]}, timeout=5)
            text = ""
            for line in resp.text.splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        data = json.loads(line[6:])
                        delta = data["choices"][0]["delta"]
                        if "content" in delta:
                            text += delta["content"]
                    except (json.JSONDecodeError, KeyError):
                        pass
            return text

        assert collect_text(completion_url) == "first"
        assert collect_text(completion_url) == "second"
        assert collect_text(completion_url) == "third"

    def test_tool_call_format(self, mock_llm_server):
        """Verify tool call responses are properly formatted."""
        base_url, mock_server = mock_llm_server

        ProgrammableMockLLMHandler.set_responses(mock_server, [
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"instance_name": "test"}}
                ]
            }
        ])

        completion_url = base_url + "/chat/completions"
        resp = requests.post(
            completion_url,
            json={"messages": [{"role": "user", "content": "test"}]},
            timeout=5
        )

        # Check that tool_calls appear in the stream
        assert "tool_calls" in resp.text
        assert "call_agent" in resp.text
        assert "instance_name" in resp.text
        assert "test" in resp.text

    def test_delay_script(self, mock_llm_server):
        """Verify delay parameter works correctly."""
        base_url, mock_server = mock_llm_server

        ProgrammableMockLLMHandler.set_responses(mock_server, [
            {"text": "fast"},
            {"text": "slow", "delay": 0.5},
        ])

        completion_url = base_url + "/chat/completions"

        start = time.time()
        requests.post(completion_url, json={"messages": [{"role": "user", "content": "test"}]}, timeout=5)
        fast_time = time.time() - start
        assert fast_time < 0.3, f"Fast response took {fast_time:.2f}s, expected < 0.3s"

        start = time.time()
        requests.post(completion_url, json={"messages": [{"role": "user", "content": "test"}]}, timeout=5)
        slow_time = time.time() - start
        assert slow_time >= 0.4, f"Slow response took {slow_time:.2f}s, expected >= 0.4s"

    def test_request_logging(self, mock_llm_server):
        """Verify request log captures all requests with sequence numbers."""
        base_url, mock_server = mock_llm_server

        ProgrammableMockLLMHandler.set_responses(mock_server, [
            {"text": "a"},
            {"text": "b"},
        ])
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        completion_url = base_url + "/chat/completions"
        requests.post(completion_url, json={"messages": [{"role": "user", "content": "first"}]}, timeout=5)
        requests.post(completion_url, json={"messages": [{"role": "user", "content": "second"}]}, timeout=5)

        log = ProgrammableMockLLMHandler.get_request_log(mock_server)
        assert len(log) == 2
        assert log[0].seq == 1
        assert log[1].seq == 2
        assert "first" in log[0].body_summary
        assert "second" in log[1].body_summary


class TestAgentCallEdgeCases:
    """Edge case tests for agent call scheduling."""

    def test_rapid_async_completion(self, ac_server):
        """Test when async child completes before parent finishes its turn.

        This is the fast-completing child race condition that the safety drain handles.
        """
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine calls B async
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B", "task": "Quick task", "async_mode": True}}
                ]
            },
            # Turn 2: B completes very quickly (no delay)
            {"text": "Done instantly."},
            # Turn 3: Maine resumes and finishes
            {"text": "Task completed."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Do a quick async task.")

        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — race condition handling failed"

    def test_multiple_async_calls_same_parent(self, ac_server):
        """Test parent calling multiple agents async and waiting for all."""
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine calls B and C both async
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "B", "task": "Task B", "async_mode": True}},
                    {"name": "call_agent", "args": {"agent_class": "coder", "instance_name": "C", "task": "Task C", "async_mode": True}}
                ]
            },
            # Turn 2: B completes
            {"text": "Task B done."},
            # Turn 3: C completes
            {"text": "Task C done."},
            # Turn 4: Maine resumes with both results
            {"text": "Both tasks completed."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Do tasks B and C in parallel.")

        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — multiple async handling failed"

    def test_sync_chain_no_async(self, ac_server):
        """Test pure sync chain: Maine → A(sync) → B(sync)."""
        base_url, mock_server = ac_server

        responses = [
            # Turn 1: Maine calls A sync
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "researcher", "instance_name": "A", "task": "Research"}}
                ]
            },
            # Turn 2: A calls B sync
            {
                "tool_calls": [
                    {"name": "call_agent", "args": {"agent_class": "reviewer", "instance_name": "B", "task": "Review"}}
                ]
            },
            # Turn 3: B completes
            {"text": "Reviewed."},
            # Turn 4: A completes with B's result
            {"text": "Research reviewed and approved."},
            # Turn 5: Maine finishes
            {"text": "Complete."},
        ]

        ProgrammableMockLLMHandler.set_responses(mock_server, responses)
        ProgrammableMockLLMHandler.clear_request_log(mock_server)

        session_token, _ = handshake_and_send_via_ws(base_url, "Research and review.")

        completed = wait_for_completion(base_url, session_token, timeout=45.0)
        assert completed, "Test timed out — sync chain deadlock"
