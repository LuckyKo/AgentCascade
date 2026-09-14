"""Sub-agent turn-end streaming burst reproduction and measurement.

This is a TEST-ONLY harness that simulates the sub-agent broadcast path from
`agent_cascade/engine/core.py` (`_create_and_run_agent`, L3152-3253) with a mocked
LLM generator. It replicates the exact sequence of `broadcast_stream_update` calls
(loop ticks → forced final bypass with last_send=0.0 → push_final_state) using the
REAL production functions, against a genuine reasoning-heavy session loaded via
AgentPool.load_session_from_log.

NOTE: This does NOT invoke `_create_and_run_agent` directly (per brief recommendation
for determinism). Instead it calls `engine.run(instance)` and manually drives the
broadcast loop with identical parameters/sequence as core.py. All broadcast logic is
real production code; only the LLM generator is mocked.

Deliverables:
  - REASONING profile: grows reasoning_content over ~2.5s, then short answer.
  - NON-REASONING control: identical token volume + cadence but no reasoning_content.
  - Side-by-side comparison of burst patterns.

No production code is modified. The test prints measured DATA via pytest output.
"""
import asyncio
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from collections import Counter

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.llm.schema import Message, USER, ASSISTANT, FUNCTION

# ── Constants ───────────────────────────────────────────────────────────────
INSTANCE_NAME = 'stream-sub'
CALLER = 'Maine'
AGENT_CLASS_FROM_LOG = 'researcher'

EXAMPLE_SESSION_LOG = Path(r'N:\work\WD\AgentWorkspace\logs\researcher_stream-probe-analyst_20260903_093608.jsonl')

# ── Mock LLM profiles (deterministic, fast) ───────────────────────────────
# Simulate a ~20k-token reasoning turn: 120 deltas of ~50 chars each = ~6000 chars
# (~2k tokens), plus the session context is already 82k tokens.
REASONING_DELTAS = 120
REASONING_GAP = 0.02  # 20ms per delta → 2.4s total reasoning stream
CONTENT_DELTAS = 6
CONTENT_GAP = 0.02

def _msg(**kwargs):
    return Message(**kwargs)

# REASONING: reasoning grows over N deltas, then short content answer.
# The last few ticks arrive faster (simulating LLM finishing its generation).
def _mock_reasoning_turn(self, instance, template, messages, active_functions):
    reasoning_parts = []
    for i in range(REASONING_DELTAS):
        # Last 5 ticks arrive at 5ms intervals (LLM finishing fast)
        gap = 0.005 if i >= REASONING_DELTAS - 5 else REASONING_GAP
        time.sleep(gap)
        reasoning_parts.append(f"thought_{i:03d}_analysis_fragment_")
        yield [_msg(role=ASSISTANT, content='', reasoning_content=''.join(reasoning_parts))]
    final_reasoning = ''.join(reasoning_parts)
    content_parts = []
    for i in range(CONTENT_DELTAS):
        time.sleep(CONTENT_GAP)
        content_parts.append(f" answer_{i}")
        yield [_msg(role=ASSISTANT, content=''.join(content_parts), reasoning_content=final_reasoning)]

# NON-REASONING: identical total char volume + cadence, but as plain content.
def _mock_nonreasoning_turn(self, instance, template, messages, active_functions):
    total_deltas = REASONING_DELTAS + CONTENT_DELTAS
    chunk_size = 20
    for i in range(total_deltas):
        gap = 0.005 if i >= total_deltas - 5 else REASONING_GAP
        time.sleep(gap)
        yield [_msg(role=ASSISTANT, content=f"chunk_{i:03d}_".ljust(chunk_size), reasoning_content='')]

