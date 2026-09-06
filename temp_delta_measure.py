"""Focused measurement: which stream_update frame field grows with conversation length?

Boots the real app in-process (like the e2e fixture) with delta ON, drives a few turns,
and breaks down each captured frame's bytes by top-level field + per-instance messages.
Run:  AGENT_CASCADE_STREAM_DELTA=1 python temp_delta_measure.py
"""
import os, sys, json, time, threading, statistics
from pathlib import Path

os.environ["AGENT_CASCADE_STREAM_DELTA"] = "1"
sys.path.insert(0, str(Path(__file__).parent))

# Reuse the e2e module's mock server + seed builder (import without running tests).
import importlib.util
spec = importlib.util.spec_from_file_location(
    "e2e", Path(__file__).parent / "tests" / "test_streaming_fullstack_e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)

from agent_cascade.api_server import create_app
from agent_cascade.agent_pool import AgentPool
from agent_cascade.agent_factory import load_agent
from agent_cascade.api_router import APIEndpoint, APIRouter
from agent_cascade.api_integration_pkg.state_builder import build_stream_update_from_pool

AC_ROOT = Path(__file__).parent
INSTANCE_NAME = e2e.INSTANCE_NAME

# ── Boot mock LLM server ──────────────────────────────────────────────
mock_server = e2e.HTTPServer(("127.0.0.1", 0), e2e._MockLLMHandler)
host, port = mock_server.server_address
mock_url = f"http://{host}:{port}/v1"
threading.Thread(target=mock_server.serve_forever, daemon=True).start()

# ── Build pool + app (mirror the fixture) ─────────────────────────────
llm_cfg = {"model": "mock-model", "model_server": mock_url, "api_key": "EMPTY",
           "model_type": "qwenvl_oai", "max_input_tokens": 1_000_000}
agents_dir = AC_ROOT / "agents" if (AC_ROOT / "agents").exists() else e2e.PROJECT_ROOT / "agents"
pool = AgentPool(llm_cfg, agents_dir=str(agents_dir))
with pool.api_router._lock:
    pool.api_router.endpoints.clear(); pool.api_router.agent_priorities.clear()
    pool.api_router._agent_types_with_priorities.clear()
mock_ep = APIEndpoint(id="mock-endpoint", name="Mock LLM Server", api_base=mock_url,
                      model="mock-model", concurrency_limit=0, enabled=True, max_input_tokens=1_000_000)
pool.api_router.add_endpoint(mock_ep)
pool.api_router.default_llm_cfg = mock_ep.to_llm_cfg()
for _at in ("researcher","coder","generalist","orchestrator","compressor","reviewer","security","writer","ponytail"):
    try: pool.api_router.set_agent_priorities(_at, [mock_ep.id])
    except Exception: pass
researcher = load_agent(pool, "researcher", llm_cfg)
pool.templates["researcher"] = researcher

# ── Seed the instance via production loader ───────────────────────────
import tempfile
tmpd = Path(tempfile.mkdtemp())
seed = e2e._build_seed_with_turns(tmpd)
status = pool.load_session_from_log(str(seed), target_instance=INSTANCE_NAME, clear_sub_agents_before_load=False)
print(f"[measure] seed status: {status[:80]}")
inst = pool.get_instance(INSTANCE_NAME)
if inst is None:
    sys.exit("instance not found after seed")
conv_len = len(inst.conversation)
print(f"[measure] seeded conversation length = {conv_len}")
# DIAGNOSTIC: show the tail roles + safe_start to confirm whether the delta cut can engage.
from agent_cascade.api_integration_pkg import state_builder as _sb
def role_of(m):
    return (m.get("role") if isinstance(m, dict) else getattr(m, "role", "") or "").lower()
_msgs_diag = list(inst.conversation)
_roles_tail = [role_of(m) for m in _msgs_diag[-6:]]
print(f"[measure] TAIL ROLES (last 6): {_roles_tail}")
print(f"[measure] safe_tail_start_index(seeded conv) = {_sb._safe_tail_start_index(_msgs_diag)}")
# DIAGNOSTIC: confirm both flag values as seen by the two modules.
import agent_cascade.api_integration_pkg.state_builder as _sb2
print(f"[measure] state_builder.STREAM_DELTA_ENABLED = {_sb2.STREAM_DELTA_ENABLED}, TAIL_COMMITTED={_sb2.TAIL_COMMITTED}")

# ── Drive turns, capturing frames + byte breakdowns ───────────────────
def field_bytes(d):
    out = {}
    for k, v in d.items():
        try: out[k] = len(json.dumps(v, ensure_ascii=False).encode())
        except Exception: out[k] = -1
    return out

from agent_cascade.api_integration_pkg import state_builder as _sb
from agent_cascade.llm.schema import Message

def role_of(m):
    return (m.get("role") if isinstance(m, dict) else getattr(m, "role", "") or "").lower()

frames = []  # (turn_idx, conv_len, total_bytes, field_breakdown, active_msgs_count)
# Scenario A: conversation as-is (ends in tool chain -> expect full send)
for turn in range(2):
    with inst._compression_lock:
        msgs_now = list(inst.conversation)
        print(f"[measure] SCENARIO A turn {turn}: last 3 roles = {[role_of(m) for m in msgs_now[-3:]]}, "
              f"safe_start={_sb._safe_tail_start_index(msgs_now)}")
        inst._streaming_responses = [Message(role="assistant", content="", reasoning_content=f"reasoning chunk {turn} " * 20)]
    frame = build_stream_update_from_pool(pool, INSTANCE_NAME, responses=None, force_full=False)
    if frame is None:
        print(f"[measure] turn {turn}: frame is None")
        continue
    raw = len(json.dumps(frame, ensure_ascii=False).encode())
    fb = field_bytes(frame)
    ai = (frame.get("agent_instances") or frame.get("instances") or {})
    active = ai.get(INSTANCE_NAME, {})
    n_msgs = len(active.get("messages", [])) if isinstance(active, dict) else -1
    frames.append((turn, conv_len, raw, fb, n_msgs))
    print(f"[measure] SCENARIO A turn {turn}: total={raw}B msgs={n_msgs}")

# Scenario B: append a PLAIN assistant message (no tool call) so the tail cut can engage.
# This simulates the common case where a turn ends with a plain text answer.
with inst._compression_lock:
    conv = list(inst.conversation)
    # add 20 plain user/assistant pairs to grow history, ending on a plain assistant
    for k in range(20):
        conv.append({"role": "user", "content": f"follow-up question {k} with some padding text here"})
        conv.append({"role": "assistant", "content": f"plain answer {k} to the follow-up, no tool call involved."})
    inst.conversation = conv
    print(f"[measure] SCENARIO B: appended 20 plain pairs -> conv_len={len(conv)}, last role={role_of(conv[-1])}, safe_start={_sb._safe_tail_start_index(conv)}")

for turn in range(4):
    with inst._compression_lock:
        msgs_now = list(inst.conversation)
        inst._streaming_responses = [Message(role="assistant", content="", reasoning_content=f"reasoning chunk B{turn} " * 20)]
    frame = build_stream_update_from_pool(pool, INSTANCE_NAME, responses=None, force_full=False)
    if frame is None:
        print(f"[measure] SCENARIO B turn {turn}: frame is None")
        continue
    raw = len(json.dumps(frame, ensure_ascii=False).encode())
    fb = field_bytes(frame)
    ai = (frame.get("agent_instances") or frame.get("instances") or {})
    active = ai.get(INSTANCE_NAME, {})
    n_msgs = len(active.get("messages", [])) if isinstance(active, dict) else -1
    frames.append((f"B{turn}", len(msgs_now), raw, fb, n_msgs))
    print(f"[measure] SCENARIO B turn {turn}: total={raw}B msgs={n_msgs} (conv_len={len(msgs_now)})")

print("\n=== SUMMARY (bytes per field across turns) ===")
if frames:
    keys = set()
    for f in frames: keys.update(f[3].keys())
    print(f"{'field':<20}" + "".join(f" t{f[0]:>4}={f[3].get(k,0):>9}" for k in ["instances"] for f in [frames[0]]))
    # simpler: table
    hdr = "field".ljust(20) + "".join(f"t{i}".rjust(11) for i in range(len(frames)))
    print(hdr)
    for k in sorted(keys):
        row = k.ljust(20)
        for f in frames:
            row += str(f[3].get(k, 0)).rjust(11)
        print(row)
    print("total".ljust(20) + "".join(str(f[2]).rjust(11) for f in frames))
    print("msgs".ljust(20) + "".join(str(f[4]).rjust(11) for f in frames))
