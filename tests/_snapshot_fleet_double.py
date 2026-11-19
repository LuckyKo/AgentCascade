"""Shared fleet-snapshot test doubles for BUG_0323 S3/S4/S5 (plan §9, R7).

Single home for the pool/instance doubles so the rehydration suites do not fork
``_PoolDouble``. ``PoolDouble`` deliberately inherits ``SessionIOMixin`` so the
restore path exercises the REAL ``_reattach_instance_log`` / ``_parse_json_input``
/ ``_build_working_set`` rather than a hand-rolled stub. It also exposes both
accessors the fleet machinery uses: ``active_stack`` (the property capture reads,
line snapshot.py:648) backed by the canonical ``_execution.active_stack`` list
that ``restore_fleet_snapshot`` re-seeds.
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from agent_cascade.agent_instance import AgentState
from agent_cascade.pool.session_io import SessionIOMixin

# A save-capable endpoint config (state_save_enabled + api_base + model).
CAPABLE_CFG = {'api_base': 'http://127.0.0.1:1234/v1', 'model': 'm-a', 'state_save_enabled': True}

# Canonical root instance name, shared by make_fleet and the restore suites (S3).
ROOT = 'Maine_20261115_143022'


class LoggerDouble:
    """Stands in for AgentInstanceLogger — capture only reads ``.log_path``."""

    def __init__(self, path):
        self.log_path = path


class PoolDouble(SessionIOMixin):
    """Minimal pool surface for capture_fleet_snapshot + restore_fleet_snapshot.

    Mirrors the AgentPool containers/locks those functions touch. The logger
    manager is a SimpleNamespace with the attributes ``_reattach_instance_log``
    needs (``log_dir``, ``workspace_dir``, ``_lock``, ``_loggers``).
    """

    def __init__(self, log_dir=None):
        log_dir = Path(log_dir) if log_dir is not None else Path('.')
        self.instances = {}
        self.children = {}
        self.instance_loggers = {}
        self.message_queues = {}
        self.instance_summaries = {}
        self.terminated_instances = set()
        self._halted_instances = set()
        self._compression_halted = set()
        # Execution manager: capture reads pool.active_stack (property below);
        # restore re-seeds _execution.active_stack under _execution._state_lock.
        self._execution = SimpleNamespace(active_stack=[], _state_lock=threading.RLock())
        self._instances_version = 0
        self._logger = SimpleNamespace(
            log_dir=log_dir, workspace_dir=log_dir,
            _lock=threading.RLock(), _loggers={})
        self._pool_lock = threading.RLock()
        self._children_lock = threading.RLock()
        self._queue_lock = threading.RLock()

    @property
    def active_stack(self):
        # Shared list object: capture reads it, restore mutates it in place.
        return self._execution.active_stack

    def enqueue_message(self, instance_name, text):
        # Mirrors the real pool's self-locking, non-blocking enqueue.
        with self._queue_lock:
            self.message_queues.setdefault(instance_name, []).append(text)


def make_instance(name, *, agent_class='coder', parent=None, depth=0,
                  display=None, state=AgentState.RUNNING, cfg=None):
    """A lightweight instance double with exactly the fields capture reads.

    SimpleNamespace (not MagicMock) so missing fields return capture defaults
    instead of auto-creating truthy attributes.
    """
    return SimpleNamespace(
        instance_name=name,
        agent_class=agent_class,
        parent_instance=parent,
        _nest_depth=depth,
        display_name=display if display is not None else name,
        state=state,
        is_terminated=False,
        _terminated_reason=None,
        restricted_shell=False,
        created_at=0.0,
        last_activity=0.0,
        system_started_at=None,
        compression_summary=None,
        _force_compress_count=0,
        _force_compress_fail_streak=0,
        _last_force_compress_time=0.0,
        _last_force_compress_fail_at=0.0,
        _system_prompt_frozen=False,
        _system_prompt_initialized=False,
        _last_actual_token_count=0,
        _allocated_max_input_tokens=0,
        _current_turn=0,
        _turn_consumed=False,
        max_turns=None,
        token_budget=0,
        budget_unit='turns',
        _token_warned_50=False,
        _token_warned_90=False,
        _token_final_fired=False,
        _token_last_warn_past=0,
        _loop_rollback_count=0,
        _suppress_loop_detection_next_turn=False,
        _fuzzy_warn_armed=True,
        _fuzzy_warn_last_turn=-10**9,
        _fuzzy_escalation_armed=False,
        _discovered_mcp_tools=[],
        _unloaded_mcp_tools=[],
        _mcp_unload_seq=0,
        _memories_read=set(),
        _skill_hint_counts={},
        _last_memory_hint_turn=-1,
        _last_skill_hint_turn=-1,
        _generate_cfg_override=None,
        _last_endpoint_config=cfg,
        _session_digest='dig-' + name,
        _session_digest_endpoint=(cfg['api_base'], cfg['model']) if cfg else None,
        # Live-stream flag read by the canonical ``instance_llm_in_flight`` predicate
        # (no ``_stream_reader_done`` here, so this is the authority).
        _llm_call_active=False,
        _state_lock=threading.RLock(),
    )


def register(pool, inst, *, on_stack=True):
    pool.instances[inst.instance_name] = inst
    if on_stack:
        pool.active_stack.append((inst.instance_name, inst._nest_depth))


def write_jsonl(path, meta_instance_name, message_count, *, roles=None):
    """Write a real JSONL log: 1 metadata line + ``message_count`` message lines.

    Message lines match the logger's load semantics (JSON dicts without
    'metadata'/'event' keys). ``roles`` defaults to alternating user/assistant.
    Returns the file's byte size at write time.
    """
    lines = [json.dumps({'metadata': {'instance_name': meta_instance_name}})]
    for i in range(message_count):
        if roles is not None:
            role = roles[i]
        else:
            role = 'user' if i % 2 else 'assistant'
        lines.append(json.dumps({'role': role, 'content': f'msg {i}'}))
    text = '\n'.join(lines) + '\n'
    path.write_text(text, encoding='utf-8')
    return path.stat().st_size


def make_fleet(pool, tmp_path, monkeypatch, *, streaming_names=()):
    """Root + streaming child + a capable-but-no-slot instance + a non-capable child.

    Writes placeholder logs for each instance, wires log paths, and fakes
    ``resolve_held_endpoint`` to return a held endpoint only for ``streaming_names``.
    Returns ``(root, worker, parked, incapable)``.
    """
    import agent_cascade.state_ops as state_ops

    root = make_instance(ROOT, agent_class='orchestrator',
                         display='Maine', depth=0, cfg=CAPABLE_CFG)
    worker = make_instance('coder_worker1', parent=ROOT, depth=1, cfg=CAPABLE_CFG)
    parked = make_instance('coder_parked', parent=ROOT, depth=1, cfg=CAPABLE_CFG)
    incapable = make_instance('coder_plain', parent=ROOT, depth=1, cfg=None)
    for inst in (root, worker, parked, incapable):
        inst._llm_call_active = inst.instance_name in streaming_names
        register(pool, inst)

    pool.instance_loggers = {
        ROOT: LoggerDouble(str(tmp_path / 'maine.jsonl')),
        'coder_worker1': LoggerDouble(str(tmp_path / 'worker.jsonl')),
        'coder_parked': LoggerDouble(str(tmp_path / 'parked.jsonl')),
        'coder_plain': LoggerDouble(str(tmp_path / 'plain.jsonl')),
    }
    pool.children = {ROOT: ['coder_worker1', 'coder_parked', 'coder_plain']}

    def fake_held(p, inst):
        if inst.instance_name in streaming_names:
            return {'api_base': CAPABLE_CFG['api_base'], 'model': CAPABLE_CFG['model']}
        return None

    monkeypatch.setattr(state_ops, 'resolve_held_endpoint', fake_held)
    return root, worker, parked, incapable


def log_paths(pool):
    """Map instance_name -> log path string for a pool built by make_fleet."""
    return {name: lg.log_path for name, lg in pool.instance_loggers.items()}