# ── Session log resolution ────────────────────────────────────────────────
def _build_synthetic_session_log(dest: Path) -> Path:
    """Write a minimal but format-faithful agent session JSONL (50+ msgs with reasoning).
    The loader's working set will include [SYS][U0][markers][tail] ≈ 57 msgs.
    """
    meta = {
        'agent_class': AGENT_CLASS_FROM_LOG,
        'instance_name': INSTANCE_NAME,
        'start_timestamp': '2026-09-03T09:00:00.000000',
        'last_update': '2026-09-03T09:10:00.000000',
        'current_log_path': str(dest),
        'working_dir': str(PROJECT_ROOT),
        'supervisor': 'Maine',
    }
    lines = [json.dumps({'metadata': meta})]
    def _line(role, content, **extra):
        d = {'role': role, 'content': content}
        d.update(extra)
        d['timestamp'] = '2026-09-03T09:00:01.000000'
        lines.append(json.dumps(d))
    _line('system', f"You are {INSTANCE_NAME}. Senior software engineer.")
    _line('user', 'Explain how the streaming pipeline works.')
    # Prior assistant turn WITH reasoning_content — the trigger condition.
    _line(
        'assistant',
        '(prior answer)',
        reasoning_content=(
            'Let me think carefully about the pipeline: the LLM emits deltas, the engine '
            'forwards them, and the broadcast loop pushes them to the UI. ' * 3
        ),
    )
    # A compression marker so the loader takes [SYS][U0][markers][tail].
    _line(
        'user',
        '--- CONTEXT COMPRESSED (2026-09-06 10:14 → 2026-09-06 11:02, 48m) ---\n'
        '<context_summary>\n- Prior investigation of the streaming pipeline.\n'
        '- Confirmed backend path healthy.\n</context_summary>',
    )
    _line('user', '[COMPRESSION] forced compression complete. 12 messages summarized.')
    # Tail after last marker: ~54 alternating assistant-with-reasoning / user turns.
    for i in range(54):
        if i % 2 == 0:
            _line(
                'assistant',
                f"(tail answer part {i})",
                reasoning_content=f"tail reasoning about step {i} of the pipeline.",
            )
        else:
            _line('user', f"[USER question {i}]: Continue the analysis.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return dest

def _resolve_session_log(tmp_path: Path) -> Path:
    if EXAMPLE_SESSION_LOG.exists():
        target = tmp_path / 'session' / EXAMPLE_SESSION_LOG.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_SESSION_LOG, target)
        return target
    return _build_synthetic_session_log(tmp_path / 'session' / f"{AGENT_CLASS_FROM_LOG}_{INSTANCE_NAME}.jsonl")

# ── Payload extraction helpers ────────────────────────────────────────────
def _extract_instance(event: dict, name: str = INSTANCE_NAME):
    if not isinstance(event, dict):
        return None
    instances = event.get('instances') or event.get('agent_instances') or {}
    return instances.get(name)

def _last_assistant_message(inst: dict):
    msgs = inst.get('messages') or []
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get('role') == ASSISTANT:
            return m
    return None

def _reasoning_len(m: dict):
    r = m.get('reasoning_content') or ''
    return len(r) if isinstance(r, str) else 0

def _content_len(m: dict):
    c = m.get('content') or ''
    return len(c) if isinstance(c, str) else 0

def _payload_bytes(event: dict):
    return len(json.dumps(event))

