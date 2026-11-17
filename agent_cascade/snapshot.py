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


class SnapshotIntegrityError(Exception):
    """Fail-closed restore refusal: referenced log missing/truncated/mismatched,
    or envelope structurally unfit for rehydration. Raised ONLY by restore-path
    validators, never by capture. S4 rollback guard catches this type."""


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


def validate_log_reference(ref: Optional[dict]) -> Optional[str]:
    """Single source of truth for the log-integrity comparison (refinement 4).

    Re-counts the referenced file with the SAME semantics as capture
    (:func:`_capture_log_reference`), so gate and capture can never disagree.
    Returns ``None`` when the reference is intact, else a human-readable reason
    (no instance-name prefix — callers add that). Rules (fail-closed):
      - ``ref`` must be a dict with a non-empty string ``path``;
      - the path must exist on disk (a ref captured against a missing log is still
        refused at restore — the snapshot is already degraded);
      - re-counted ``message_count`` == ``ref['message_count']`` AND byte size ==
        ``ref['offset']`` (strict equality; ANY divergence — truncation OR append —
        means the log moved under the snapshot → refuse).
    """
    if not isinstance(ref, dict):
        return 'log reference is missing or not a dict'
    path = ref.get('path')
    if not isinstance(path, str) or not path:
        return 'log reference has no usable path'
    if not os.path.exists(path):
        return f'referenced log missing: {path}'
    current = _capture_log_reference(path)
    expected_count = ref.get('message_count')
    if current['message_count'] != expected_count:
        return (f'message_count mismatch for {path}: snapshot {expected_count!r} '
                f'!= on-disk {current['message_count']!r}')
    expected_offset = ref.get('offset')
    if current['offset'] != expected_offset:
        return (f'byte offset mismatch for {path}: snapshot {expected_offset!r} '
                f'!= on-disk {current['offset']!r}')
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
    from agent_cascade.agent_instance import AgentInstance, AgentState

    for name, snap in envelope.instances.items():
        created = wall_to_monotonic(
            snap.clocks['created_at'], now_wall=now_wall, now_mono=now_mono)
        last_act = wall_to_monotonic(
            snap.clocks['last_activity'], now_wall=now_wall, now_mono=now_mono)
        cm = snap.compression
        tc = snap.token_counters
        inst = AgentInstance(
            instance_name=name,                             # identity verbatim — never re-mint
            agent_class=snap.identity['agent_class'],
            conversation=[],                                # filled by _reattach_instance_log (5a)
            max_turns=snap.budget['max_turns'],
            parent_instance=snap.identity['parent_instance'],
            display_name=snap.identity['display_name'],
            created_at=created,
            last_activity=last_act,
            compression_summary=cm['compression_summary'],
            latest_marker_index=-1,                         # not captured; matches load_session default
        )
        inst._pool_ref = pool
        inst._nest_depth = snap.identity['nest_depth']
        # system_started_at is a wall-clock '%Y-%m-%d %H:%M' string — verbatim, NOT
        # projected. Capture stores it in the clocks block (snapshot.py:405).
        inst.system_started_at = snap.clocks.get('system_started_at')
        inst.state = AgentState[snap.state['state']]        # provisional; refined in 5b
        inst.is_terminated = bool(snap.state['is_terminated'])
        inst._terminated_reason = snap.state['terminated_reason']
        inst.restricted_shell = bool(snap.state.get('restricted_shell', False))
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
        inst._generate_cfg_override = snap.llm_override.get('generate_cfg_override')
        inst._system_prompt_frozen = bool(snap.system_prompt['system_prompt_frozen'])
        inst._system_prompt_initialized = bool(snap.system_prompt['system_prompt_initialized'])
        # Compression counters verbatim + the two projected force-compress clocks.
        inst._force_compress_count = cm['force_compress_count']
        inst._force_compress_fail_streak = cm['force_compress_fail_streak']
        inst._last_force_compress_time = wall_to_monotonic(
            cm['last_force_compress_time'], now_wall=now_wall, now_mono=now_mono)
        inst._last_force_compress_fail_at = wall_to_monotonic(
            cm['last_force_compress_fail_at'], now_wall=now_wall, now_mono=now_mono)

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
        pool._reattach_instance_log(inst, snap.log, mcp_block=snap.mcp)
        # Re-apply after rebuild_conversation (5a) zeroed it (agent_instance.py:756).
        inst._last_actual_token_count = snap.token_counters['last_actual_token_count']

    # Phase 5b — resume classification (data-level; state writes under each instance lock).
    for name, snap in envelope.instances.items():
        inst = pool.instances[name]
        captured = snap.state['state'] or 'IDLE'
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
