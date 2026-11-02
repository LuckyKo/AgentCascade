"""Backend-authoritative settings delta pipeline (Batch B, Phase 3).

Validates a per-key settings delta against the :mod:`agent_cascade.settings_registry`,
applies the accepted keys to live state through the existing
:class:`agent_cascade.config_handlers.ConfigUpdateRouter` handlers, persists each
touched bucket exactly once, and bumps the shared ``settings_revision`` counter.

Shared by the ``update_settings`` WebSocket message and ``PATCH /api/settings`` so the
two entry points cannot drift. The design invariant (plan §11.3): a stale ``base_revision``
never rejects a batch — it is logged, the touched keys are still applied, and a fresh
revision is broadcast.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agent_cascade.log import logger
from agent_cascade.settings_registry import SETTINGS_REGISTRY

# ── Per-key value-type expectations ─────────────────────────────────────────────
#
# The registry is the source of truth for expected types: every spec carries
# ``py_type``, derived from its default value with explicit overrides for
# collection-valued keys. A value whose type does not match is rejected per-key;
# a value the registry does not know about is rejected as an unknown key.


def _type_ok(key: str, value: Any) -> bool:
    """Return True if ``value`` matches the registry's expected type for ``key``."""
    spec = SETTINGS_REGISTRY.get(key)
    expected = spec.py_type if spec is not None else None
    if expected is None or expected is object:
        return True
    # bool is a subclass of int — a bool is never a valid number, but is a valid bool field.
    if isinstance(value, bool):
        return expected is bool
    if isinstance(expected, tuple) and (int in expected or float in expected):
        return isinstance(value, (int, float))
    return isinstance(value, expected)


def log_stale_revision(base_revision: Any, current_revision: int, channel: str) -> None:
    """Log a revision mismatch without rejecting the batch (plan §11.3, last-write-wins).

    Shared by the ``update_settings``/``update_endpoints`` WS handlers and the
    ``PATCH /api/settings`` / ``POST /api/endpoints/bulk`` REST endpoints so the
    guard wording cannot drift.
    """
    if base_revision is None:
        return
    try:
        base = int(base_revision)
    except (TypeError, ValueError):
        return
    current = int(current_revision)
    if base != current:
        logger.info(f'[{channel}] stale base_revision={base} (current={current}); applying anyway')


def validate_settings_changes(changes: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Validate a ``{key: value}`` settings delta against the registry.

    Returns ``(accepted, rejected)``:
      * ``accepted`` — the keys that passed validation (value kept as-is);
      * ``rejected`` — a ``{key: reason}`` map for every unknown key or type/enum violation.

    Validation is strictly per-key: one bad key never poisons the rest of the batch.
    """
    accepted: Dict[str, Any] = {}
    rejected: Dict[str, str] = {}
    for key, value in (changes or {}).items():
        spec = SETTINGS_REGISTRY.get(key)
        if spec is None:
            rejected[key] = 'unknown key'
            continue
        if not _type_ok(key, value):
            rejected[key] = 'type mismatch'
            continue
        if spec.handler is None and spec.kind != 'session':
            # A key the registry knows but no handler routes to would be reported as
            # applied while nothing changes — reject it instead.
            rejected[key] = 'no handler for key'
            continue
        accepted[key] = value
    return accepted, rejected


def _read_applied_values(pool, accepted):
    """Re-read each applied key's live (post-handler) value for the ``settings_changed`` broadcast.

    Enum-valued handlers normalize invalid input to the default, so the raw accepted value can
    differ from what actually took effect; broadcasting the raw value would stomp the UI with a
    value the server did not keep. Falls back to the accepted value when the live source is absent.
    """
    out: Dict[str, Any] = {}
    llm_cfg = None
    if pool is not None:
        llm_cfg = getattr(pool, 'llm_cfg', None)
        if not isinstance(llm_cfg, dict):
            llm_cfg = None
    for key, raw in accepted.items():
        spec = SETTINGS_REGISTRY.get(key)
        kind = spec.kind if spec is not None else 'pool'
        if kind in ('pool', 'extra') and pool is not None and hasattr(pool, 'settings'):
            out[key] = getattr(pool.settings, key, raw)
        elif kind == 'llm' and llm_cfg is not None:
            out[key] = llm_cfg.get(key, raw)
        else:
            out[key] = raw
    return out


async def apply_settings_changes(
    pool: Any,
    agents: list,
    changes: Dict[str, Any],
    session: Optional[dict] = None,
) -> Tuple[List[str], Dict[str, str], Dict[str, Any], int]:
    """Apply a settings delta to live state, persisting each touched bucket once.

    Pipeline (plan §11):
      1. validate each key against the registry (unknown / type / enum → rejected per-key);
      2. apply the accepted pool/extra/llm keys through ``ConfigUpdateRouter`` (the LLM keys
         persist themselves via ``api_router.update_default_llm_cfg``);
      3. apply ``session_name`` (no config handler) by updating the session + persisting;
      4. persist the pool/extra bucket exactly once via ``pool._save_pool_settings()``;
      5. return the new shared ``settings_revision``.

    Returns ``(applied, rejected, changes_out, revision)`` where ``applied`` is the list of
    applied key names, ``rejected`` is the ``{key: reason}`` map, ``changes_out`` is the
    applied ``{key: value}`` dict for the ``settings_changed`` broadcast, and ``revision``
    is the post-apply shared settings revision.
    """
    from agent_cascade.config_handlers import ConfigUpdateRouter
    from agent_cascade.runtime_state import state as _runtime_state

    accepted, rejected = validate_settings_changes(changes)
    revision = int(_runtime_state.settings_revision)

    if not accepted:
        return [], rejected, {}, revision

    # Split the accepted keys by registry kind so each bucket is persisted exactly once.
    llm_keys = {k for k, v in accepted.items() if SETTINGS_REGISTRY[k].kind == 'llm'}
    session_keys = {k for k, v in accepted.items() if SETTINGS_REGISTRY[k].kind == 'session'}
    pool_extra_keys = {k for k, v in accepted.items() if SETTINGS_REGISTRY[k].kind in ('pool', 'extra')}

    # 2. Apply pool/extra/llm keys through the existing handlers (LLM persists itself).
    if pool_extra_keys or llm_keys:
        router_cfg = {k: accepted[k] for k in (pool_extra_keys | llm_keys)}
        router = ConfigUpdateRouter(pool, agents)
        await router.apply(router_cfg)

    # 3. session_name has no config handler — update the session dict + migrate summaries,
    #    then persist to config/session_state.json (which bumps the shared revision).
    if 'session_name' in accepted:
        from agent_cascade.session_state_store import DEFAULT_SESSION_NAME
        new_name = str(accepted['session_name']).strip() or DEFAULT_SESSION_NAME
        if session is not None:
            old_name = session.get('session_name')
            session['session_name'] = new_name
            summaries = getattr(pool, 'instance_summaries', None)
            if old_name and summaries is not None and old_name in summaries:
                summaries[new_name] = summaries.pop(old_name)
        from agent_cascade.session_state_store import save_session_name
        save_session_name(new_name)

    # 4. Persist the pool/extra bucket exactly once (the LLM bucket already persisted via
    #    update_default_llm_cfg → _save, and session_name persisted via save_session_name).
    if pool_extra_keys and pool is not None and hasattr(pool, '_save_pool_settings'):
        pool._save_pool_settings()

    # 5. Read the post-apply revision (every persist path above bumps the shared counter).
    revision = int(_runtime_state.settings_revision)
    applied = list(accepted.keys())
    changes_out = _read_applied_values(pool, accepted)
    return applied, rejected, changes_out, revision