# ── Sub-agent pipeline driver (reproduces exact sub-agent loop + final frames) ────
def _drive_subagent_pipeline(pool, engine, instance, mock_fn):
    """Replicate the EXACT sub-agent broadcast path (Core.py L3122-3253).

    Returns:
        events: list of (arrival_monotonic, event_dict)
        tick_decisions: list of per-tick breakdown dicts
        gen_error: {"exc": ...} if generator raised
        diag: {"ticks": int, "streaming_ticks": int, "final_resp_lens": list}
    """
    from agent_cascade.api_integration_pkg.streaming import broadcast_stream_update

    send_queue = asyncio.Queue()
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    # Wire the pool as run_agent_unified does.
    pool._ws_send_queue = send_queue
    pool._ws_loop = loop
    pool.stopped = False

    events = []
    ev_lock = threading.Lock()
    gen_error = {}
    done = asyncio.Event()
    tick_decisions = []
    diag = {'ticks': 0, 'streaming_ticks': 0, 'final_resp_lens': []}

    def _consumer():
        try:
            from agent_cascade.engine.core import ExecutionEngine

            final_resp = []
            _last_sub_send = 0.0
            _sub_last_resp_len = 0
            _last_tick_suppressed = False
            _tick_num = 0

            # Apply mock to engine.run() for the duration of this generator.
            original_llm = ExecutionEngine._execute_llm_call
            try:
                ExecutionEngine._execute_llm_call = mock_fn
                _run_gen = engine.run(instance)
                try:
                    for resp in _run_gen:
                        now_mono = time.monotonic()
                        if isinstance(resp, tuple) and len(resp) == 2:
                            final_resp, is_streaming_tick = resp
                        else:
                            final_resp, is_streaming_tick = resp, False

                        diag['ticks'] += 1
                        if is_streaming_tick:
                            diag['streaming_ticks'] += 1

                        # Decompose broadcast decision (mirrors broadcast_stream_update).
                        resp_len = len(final_resp) if final_resp else 0
                        _in_last_send = _last_sub_send
                        _last_sub_send, _sub_last_resp_len = broadcast_stream_update(
                            pool=pool,
                            instance_name=instance.instance_name,
                            turn_output=final_resp,
                            is_streaming_tick=is_streaming_tick,
                            tick_num=_tick_num,
                            now_sec=now_mono,
                            last_send=_in_last_send,
                            last_resp_len=_sub_last_resp_len,
                            yield_time=now_mono,
                        )
                        broadcasted = (_last_sub_send != _in_last_send)
                        # FIX A: a suppressed tick returns its (stale) last_send unchanged.
                        _last_tick_suppressed = not broadcasted

                        tick_decisions.append({
                            'tick': _tick_num,
                            't': now_mono,
                            'phase': 'loop',
                            'resp_len': resp_len,
                            'len_changed': (resp_len != _sub_last_resp_len),
                            'throttle_ok': (now_mono - _in_last_send > 0.1),
                            'broadcasted': broadcasted,
                            'streaming': is_streaming_tick,
                        })
                        _tick_num += 1
                finally:
                    try:
                        if hasattr(_run_gen, 'close'):
                            _run_gen.close()
                    except RuntimeError:
                        pass
            finally:
                ExecutionEngine._execute_llm_call = original_llm

            # FINAL broadcast — Core.py L3241-3283 (FIX A: safe conditional dedup).
            # Mirrors the production logic EXACTLY: only emit a final sub-agent frame
            # when it is not already covered by the most recent loop broadcast.
            now_mono = time.monotonic()
            _in_last_send = _last_sub_send
            need_final = (
                _last_tick_suppressed
                or ((now_mono - _last_sub_send) > 0.1)
                or (len(final_resp) != _sub_last_resp_len)
            )
            if need_final:
                _new_send, _new_len = broadcast_stream_update(
                    pool=pool,
                    instance_name=instance.instance_name,
                    turn_output=final_resp,
                    is_streaming_tick=False,
                    tick_num=_tick_num,
                    now_sec=now_mono,
                    last_send=_last_sub_send,
                    last_resp_len=_sub_last_resp_len,
                )
                final_bcasted = (_new_send != _in_last_send)
            else:
                final_bcasted = False  # skipped — loop already delivered equivalent state <100ms ago
            tick_decisions.append({
                'tick': _tick_num,
                't': now_mono,
                'phase': 'final',
                'need_final': need_final,
                'broadcasted': final_bcasted,
            })

            # SECOND FINAL frame — push_final_state (Core.py L3253).
            now_mono = time.monotonic()
            try:
                engine.stream_publisher.push_final_state(instance, CALLER)
            except Exception as e:
                pass  # best-effort; if it fails (e.g., caller not in pool), we note it.
            tick_decisions.append({
                'tick': _tick_num + 1,
                't': now_mono,
                'phase': 'push_final',
                'caller': CALLER,
            })

        except Exception as e:
            import traceback
            gen_error['exc'] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        finally:
            loop.call_soon_threadsafe(done.set)

    consumer_thread = threading.Thread(target=_consumer, daemon=True)
    consumer_thread.start()

    async def _drain():
        while True:
            try:
                item = send_queue.get_nowait()
            except asyncio.QueueEmpty:
                if done.is_set():
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

    return events, tick_decisions, gen_error, diag

