"""Durable fleet snapshot — read-only envelope writer (BUG_0323 S2).

Captures every instance in the active stack (active, sleeping and idle alike) as
ONE versioned JSON envelope: a **manifest of references** to each instance's
existing JSONL log (path + message count + offset), NOT a copy of the messages.
The per-instance fields are the "S" (serialize) rows of the instance-state
inventory in the ticket; transient/live handles are deliberately excluded.

The envelope is read-only with respect to the fleet: it copies state out and
never mutates instances or pool dicts. The only side effect is the KV save for
the *actively-streaming* set (decision 1), which writes server-side KV state but
leaves no in-memory fleet state behind (``set_label=False``).

Scope here is capture + write only. Restore (S3) and park/activate swap (S4) are
separate slices and are intentionally absent.

All behaviour is gated behind ``settings.ENABLE_STACK_SWAP`` (default off); with
the flag off ``capture_fleet_snapshot`` is a no-op and returns None, so legacy
single-session behavior is untouched.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_cascade import settings

logger = logging.getLogger(__name__)

#: Schema this writer emits and that readers accept.
CURRENT_SCHEMA_VERSION = '1.0.0'
#: Oldest readable schema (no migrations exist yet).
MINIMUM_READABLE_VERSION = '1.0.0'


# ── Clock projection (M3) ────────────────────────────────────────────────────

def monotonic_to_wall(value: float, *, now_wall: float, now_mono: float) -> float:
    """Project a process-local monotonic timestamp onto wall-clock epoch seconds.

    ``value == 0.0`` is the "never set" sentinel used by several instance clocks
    (e.g. ``_last_force_compress_time``); it is returned unchanged so a restore
    can re-invert it to ``0.0`` rather than a bogus epoch.
    """
    if not value:
        return value
    return now_wall - now_mono + value


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class InstanceSnapshot:
    """One instance's restorable fields plus a *reference* to its log.

    ``log`` is a manifest entry, not a body copy: the JSONL is the single source
    of truth for message content. ``kv`` is ``None`` for every non-streaming
    instance (decision 1).
    """

    identity: Dict[str, Any]
    clocks: Dict[str, Any]
    log: Dict[str, Any]
    state: Dict[str, Any]
    compression: Dict[str, Any]
    system_prompt: Dict[str, Any]
    token_counters: Dict[str, Any]
    budget: Dict[str, Any]
    loop_detection: Dict[str, Any]
    mcp: Dict[str, Any]
    memory_hints: Dict[str, Any]
    llm_override: Dict[str, Any]
    kv: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'identity': self.identity,
            'clocks': self.clocks,
            'log': self.log,
            'state': self.state,
            'compression': self.compression,
            'system_prompt': self.system_prompt,
            'token_counters': self.token_counters,
            'budget': self.budget,
            'loop_detection': self.loop_detection,
            'mcp': self.mcp,
            'memory_hints': self.memory_hints,
            'llm_override': self.llm_override,
            'kv': self.kv,
        }


@dataclass
class PoolStateSnapshot:
    """Pool-level, per-stack state the fleet cannot be re-formed without.

    ``active_stack`` is the ordered ``[(name, nest_depth), ...]`` stack identity;
    ``blocking_calls`` records the parent→child ``call_agent`` waiting edges
    captured from the live ``active_stack`` + parent links (D-CONTINUE), so a
    restore can re-block parents on their children rather than park everything.
    ``message_queues`` holds plain message strings (the live queue type carries
    no sender/timestamp, so those are not preserved — only ordering, OQ2).
    """

    active_stack: List[List[Any]] = field(default_factory=list)
    children: Dict[str, List[str]] = field(default_factory=dict)
    blocking_calls: List[Dict[str, str]] = field(default_factory=list)
    message_queues: Dict[str, List[str]] = field(default_factory=dict)
    halted_instances: List[str] = field(default_factory=list)
    compression_halted: List[str] = field(default_factory=list)
    terminated_instances: List[str] = field(default_factory=list)
    instance_summaries: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'active_stack': self.active_stack,
            'children': self.children,
            'blocking_calls': self.blocking_calls,
            'message_queues': self.message_queues,
            'halted_instances': self.halted_instances,
            'compression_halted': self.compression_halted,
            'terminated_instances': self.terminated_instances,
            'instance_summaries': self.instance_summaries,
        }


@dataclass
class SnapshotEnvelope:
    snapshot_id: str
    stack_id: str
    created_at: float
    trigger: str
    instances: Dict[str, InstanceSnapshot] = field(default_factory=dict)
    pool_state: PoolStateSnapshot = field(default_factory=PoolStateSnapshot)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'schema_version': CURRENT_SCHEMA_VERSION,
            'snapshot_id': self.snapshot_id,
            'stack_id': self.stack_id,
            'created_at': self.created_at,
            'trigger': self.trigger,
            'instances': {name: snap.to_dict() for name, snap in self.instances.items()},
            'pool_state': self.pool_state.to_dict(),
        }


def migrate_envelope(raw: dict) -> dict:
    """Single migration entry point: return *raw* normalized to the current schema.

    v1.0.0 is the only schema in existence, so this validates the version window
    and returns the payload unchanged. Future schemas bump here, never the reader.
    """
    version = raw.get('schema_version')
    if version is None:
        raise ValueError('envelope is missing schema_version')
    if version < MINIMUM_READABLE_VERSION or version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f'envelope schema_version {version!r} is outside the readable range '
            f'[{MINIMUM_READABLE_VERSION}, {CURRENT_SCHEMA_VERSION}]'
        )
    return raw


# ── Per-instance capture ─────────────────────────────────────────────────────

def _safe_get(obj, name: str, default: Any = None) -> Any:
    """getattr with a default; keeps capture robust against partially-built doubles.

    Not a substitute for correctness — the fields we require are read directly.
    """
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 — defensive against exotic descriptors
        return default


def _copy_jsonable(value: Any, *, label: str = 'field') -> Any:
    """Return a JSON-safe deep copy, or ``None`` when serialization fails.

    ``None`` returned here means either the input *was* ``None`` (json round-trip
    of ``None`` is ``None``) or serialization raised — the latter is logged so a
    caller's ``or []`` fallback is never silent. Tuples become lists.
    """
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as e:
        logger.warning('snapshot: could not serialize %s (omitting): %s', label, e)
        return None


def _capture_log_reference(log_path: Optional[str]) -> Dict[str, Any]:
    """Read a log's message count + append-resume byte offset (file I/O — Phase 2).

    Counts records that are message bodies (JSON dicts without ``metadata``/
    ``event`` keys, matching the logger's load semantics) so the count agrees
    with the in-memory conversation; a malformed or missing file yields count 0.
    ``offset`` is the current byte size — the append point a restore resumes at.
    """
    ref: Dict[str, Any] = {'path': log_path, 'message_count': 0, 'offset': 0}
    if not log_path or not os.path.exists(log_path):
        return ref
    count = 0
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and 'metadata' not in rec and 'event' not in rec:
                    count += 1
        ref['message_count'] = count
        ref['offset'] = os.path.getsize(log_path)
    except OSError as e:
        logger.warning('Snapshot log reference read failed for %s: %s', log_path, e)
        ref['message_count'] = 0
        ref['offset'] = 0
    return ref


def _resolve_instance_log_path(pool, instance_name: str) -> Optional[str]:
    """Resolve an instance's JSONL path from the canonical logger registry.

    Same source of truth as ``child_runner._resolve_child_log_path`` (pool.instance_loggers).
    Returns None when no logger is registered (caller records count/offset 0).
    """
    try:
        loggers = getattr(pool, 'instance_loggers', None)
        if loggers is not None and hasattr(loggers, 'get'):
            inst_logger = loggers.get(instance_name)
            if inst_logger is not None:
                path = getattr(inst_logger, 'log_path', None)
                if path:
                    return str(path)
    except Exception as e:  # noqa: BLE001 — log resolution must never abort capture
        logger.debug('Snapshot log-path resolution failed for %s: %s', instance_name, e)
    return None


def _capture_instance(
    instance,
    *,
    now_wall: float,
    now_mono: float,
    log_ref: Dict[str, Any],
    kv_block: Optional[Dict[str, Any]],
    display_name: str,
    birth_stamp: Optional[str],
) -> InstanceSnapshot:
    """Assemble one :class:`InstanceSnapshot` from an already-queried instance.

    Scalar/string fields are read live here: they are CPython-atomic single reads
    and any value is a coherent point-in-time snapshot, so no lock is needed for a
    read-only writer. The KV triple (endpoint/digest/digest_endpoint) is the one
    field set that changes as a *group* under the instance ``_state_lock``; it is
    therefore frozen in Phase 1 via :func:`_kv_config_snapshot` and passed in as a
    coherent ``kv_block``. S3 restore must still re-validate that digest against
    the live endpoint (fail-closed backstop), never trust it blind.
    """
    identity = {
        'instance_name': instance.instance_name,
        'agent_class': instance.agent_class,
        'parent_instance': _safe_get(instance, 'parent_instance'),
        'nest_depth': _safe_get(instance, '_nest_depth', 0),
        'display_name': display_name,
        'birth_stamp': birth_stamp,
    }
    clocks = {
        'created_at': monotonic_to_wall(instance.created_at, now_wall=now_wall, now_mono=now_mono),
        'last_activity': monotonic_to_wall(instance.last_activity, now_wall=now_wall, now_mono=now_mono),
        'system_started_at': _safe_get(instance, 'system_started_at'),
    }
    state = {
        'state': _safe_get(instance, 'state').name if _safe_get(instance, 'state') is not None else 'IDLE',
        'is_terminated': bool(_safe_get(instance, 'is_terminated', False)),
        'terminated_reason': _safe_get(instance, '_terminated_reason'),
        'restricted_shell': bool(_safe_get(instance, 'restricted_shell', False)),
    }
    compression = {
        'compression_summary': _safe_get(instance, 'compression_summary'),
        'force_compress_count': _safe_get(instance, '_force_compress_count', 0),
        'force_compress_fail_streak': _safe_get(instance, '_force_compress_fail_streak', 0),
        'last_force_compress_time': monotonic_to_wall(
            _safe_get(instance, '_last_force_compress_time', 0.0), now_wall=now_wall, now_mono=now_mono),
        'last_force_compress_fail_at': monotonic_to_wall(
            _safe_get(instance, '_last_force_compress_fail_at', 0.0), now_wall=now_wall, now_mono=now_mono),
    }
    system_prompt = {
        'system_prompt_frozen': bool(_safe_get(instance, '_system_prompt_frozen', False)),
        'system_prompt_initialized': bool(_safe_get(instance, '_system_prompt_initialized', False)),
    }
    token_counters = {
        'last_actual_token_count': _safe_get(instance, '_last_actual_token_count', 0),
        'allocated_max_input_tokens': _safe_get(instance, '_allocated_max_input_tokens', 0),
        'current_turn': _safe_get(instance, '_current_turn', 0),
        'turn_consumed': bool(_safe_get(instance, '_turn_consumed', False)),
    }
    budget = {
        'max_turns': _safe_get(instance, 'max_turns'),
        'token_budget': _safe_get(instance, 'token_budget', 0),
        'budget_unit': _safe_get(instance, 'budget_unit', 'turns'),
        'token_warned_50': bool(_safe_get(instance, '_token_warned_50', False)),
        'token_warned_90': bool(_safe_get(instance, '_token_warned_90', False)),
        'token_final_fired': bool(_safe_get(instance, '_token_final_fired', False)),
        'token_last_warn_past': _safe_get(instance, '_token_last_warn_past', 0),
    }
    loop_detection = {
        'loop_rollback_count': _safe_get(instance, '_loop_rollback_count', 0),
        'suppress_loop_detection_next_turn': bool(_safe_get(instance, '_suppress_loop_detection_next_turn', False)),
        'fuzzy_warn_armed': bool(_safe_get(instance, '_fuzzy_warn_armed', True)),
        'fuzzy_warn_last_turn': _safe_get(instance, '_fuzzy_warn_last_turn', -10**9),
        'fuzzy_escalation_armed': bool(_safe_get(instance, '_fuzzy_escalation_armed', False)),
    }
    mcp = {
        'discovered_mcp_tools': _copy_jsonable(_safe_get(instance, '_discovered_mcp_tools', []), label='mcp.discovered') or [],
        'unloaded_mcp_tools': _copy_jsonable(_safe_get(instance, '_unloaded_mcp_tools', []), label='mcp.unloaded') or [],
        'mcp_unload_seq': _safe_get(instance, '_mcp_unload_seq', 0),
    }
    memory_hints = {
        'memories_read': _copy_jsonable(sorted(_safe_get(instance, '_memories_read', set())), label='memory.memories_read') or [],
        'skill_hint_counts': _copy_jsonable(_safe_get(instance, '_skill_hint_counts', {}), label='memory.skill_hint_counts') or {},
        'last_memory_hint_turn': _safe_get(instance, '_last_memory_hint_turn', -1),
        'last_skill_hint_turn': _safe_get(instance, '_last_skill_hint_turn', -1),
    }
    llm_override = {'generate_cfg_override': _copy_jsonable(_safe_get(instance, '_generate_cfg_override'), label='llm_override')}

    return InstanceSnapshot(
        identity=identity,
        clocks=clocks,
        log=log_ref,
        state=state,
        compression=compression,
        system_prompt=system_prompt,
        token_counters=token_counters,
        budget=budget,
        loop_detection=loop_detection,
        mcp=mcp,
        memory_hints=memory_hints,
        llm_override=llm_override,
        kv=kv_block,
    )


def _kv_config_snapshot(instance) -> Dict[str, Any]:
    """Freeze the KV triple under the instance ``_state_lock`` (Phase 1).

    ``_last_endpoint_config`` / ``_session_digest`` / ``_session_digest_endpoint``
    are written together under that lock during endpoint allocation, so reading them
    unlocked can capture a torn triple (a digest from config-A paired with the
    endpoint from config-B). Capturing them inside the lock guarantees the recorded
    block is internally coherent. Deep-copied so no live object is aliased.
    """
    lock = getattr(instance, '_state_lock', None)
    with lock if lock else _NullContext():
        cfg = _copy_jsonable(_safe_get(instance, '_last_endpoint_config'), label='kv.endpoint_cfg') or {}
        digest = _safe_get(instance, '_session_digest')
        digest_endpoint = _copy_jsonable(_safe_get(instance, '_session_digest_endpoint'), label='kv.digest_endpoint')
    return {
        'endpoint_cfg': cfg,
        'session_digest': digest,
        'session_digest_endpoint': digest_endpoint,
    }


class _NullContext:
    """Stand-in when an instance exposes no ``_state_lock`` (e.g. a test double)."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _is_streaming(pool, instance, endpoint_cfg: Dict[str, Any]) -> bool:
    """True when ``instance`` is in the actively-streaming set (decision 1).

    Streaming == ``state_save_enabled`` (per the frozen endpoint config) AND
    currently holds that endpoint's slot — the precise mechanical condition the save
    API requires. The slot is resolved live via the real ``state_ops.resolve_held_endpoint``
    seam (we want the slot state at capture time), never inferred from a cached flag.
    """
    from agent_cascade.state_ops import is_state_save_capable, resolve_held_endpoint

    if not is_state_save_capable(endpoint_cfg):
        return False
    return resolve_held_endpoint(pool, instance) is not None


def _save_streaming_kv(instance, kv_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Save KV for a streaming instance (no in-memory side effect) and build its block.

    Uses the existing ``save_instance_state(..., set_label=False)`` so the save is
    warm-only and no automatic restore is armed — i.e. the snapshot leaves no
    ``_state_label`` behind on the live instance. The recorded triple comes from the
    Phase-1 ``kv_cfg`` freeze so it is coherent; the actual save re-reads authoritative
    config under its own lock. Recorded ``saved`` lets a restore fail closed.
    """
    from agent_cascade.state_ops import save_instance_state

    endpoint_cfg = kv_cfg['endpoint_cfg']
    saved = False
    try:
        saved = bool(save_instance_state(instance, set_label=False))
    except Exception as e:  # noqa: BLE001 — a save failure records saved:false, never aborts capture
        logger.info('Snapshot KV save failed for %s: %s', instance.instance_name, e)

    return {
        'capable': True,
        'saved': saved,
        'reason': None if saved else 'save_failed',
        'label': instance.instance_name,
        'session_digest': kv_cfg['session_digest'],
        'session_digest_endpoint': kv_cfg['session_digest_endpoint'],
        'endpoint_cfg': {
            'api_base': endpoint_cfg.get('api_base', ''),
            'model': endpoint_cfg.get('model', ''),
        },
    }


# ── Pool-level capture ───────────────────────────────────────────────────────

def _snapshot_string_set(pool, attr: str, *, attempts: int = 3) -> List[str]:
    """Best-effort sorted copy of a pool string set that has no single guarding lock.

    ``_halted_instances`` / ``_compression_halted`` are mutated by several code
    paths (halt/resume, session restore, pool reset) under no common lock, so taking
    a lock here would protect nothing. ``sorted()`` over a set being mutated can raise
    ``RuntimeError: Set changed size during iteration``; we retry to absorb a transient
    race and degrade to empty with a warning on persistent contention, rather than
    aborting a read-only capture.
    """
    for _attempt in range(attempts):
        try:
            return sorted(getattr(pool, attr, set()))
        except RuntimeError:
            continue  # concurrent mutation during iteration — retry
    logger.warning('snapshot: could not snapshot %s after %d attempts (concurrent mutation)', attr, attempts)
    return []


def _blocking_edges(instances: Dict[str, Any], active_stack: List[tuple]) -> List[Dict[str, str]]:
    """Derive parent→child ``call_agent`` waiting edges from the active stack.

    A ``call_agent`` blocks its parent on the child's result, so a parent that is
    on the active stack *and* has a child instance also on the stack is currently
    re-blocked on that child. Grounded in the *frozen* ``instances`` copy (so no live
    pool read after the I/O phase) + ``active_stack`` membership + each child's
    ``parent_instance``; S3 re-applies them to re-form the execution graph
    (D-CONTINUE). No live handle is captured.
    """
    on_stack = {entry[0] for entry in active_stack}
    edges: List[Dict[str, str]] = []
    for name in on_stack:
        child = instances.get(name)
        if child is None:
            continue
        parent = _safe_get(child, 'parent_instance')
        if parent and parent in on_stack:
            edges.append({'parent': parent, 'child': name})
    return edges


def capture_fleet_snapshot(
    pool,
    trigger: str = 'user',
    *,
    stack_id: Optional[str] = None,
    kv_saver=None,
) -> Optional[SnapshotEnvelope]:
    """Build a versioned fleet-snapshot envelope from the live pool.

    Two-phase by design (M5): Phase 1 copies state out under the pool's locks and
    projects clocks; the locks are released before Phase 2, which does all file
    and network I/O (log counting, log-path reads, streaming KV saves) with no
    pool lock held. Returns None when ``ENABLE_STACK_SWAP`` is off (no-op).

    Args:
        pool: The live AgentPool.
        trigger: Why the snapshot was taken ('user' default; the job system passes
            its own later — this slice has no scheduler, decision 2).
        stack_id: Stack key; defaults to the stack's root instance name.
        kv_saver: Injectable override for the per-instance KV save (defaults to
            :func:`_save_streaming_kv`). Exists so tests exercise the streaming
            path without a real autoloader; production never passes one.
    """
    if not settings.ENABLE_STACK_SWAP:
        logger.debug('Fleet snapshot requested but ENABLE_STACK_SWAP is off — no-op')
        return None

    now_wall = time.time()
    now_mono = time.monotonic()

    # ── Phase 1: copy state under the pool's locks, freeze the KV triple ──────
    # Read the lock-guarded containers under their locks, then release before I/O.
    # The instance containers (instances / instance_summaries / terminated) are
    # copied here so Phase 2 iterates frozen copies across file/network I/O without
    # racing a spawn/dismiss (RuntimeError: dict changed size during iteration) or
    # saving a dismissed one. Slot resolution (resolve_held_endpoint) is read LIVE
    # at capture time by design — we want current slot state, not a stale copy.
    # _halted_instances / _compression_halted have no single guarding lock, so they
    # are snapshotted best-effort (see _snapshot_string_set), NOT under a lock.
    with getattr(pool, '_pool_lock', threading.Lock()):
        instances = dict(getattr(pool, 'instances', {}))
        terminated_instances = sorted(getattr(pool, 'terminated_instances', set()))
        instance_summaries = dict(getattr(pool, 'instance_summaries', {}))
    if not instances:
        logger.warning('Fleet snapshot requested on an empty pool — writing empty envelope')
    children_lock = getattr(pool, '_children_lock', threading.Lock())
    with children_lock:
        children = {k: list(v) for k, v in getattr(pool, 'children', {}).items()}
    # active_stack is already a thread-safe defensive copy via the pool property.
    active_stack = [(name, depth) for name, depth in getattr(pool, 'active_stack', [])]
    queue_lock = getattr(pool, '_queue_lock', threading.Lock())
    with queue_lock:
        message_queues = {k: list(v) for k, v in getattr(pool, 'message_queues', {}).items() if v}
    # No single lock guards these sets (see _snapshot_string_set), so best-effort
    # copy with retry — NOT under _children_lock, which protects nothing here.
    halted_instances = _snapshot_string_set(pool, '_halted_instances')
    compression_halted = _snapshot_string_set(pool, '_compression_halted')
    # Freeze the KV triple per instance under each instance's own _state_lock
    # (sequentially, NOT nested under _pool_lock — preserves the lock ordering).
    kv_configs = {name: _kv_config_snapshot(inst) for name, inst in instances.items()}

    from agent_cascade import agent_identity

    # ── Phase 2: I/O (log refs) and KV saves — NO pool lock held ────────────
    log_refs: Dict[str, Dict[str, Any]] = {}
    kv_blocks: Dict[str, Optional[Dict[str, Any]]] = {}
    for name, inst in instances.items():
        log_refs[name] = _capture_log_reference(_resolve_instance_log_path(pool, name))
        endpoint_cfg = kv_configs[name]['endpoint_cfg']
        if _is_streaming(pool, inst, endpoint_cfg):
            kv_blocks[name] = kv_saver(inst, kv_configs[name]) if kv_saver else _save_streaming_kv(inst, kv_configs[name])
        else:
            kv_blocks[name] = None

    inst_snapshots: Dict[str, InstanceSnapshot] = {}
    for name, inst in instances.items():
        display_name = agent_identity.get_display_name(inst)
        inst_snapshots[name] = _capture_instance(
            inst,
            now_wall=now_wall,
            now_mono=now_mono,
            log_ref=log_refs[name],
            kv_block=kv_blocks[name],
            display_name=display_name,
            birth_stamp=agent_identity.birth_stamp(name),
        )

    if stack_id is None:
        stack_id = active_stack[0][0] if active_stack else _root_instance_name(instances)

    pool_state = PoolStateSnapshot(
        active_stack=[[name, depth] for name, depth in active_stack],
        children=children,
        blocking_calls=_blocking_edges(instances, active_stack),
        message_queues=message_queues,
        halted_instances=halted_instances,
        compression_halted=compression_halted,
        terminated_instances=terminated_instances,
        instance_summaries=instance_summaries,
    )

    envelope = SnapshotEnvelope(
        snapshot_id=str(uuid.uuid4()),
        stack_id=stack_id,
        created_at=now_wall,
        trigger=trigger,
        instances=inst_snapshots,
        pool_state=pool_state,
    )
    return envelope


def _root_instance_name(instances: Dict[str, Any]) -> str:
    """Best-effort name of the stack's root (the parentless instance)."""
    for name, inst in instances.items():
        if not _safe_get(inst, 'parent_instance'):
            return name
    return next(iter(instances), '')


def get_snapshot_dir(pool) -> Path:
    """Resolve the instance-scoped snapshot dir (``<log_dir>/snapshots``).

    Sits next to the logs the envelope references, so a stack's logs and envelope
    stay together and inherit the existing per-instance isolation rules.
    """
    from agent_cascade.instance_id import get_session_log_dir
    log_dir = get_session_log_dir(pool)
    return log_dir / 'snapshots'


def write_snapshot_to_disk(envelope: SnapshotEnvelope, base_dir: Path) -> Path:
    """Atomically write the envelope as ``<snapshot_id>.json`` under ``base_dir``.

    Writes to a ``.tmp`` sibling then ``os.replace`` so a crash mid-write leaves
    the previous file (or nothing) intact, never a truncated envelope.
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    final_path = base_dir / f'{envelope.snapshot_id}.json'
    tmp_path = final_path.with_suffix('.tmp')
    payload = envelope.to_dict()
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, final_path)
    return final_path
