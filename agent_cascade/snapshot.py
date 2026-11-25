"""Durable fleet snapshot — read-only envelope writer.

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

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_cascade import settings

logger = logging.getLogger(__name__)

#: Schema this writer emits and that readers accept.
CURRENT_SCHEMA_VERSION = '1.0.0'
#: Oldest readable schema (no migrations exist yet).
MINIMUM_READABLE_VERSION = '1.0.0'


class SnapshotIntegrityError(Exception):
    """Fail-closed restore refusal: referenced log missing/truncated/mismatched,
    or envelope structurally unfit for rehydration. Raised ONLY by restore-path
    validators, never by capture. S4 rollback guard catches this type."""


class ActiveStackDeletionError(Exception):
    """Refusal to delete the currently-active stack.

    Raised by :func:`delete_stack` when ``stack_id`` equals
    ``StackRegistry.active_stack_id`` — deleting the live stack would pull it out
    from under the running fleet. The REST layer maps this to HTTP 409 Conflict;
    the caller must park or swap to another stack first."""


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


def wall_to_monotonic(value: float, *, now_wall: float, now_mono: float) -> float:
    """Exact mirror of ``monotonic_to_wall`` (M3 inversion).

    ``mono = now_mono - (now_wall - value)`` — algebraically ``now_mono + (value - now_wall)``.
    Sentinel parity REQUIRED: falsy input (``0.0`` = "never happened") returns
    unchanged — otherwise ``0.0`` inverts to a meaningless epoch-relative value.
    """
    if not value:
        return value
    return now_mono - (now_wall - value)


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

    @classmethod
    def from_dict(cls, raw: dict) -> 'InstanceSnapshot':
        """Reconstruct from a ``to_dict`` payload (exact key mirror)."""
        if not isinstance(raw, dict):
            raise TypeError('InstanceSnapshot.from_dict expects a dict')
        _validate_state_block(raw['state'])
        return cls(
            identity=raw['identity'],
            clocks=raw['clocks'],
            log=raw['log'],
            state=raw['state'],
            compression=raw['compression'],
            system_prompt=raw['system_prompt'],
            token_counters=raw['token_counters'],
            budget=raw['budget'],
            loop_detection=raw['loop_detection'],
            mcp=raw['mcp'],
            memory_hints=raw['memory_hints'],
            llm_override=raw['llm_override'],
            kv=raw.get('kv'),
        )


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

    @classmethod
    def from_dict(cls, raw: dict) -> 'PoolStateSnapshot':
        """Reconstruct from a ``to_dict`` payload. Declared set-like fields stay
        ``List[str]`` (restore precomputes ``set(...)`` locally where membership
        matters) — do not coerce here, or the type contract drifts from ``to_dict``."""
        if not isinstance(raw, dict):
            raise TypeError('PoolStateSnapshot.from_dict expects a dict')
        return cls(
            active_stack=raw.get('active_stack', []),
            children=raw.get('children', {}),
            blocking_calls=raw.get('blocking_calls', []),
            message_queues=raw.get('message_queues', {}),
            halted_instances=raw.get('halted_instances', []),
            compression_halted=raw.get('compression_halted', []),
            terminated_instances=raw.get('terminated_instances', []),
            instance_summaries=raw.get('instance_summaries', {}),
        )


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

    @classmethod
    def from_dict(cls, raw: dict) -> 'SnapshotEnvelope':
        """Reconstruct from a ``to_dict`` payload. Rejects a non-dict ``instances``;
        ``to_dict`` emits ``schema_version`` but this reader is fed the already
        version-validated payload, so the version is not re-checked here."""
        if not isinstance(raw, dict):
            raise TypeError('SnapshotEnvelope.from_dict expects a dict')
        instances_raw = raw['instances']
        if not isinstance(instances_raw, dict):
            raise TypeError('snapshot instances must be a dict')
        return cls(
            snapshot_id=raw['snapshot_id'],
            stack_id=raw['stack_id'],
            created_at=raw['created_at'],
            trigger=raw['trigger'],
            instances={name: InstanceSnapshot.from_dict(v) for name, v in instances_raw.items()},
            pool_state=PoolStateSnapshot.from_dict(raw['pool_state']),
        )


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
        logger.warning('[FLEET_SNAPSHOT] could not serialize %s (omitting): %s', label, e)
        return None


def _validate_state_block(block) -> None:
    """Fail-closed guard for a captured ``state`` block (S3 restore contract).

    ``_capture_instance`` always writes ``state.name`` (falling back to ``'IDLE'``),
    so a missing/None/unknown ``state`` name can only reach here from a corrupt or
    hand-edited envelope. Refusing it at load keeps :func:`restore_fleet_snapshot`
    phase-3's strict ``AgentState[snap.state['state']]`` indexing from raising a raw
    KeyError/TypeError mid-mutation; the load path surfaces it as a ``ValueError``.
    """
    from agent_cascade.agent_instance import AgentState

    if not isinstance(block, dict):
        raise ValueError('snapshot state block is missing or not a dict')
    name = block.get('state')
    if not isinstance(name, str) or name not in AgentState.__members__:
        raise ValueError(f'snapshot state block has invalid/missing state name: {name!r}')


_HASH_CACHE_MAX = 64
_hash_cache: Dict[Tuple[str, int, float], str] = {}


def _capture_log_reference(log_path: Optional[str]) -> Dict[str, Any]:
    """Read a log's message count, byte offset and prefix hash (single pass).

    ``offset`` is the current byte size and ``prefix_sha256`` the sha256 of exactly
    ``bytes[:offset]`` — the identity of the captured history. Counting stays the same
    (JSON dicts without ``metadata``/``event``).
    """
    ref: Dict[str, Any] = {'path': log_path, 'message_count': 0, 'offset': 0, 'prefix_sha256': ''}
    if not log_path or not os.path.exists(log_path):
        return ref
    t0 = time.perf_counter()
    count = 0
    size = 0
    digest = hashlib.sha256()
    try:
        with open(log_path, 'rb') as fb:
            for raw in fb:
                size += len(raw)
                digest.update(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(rec, dict) and 'metadata' not in rec and 'event' not in rec:
                    count += 1
        hexdigest = digest.hexdigest()
        ref.update(offset=size, prefix_sha256=hexdigest, message_count=count)
        mtime = os.stat(log_path).st_mtime
        if len(_hash_cache) >= _HASH_CACHE_MAX:
            _hash_cache.pop(next(iter(_hash_cache)), None)
        _hash_cache[(log_path, size, mtime)] = hexdigest
        elapsed = (time.perf_counter() - t0) * 1000
        if elapsed > 200:
            logger.info('[STACK_SWAP] log reference capture took %.0fms for %s (size=%d)',
                        elapsed, log_path, size)
    except OSError as e:
        logger.warning('[FLEET_SNAPSHOT] log reference read failed for %s: %s', log_path, e)
        ref['message_count'] = 0
        ref['offset'] = 0
        ref['prefix_sha256'] = ''
    return ref


def validate_log_reference(ref: Optional[dict]) -> Optional[str]:
    """Single source of truth for the log-integrity comparison.

    Refuse only real corruption; accept append-only growth.
    Rules (fail-closed on anything ambiguous):
      * ``ref`` has a non-empty string ``path`` and the file exists;
      * ``size >= ref['offset']`` (a shorter file was truncated -> refuse);
      * ``sha256(bytes[:offset]) == ref['prefix_sha256']`` (same history -> valid);
      * a legacy ref with no ``prefix_sha256`` (written before this change) falls back
        to the old strict ``size == offset`` + message count comparison.
    ``size > offset`` is NORMAL for a live fleet and must not refuse.
    """
    if not isinstance(ref, dict):
        return 'log reference is missing or not a dict'
    path = ref.get('path')
    if not isinstance(path, str) or not path:
        return 'log reference has no usable path'
    if not os.path.exists(path):
        return f'referenced log missing: {path}'

    try:
        st = os.stat(path)
        current_size = st.st_size
        current_mtime = st.st_mtime
    except OSError as e:
        return f'could not stat log {path}: {e}'

    expected_offset = ref.get('offset', 0)
    if not isinstance(expected_offset, int) or expected_offset < 0:
        return f'invalid offset in log reference: {expected_offset!r}'

    if current_size < expected_offset:
        return (f'byte offset truncated for {path}: snapshot expected >= {expected_offset!r} '
                f'but on-disk size is {current_size!r}')

    prefix_sha256 = ref.get('prefix_sha256')
    if not prefix_sha256:
        # Legacy envelope without prefix_sha256: fall back to old strict comparison
        if current_size != expected_offset:
            return (f'byte offset mismatch for legacy snapshot of {path}: snapshot {expected_offset!r} '
                    f'!= on-disk {current_size!r}')
        current = _capture_log_reference(path)
        expected_count = ref.get('message_count')
        if current['message_count'] != expected_count:
            return (f'message_count mismatch for {path}: snapshot {expected_count!r} '
                    f'!= on-disk {current['message_count']!r}')
        return None

    # Cached hash check
    cached = _hash_cache.get((path, expected_offset, current_mtime))
    if cached == prefix_sha256 and current_size == expected_offset:
        return None

    # Calculate sha256 of exactly bytes[:expected_offset]
    try:
        digest = hashlib.sha256()
        bytes_read = 0
        with open(path, 'rb') as fb:
            while bytes_read < expected_offset:
                chunk = fb.read(min(65536, expected_offset - bytes_read))
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
        if bytes_read != expected_offset:
            return f'failed to read {expected_offset} bytes from {path} (read {bytes_read})'
        actual_hash = digest.hexdigest()
        if actual_hash != prefix_sha256:
            return (f'prefix hash mismatch for {path}: snapshot {prefix_sha256[:12]}... '
                    f'!= on-disk {actual_hash[:12]}...')
    except OSError as e:
        return f'could not read log {path} for integrity check: {e}'
    return None


def validate_envelope_integrity(envelope: SnapshotEnvelope) -> None:
    """Refinement-4 gate, run BEFORE any pool mutation (all-or-nothing, M2).

    Iterates every instance's captured log reference and refuses on the first
    mismatch. Raises :class:`SnapshotIntegrityError` naming the offending
    instance, path, and expected-vs-actual counts; returns ``None`` on success.
    """
    for name, snap in envelope.instances.items():
        reason = validate_log_reference(snap.log)
        if reason:
            raise SnapshotIntegrityError(f'{name}: {reason}')


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
        logger.debug('[FLEET_SNAPSHOT] log-path resolution failed for %s: %s', instance_name, e)
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
        # kv_birth_ts is a wall-clock 'YYYYMMDD_HHMMSS' string (the per-session KV key birth
        # TS) — verbatim, NOT projected. Restored verbatim in _instantiate_instance so a
        # rehydrated instance keeps the SAME KV save key and the arm guard arms, not refuses.
        'kv_birth_ts': _safe_get(instance, 'kv_birth_ts'),
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


def _is_streaming(pool, instance, endpoint_cfg: Dict[str, Any], *,
                  just_finished: bool = False) -> bool:
    """True when ``instance`` is in the actively-streaming set (decision 1).

    A KV save can only capture a session the engine still retains, so the instance
    must either be mid-stream right now (its stream owns the retained catalog cell)
    or be the instance whose stream just ended (:attr:`just_finished` — its cell is
    the freshest one and is exactly what the snapshot wants to resume from).

    ``state_save_enabled`` plus a resolvable endpoint is NOT sufficient: the endpoint
    resolver returns the instance's cached/effective endpoint whether or not it is
    generating, so idle siblings and callers blocked on a child both look "streaming".
    Saving them writes sessions the engine no longer retains (the digest is stale by
    the next turn) and, on a shared concurrency-1 slot, races the live stream's KV.
    ``instance_llm_in_flight`` is the canonical live-stream predicate, so an abandoned
    stream whose reader has already finished is not mistaken for an active one.
    """
    from agent_cascade.state_ops import is_state_save_capable, resolve_held_endpoint
    from agent_cascade.utils.streaming import instance_llm_in_flight

    if not is_state_save_capable(endpoint_cfg):
        return False
    if just_finished:
        # Its stream ended on this endpoint (the permit is already released, so the
        # slot resolver need not answer); the engine still retains its session cell.
        return True
    if resolve_held_endpoint(pool, instance) is None:
        return False
    return instance_llm_in_flight(instance)


def _loaded_kv_names(active_stack, instances, root_name) -> set:
    """Names whose KV the engine currently retains.

    The warm cell holds the KV of the loaded agent(s) only. After an explicit
    park/swap force-halts every in-flight stream, the engine still retains the
    session cell of whichever agent was loaded when the stack froze:

    - Non-empty ``active_stack``  -> the active leaf/leaves (deepest nest_depth).
    - Empty / root-only           -> the root (the whole fleet is idle).

    The returned set is fed to :func:`_is_streaming` as its ``just_finished``
    argument on the explicit path, i.e. "its stream ended; the engine still
    retains its cell". Agents outside this set are cold-started (``kv=None``)
    by design, never issued a stale/racy save. The engine holds at most one
    warm cell, so in practice this is a single leaf; the set form is the
    forward-looking hedge for parallel leaves.
    """
    if not active_stack:
        return {root_name}
    max_depth = max(depth for _, depth in active_stack)
    return {name for name, depth in active_stack if depth == max_depth}


def _save_streaming_kv(instance, kv_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Save KV for a streaming instance (no in-memory side effect) and build its block.

    Uses the existing ``save_instance_state(..., set_label=False)`` so the save is
    warm-only and no automatic restore is armed — i.e. the snapshot leaves no
    ``_state_label`` behind on the live instance. The recorded triple comes from the
    Phase-1 ``kv_cfg`` freeze so it is coherent; the actual save re-reads authoritative
    config under its own lock. Recorded ``saved`` lets a restore fail closed.
    """
    from agent_cascade.state_ops import save_instance_state, kv_save_label

    endpoint_cfg = kv_cfg['endpoint_cfg']
    saved = False
    try:
        saved = bool(save_instance_state(instance, set_label=False))
    except Exception as e:  # noqa: BLE001 — a save failure records saved:false, never aborts capture
        logger.info('[FLEET_SNAPSHOT] KV save failed for %s: %s', instance.instance_name, e)

    return {
        'capable': True,
        'saved': saved,
        'reason': None if saved else 'save_failed',
        'label': kv_save_label(instance),
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
    logger.warning('[FLEET_SNAPSHOT] could not snapshot %s after %d attempts (concurrent mutation)', attr, attempts)
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
    just_finished: Optional[Iterable[str]] = None,
    only_streaming: bool = False,
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
        just_finished: Instance names whose LLM stream ended immediately before this
            capture (the task-end autosave passes the finishing root). They no longer
            have a stream in flight, but their KV is the freshest retained session, so
            they are saved alongside instances that are still streaming. Only read when
            ``only_streaming`` is true.
        only_streaming: Restrict KV saves to instances that are streaming right now
            (plus ``just_finished``). The automatic task-end save sets this. Explicit
            captures (park / Save now / shutdown / restart) leave it false but are still
            narrow: they save only the loaded set — the active leaf, or the root when the
            fleet is idle — i.e. the agents whose KV the engine actually retains. Sweeping
            the idle/blocked fleet there wrote stale sessions (400/409) and multi-GB files
            on every turn end.
    """
    if not settings.ENABLE_STACK_SWAP:
        logger.debug('[FLEET_SNAPSHOT] requested but ENABLE_STACK_SWAP is off — no-op')
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
        logger.warning('[FLEET_SNAPSHOT] requested on an empty pool — writing empty envelope')
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
    finished = frozenset(just_finished or ())
    # The engine retains the KV of only the loaded agent(s). For the explicit path
    # (only_streaming=False) this is the active leaf (or the root when the fleet is
    # idle), computed from the frozen active_stack — NOT resolve_held_endpoint, which
    # resolves for blocked callers and idle siblings and so swept stale saves fleet-wide.
    # Fed as _is_streaming's just_finished so the capable guard is preserved.
    root_name = _root_instance_name(instances)
    loaded = _loaded_kv_names(active_stack, instances, root_name)
    log_refs: Dict[str, Dict[str, Any]] = {}
    kv_blocks: Dict[str, Optional[Dict[str, Any]]] = {}
    for name, inst in instances.items():
        log_refs[name] = _capture_log_reference(_resolve_instance_log_path(pool, name))
        endpoint_cfg = kv_configs[name]['endpoint_cfg']
        if only_streaming:
            save_kv = _is_streaming(pool, inst, endpoint_cfg, just_finished=name in finished)
        else:
            save_kv = _is_streaming(pool, inst, endpoint_cfg, just_finished=name in loaded)
        if save_kv:
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


def load_snapshot_from_disk(snapshot_path: Path) -> SnapshotEnvelope:
    """Validate + migrate + reconstruct a snapshot envelope from disk.

    Fail-closed on EVERY bad input. NOT gated behind ENABLE_STACK_SWAP: this is a
    pure read-only validator with zero side effects, and its only callers live
    inside gated swap flows — gating a total function would be dead code (the
    off-path is a no-op by construction at :func:`restore_fleet_snapshot`).

    1. read_text   -> OSError wrapped into ``ValueError('cannot read snapshot ...')``
    2. json.loads  -> JSONDecodeError wrapped into ``ValueError('corrupt JSON ...')``
    3. migrate_envelope -> validates the ``[MINIMUM_READABLE_VERSION, CURRENT_SCHEMA_VERSION]``
       window; the SINGLE source of truth for the version check — do NOT re-implement.
    4. SnapshotEnvelope.from_dict -> KeyError/TypeError wrapped into ValueError.
    """
    path = Path(snapshot_path)
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise ValueError(f'cannot read snapshot {path}: {exc}') from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'corrupt JSON at {path}: {exc}') from exc
    raw = migrate_envelope(raw)
    try:
        return SnapshotEnvelope.from_dict(raw)
    except (KeyError, TypeError) as exc:
        raise ValueError(f'snapshot {path} does not match schema: {exc}') from exc


def _envelope_log_snapshots(raw, target: str):
    """Yield each instance-snapshot dict in parsed envelope ``raw`` whose log-file
    basename equals ``target`` (the proven log-basename join key — path-spelling-
    independent, since the JSONL filename timestamp is immutable and unique)."""
    instances = raw.get('instances') if isinstance(raw, dict) else None
    if not isinstance(instances, dict):
        return
    for snap in instances.values():
        if not isinstance(snap, dict):
            continue
        p = (snap.get('log') or {}).get('path')
        if p and os.path.normcase(os.path.basename(str(p))) == target:
            yield snap


def find_birth_ts_for_log(pool, log_path) -> Optional[str]:
    """Return the ``kv_birth_ts`` recorded by the MOST RECENT snapshot envelope whose
    instance log reference matches ``log_path``; ``None`` if no envelope references it
    (the caller then lets ``AgentInstance.__post_init__`` mint a fresh birth TS).

    This is the bare-log-import carrier: the JSONL is NOT a home for ``kv_birth_ts`` —
    the envelope is (E1b captures it verbatim into ``clocks``). Reading it back here means
    a ``load_session_from_log`` keeps the prior session's save key instead of re-minting,
    which would orphan the prior ``<name>_<oldTS>`` KV file and force a cold start.

    Deliberately does NOT use :func:`load_snapshot_from_disk`: that path is fail-closed
    (integrity validation can refuse an *unrelated* snapshot) and we only need a string,
    so a raw JSON parse keeps this side-effect-free and robust to one bad envelope.
    The match key is the log FILENAME basename — the JSONL filename timestamp (the logger's
    start_time) is immutable and uniquely identifies the session log, so it is a safe join
    key regardless of relative-vs-absolute path spelling. The JSONL filename TS itself is
    never read or written here. Read-only: no pool or disk mutation.
    """
    if not log_path:
        return None
    target = os.path.normcase(os.path.basename(str(log_path)))
    if not target:
        return None
    try:
        snap_dir = get_snapshot_dir(pool)
    except Exception:  # snapshot-dir resolution must never abort a load
        return None
    try:
        candidates = list(Path(snap_dir).glob('*.json')) if snap_dir else []
    except OSError:
        return None
    best_ts: Optional[str] = None
    best_mtime = -1.0
    for f in candidates:
        try:
            raw = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        for snap in _envelope_log_snapshots(raw, target):
            ts = (snap.get('clocks') or {}).get('kv_birth_ts')
            if not ts:
                continue
            try:
                m = f.stat().st_mtime
            except OSError:
                m = 0.0
            if m > best_mtime:
                best_mtime, best_ts = m, ts
    return best_ts


def retire_snapshots_for_log(pool, log_path, *, registry: Optional[StackRegistry] = None) -> int:
    """Drop every live snapshot whose captured log reference matches ``log_path``.

    An in-place JSONL rewrite (compaction / L1->L2 consolidation) changes the log's first
    bytes, so every pinned ``prefix_sha256`` for that log goes stale *by design* -- those
    snapshots are invalidated, not corrupt. Retiring them here (one aggregate INFO line)
    means the next boot's validation pass finds nothing to quarantine, while the agent
    still falls open to the full-lineage JSONL loader. KV files are untouched (KV is a
    cache) and the JSONL log itself is never modified. Best-effort: never raises into the
    compaction that triggered it.

    The match key is the log FILENAME basename -- the same immutable, uniquely-identifying
    join key :func:`find_birth_ts_for_log` uses -- so it is independent of relative-vs-
    absolute path spelling. Envelopes are parsed with a bare ``json.loads`` (NOT
    :func:`load_snapshot_from_disk`, which is fail-closed and could refuse an unrelated
    bad envelope here). The removal reuses :func:`delete_snapshot` so retirement never
    re-implements the unlink / registry-drop. A schema-valid envelope is unlinked directly
    (never routed through ``quarantine_snapshot``); a schema-invalid one is genuinely
    malformed and is quarantined by ``delete_snapshot`` as it should be.
    """
    if not settings.ENABLE_STACK_SWAP:
        return 0
    if not log_path:
        return 0
    target = os.path.normcase(os.path.basename(str(log_path)))
    if not target:
        return 0
    reg = _ensure_registry(pool, registry)

    # Resolve matching (stack_id, snapshot_id) pairs up front: delete_snapshot mutates
    # reg._snapshots, so we must not mutate the structure we are walking.
    matches: List[Tuple[str, str]] = []
    for stack_id, entries in list(reg._snapshots.items()):
        for sid, path_str in list(entries):
            try:
                raw = json.loads(Path(path_str).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue  # unreadable / bad envelope: leave it for the boot scan to handle
            if any(_envelope_log_snapshots(raw, target)):
                matches.append((stack_id, sid))

    retired = 0
    for stack_id, sid in matches:
        try:
            delete_snapshot(pool, stack_id, sid, registry=reg)
            retired += 1
        except Exception as exc:  # best-effort: one failure must not abort the rest
            logger.debug('[STACK_SWAP] could not retire snapshot %s for log %s: %s',
                         sid, log_path, exc)
    if retired:
        logger.info('[STACK_SWAP] retired %d snapshot(s) invalidated by log rewrite %s',
                    retired, log_path)
    return retired


def _instantiate_instance(
    snap: InstanceSnapshot, pool, now_wall: float, now_mono: float) -> 'AgentInstance':
    """Rebuild one fresh ``AgentInstance`` from a captured ``InstanceSnapshot``.

    Split out of :func:`restore_fleet_snapshot` phase 3 so the field mapping is
    unit-testable in isolation. Every value is carried verbatim EXCEPT the monotonic
    clocks (``created_at``/``last_activity``/force-compress), which are re-projected
    from their wall-clock projection onto the restore-time origin (``now_wall`` /
    ``now_mono``). The returned instance is NOT registered — the caller places it
    under ``pool._pool_lock``. ``conversation`` stays empty here: phase 5a's
    ``_reattach_instance_log`` fills it and re-applies ``_last_actual_token_count``
    (which ``rebuild_conversation`` zeroes), so that counter is deliberately NOT set
    here.
    """
    from agent_cascade.agent_instance import AgentInstance, AgentState

    created = wall_to_monotonic(
        snap.clocks['created_at'], now_wall=now_wall, now_mono=now_mono)
    last_act = wall_to_monotonic(
        snap.clocks['last_activity'], now_wall=now_wall, now_mono=now_mono)
    cm = snap.compression
    tc = snap.token_counters
    inst = AgentInstance(
        instance_name=snap.identity['instance_name'],        # identity verbatim — never re-mint
        agent_class=snap.identity['agent_class'],
        conversation=[],                                     # filled by _reattach_instance_log (5a)
        max_turns=snap.budget['max_turns'],
        parent_instance=snap.identity['parent_instance'],
        display_name=snap.identity['display_name'],
        created_at=created,
        last_activity=last_act,
        compression_summary=cm['compression_summary'],
        latest_marker_index=-1,                              # not captured; matches load_session default
    )
    inst._pool_ref = pool
    inst._nest_depth = snap.identity['nest_depth']
    # system_started_at is a wall-clock '%Y-%m-%d %H:%M' string — verbatim, NOT
    # projected. Capture stores it in the clocks block (snapshot.py:~491-495).
    inst.system_started_at = snap.clocks.get('system_started_at')
    # kv_birth_ts (per-session KV key birth TS) carried VERBATIM — a rehydrated instance is the
    # SAME continuing session, so it must keep the same kv_save_label; __post_init__ already
    # minted a fresh stamp on the new AgentInstance, so overwrite it with the captured value.
    # Fall back to the freshly minted stamp only for legacy snapshots with no kv_birth_ts.
    _carried_birth = snap.clocks.get('kv_birth_ts')
    if _carried_birth:
        inst.kv_birth_ts = _carried_birth
    inst.state = AgentState[snap.state['state']]            # provisional; refined in 5b
    inst.is_terminated = bool(snap.state['is_terminated'])
    inst._terminated_reason = snap.state['terminated_reason']
    inst.restricted_shell = bool(snap.state['restricted_shell'])
    # Token counters carried verbatim (ints/bools, no monotonic). _last_actual_token_count
    # is deliberately NOT set here — Phase 5a's rebuild_conversation zeroes it
    # (agent_instance.py:756); it is re-applied after re-attach.
    inst._allocated_max_input_tokens = tc['allocated_max_input_tokens']
    inst._current_turn = tc['current_turn']
    inst._turn_consumed = bool(tc['turn_consumed'])
    # Budget block (max_turns already passed in ctor).
    inst.token_budget = snap.budget['token_budget']
    inst.budget_unit = snap.budget['budget_unit']
    inst._token_warned_50 = bool(snap.budget['token_warned_50'])
    inst._token_warned_90 = bool(snap.budget['token_warned_90'])
    inst._token_final_fired = bool(snap.budget['token_final_fired'])
    # _token_last_warn_past carried RAW, not projected (R5: cadence-only gate;
    # a stale value only re-fires or delays one warning).
    inst._token_last_warn_past = snap.budget['token_last_warn_past']
    # Loop detection carried verbatim (bools/ints).
    ld = snap.loop_detection
    inst._loop_rollback_count = ld['loop_rollback_count']
    inst._suppress_loop_detection_next_turn = bool(ld['suppress_loop_detection_next_turn'])
    inst._fuzzy_warn_armed = bool(ld['fuzzy_warn_armed'])
    inst._fuzzy_warn_last_turn = ld['fuzzy_warn_last_turn']
    inst._fuzzy_escalation_armed = bool(ld['fuzzy_escalation_armed'])
    # Memory hints.
    mh = snap.memory_hints
    inst._memories_read = set(mh['memories_read'])
    inst._skill_hint_counts = dict(mh['skill_hint_counts'])
    inst._last_memory_hint_turn = mh['last_memory_hint_turn']
    inst._last_skill_hint_turn = mh['last_skill_hint_turn']
    # LLM override + system-prompt frozen state, verbatim.
    inst._generate_cfg_override = snap.llm_override['generate_cfg_override']
    inst._system_prompt_frozen = bool(snap.system_prompt['system_prompt_frozen'])
    inst._system_prompt_initialized = bool(snap.system_prompt['system_prompt_initialized'])
    # Compression counters verbatim + the two projected force-compress clocks.
    inst._force_compress_count = cm['force_compress_count']
    inst._force_compress_fail_streak = cm['force_compress_fail_streak']
    inst._last_force_compress_time = wall_to_monotonic(
        cm['last_force_compress_time'], now_wall=now_wall, now_mono=now_mono)
    inst._last_force_compress_fail_at = wall_to_monotonic(
        cm['last_force_compress_fail_at'], now_wall=now_wall, now_mono=now_mono)
    return inst


def restore_fleet_snapshot(pool, envelope: SnapshotEnvelope) -> None:
    """D-CONTINUE rehydration — THE ONLY state-mutating S3 entry point (plan §8 gate).

    Phases (fail-closed; nothing touches the pool until Phase 3 passes):
      1 gate · 2 validate+classify · 3 instantiate+register · 4 topology
      5 log re-attach + graph re-formation · 6 KV verify/arm · 7 message re-enqueue.
    Raises :class:`SnapshotIntegrityError` (log gate / name collision) BEFORE any
    mutation; returns ``None``. NEVER reads or writes ``pool._instance_threads`` or
    ``_slot_release`` (m2 invariant): the execution graph is re-seeded from captured
    ORDERING only — no live handle, thread, or Future is resurrected. No I/O runs
    under a pool lock and no pool lock is nested.
    """
    # Phase 1 — the ONLY mutation gate (HARD CONSTRAINT 1). Zero mutation off-path.
    if not settings.ENABLE_STACK_SWAP:
        logger.debug('[FLEET_RESTORE] ENABLE_STACK_SWAP off — no-op')
        return

    ps = envelope.pool_state

    # Phase 2 — validation BEFORE any pool mutation (M2 all-or-nothing).
    validate_envelope_integrity(envelope)                       # raises SnapshotIntegrityError
    collisions = set(pool.instances) & set(envelope.instances)  # plain dict read; GIL-safe
    if collisions:
        raise SnapshotIntegrityError(
            f'live instances collide with snapshot: {sorted(collisions)}'
        )

    # Phase 3 — instantiate fresh AgentInstance objects (no thread/slot touched).
    now_wall = time.time()
    now_mono = time.monotonic()
    from agent_cascade.agent_instance import AgentState

    for name, snap in envelope.instances.items():
        inst = _instantiate_instance(snap, pool, now_wall, now_mono)
        with pool._pool_lock:
            pool.instances[name] = inst
            pool._instances_version += 1

    # Phase 4 — pool topology. NO lock nesting: three separate single-lock passes,
    # mirroring the authoritative writers (_update_child_relationship / lifecycle),
    # since children + per-instance child lists are NOT both guarded by _children_lock.
    with pool._children_lock:
        for parent, kids in ps.children.items():
            existing = pool.children.setdefault(parent, [])
            existing.extend(k for k in kids if k not in existing)
    for parent, kids in ps.children.items():
        inst = pool.instances.get(parent)
        if inst is not None:
            with inst._state_lock:
                inst._child_instances = list(kids)
    terminated_set = set(ps.terminated_instances)            # set-like fields are lists
    with pool._pool_lock:
        pool.instance_summaries.update(ps.instance_summaries)
        pool._halted_instances.update(ps.halted_instances)
        pool._compression_halted.update(ps.compression_halted)
        pool.terminated_instances.update(ps.terminated_instances)   # merge, NOT replace

    # Phase 5a — log re-attach (per instance, OUTSIDE all pool locks — this does file I/O).
    for name, snap in envelope.instances.items():
        inst = pool.instances[name]
        pool._reattach_instance_log(inst, snap.log, mcp_block=snap.mcp,
                                    snapshot_id=envelope.snapshot_id)
        # Re-apply after rebuild_conversation (5a) zeroed it (agent_instance.py:756).
        inst._last_actual_token_count = snap.token_counters['last_actual_token_count']

    # Phase 5b — resume classification (data-level; state writes under each instance lock).
    for name, snap in envelope.instances.items():
        inst = pool.instances[name]
        captured = snap.state['state']
        with inst._state_lock:
            if name in terminated_set or snap.state['is_terminated']:
                inst.state = AgentState.TERMINATED
                inst.is_terminated = True
            elif captured == 'COMPLETING':
                inst.state = AgentState.RUNNING              # transient cleanup state; do not strand
                logger.info('[FLEET_RESTORE] %s COMPLETING -> RUNNING (no cleanup thread)', name)
            # else: captured IDLE stays IDLE; captured RUNNING/SLEEPING kept verbatim
            # (never blanket to IDLE/RUNNING — plan §6 "Never blanket").

    # Phase 5c — re-seed the call graph from captured ORDERING only (m2). active_stack is
    # a concrete List[tuple] guarded by _execution._state_lock (parallel_manager.py:24-27),
    # so an in-place slice re-seed under that lock is the only (non-nested) path.
    stack = pool._execution.active_stack
    restored_order = [(e[0], e[1]) for e in ps.active_stack if e[0] in pool.instances]
    missing = [e[0] for e in ps.active_stack if e[0] not in pool.instances]
    if missing:
        logger.warning('[FLEET_RESTORE] stack entries absent from snapshot, dropped: %s', missing)
    with pool._execution._state_lock:
        stack[:] = restored_order

    # Phase 5d — re-block parents from captured parent->child waiting edges. Edges are used
    # for STATE SELECTION + validation only, never as a runtime container (no live blocking_calls
    # field on the pool; adding one = dead code).
    index = {name: i for i, (name, _d) in enumerate(restored_order)}
    for edge in ps.blocking_calls:
        parent, child = edge['parent'], edge['child']
        if parent not in index or child not in index:
            logger.warning('[FLEET_RESTORE] blocking edge references non-restored name, skipped: %r', edge)
            continue
        if index[parent] >= index[child]:
            logger.warning('[FLEET_RESTORE] blocking edge order inconsistent with stack, skipped: %r', edge)
            continue
        inst = pool.instances[parent]
        with inst._state_lock:
            inst.state = AgentState.RUNNING                 # a blocked parent was RUNNING live

    # Phase 6 — KV verify/arm per streaming instance (verify+arm only; plan §5).
    from agent_cascade import state_ops
    for name, snap in envelope.instances.items():
        if snap.kv is None:
            continue
        inst = pool.instances[name]
        if inst.is_terminated:
            continue
        if not state_ops.verify_and_restore_instance_kv(pool, inst, snap.kv):
            logger.info(
                '[FLEET_RESTORE] %s — KV not restorable, falling back to message-only cold start', name)

    # Phase 7 — message re-enqueue (self-locking, OUTSIDE pool locks). Per-instance FIFO
    # order is preserved by enqueue; terminated/dead names drop their queued messages.
    for name, texts in ps.message_queues.items():
        if name in pool.terminated_instances or name not in pool.instances:
            logger.debug('[FLEET_RESTORE] dropping queued messages for dead instance %s', name)
            continue
        for text in texts:
            pool.enqueue_message(name, text)                # self-locks _queue_lock; non-blocking


# ── Park / Activate swap primitive & stack registry (S4) ─────────────────────
#
# A "stack" is one parked-or-active fleet (the set of instances reachable from one
# root). At most one stack is active in the pool at a time; parked stacks live on
# disk as snapshot envelopes. The swap primitive parks the active stack (bounded
# grace window per in-flight turn, then force-halt + discard on expiry, then real
# async teardown) and activates a parked one — atomically, with rollback so the
# pool is never left empty. All behaviour is gated behind ``settings.ENABLE_STACK_SWAP``.

#: Standard killed-tool-result text injected when a wedged in-flight turn is force-
#: halted. Matches ``tool_execution._handle_orphaned_tool_calls`` verbatim so a
#: discarded turn and a normally-halted turn carry the same coherent tool result;
#: the async-shell/CLI heartbeat variant of this result is emitted by the trackers'
#: ``kill_all`` themselves, not duplicated here.
KILLED_TOOL_RESULT_TEXT = 'Tool execution skipped: instance {inst_name} was halted/stopped'

#: Standard marker text substituted for the result of an in-flight or not-yet-run
#: tool when the user presses the Terminate-tool button. Distinct from
#: ``KILLED_TOOL_RESULT_TEXT``: terminate does NOT halt the run — the turn
#: completes normally with these markers and the agent stays RUNNING, so the
#: wording names an explicit user abort rather than a halt/stop. Emitted by
#: ``tool_execution`` for both the not-yet-dispatched tools (loop head) and the
#: currently-blocking tool (post-dispatch overwrite); the foreground OS-tree kill
#: unblocks the blocking call, but a kill failure never breaks this marker path.
TOOL_TERMINATED_TEXT = '[TOOL TERMINATED] execution aborted by user request (instance {inst_name})'


class StackRegistry:
    """Which stacks are known, which is active, and where each snapshot lives.

    A lightweight, pool-adjacent bookkeeping object (NOT the source of truth for
    live instance state — that stays on the pool). It only records ``stack_id`` →
    ordered list of ``(snapshot_id, path)`` and the currently-active ``stack_id``,
    so callers can resolve a parked stack back to a readable envelope file.

    The active-stack pointer is the ONLY field that gates user-facing behaviour
    (e.g. the REST layer must refuse to DELETE it), so it is kept as a single
    string updated under the pool's lock-free contract (read-mostly, one writer).
    """

    def __init__(self) -> None:
        self.active_stack_id: Optional[str] = None
        #: stack_id -> list of (snapshot_id, path_str) in write order (oldest first).
        self._snapshots: Dict[str, List[List[str]]] = {}

    def register(self, stack_id: str) -> None:
        """Ensure ``stack_id`` has a (possibly empty) snapshot list."""
        self._snapshots.setdefault(stack_id, [])

    def add_snapshot(self, stack_id: str, snapshot_id: str, path: str) -> None:
        """Record a freshly written snapshot for ``stack_id``."""
        self.register(stack_id)
        self._snapshots[stack_id].append([snapshot_id, str(path)])

    def list_snapshots(self, stack_id: str) -> List[str]:
        """Snapshot ids for ``stack_id`` in write order."""
        return [sid for sid, _path in self._snapshots.get(stack_id, [])]

    def resolve_path(self, stack_id: str, snapshot_id: Optional[str] = None) -> Optional[str]:
        """Path of ``snapshot_id`` (latest when ``snapshot_id`` is None), else None."""
        entries = self._snapshots.get(stack_id, [])
        if snapshot_id is None:
            return entries[-1][1] if entries else None
        for sid, path in entries:
            if sid == snapshot_id:
                return path
        return None

    def set_active(self, stack_id: Optional[str]) -> None:
        """Point the pool at ``stack_id`` (None = nothing active, e.g. mid-swap)."""
        self.active_stack_id = stack_id


def _ensure_registry(pool, registry: Optional[StackRegistry] = None) -> StackRegistry:
    """Return the pool's stack registry, creating it on first use.

    Lazily attached so a pool built before ENABLE_STACK_SWAP ever turned on is
    untouched; once created it persists on the pool for the swap lifetime.
    """
    if registry is not None:
        return registry
    reg = getattr(pool, '_swap_registry', None)
    if reg is None:
        reg = StackRegistry()
        pool._swap_registry = reg
    return reg


_CORRUPT_DIRNAME = '_corrupt'
_REASON_SIDECAR_SUFFIX = '.reason.txt'


def snapshot_problem(path: Path) -> Optional[str]:
    """Return a human reason when a snapshot is NOT safely restorable, else None.

    Checks: readable envelope + schema, then the append-tolerant log gate (§6.3.2).
    A missing, truncated or rewritten log is corruption; a log that merely grew is a
    normal live save and stays valid.
    """
    try:
        env = load_snapshot_from_disk(path)
        validate_envelope_integrity(env)
    except (ValueError, OSError, SnapshotIntegrityError) as exc:
        return str(exc)
    return None


def quarantine_snapshot(path: Path, reason: str) -> Optional[Path]:
    """Move a corrupt snapshot into ``snapshots/_corrupt/`` (never delete) + reason sidecar."""
    target_dir = path.parent / _CORRUPT_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    try:
        os.replace(path, target)
        (target_dir / f'{path.name}{_REASON_SIDECAR_SUFFIX}').write_text(reason, encoding='utf-8')
        logger.warning('[STACK_SWAP] quarantined corrupt snapshot %s: %s', path, reason)
        return target
    except OSError as exc:
        logger.warning('[STACK_SWAP] could not quarantine %s: %s', path, exc)
        return None


def ensure_registry_loaded(pool) -> StackRegistry:
    """Rebuild the registry from disk once (envelopes = source of truth).

    Registers only snapshots that pass snapshot_problem(); corrupt files are moved to
    ``_corrupt/`` so they neither surface in the UI nor block the rest. Sorted by
    ``created_at``; duplicate check keeps live park entries. Never raises.
    """
    reg = _ensure_registry(pool)
    if not settings.ENABLE_STACK_SWAP or getattr(pool, '_swap_registry_loaded', False):
        return reg
    snap_dir = get_snapshot_dir(pool)
    entries = []
    if snap_dir.exists():
        for path in sorted(snap_dir.glob('*.json')):
            reason = snapshot_problem(path)
            if reason:
                quarantine_snapshot(path, reason)
                continue
            try:
                env = load_snapshot_from_disk(path)
            except (ValueError, OSError):
                continue
            entries.append((env.created_at, env.stack_id, env.snapshot_id, str(path)))
    entries.sort(key=lambda e: (e[0], e[2]))
    for _created, stack_id, snapshot_id, path_str in entries:
        reg.register(stack_id)
        if snapshot_id not in reg.list_snapshots(stack_id):
            reg.add_snapshot(stack_id, snapshot_id, path_str)
    from agent_cascade.session_state_store import load_active_stack_id
    persisted = load_active_stack_id()
    reg.set_active(persisted if persisted in reg._snapshots else None)
    pool._swap_registry_loaded = True
    # Bound the quarantine backlog once per boot: this scan is itself the pass that can
    # produce new _corrupt/ entries, so pruning here needs no separate boot hook.
    try:
        prune_corrupt_snapshots(pool)
    except Exception as exc:  # backlog cleanup must never abort the registry load
        logger.debug('[SNAPSHOT_PRUNE] corrupt-snapshot prune failed: %s', exc)
    return reg


def _persist_active_stack(stack_id: Optional[str]) -> None:
    from agent_cascade.session_state_store import save_active_stack_id
    save_active_stack_id(stack_id)


def register_active_stack(pool, stack_id: str) -> None:
    reg = ensure_registry_loaded(pool)
    reg.register(stack_id)
    reg.set_active(stack_id)
    _persist_active_stack(stack_id)


def set_active_stack(pool, stack_id: Optional[str]) -> None:
    reg = ensure_registry_loaded(pool)
    reg.set_active(stack_id)
    _persist_active_stack(stack_id)


def load_boot_stack(pool) -> Optional[SnapshotEnvelope]:
    """Envelope to activate at boot, or None to use the legacy log loader.

    Uses the persisted active stack id and its newest *valid* snapshot. Anything that
    would make the restore unsafe (unreadable envelope, missing/diverged log) returns
    None — fail-open to the legacy path and never touch the pool first.
    """
    reg = ensure_registry_loaded(pool)
    target = reg.active_stack_id
    if not target:
        return None
    path_str = reg.resolve_path(target)          # newest
    if not path_str:
        return None
    try:
        env = load_snapshot_from_disk(Path(path_str))
        validate_envelope_integrity(env)         # missing/truncated/diverged log -> legacy
    except (ValueError, OSError, SnapshotIntegrityError) as exc:
        logger.warning('[STACK_SWAP] boot snapshot unusable (%s) - legacy load', exc)
        return None
    return env


def _swap_latch(pool) -> Optional[threading.Event]:
    """Return (and lazily create) the per-pool turn-boundary swap latch.

    The real pool mints ``_pending_swap_event`` in ``__init__`` under the feature
    flag; this self-heals it for pools built while the flag was off (e.g. a test
    that flips the flag later), so park always has a latch to set and run_loop,
    which reads it with a ``getattr`` default, can observe it.
    """
    ev = getattr(pool, '_pending_swap_event', None)
    if ev is None:
        ev = threading.Event()
        pool._pending_swap_event = ev
    return ev


def _inflight_turn_instances(pool) -> List[Any]:
    """Instances whose run loop is mid-turn (the turns a grace window protects).

    In-flight == state ``RUNNING`` and not terminated: that is exactly the window
    during which the engine is in an LLM call or tool batch, i.e. the point a
    force-halt could corrupt. ``IDLE`` instances are already at a turn boundary;
    ``SLEEPING`` instances are parked on async results (drained, not interrupted)
    and ``TERMINATED`` are gone. State reads are GIL-atomic single reads — a read-
    only writer needs no lock.
    """
    out: List[Any] = []
    for name, inst in list(dict(getattr(pool, 'instances', {})).items()):
        if name in getattr(pool, 'terminated_instances', set()):
            continue
        state = getattr(inst, 'state', None)
        if state is not None and getattr(state, 'name', None) == 'RUNNING':
            out.append(inst)
    return out


def _kill_instance_tools(pool, name: str) -> None:
    """Kill a single instance's in-flight background tools via the REAL trackers.

    Delegates to the shared ``kill_instance_tools`` helper (best-effort per
    tracker so one tracker's absence or failure never aborts the park).
    """
    from agent_cascade.async_shell_pkg.kill import kill_instance_tools
    kill_instance_tools(pool, name)


def _drain_async_tasks(pool, names: List[str]) -> None:
    """Terminate every instance's background tools, then shut down the async executor.

    M4: ``AsyncShellTracker.kill_all`` for shell and CLI tasks per instance, then a
    single non-blocking ``_async_registry.shutdown(wait=False)``. ``kill_all`` is
    idempotent (already-killed tasks return 0), so it is safe to run this fleet-wide
    even after a force-halt already killed a wedged instance's tools. Tracker
    lookups are guarded so a double without trackers simply no-ops.
    """
    for name in names:
        _kill_instance_tools(pool, name)
    registry = getattr(pool, '_async_registry', None)
    if registry is not None and hasattr(registry, 'shutdown'):
        try:
            registry.shutdown(wait=False)
        except Exception as e:  # noqa: BLE001 — executor shutdown is non-critical to the park
            logger.debug('[STACK_SWAP] async registry shutdown failed (non-critical): %s', e)


def _msg_get(msg, key, default=None):
    """Read ``key`` off a Message that is either an attr-style model or a dict."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _inject_killed_tool_result(pool, inst, name: str) -> None:
    """Close a force-halted turn's dangling tool calls with the standard killed result.

    Mirrors ``tool_execution._handle_orphaned_tool_calls`` in SHAPE: for every trailing
    assistant ``function_call`` (this codebase uses the legacy single-call model — the id
    lives in ``extra.function_id``) that has NO matching FUNCTION result yet, append one
    ``Message(role=FUNCTION, name=call.name, content=<standard text>,
    extra={'function_id': <call id>, 'tool_success': False})``. Pairing by ``function_id``
    is what keeps the half-discarded turn free of orphaned call ids on the next restored
    LLM call. If the turn has no unpaired call, nothing is appended — a lone FUNCTION
    result with no matching call is itself an invalid shape and would 400 on replay.

    This is a best-effort coherence close at park time for a turn whose engine thread is
    wedged in a blocking tool and may never reach the live orphan handler. The FUNCTION
    close below is appended under ``inst._compression_lock`` -- the same lock the engine's
    ``_handle_orphaned_tool_calls`` serializes on -- but that lock only SERIALIZES the two
    appends against interleaving; it does NOT by itself stop the double-close. What
    discards a tool that unblocks mid-park is the identity halt: the run-loop turn-boundary
    gate (engine/run_loop.py) and the engine-side ``_swap_identity_lost`` guard
    (engine/tool_execution.py) drop the late real result once this instance has been parked
    out of ``pool.instances``, so exactly one FUNCTION result -- this killed close -- stands
    per function_id.
    Best-effort throughout: a double without a list ``conversation`` is skipped silently.
    """
    from agent_cascade.llm.schema import FUNCTION, Message
    from contextlib import nullcontext

    text = KILLED_TOOL_RESULT_TEXT.format(inst_name=name)
    conversation = getattr(inst, 'conversation', None)
    if conversation is None or not hasattr(conversation, 'append') or not hasattr(conversation, '__iter__'):
        return
    # Serialize against the engine's orphan handler (engine/tool_execution.
    # _handle_orphaned_tool_calls), which appends FUNCTION results under this same lock.
    # The lock keeps the answered_ids scan + append below atomic vs the orphan handler so
    # the scan is not torn mid-park. It does NOT prevent the late double close -- that is
    # the engine-side _swap_identity_lost guard, which drops the wedged tool's real result
    # once this instance is parked out of pool.instances. Doubles without a lock (capture
    # tests) fall through to nullcontext and behave as before.
    lock = getattr(inst, '_compression_lock', None)

    injected = []
    try:
        with (lock if lock is not None else nullcontext()):
            msgs = list(conversation)

            # Collect ids/names already answered by a FUNCTION result.
            answered_ids, answered_names = set(), set()
            for m in msgs:
                if _msg_get(m, 'role') == FUNCTION:
                    fc_extra = _msg_get(m, 'extra') or {}
                    if fc_extra.get('function_id'):
                        answered_ids.add(fc_extra['function_id'])
                    if _msg_get(m, 'name'):
                        answered_names.add(_msg_get(m, 'name'))

            # Close every trailing assistant function_call that has no matching result.
            for m in msgs:
                if _msg_get(m, 'role') != 'assistant':
                    continue
                fc = _msg_get(m, 'function_call')
                if fc is None:
                    continue
                call_name = _msg_get(fc, 'name') or _msg_get(m, 'name')
                call_id = (_msg_get(m, 'extra') or {}).get('function_id') or '1'
                if (call_id and call_id in answered_ids) or (call_name and call_name in answered_names):
                    continue
                fn_msg = Message(
                    role=FUNCTION, name=call_name or 'stack_swap', content=text,
                    extra={'function_id': call_id, 'tool_success': False},
                )
                conversation.append(fn_msg)
                injected.append(fn_msg)
                answered_ids.add(call_id)
                if call_name:
                    answered_names.add(call_name)
    except Exception as e:  # noqa: BLE001 — schema build must never abort the park
        logger.debug('[STACK_SWAP] could not inject killed-tool result for %s: %s', name, e)

    # Mirror the injected closes to the on-disk JSONL so the restored log is coherent.
    # Mirrors only what THIS call appended (``injected``), kept OUTSIDE the compression
    # lock so disk IO never holds it; best-effort, never raises.
    loggers = getattr(pool, 'instance_loggers', None)
    try:
        logger_obj = loggers.get(name) if (loggers is not None and hasattr(loggers, 'get')) else None
        if logger_obj is not None and hasattr(logger_obj, 'log_message'):
            for m in injected:
                logger_obj.log_message({
                    'role': FUNCTION, 'name': _msg_get(m, 'name'),
                    'content': _msg_get(m, 'content'),
                    'extra': _msg_get(m, 'extra'),
                    'metadata': {'instance_name': name},
                })
    except Exception as e:  # noqa: BLE001 — logging the discarded turn is non-critical
        logger.debug('[STACK_SWAP] could not log killed-tool result for %s: %s', name, e)


def _force_halt_turn(pool, inst, name: str) -> None:
    """Force-halt + discard one wedged in-flight turn at its last coherent point.

    Order is deliberate: kill the instance's background tools FIRST (so nothing still
    writing produces a result for a turn we are discarding), mark the instance halted,
    transition it to ``IDLE`` via the engine's canonical ``_transition`` (the discarded-
    turn boundary — NOT ``TERMINATED``; the instance itself is preserved and rehydrates
    later, and the run-loop identity gate keeps its wedged thread from resuming), then
    inject the paired killed-tool-results to close the partial turn's tool calls.
    """
    _kill_instance_tools(pool, name)
    halted = getattr(pool, '_halted_instances', None)
    if halted is not None and hasattr(halted, 'add'):
        halted.add(name)
    try:
        from agent_cascade.agent_instance import AgentState
        transition = getattr(inst, '_transition', None)
        if callable(transition):
            transition(AgentState.IDLE)
        else:
            inst.state = AgentState.IDLE
    except Exception as e:  # noqa: BLE001 — state transition must not block the park
        logger.warning('[STACK_SWAP] could not transition wedged turn %s to IDLE: %s', name, e)
        try:
            from agent_cascade.agent_instance import AgentState
            inst.state = AgentState.IDLE
        except Exception:  # noqa: BLE001 — last-resort direct set, park must proceed
            pass
    _inject_killed_tool_result(pool, inst, name)


def _release_held_slots(pool, instances: Dict[str, Any]) -> None:
    """Release sticky-slot permits held by the instances being parked.

    A parked stack must not keep an autoloader slot resident — otherwise the slot
    stays pinned against a stack that is no longer running. Mirrors the capture-and-
    nullify-release pattern of ``lifecycle._release_slot_on_lifecycle_event``: only
    instances actually holding a permit (``_slot_release`` set) are released, via the
    real ``slot_queue.release_slot_permit`` seam. Fully best-effort and inert when
    the scheduler seam is absent (a bare double, or a pool with no router) so it does
    nothing the pool was not already doing.
    """
    try:
        from agent_cascade.slot_queue import release_slot_permit
    except Exception as e:  # noqa: BLE001 — slot teardown is non-critical to the park
        logger.debug('[STACK_SWAP] slot release unavailable, skipping: %s', e)
        return
    scheduler = getattr(getattr(pool, 'api_router', None), 'scheduler', None)
    pools = getattr(scheduler, '_pools', None) if scheduler is not None else None
    for name, inst in instances.items():
        if getattr(inst, '_slot_release', None) is None:
            continue
        try:
            _pool = None
            if pools is not None:
                _pool = pools.get(getattr(inst, '_slot_key', None))
            release_slot_permit(inst, name, action='drop-park',
                                context='stack park', pool=_pool)
        except Exception as e:  # noqa: BLE001 — a failed permit release must not abort the park
            logger.debug('[STACK_SWAP] slot release failed for %s (non-critical): %s', name, e)


def _clear_pool_state(pool) -> None:
    """Empty the pool's live fleet containers (parked → pool is empty).

    Runs with each guard acquired in turn, never nested, mirroring ``reset_pool`` /
    the restore-side dismiss: ``_pool_lock`` for the instance dicts + terminated
    sets, ``_children_lock`` for the child graph, ``_execution._state_lock`` for the
    active call stack, ``_queue_lock`` for the message queues. Locks are lookups with
    a benign ``threading.Lock()`` default so a double lacking a guard is not crashed
    by a clear. Does NOT touch threads or slot machinery — those are handled by the
    tracker drain (M4) and slot release (above), keeping the m2 no-thread-handle
    invariant intact.
    """
    with getattr(pool, '_pool_lock', threading.Lock()):
        instances = getattr(pool, 'instances', None)
        if instances is not None:
            instances.clear()
        for attr in ('instance_summaries', 'terminated_instances',
                     '_halted_instances', '_compression_halted'):
            target = getattr(pool, attr, None)
            if target is not None and hasattr(target, 'clear'):
                target.clear()
    with getattr(pool, '_children_lock', threading.Lock()):
        children = getattr(pool, 'children', None)
        if children is not None:
            children.clear()
    execution = getattr(pool, '_execution', None)
    if execution is not None:
        stack = getattr(execution, 'active_stack', None)
        if stack is not None and hasattr(stack, 'clear'):
            with getattr(execution, '_state_lock', threading.Lock()):
                stack.clear()
    with getattr(pool, '_queue_lock', threading.Lock()):
        queues = getattr(pool, 'message_queues', None)
        if queues is not None:
            queues.clear()


def park_active_stack(
    pool,
    *,
    trigger: str = 'swap',
    stack_id: Optional[str] = None,
    grace_seconds: Optional[float] = None,
    kv_saver=None,
    sleeper=time.sleep,
    clock=time.monotonic,
) -> Optional[SnapshotEnvelope]:
    """Park the active stack into a snapshot and empty the pool, readying a swap.

    Sequence (all gated on ``ENABLE_STACK_SWAP``; returns ``None`` when off):
      1. Latch the turn-boundary swap event so every run loop stops at its next
         boundary (cooperative, work preserved).
      2. Grace window (``settings.SNAPSHOT_GRACE_SECONDS``): wait for in-flight turns
         to reach that boundary. If they do, the swap is clean.
      3. On expiry, force-halt every still-in-flight turn: kill its background tools
         on the spot, discard the turn at the last coherent point, inject the
         standard killed-tool-result — then CONTINUE (never a ``SnapshotTimeoutError``
         rejection) (D-GRACE / D-KILLED).
      4. Drain all instances' async tasks via the real trackers + executor shutdown.
      5. Capture the fleet snapshot, write it to disk, register it, release held
         slots, and clear the pool. The in-memory envelope is returned so ``swap_stack``
         can roll back to it without a re-read.

    Args:
        pool: The live ``AgentPool``.
        trigger: Snapshot trigger label (``'swap'`` by default).
        stack_id: Stack key; defaults to the root/active-stack head (capture decides).
        grace_seconds: Override the grace window (tests); ``None`` → settings value.
        kv_saver: Injectable KV saver, forwarded to ``capture_fleet_snapshot``.
        sleeper / clock: Time seams so the grace window is testable without real waits.
    """
    if not settings.ENABLE_STACK_SWAP:
        logger.debug('[STACK_SWAP] park requested but ENABLE_STACK_SWAP is off — no-op')
        return None

    # 1. Latch the swap so running turns wind down at their next boundary (M6).
    latch = _swap_latch(pool)
    if latch is not None:
        latch.set()

    # 2. Bounded grace window. Read the knob AT CALL time so a flipped setting takes
    #    effect immediately and the off-path never reads it.
    grace = settings.SNAPSHOT_GRACE_SECONDS if grace_seconds is None else grace_seconds
    deadline = clock() + max(0.0, grace)
    while True:
        inflight = _inflight_turn_instances(pool)
        if not inflight or clock() >= deadline:
            break
        sleeper(min(0.05, max(0.0, deadline - clock())))
    inflight = _inflight_turn_instances(pool)  # final sweep: who is still wedged?

    # 3. Force-halt + discard each still-wedged turn (D-GRACE / D-KILLED).
    if inflight:
        logger.info('[STACK_SWAP] grace window (%.1fs) expired for %d in-flight turn(s), '
                    'force-halting: %s', grace, len(inflight),
                    [i.instance_name for i in inflight])
        for inst in inflight:
            _force_halt_turn(pool, inst, inst.instance_name)

    # 4. Drain async tasks fleet-wide via the real APIs (M4).
    drained = sorted(set(dict(getattr(pool, 'instances', {})).keys()))
    _drain_async_tasks(pool, drained)

    # 5. Capture the snapshot (read-only copy-out) — this is the retained-in-memory
    #    envelope used for rollback if the subsequent activation fails.
    env = capture_fleet_snapshot(pool, trigger=trigger, stack_id=stack_id, kv_saver=kv_saver)
    if env is None:
        # capture_fleet_snapshot is itself gated; a flip between our gate check and here
        # (or an empty pool) yields None. The fleet was latched + any wedged turns force-
        # halted but NOT cleared, so callers (swap_stack) un-latch and abort rather than
        # activate into a half-parked pool with no rollback envelope.
        logger.warning('[STACK_SWAP] capture returned None during park — returning None '
                       'for caller to un-latch and abort (pool NOT cleared)')
        return None

    # Persist + register the snapshot so a future swap can resolve it by id.
    reg = _ensure_registry(pool)
    reg.register(env.stack_id)
    path = write_snapshot_to_disk(env, get_snapshot_dir(pool))
    reg.add_snapshot(env.stack_id, env.snapshot_id, str(path))

    # Enforce per-stack retention (OQ1): retire this stack's oldest snapshots beyond
    # SNAPSHOT_RETENTION_COUNT and delete any KV they carried (m3). Best-effort and
    # gated, so it never aborts the park.
    prune_snapshots(pool, registry=reg)

    # Release sticky-slot permits before emptying the fleet, using the instance objects
    # we are about to drop (still in the pool at this point).
    _release_held_slots(pool, dict(getattr(pool, 'instances', {})))

    # Flush a telemetry checkpoint so the outgoing stack's running totals survive
    # this swap (and any later process restart) no matter how this process ends.
    # Done BEFORE clearing pool state, while the collector still tracks the source
    # stack — this is the continuity guarantee for the next resume/swap-back.
    telem = getattr(pool, 'telemetry', None)
    if telem is not None and settings.ENABLE_STACK_SWAP and hasattr(telem, 'write_checkpoint'):
        try:
            telem.write_checkpoint()
        except Exception:
            logger.exception('[STACK_SWAP] telemetry write_checkpoint failed before park')

    # Empty the pool; the swap event stays latched until activation clears it so any
    # straggler thread cannot resume into the now-empty (or target) pool mid-swap.
    _clear_pool_state(pool)
    reg.set_active(None)
    _persist_active_stack(None)
    logger.info('[STACK_SWAP] parked stack %s (%d instance(s), snapshot %s)',
                env.stack_id, len(env.instances), env.snapshot_id)
    return env


def normalize_boot_restore_states(pool) -> None:
    """A boot-restore has NO live run threads (unlike a runtime swap), so any
    restored instance left in a live-run state (RUNNING/SLEEPING/COMPLETING) is STALE —
    the engine's IDLE->RUNNING entry guard would reject the first run and strand queued
    messages (the UI shows the root "idle" while its internal state is RUNNING).

    Normalize live-run states to IDLE (TERMINATED kept verbatim). This is the
    boot-restore exception to phase 5b's "never blanket" rule, which is correct for
    runtime swaps (where the live run threads continue) but not for a fresh boot
    (where they do not).
    """
    from agent_cascade.agent_instance import AgentState
    for inst in pool.instances.values():
        with inst._state_lock:
            if inst.state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
                prev = inst.state
                inst.state = AgentState.IDLE
                logger.info('[FLEET_RESTORE] boot-restore: %s %s -> IDLE (no live run thread)',
                            inst.instance_name, prev.name)


def activate_stack(pool, envelope: SnapshotEnvelope, *, gate_checked: bool = False) -> None:
    """Activate a parked stack by rehydrating ``envelope`` into the (empty) pool.

    Thin, gated wrapper around :func:`restore_fleet_snapshot` that also re-points the
    registry and clears the swap latch so the newly active stack's run loops start
    running. Assumes the pool is empty (as park leaves it) — the restore's name-
    collision gate is the backstop if it is not. Raises whatever restore raises
    (``SnapshotIntegrityError`` / ``ValueError``) for the caller's rollback.

    ``gate_checked=True`` (used by :func:`swap_stack`, which has already confirmed the
    feature is enabled at entry) skips the off-state early-return so a runtime flag flip
    mid-swap can never turn an activation into a silent no-op that leaves the pool empty
    while logging success (M2 "under no condition").
    """
    if not gate_checked and not settings.ENABLE_STACK_SWAP:
        logger.debug('[STACK_SWAP] activate requested but ENABLE_STACK_SWAP is off — no-op')
        return

    restore_fleet_snapshot(pool, envelope)

    reg = _ensure_registry(pool)
    reg.set_active(envelope.stack_id)
    _persist_active_stack(envelope.stack_id)
    # Re-point telemetry at the now-active stack so its aggregates track whichever
    # stack is live (per-stack continuity). This single hook covers boot-resume and
    # runtime swap; attach_session is idempotent, so the normal boot (which already
    # opened this stack's keyed file in the collector __init__) is a no-op. The swap
    # rollback path re-invokes this function with the source envelope, so telemetry
    # follows the pool back to the source without a separate hook.
    telem = getattr(pool, 'telemetry', None)
    if telem is not None and settings.ENABLE_STACK_SWAP and hasattr(telem, 'attach_session'):
        try:
            telem.attach_session(envelope.stack_id)
        except Exception:
            logger.exception('[STACK_SWAP] telemetry attach_session(%s) failed', envelope.stack_id)
    # Clear the latch so the activated stack begins executing again.
    latch = getattr(pool, '_pending_swap_event', None)
    if latch is not None:
        latch.clear()
    logger.info('[STACK_SWAP] activated stack %s (%d instance(s))',
                envelope.stack_id, len(envelope.instances))


def _prevalidate_target(pool, reg: StackRegistry, target_stack_id: str,
                        target_snapshot_id: Optional[str]) -> SnapshotEnvelope:
    """Load + integrity-check the target stack's snapshot WITHOUT touching the pool.

    All-or-nothing gate (M2): resolve the snapshot file, validate schema (via
    :func:`load_snapshot_from_disk`) and referenced-log integrity (via
    :func:`validate_envelope_integrity`) BEFORE any parking occurs. Raises
    ``ValueError`` (missing/malformed snapshot) or ``SnapshotIntegrityError``
    (missing/truncated log) so an invalid target is rejected with the active stack
    untouched. Returns the reconstructed envelope on success.
    """
    path = reg.resolve_path(target_stack_id, target_snapshot_id)
    if path is None:
        raise ValueError(
            f'target stack {target_stack_id!r} has no registered snapshot '
            f'{'with id ' + repr(target_snapshot_id) if target_snapshot_id else ''}'
        )
    envelope = load_snapshot_from_disk(Path(path))          # schema/version validation
    validate_envelope_integrity(envelope)                   # log-integrity gate (refinement 4)
    if envelope.stack_id != target_stack_id:
        raise SnapshotIntegrityError(
            f'target snapshot {envelope.snapshot_id!r} belongs to stack '
            f'{envelope.stack_id!r}, not {target_stack_id!r}'
        )
    return envelope


def swap_stack(
    pool,
    target_stack_id: str,
    target_snapshot_id: Optional[str] = None,
    *,
    registry: Optional[StackRegistry] = None,
    trigger: str = 'swap',
    **park_kwargs,
) -> None:
    """Atomically park the active stack and activate ``target_stack_id``.

    Guarantee (M2): the target is pre-validated BEFORE the source is parked, and any
    failure during target activation rolls back by re-activating the source — the
    pool is never left empty. ``park_active_stack`` is never used to "retry" a bad
    target; it is only reached once the target is known-good.

    Raises:
        ValueError / SnapshotIntegrityError: target pre-validation failed — the active
        stack was never parked, so the pool is exactly as it was.
        The original activation error (after rollback): target activation failed; the
        source stack has been restored.
    """
    if not settings.ENABLE_STACK_SWAP:
        logger.debug('[STACK_SWAP] swap requested but ENABLE_STACK_SWAP is off — no-op')
        return

    reg = _ensure_registry(pool, registry)

    # A self-swap would park the LIVE active stack just to "swap" it back onto itself,
    # silently discarding unsnapshotted live turns -- almost always a caller
    # misconfiguration, not intent. Reject it before any parking happens, at WARNING so
    # it is visible rather than swallowed. (Off-path unaffected: this is after the
    # ENABLE_STACK_SWAP gate.)
    if target_stack_id == reg.active_stack_id:
        logger.warning(
            '[STACK_SWAP] swap(%r) rejected: target is already the active stack — a '
            'self-swap would discard live unsnapshotted turns (no-op)', target_stack_id,
        )
        return

    # 1. Pre-validate the target BEFORE parking the source (M2 all-or-nothing).
    #    Raises here with the active stack fully intact.
    target_env = _prevalidate_target(pool, reg, target_stack_id, target_snapshot_id)

    source_stack_id = reg.active_stack_id

    # 2. Park the active stack; retain the envelope in memory for rollback.
    source_env = park_active_stack(pool, trigger=trigger, stack_id=source_stack_id, **park_kwargs)
    if source_env is None:
        # The source could not be snapshotted (e.g. capture gated off by a runtime flip,
        # or nothing to park). We must NOT activate the target into a half-parked pool,
        # because that path has no rollback envelope. Un-latch the still-present fleet so
        # it resumes, then abort before touching the registry. Pool is never left empty.
        latch = getattr(pool, '_pending_swap_event', None)
        if latch is not None:
            latch.clear()
        raise SnapshotIntegrityError(
            'stack-swap aborted: the active stack could not be parked into a snapshot, so '
            'the swap cannot proceed with a rollback guarantee (active stack left intact)'
        )
    if source_stack_id is None:
        source_stack_id = source_env.stack_id
    reg.set_active(None)
    _persist_active_stack(None)

    # 3. Activate the target; roll back to the source on ANY error. Activations are
    #    gate_checked so a mid-swap flag flip cannot silently no-op and empty the pool.
    try:
        activate_stack(pool, target_env, gate_checked=True)
        reg.set_active(target_stack_id)
        _persist_active_stack(target_stack_id)
    except Exception:
        logger.exception('[STACK_SWAP] target %s activation failed — rolling back to source %s',
                         target_stack_id, source_stack_id)
        # Drop any partial target residue so the source rehydration lands on a clean pool.
        _clear_pool_state(pool)
        # Re-activate the source from the retained in-memory envelope (no disk re-read),
        # guaranteeing the pool is never left empty.
        try:
            activate_stack(pool, source_env, gate_checked=True)
        except Exception:
            logger.critical('[STACK_SWAP] source %s rollback re-activation FAILED — pool may be '
                            'left empty; manual recovery required', source_stack_id, exc_info=True)
            raise
        reg.set_active(source_stack_id)
        _persist_active_stack(source_stack_id)
        raise


# ── Pruning & stack operations (Phase S5) ──────────────────────────────────────

def _retire_snapshot(stack_id: str, snapshot_id: str, path_str: str) -> None:
    """Remove one retired snapshot's FILE only — KV files are left untouched.

    KV is a cache and the JSONL log is the source of truth; orphaned KV files are
    acceptable. Retiring (pruning / deleting a stack or a single snapshot) therefore
    removes ONLY its envelope and never orchestrates KV-file lifecycle (no keep-set, no
    delete_state).

    A snapshot whose envelope cannot be read (corrupt/absent) is moved to ``_corrupt/``
    with a reason sidecar so it can be inspected without lingering in the active set.
    """
    try:
        load_snapshot_from_disk(Path(path_str))
    except (ValueError, OSError) as exc:
        quarantine_snapshot(Path(path_str), str(exc))
        return
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug('[SNAPSHOT_PRUNE] could not remove snapshot file %s: %s', path_str, exc)


def prune_snapshots(pool, *, registry: Optional[StackRegistry] = None) -> int:
    """Enforce per-stack snapshot retention: keep the newest ``SNAPSHOT_RETENTION_COUNT``.

    Walks every known stack and drops the OLDEST snapshots beyond the retention count
    (write order — oldest first). Only snapshot files are pruned; KV files are left
    untouched (KV is a cache, the JSONL log is the source of truth).
    """
    if not settings.ENABLE_STACK_SWAP:
        return 0
    reg = _ensure_registry(pool, registry)
    retention = max(1, settings.SNAPSHOT_RETENTION_COUNT)

    pruned = 0
    for stack_id, entries in list(reg._snapshots.items()):
        if len(entries) <= retention:
            continue
        keep = entries[len(entries) - retention:]
        for sid, path in entries[:len(entries) - retention]:
            _retire_snapshot(stack_id, sid, path)
            pruned += 1
        reg._snapshots[stack_id] = keep
    if pruned:
        logger.info('[SNAPSHOT_PRUNE] retired %d snapshot(s) beyond retention %d',
                    pruned, retention)
    return pruned


def prune_corrupt_snapshots(pool) -> int:
    """Enforce ``CORRUPT_SNAPSHOT_RETENTION_COUNT`` on the ``snapshots/_corrupt/`` backlog.

    Keeps the newest N quarantined envelopes and deletes older ones together with their
    ``.reason.txt`` sidecars, so the quarantine backlog stays bounded across boots instead
    of accumulating forever. Count-based mirror of :func:`prune_snapshots` (sort by mtime,
    newest first). ``CORRUPT_SNAPSHOT_RETENTION_COUNT == 0`` disables pruning entirely
    (keep every envelope). Best-effort: never raises into the boot scan. Returns the
    number of envelopes deleted (and their sidecars).
    """
    if not settings.ENABLE_STACK_SWAP:
        return 0
    retention = settings.CORRUPT_SNAPSHOT_RETENTION_COUNT
    if retention <= 0:
        return 0  # off: retain every quarantined envelope
    try:
        corrupt_dir = get_snapshot_dir(pool) / _CORRUPT_DIRNAME
    except Exception as exc:  # snapshot-dir resolution must never abort the boot scan
        logger.debug('[SNAPSHOT_PRUNE] corrupt-dir resolution failed: %s', exc)
        return 0
    if not corrupt_dir.exists():
        return 0
    try:
        envelopes = list(corrupt_dir.glob('*.json'))
    except OSError as exc:
        logger.debug('[SNAPSHOT_PRUNE] could not list %s: %s', corrupt_dir, exc)
        return 0

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    envelopes.sort(key=_mtime, reverse=True)
    deleted = 0
    for path in envelopes[retention:]:
        sidecar = corrupt_dir / f'{path.name}{_REASON_SIDECAR_SUFFIX}'
        # Sidecar first: a leaked .json is re-pruned next boot, but a leaked
        # .reason.txt would never be (prune only globs *.json).
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug('[SNAPSHOT_PRUNE] could not remove sidecar %s: %s', sidecar, exc)
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            logger.debug('[SNAPSHOT_PRUNE] could not remove corrupt snapshot %s: %s', path, exc)
    if deleted:
        logger.info('[SNAPSHOT_PRUNE] removed %d corrupt snapshot(s) beyond retention %d from %s',
                    deleted, retention, corrupt_dir)
    return deleted


def list_stacks(pool, *, registry: Optional[StackRegistry] = None) -> Dict[str, Any]:
    """Return the active stack id plus every known stack's snapshot ids and details (read-only).

    The shape mirrors the REST ``GET /api/stacks`` contract and is the single
    source the REST, WS and Telegram surfaces render from, so they never drift.
    """
    reg = ensure_registry_loaded(pool) if registry is None else registry
    active_id = reg.active_stack_id
    stacks_dict: Dict[str, List[str]] = {}
    details_dict: Dict[str, Dict[str, Any]] = {}

    from agent_cascade.session_caption import read_session_caption
    from agent_cascade.agent_identity import strip_birth_stamp

    for stack_id, entries in list(reg._snapshots.items()):
        valid_snapshots = []
        valid_sids = []
        display_name = strip_birth_stamp(stack_id) or stack_id
        instance_count = 0
        latest_snapshot_at = 0.0

        for sid, path_str in entries:
            p = Path(path_str)
            if not p.exists():
                continue
            try:
                env = load_snapshot_from_disk(p)
                validate_envelope_integrity(env)
            except Exception:
                continue

            valid_sids.append(sid)
            root_snap = None
            for _name, isnap in env.instances.items():
                ident = isnap.identity or {}
                if not ident.get('parent_instance'):
                    root_snap = isnap
                    break
            if root_snap is None and env.instances:
                root_snap = next(iter(env.instances.values()))

            if root_snap is not None:
                dname = (root_snap.identity or {}).get('display_name')
                if dname:
                    display_name = dname

            caption = ''
            if root_snap is not None and root_snap.log and root_snap.log.get('path'):
                log_path = Path(root_snap.log['path'])
                caption = read_session_caption(log_path)

            instance_count = len(env.instances)
            latest_snapshot_at = max(latest_snapshot_at, env.created_at)

            valid_snapshots.append({
                'snapshot_id': sid,
                'created_at': env.created_at,
                'trigger': env.trigger,
                'caption': caption,
            })

        if not valid_sids and stack_id != active_id:
            continue

        stacks_dict[stack_id] = valid_sids
        details_dict[stack_id] = {
            'display_name': display_name,
            'instance_count': instance_count,
            'latest_snapshot_at': latest_snapshot_at,
            'snapshots': valid_snapshots,
        }

    return {
        'active_stack_id': active_id,
        'stacks': stacks_dict,
        'details': details_dict,
        'enabled': bool(settings.ENABLE_STACK_SWAP),
    }


def snapshot_stack(pool, stack_id: Optional[str] = None, *,
                   registry: Optional[StackRegistry] = None,
                   trigger: str = 'user', kv_saver=None,
                   just_finished: Optional[Iterable[str]] = None,
                   only_streaming: bool = False) -> Optional[Dict[str, Any]]:
    """Capture a non-destructive fleet snapshot and register it under ``stack_id``.

    Unlike :func:`park_active_stack`, this does NOT drain or clear the pool — it
    is a read-only copy-out the user can take while the fleet keeps running. The
    live fleet (the active stack) is captured; ``stack_id`` defaults to the root
    instance the capture derives. Writes the envelope to disk, registers it, then
    applies retention (pruning). Returns ``{'stack_id', 'snapshot_id'}``, or
    ``None`` when the capture yielded nothing (empty/gated pool)."""
    env = capture_fleet_snapshot(pool, trigger=trigger, stack_id=stack_id,
                                 kv_saver=kv_saver, just_finished=just_finished,
                                 only_streaming=only_streaming)
    if env is None:
        logger.debug('[STACK_OPS] snapshot requested but capture returned None (empty/gated pool)')
        return None
    reg = _ensure_registry(pool, registry)
    reg.register(env.stack_id)
    path = write_snapshot_to_disk(env, get_snapshot_dir(pool))
    reg.add_snapshot(env.stack_id, env.snapshot_id, str(path))
    prune_snapshots(pool, registry=reg)
    logger.info('[STACK_OPS] snapshotted stack %s (snapshot %s)', env.stack_id, env.snapshot_id)
    return {'stack_id': env.stack_id, 'snapshot_id': env.snapshot_id}


def delete_stack(pool, stack_id: str, *, registry: Optional[StackRegistry] = None) -> Dict[str, Any]:
    """Delete a parked stack: its snapshot files and its registry entry.

    Rejects the active stack with :class:`ActiveStackDeletionError` (m4/409) — the
    caller must park or swap to another stack first. Only the envelope files are
    removed; KV files are left untouched (KV is a cache, the JSONL log is the source
    of truth). Returns ``{'stack_id', 'removed_snapshots'}``."""
    reg = ensure_registry_loaded(pool) if registry is None else registry
    if stack_id == reg.active_stack_id:
        raise ActiveStackDeletionError(
            f"cannot delete active stack {stack_id!r}; park or swap to another stack first")
    entries = reg._snapshots.get(stack_id, [])
    # Stale-registry fallback: if the target stack is missing from the
    # in-memory registry but the registry was previously loaded, the in-memory
    # view may be stale (e.g. a snapshot was written after the last disk glob).
    # Clear the loaded flag and re-glob so the delete can find the entry.
    if not entries and registry is None and getattr(pool, '_swap_registry_loaded', False):
        pool._swap_registry_loaded = False
        reg = ensure_registry_loaded(pool)
        entries = reg._snapshots.get(stack_id, [])
    for sid, path in entries:
        _retire_snapshot(stack_id, sid, path)
    reg._snapshots.pop(stack_id, None)
    logger.info('[STACK_OPS] deleted stack %s (%d snapshot(s))', stack_id, len(entries))
    return {'stack_id': stack_id, 'removed_snapshots': len(entries)}


def delete_snapshot(pool, stack_id: str, snapshot_id: str, *, registry=None) -> Dict[str, Any]:
    """Delete one saved session (snapshot file + registry entry). KV files are left untouched.

    Mirrors :func:`delete_stack` for a single entry: only the envelope is removed and the
    registry row updated; KV is never touched (KV is a cache, the JSONL log is the source
    of truth). The stack key is dropped when its last snapshot goes. Returns
    ``{'stack_id', 'snapshot_id', 'removed_stack': bool}``.
    """
    reg = ensure_registry_loaded(pool) if registry is None else registry
    entries = reg._snapshots.get(stack_id)
    # Stale-registry fallback: same as delete_stack — if the target
    # stack is missing from the in-memory registry but was previously loaded,
    # force a fresh disk re-glob before raising.
    if not entries and registry is None and getattr(pool, '_swap_registry_loaded', False):
        pool._swap_registry_loaded = False
        reg = ensure_registry_loaded(pool)
        entries = reg._snapshots.get(stack_id)
    if not entries:
        raise ValueError(f'unknown stack {stack_id!r}')
    path_str = reg.resolve_path(stack_id, snapshot_id)
    if path_str is None:
        raise ValueError(f'unknown snapshot {snapshot_id!r} in stack {stack_id!r}')
    _retire_snapshot(stack_id, snapshot_id, path_str)
    reg._snapshots[stack_id] = [[sid, p] for sid, p in entries if sid != snapshot_id]
    removed_stack = not reg._snapshots[stack_id]
    if removed_stack:
        reg._snapshots.pop(stack_id, None)
    return {'stack_id': stack_id, 'snapshot_id': snapshot_id, 'removed_stack': removed_stack}


def maybe_autosave_on_task_end(pool, instance_name: str) -> None:
    """Root run end -> a session save. Never raises; no-op when not applicable.

    A save is a pure manifest, so it is safe while the rest of the fleet is still
    running (user decision: "stack save should support live swap"). There is no
    quiescence scan and no rotation — the finishing instance is simply the trigger.

    Only the instances that are actually streaming are snapshotted: ``instance_name``
    (whose stream just ended and whose retained session is the freshest) plus any
    other instance whose LLM stream is live. Idle siblings — including callers
    blocked on a ``call_agent`` child — are not saved; their sessions are no longer
    retained, so the save would either be refused or race the live stream.
    """
    if not (settings.ENABLE_STACK_SWAP and settings.SNAPSHOT_AUTOSAVE_ENABLED):
        return
    inst = getattr(pool, 'get_instance', lambda _: None)(instance_name)
    if inst is None or getattr(inst, 'parent_instance', None) is not None:
        return
    latch = getattr(pool, '_pending_swap_event', None)
    if latch is not None and latch.is_set():
        return                       # a park/swap owns the fleet right now
    lock = getattr(pool, '_stack_autosave_lock', None)
    if lock is None:
        lock = threading.Lock()
        pool._stack_autosave_lock = lock
    if not lock.acquire(blocking=False):
        return                       # a save is already in flight
    try:
        started = time.perf_counter()
        snapshot_stack(pool, trigger='task_end', just_finished={instance_name},
                       only_streaming=True)
        logger.info('[STACK_SWAP] task-end autosave took %.0fms',
                    (time.perf_counter() - started) * 1000)
    except Exception:
        logger.exception('[STACK_SWAP] task-end autosave failed (non-fatal)')
    finally:
        lock.release()