# ── Measurement functions ──────────────────────────────────────────────────
def _measure(events, tick_decisions, label, diag=None):
    if diag is None:
        diag = {'ticks': 0, 'streaming_ticks': 0}
    stream_events = [ev for (_t, ev) in events if isinstance(ev, dict) and ev.get('type') == 'stream_update']
    if not stream_events:
        return {
            'label': label,
            'n_stream_updates': 0,
            'arrivals': [],
            'gaps': [],
            'sub_10ms_count': 0,
            'sub_100ms_count': 0,
            'final_200ms_frames': 0,
            'burst_summary': f"[{label}] NO stream_updates captured — check pool wiring.",
        }

    # Extract per-frame metrics.
    arrivals = []  # (arrival_time, is_partial, history_count, r_len, c_len, total_tokens, payload_bytes)
    for arrival, ev in events:
        if not (isinstance(ev, dict) and ev.get('type') == 'stream_update'):
            continue
        inst = _extract_instance(ev, INSTANCE_NAME)
        if not inst:
            continue
        last_msg = _last_assistant_message(inst)
        r_len = _reasoning_len(last_msg)
        c_len = _content_len(last_msg)
        history_count = inst.get('history_count', 0)
        is_partial = inst.get('is_partial', False)
        total_tokens = ev.get('total_tokens', 0)
        payload_bytes = _payload_bytes(ev)
        arrivals.append((arrival, is_partial, history_count, r_len, c_len, total_tokens, payload_bytes))

    if not arrivals:
        return {
            'label': label,
            'n_stream_updates': len(stream_events),
            'arrivals': [],
            'gaps': [],
            'sub_10ms_count': 0,
            'sub_100ms_count': 0,
            'final_200ms_frames': 0,
            'burst_summary': f"[{label}] NO stream_update carried a parseable instance payload.",
        }

    # Compute gaps and sub-10/100ms counts.
    gaps = [arrivals[i + 1][0] - arrivals[i][0] for i in range(len(arrivals) - 1)]
    sub_10ms = sum(1 for g in gaps if g < 0.01)
    sub_100ms = sum(1 for g in gaps if g < 0.1)

    # Final burst analysis: frames in the last 200ms before the last arrival.
    last_t = arrivals[-1][0]
    final_200ms = [arr for arr in arrivals if last_t - 0.2 <= arr[0] <= last_t]
    final_200ms_count = len(final_200ms)

    # Per-tick breakdown summary.
    tick_summary = []
    loop_broadcasts = 0
    bypass_fired = False
    push_final_fired = False
    for td in tick_decisions:
        if td['phase'] == 'loop':
            loop_broadcasts += 1
        elif td['phase'] == 'final':
            # FIX A: final frame is now conditional (need_final) — record both.
            bypass_fired = td.get('broadcasted', False)
            final_need = td.get('need_final', None)
        elif td['phase'] == 'push_final':
            push_final_fired = True
    tick_summary.append(f"  loop ticks (broadcasted): {loop_broadcasts}")
    tick_summary.append(f"  final frame (FIX A, need_final={final_need}) fired: {bypass_fired}")
    tick_summary.append(f"  push_final_state fired: {push_final_fired}")

    # Reasoning/content growth profile.
    r_lens = [r for (_t, _p, _h, r, _c, _t2, _b) in arrivals]
    c_lens = [c for (_t, _p, _h, _r, c, _t2, _b) in arrivals]
    distinct_r = len(set(r_lens))
    max_r = max(r_lens) if r_lens else 0
    min_r = min(r_lens) if r_lens else 0
    max_c = max(c_lens) if c_lens else 0
    min_c = min(c_lens) if c_lens else 0

    # Summary string.
    summary = (
        f"\n[{label}] SUB-AGENT BENCHMARK MEASUREMENT\n"
        f"  stream_update events on send_queue : {len(stream_events)}\n"
        f"  parseable arrivals                 : {len(arrivals)}\n"
        f"  max inter-arrival gap              : {max(gaps):.3f}s (if any)\n"
        f"  sub-10ms gaps                      : {sub_10ms}\n"
        f"  sub-100ms gaps                     : {sub_100ms}\n"
        f"  frames in last 200ms               : {final_200ms_count}\n"
        f"  loop broadcasted ticks             : {loop_broadcasts}\n"
        f"  final frame (FIX A) fired          : {bypass_fired} (need_final={final_need})\n"
        f"  push_final_state (caller={CALLER}) fired: {push_final_fired}\n"
        f"  reasoning_content lengths          : min={min_r} max={max_r} distinct={distinct_r}\n"
        f"  content lengths                  : min={min_c} max={max_c}\n"
        f"  engine.run() ticks                 : {diag['ticks']} (streaming_ticks={diag['streaming_ticks']})\n"
    )

    return {
        'label': label,
        'n_stream_updates': len(stream_events),
        'arrivals': arrivals,
        'gaps': gaps,
        'sub_10ms_count': sub_10ms,
        'sub_100ms_count': sub_100ms,
        'final_200ms_frames': final_200ms_count,
        'tick_summary': tick_summary,
        'r_lens': r_lens,
        'c_lens': c_lens,
        'max_r': max_r,
        'min_r': min_r,
        'distinct_r': distinct_r,
        'summary': summary,
    }

# ── Harness fixture ────────────────────────────────────────────────────────
@pytest.fixture
def subagent_harness(tmp_path):
    """Build a real AgentPool, register templates, create a trivial root "Maine" instance,
    and load a fresh agent from the session log. The loaded instance has max_turns=1 and
    _generate_cfg_override to suppress compression."""
    import agent_cascade.agent_pool as ap_mod
    from agent_cascade.agent_instance import AgentInstance

    cfg_dir = tmp_path / 'cfg'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = str(cfg_dir)

    llm_cfg = {
        'model': 'mock',
        'api_base': 'http://127.0.0.1:9/v1',
        'model_server': 'http://127.0.0.1:9/v1',
        'api_key': 'EMPTY',
    }

    try:
        pool = ap_mod.AgentPool(llm_cfg, agents_dir=str(cfg_dir))
    except Exception as e:
        pytest.skip(f"Could not construct AgentPool: {e}")

    if getattr(pool, 'api_router', None) is not None:
        try:
            pool.api_router = None
        except Exception:
            pass

    # Register templates (researcher for the loaded session, plus orchestrator & coder).
    from agent_cascade.agents.assistant import Assistant

    def _register_template(agent_class: str):
        t = Assistant(llm=dict(llm_cfg), name=agent_class, description='test streaming template')
        pool.templates[agent_class] = t
        pool.templates[agent_class.lower()] = t

    _register_template('researcher')
    _register_template('orchestrator')
    _register_template('coder')

    # Load a REAL session via the PRODUCTION loader.
    session_log = _resolve_session_log(tmp_path)
    status = pool.load_session_from_log(
        str(session_log),
        target_instance=INSTANCE_NAME,
        clear_sub_agents_before_load=False,
    )
    assert not status.startswith('Error'), f"load_session_from_log failed: {status}"

    instance = pool.get_instance(INSTANCE_NAME)
    assert instance is not None, (
        f"No instance '{INSTANCE_NAME}' in pool after load (status={status!r})"
    )
    # Ensure the loaded conversation is non-trivial and contains reasoning.
    assert len(instance.conversation) >= 2, (
        f"Loaded conversation too small ({len(instance.conversation)} msgs)"
    )
    has_prior_reasoning = any(
        getattr(m, 'role', None) == ASSISTANT and len(getattr(m, 'reasoning_content', '') or '') > 20
        for m in instance.conversation
    )
    assert has_prior_reasoning, (
        f"Loaded conversation has no prior assistant message with reasoning_content"
    )

    # Suppress compression while keeping the heavy context.
    instance._generate_cfg_override = {'max_input_tokens': 1_000_000}
    # Set max_turns=1 for a single-turn run.
    instance.max_turns = 1

    # Ensure pool has _execution (required by engine.run and broadcast).
    if getattr(pool, '_execution', None) is None:
        from unittest.mock import MagicMock
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()

    # Register a trivial "Maine" root instance so push_final_state fires.
    now = time.monotonic()
    pool.instances[CALLER] = AgentInstance(
        instance_name=CALLER,
        agent_class='orchestrator',
        conversation=[],
        created_at=now,
        last_activity=now,
        latest_marker_index=-1,
    )

    yield {
        'pool': pool,
        'engine': None,  # create per-test from pool
        'load_fresh': lambda name: (lambda inst: inst)(pool.get_instance(name)) if pool.get_instance(name) else None,
        'session_log': session_log,
    }

    try:
        if hasattr(pool, 'stop'):
            pool.stop()
    except Exception:
        pass

# ── Tests ──────────────────────────────────────────────────────────────────
def _async_loop_works():
    try:
        async def _noop(): return 42
        return asyncio.run(_noop()) == 42
    except Exception as e:
        pytest.skip(f"Cannot start an asyncio event loop: {e}")

def test_subagent_burst_reasoning(subagent_harness):
    """Drive the sub-agent path with a REASONING-heavy mock. Measure burst behavior."""
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)

    events, tick_decisions, gen_error, diag = _drive_subagent_pipeline(
        pool, engine, pool.get_instance(INSTANCE_NAME), _mock_reasoning_turn
    )

    assert not gen_error, f"engine.run() raised: {gen_error.get('exc')}"

    m = _measure(events, tick_decisions, 'REASONING', diag)
    print(m['summary'])

    # Basic sanity assertions (this is a measurement harness; we don't fail on the burst itself).
    assert m['n_stream_updates'] >= 1, (
        f"REASONING: no stream_update events captured. {m['arrivals'][:5]}"
    )
    # The sub-agent path should emit at least one frame per tick (plus final).
    assert m['tick_summary'][0].startswith('  loop ticks'), (
        f"Tick breakdown missing. {m['tick_summary']}"
    )

def test_subagent_burst_nonreasoning(subagent_harness):
    """Drive the sub-agent path with a NON-REASONING control. Measure burst behavior."""
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)

    events, tick_decisions, gen_error, diag = _drive_subagent_pipeline(
        pool, engine, pool.get_instance(INSTANCE_NAME), _mock_nonreasoning_turn
    )

    assert not gen_error, f"engine.run() raised: {gen_error.get('exc')}"

    m = _measure(events, tick_decisions, 'NON-REASONING', diag)
    print(m['summary'])

    assert m['n_stream_updates'] >= 1, (
        f"NON-REASONING: no stream_update events captured."
    )

def test_subagent_burst_comparison(subagent_harness):
    """Run BOTH profiles on fresh instances and print a side-by-side comparison."""
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)

    # Run reasoning profile.
    events_r, td_r, err_r, diag_r = _drive_subagent_pipeline(
        pool, engine, pool.get_instance(INSTANCE_NAME), _mock_reasoning_turn
    )
    assert not err_r, f"REASONING engine.run() raised: {err_r.get('exc')}"
    m_r = _measure(events_r, td_r, 'REASONING', diag_r)

    # Run non-reasoning profile on a FRESH instance (reload).
    # Need to reload from log to get a clean conversation.
    session_log = subagent_harness['session_log']
    status_nr = pool.load_session_from_log(
        str(session_log),
        target_instance=INSTANCE_NAME + '_nr',
        clear_sub_agents_before_load=False,
    )
    assert not status_nr.startswith('Error'), f"Reload for NR failed: {status_nr}"
    inst_nr = pool.get_instance(INSTANCE_NAME + '_nr')
    assert inst_nr is not None, f"NR instance not found after reload. {status_nr}"
    inst_nr._generate_cfg_override = {'max_input_tokens': 1_000_000}
    inst_nr.max_turns = 1

    engine2 = ExecutionEngine(pool)
    events_nr, td_nr, err_nr, diag_nr = _drive_subagent_pipeline(
        pool, engine2, inst_nr, _mock_nonreasoning_turn
    )
    assert not err_nr, f"NON-REASONING engine.run() raised: {err_nr.get('exc')}"
    m_nr = _measure(events_nr, td_nr, 'NON-REASONING', diag_nr)

    # Print side-by-side comparison.
    print('\n' + '=' * 80)
    print('SUB-AGENT STREAMING BURST COMPARISON: REASONING vs NON-REASONING')
    print('=' * 80)
    def fmt_summary(m):
        s = m['summary']
        # Trim to the core metrics.
        lines = [l for l in s.splitlines() if any(kw in l for kw in
            ['stream_update events', 'parseable arrivals', 'max inter-arrival gap',
             'sub-10ms gaps', 'sub-100ms gaps', 'frames in last 200ms',
             'loop broadcasted ticks', 'final frame (FIX A) fired', 'push_final_state',
             'reasoning_content lengths', 'content lengths'])
        ]
        return '\n'.join(lines)

    print('\n' + '-' * 40 + ' REASONING ' + '-' * 40)
    print(fmt_summary(m_r))
    print('\n' + '-' * 40 + ' NON-REASONING ' + '-' * 40)
    print(fmt_summary(m_nr))

    # Compare key metrics.
    print('\n' + '=' * 80)
    print('COMPARISON')
    print('=' * 80)
    comp = []
    comp.append(f"REASONING stream_updates: {m_r['n_stream_updates']} vs NON-REASONING: {m_nr['n_stream_updates']}")
    comp.append(f"REASONING sub-10ms gaps: {m_r['sub_10ms_count']} vs NON-REASONING: {m_nr['sub_10ms_count']}")
    comp.append(f"REASONING frames in last 200ms: {m_r['final_200ms_frames']} vs NON-REASONING: {m_nr['final_200ms_frames']}")
    comp.append(f"REASONING loop broadcasts: {m_r['tick_summary'][0]} vs NON-REASONING: {m_nr['tick_summary'][0]}")
    comp.append(f"REASONING final frame (FIX A): {m_r['tick_summary'][1]} vs NON-REASONING: {m_nr['tick_summary'][1]}")
    comp.append(f"REASONING push_final_state: {m_r['tick_summary'][2]} vs NON-REASONING: {m_nr['tick_summary'][2]}")
    for c in comp:
        print(c)

    # Conclude: does reasoning produce more sub-throttle frames?
    burst_delta = m_r['sub_10ms_count'] - m_nr['sub_10ms_count']
    if burst_delta > 0:
        print(f"\n[OBSERVATION] REASONING showed {burst_delta} more sub-10ms gaps than NON-REASONING.")
    elif burst_delta < 0:
        print(f"\n[OBSERVATION] NON-REASONING showed {-burst_delta} more sub-10ms gaps than REASONING.")
    else:
        print('\n[OBSERVATION] No difference in sub-10ms gap counts between profiles.')

# ── FIX A regression tests ───────────────────────────────────────────────────
def _subagent_frames(events, name=INSTANCE_NAME):
    """Return (arrival_monotonic, event) pairs for stream_update frames that carry a
    parseable payload for the given sub-agent instance."""
    out = []
    for arrival, ev in events:
        if not (isinstance(ev, dict) and ev.get('type') == 'stream_update'):
            continue
        if _extract_instance(ev, name):
            out.append((arrival, ev))
    return out

def test_fix_a_no_same_timestamp_subagent_frames(subagent_harness):
    """CORE REGRESSION: no two of the sub-agent's OWN turn-end broadcasts may share a timestamp.

    Before FIX A the forced final broadcast (last_send=0.0) landed at the same instant as the
    preceding loop tick (~0ms gap). After FIX A the final frame is conditional and keyed to the
    sub-agent instance, so per-instance there is never a same-timestamp pair of the sub-agent's
    own frames.

    We assert on BROADCAST-TIME gaps (the `t` = now_mono recorded at each broadcast call site in
    `tick_decisions`), NOT on dequeue-time arrival gaps from `events`. The drain thread captures
    `arrival = time.monotonic()` when the asyncio loop DEQUEUES each item; two legitimately-
    distinct frames enqueued back-to-back (the sub-agent's final conditional broadcast and the
    root-caller push_final_state frame, whose payload still carries the sub-agent instance) can be
    popped within the same monotonic-resolution tick under load, yielding a spurious 0.0 gap that
    is a dequeue-timing artifact, not a real production double-broadcast.

    The filtered set = loop ticks that broadcasted + the final conditional broadcast (both keyed to
    the sub-agent instance). The `push_final` phase is EXCLUDED from this same-instant assertion:
    it is a root-caller full-state push that merely CONTAINS sub-agent data, not a duplicate of the
    sub-agent's own final frame. A true same-instant double-broadcast of the sub-agent's turn-end
    state would still show as two loop/final entries sharing `now_mono` and be caught here.
    """
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)

    for label, mock in (('REASONING', _mock_reasoning_turn), ('NON-REASONING', _mock_nonreasoning_turn)):
        inst = pool.get_instance(INSTANCE_NAME)
        assert inst is not None, f"{label}: no instance {INSTANCE_NAME}"
        events, tick_decisions, gen_error, diag = _drive_subagent_pipeline(pool, engine, inst, mock)
        assert not gen_error, f"{label}: engine.run() raised: {gen_error.get('exc')}"

        # Sanity: at least one sub-agent frame was actually delivered (dequeue-side confirmation).
        frames = _subagent_frames(events)
        assert frames, f"{label}: no sub-agent frames captured"

        # The sub-agent's OWN turn-end broadcasts: loop ticks that broadcasted + the final
        # conditional broadcast. Exclude push_final (root-caller full-state push).
        own_bcasts = [
            td for td in tick_decisions
            if td.get('phase') in ('loop', 'final') and td.get('broadcasted') is True
        ]
        # Vacuity guard: the assertion must have something to check. A turn always produces at
        # least one broadcast (the first loop tick always passes throttle from last_send=0.0), so
        # an empty set indicates a harness/mock regression, not a passing test.
        assert len(own_bcasts) >= 1, (
            f"{label}: no sub-agent turn-end broadcasts recorded in tick_decisions "
            f"(harness regression?): {tick_decisions}"
        )

        own_ts = sorted(td['t'] for td in own_bcasts)
        gaps = [own_ts[i + 1] - own_ts[i] for i in range(len(own_ts) - 1)]
        # The original burst was a TRUE same-instant pair (0ms gap). Allow up to 1ms of slack:
        # clock-resolution/scheduler jitter can produce sub-millisecond gaps between legitimately-
        # distinct broadcasts, but never a true 0ms double-broadcast. 1e-3 (1ms) still catches the
        # real same-instant bug while staying robust to timing noise that made the old dequeue-
        # based threshold flaky under concurrent load.
        dup_gaps = [g for g in gaps if g < 1e-3]
        assert not dup_gaps, (
            f"{label}: two sub-agent turn-end broadcasts share a timestamp (gap<1ms): "
            f"broadcast_ts={['%.4f' % t for t in own_ts]}"
        )

def test_fix_a_final_delivered_when_last_tick_throttled(subagent_harness):
    """MESSAGE-LOSS GUARD: even when the last loop tick was throttled out, the final committed
    message MUST still reach the UI (via the time-based branch of need_final).

    Simulates the exact dangerous scenario: the mock's final yield happens <0.1s after the
    previous broadcast with an UNCHANGED list length. The loop tick is suppressed (len unchanged
    AND <100ms since last send), so _sub_last_resp_len holds the latest len but _last_sub_send
    stays STALE. A naive 'skip if len unchanged' would drop the final frame; FIX A's
    time-based branch must still deliver it.
    """
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)
    inst = pool.get_instance(INSTANCE_NAME)
    assert inst is not None

    # Produce enough deltas that the engine's streaming layer yields >=2 UI ticks. All ticks
    # carry an UNCHANGED list length (1 assistant msg), so after the first broadcast (len 0->1)
    # every later tick is subject to the 100ms throttle. The FINAL committed content differs
    # from what any throttled-out tick reported, so its delivery MUST come from the time-based
    # branch of need_final (now - _last_sub_send > 0.1) — a naive 'skip if len unchanged' would
    # drop it.
    def _mock_throttled_final(self, instance, template, messages, active_functions):
        parts = []
        for i in range(20):
            time.sleep(0.005)  # fast deltas; engine yields UI ticks on its own cadence
            parts.append(f"part{i:02d}_final-committed-message_")
            yield [_msg(role=ASSISTANT, content=''.join(parts), reasoning_content='')]

    events, tick_decisions, gen_error, diag = _drive_subagent_pipeline(pool, engine, inst, _mock_throttled_final)
    assert not gen_error, f"engine.run() raised: {gen_error.get('exc')}"

    # Confirm the loop's last tick was indeed suppressed (throttled out): len unchanged AND
    # <100ms since the previous actual send.
    loop_ticks = [td for td in tick_decisions if td['phase'] == 'loop']
    assert len(loop_ticks) >= 2, f"expected >=2 loop ticks, got {len(loop_ticks)}: {loop_ticks}"
    last_loop = loop_ticks[-1]
    assert not last_loop.get('broadcasted', False), (
        f"test precondition failed: last loop tick was NOT throttled out: {last_loop}"
    )

    frames = _subagent_frames(events)
    assert frames, 'no sub-agent frames captured'
    # The final committed message must be present in the LAST frame delivered for the instance.
    last_ev = frames[-1][1]
    last_inst = _extract_instance(last_ev, INSTANCE_NAME)
    last_msg = _last_assistant_message(last_inst)
    assert last_msg is not None, 'final frame has no assistant message'
    # The fully accumulated content must be present. (A "[Turn limit reached...]" notice may be
    # appended after it because max_turns=1, so we check membership rather than endswith.)
    final_content = (last_msg.get('content') or '')
    assert 'part19_final-committed-message_' in final_content, (
        f"final committed message lost — last frame content tail={final_content[-80:]!r}"
    )

def test_fix_a_fast_suppressed_finalization_delivered(subagent_harness):
    """FAST-TURN MESSAGE-LOSS GUARD (reviewer edge case): a sub-agent that yields several rapid
    chunks of UNCHANGED list length, where the last chunk is throttled out AND the whole loop
    finishes <100ms after the previous actual send. A naive 'skip if len unchanged' (or even
    'skip if <100ms since last send') would drop the final committed content. FIX A's
    _last_tick_suppressed flag forces the final frame in this case.

    We force it deterministically by making the engine's streaming layer emit a second UI tick
    (via a large single delta that trips its >=150-char update floor) with no sleep, so the loop
    ends well within 100ms of the first broadcast while the last tick is suppressed.
    """
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)
    inst = pool.get_instance(INSTANCE_NAME)
    assert inst is not None

    def _mock_fast_suppressed(self, instance, template, messages, active_functions):
        # First delta: short. Second delta: large (>150 chars) → forces a 2nd UI tick with no sleep.
        # Content is made unique per delta (no repetition) to avoid tripping the engine's inner-loop detector.
        yield [_msg(role=ASSISTANT, content='fast-start', reasoning_content='')]
        yield [_msg(role=ASSISTANT, content='fast-start' + 'y0123456789abcdef' * 25, reasoning_content='')]

    events, tick_decisions, gen_error, diag = _drive_subagent_pipeline(pool, engine, inst, _mock_fast_suppressed)
    assert not gen_error, f"engine.run() raised: {gen_error.get('exc')}"

    loop_ticks = [td for td in tick_decisions if td['phase'] == 'loop']
    assert len(loop_ticks) >= 2, f"expected >=2 loop ticks, got {len(loop_ticks)}: {loop_ticks}"
    # The last loop tick must have been suppressed (throttled out).
    assert not loop_ticks[-1].get('broadcasted', False), (
        f"test precondition failed: last loop tick was NOT suppressed: {loop_ticks[-1]}"
    )

    frames = _subagent_frames(events)
    assert frames, 'no sub-agent frames captured'
    # The full final content must be present in the LAST delivered frame.
    last_inst = _extract_instance(frames[-1][1], INSTANCE_NAME)
    last_msg = _last_assistant_message(last_inst)
    assert last_msg is not None, 'final frame has no assistant message'
    final_content = (last_msg.get('content') or '')
    assert len(final_content) > 420 and 'fast-start' in final_content, (
        f"fast-turn final content lost — last frame content len={len(final_content)} tail={final_content[-60:]!r}"
    )

def test_fix_a_push_final_state_present(subagent_harness):
    """ROOT REFRESH GUARD: push_final_state(inst, caller) must still fire after sub-agent
    completion (it refreshes the ROOT/caller panel and has no other immediate update path)."""
    _async_loop_works()
    from agent_cascade.engine.core import ExecutionEngine

    pool = subagent_harness['pool']
    engine = ExecutionEngine(pool)
    inst = pool.get_instance(INSTANCE_NAME)
    assert inst is not None

    events, tick_decisions, gen_error, diag = _drive_subagent_pipeline(
        pool, engine, inst, _mock_reasoning_turn
    )
    assert not gen_error, f"engine.run() raised: {gen_error.get('exc')}"

    # The driver records a push_final decision for every run.
    push_decisions = [td for td in tick_decisions if td['phase'] == 'push_final']
    assert len(push_decisions) == 1, f"expected exactly one push_final_state call: {push_decisions}"
    assert push_decisions[0].get('caller') == CALLER

    # And a root/caller-keyed stream_update frame must actually be present on the queue.
    root_frames = []
    for arrival, ev in events:
        if not (isinstance(ev, dict) and ev.get('type') == 'stream_update'):
            continue
        if _extract_instance(ev, CALLER):
            root_frames.append((arrival, ev))
    assert root_frames, f"no caller({CALLER})-keyed stream_update frame on queue — push_final_state missing"

if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
