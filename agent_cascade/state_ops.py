"""State save/restore operations for llama-autoloader endpoints.

Talks to autoloader state endpoints for KV cache slot state management.
All operations are best-effort — failures are logged but never block execution.

Per-instance state limits are obsolete: we use stable labels (instance_name) and rely on
autoloader's per-model max-5 LRU eviction to manage disk usage.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Track which instances have confirmed no legacy files — skip their cleanup checks.
_legacy_cleanup_done_instances: set[str] = set()


def _normalize_api_base(api_base: str) -> str:
    """Normalize api_base to base URL without trailing /v1."""
    base = api_base.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    return base


def is_state_save_capable(endpoint_cfg) -> bool:
    """True if an endpoint config declares state-save capability.

    Shared predicate for the safe-mode caller-endpoint gate: a dict carrying
    ``state_save_enabled`` plus a usable ``api_base``/``model``. Deliberately port-
    independent (no fixed 1234/9123 check) — the capability flag is the source of
    truth. media_gen/image_gen keep their own extra ``is_autoloader_endpoint`` gate
    local; this predicate is the tool-side seam only.
    """
    return bool(
        isinstance(endpoint_cfg, dict)
        and endpoint_cfg.get('state_save_enabled')
        and endpoint_cfg.get('api_base')
        and endpoint_cfg.get('model')
    )


def save_instance_state(instance: 'AgentInstance') -> bool:
    """Save KV cache state for an instance.

    Uses cached endpoint config from the instance (set during endpoint allocation),
    saves state via autoloader, and stores the label on the instance under lock.

    Args:
        instance: AgentInstance to save state for.

    Returns:
        True if state was saved and label stored, False otherwise.
    """
    try:
        with instance._state_lock:
            endpoint_cfg = instance._last_endpoint_config

        if not endpoint_cfg or not isinstance(endpoint_cfg, dict):
            logger.debug('No cached endpoint config for %s', instance.instance_name)
            return False

        if not endpoint_cfg.get('state_save_enabled'):
            logger.debug('State save not enabled for %s (%s)', instance.instance_name, instance.agent_class)
            return False

        api_base = endpoint_cfg.get('api_base', '')
        model = endpoint_cfg.get('model', '')

        if not api_base or not model:
            logger.debug('Missing api_base/model for state save on %s', instance.instance_name)
            return False

        label = save_state(api_base, model, instance.instance_name)
        if label:
            with instance._state_lock:
                instance._state_label = label
            logger.debug('Saved state %s for instance %s', label, instance.instance_name)
            return True

        logger.debug('State save returned no label for %s', instance.instance_name)
        return False

    except Exception as e:
        logger.warning('Unexpected error during state save for %s: %s', instance.instance_name, e)
        return False


def restore_instance_state(instance: 'AgentInstance', held_endpoint_cfg: Optional[dict] = None) -> bool:
    """Restore KV cache state for an instance.

    Reads the label and endpoint config from the instance under lock, uses cached
    endpoint config to restore state via autoloader, and clears the label on failure
    to avoid retrying stale state.

    Args:
        instance: AgentInstance to restore state for.
        held_endpoint_cfg: Optional {'api_base', 'model'} dict of the endpoint the
            instance CURRENTLY holds a slot on. When provided, it is used as the
            restore target INSTEAD of ``instance._last_endpoint_config`` — which may
            be stale (e.g. pointing at a shared conc=0 autoloader the agent no longer
            owns). Restoring to an endpoint we don't hold would make the autoloader
            load the model and auto-evict a live sibling's resident model, so callers
            that own a slot must pass the held endpoint. When None (backward-compat
            path), falls back to ``_last_endpoint_config`` as before.

    Returns:
        True if state was restored successfully, False otherwise.
    """
    try:
        with instance._state_lock:
            label = instance._state_label
            endpoint_cfg = instance._last_endpoint_config

        logger.debug('Attempting restore for %s, label=%s', instance.instance_name, label)

        if not label:
            logger.debug('No state label to restore for %s', instance.instance_name)
            return False

        # Prefer the endpoint the caller confirmed we currently hold a slot on.
        # This is the eviction-safety gate: only ever load onto an owned endpoint.
        if held_endpoint_cfg and isinstance(held_endpoint_cfg, dict):
            endpoint_cfg = held_endpoint_cfg

        if not endpoint_cfg or not isinstance(endpoint_cfg, dict):
            logger.debug('No cached endpoint config for %s', instance.instance_name)
            return False

        api_base = endpoint_cfg.get('api_base', '')
        model = endpoint_cfg.get('model', '')

        if not api_base or not model:
            logger.debug('Missing api_base/model for state restore on %s', instance.instance_name)
            return False

        success = restore_state(api_base, model, label)
        if not success:
            # Restore failed — clear the label to avoid retrying stale state
            with instance._state_lock:
                instance._state_label = None
            logger.warning('State restore failed for %s (label=%s), cleared label', instance.instance_name, label)
            return False

        # Clear the label after successful restore to prevent double-restore.
        with instance._state_lock:
            instance._state_label = None
        logger.debug('Restored state for %s (label=%s)', instance.instance_name, label)
        return True

    except Exception as e:
        # Clear label on unexpected errors to avoid retrying stale state
        try:
            with instance._state_lock:
                instance._state_label = None
        except Exception:
            pass
        logger.warning('Unexpected error during state restore for %s: %s', instance.instance_name, e)
        return False


def save_state(api_base: str, model: str, instance_name: str) -> Optional[str]:
    """Save KV cache state. Returns label if successful, None on failure."""
    try:
        # Defensive check: reject obviously dangerous instance_names (belt-and-suspenders).
        # Autoloader also sanitizes labels via _sanitize_label().
        if '../' in instance_name or '..\\' in instance_name:
            logger.debug('Rejected state save for instance with unsafe name: %s', instance_name)
            return None

        base = _normalize_api_base(api_base)

        # Use instance_name as stable label — autoloader overwrites the same file on each save.
        # Previous timestamped labels caused per-model eviction of live agent state.
        # Autoloader's _sanitize_label() strips \, /, .. to prevent path traversal.
        label = instance_name
        url = f"{base}/v1/models/{model}/state/save"
        resp = httpx.post(url, json={'label': label}, timeout=30)

        if resp.status_code == 200:
            # Cleanup old states for this instance after successful save
            _cleanup_old_states(base, model, instance_name)
            return label
        logger.debug('State save returned status %d for %s', resp.status_code, instance_name)
        return None
    except Exception as e:
        logger.debug('State save failed for %s: %s', instance_name, e)
        return None


def restore_state(api_base: str, model: str, label: str) -> bool:
    """Restore KV cache state. Returns True if successful."""
    try:
        base = _normalize_api_base(api_base)

        url = f"{base}/v1/models/{model}/state/load"
        resp = httpx.post(url, json={'label': label}, timeout=30)
        if resp.status_code != 200:
            logger.debug('State restore returned status %d for label %s', resp.status_code, label)
        return resp.status_code == 200
    except Exception as e:
        logger.debug('State restore failed for label %s: %s', label, e)
        return False


def unload_all_models(api_base: str) -> bool:
    """POST /v1/unload_all on the llama-autoloader to free VRAM.

    Used by image_gen before running ComfyUI so the LLM's resident model is
    evicted and its VRAM is available for the diffusion model. Best-effort:
    failures are logged (warning) and reported via the return value, never raised.

    Args:
        api_base: The autoloader's API base URL (e.g., "http://localhost:8080/v1").

    Returns:
        True if unload succeeded (HTTP 200), False otherwise.
    """
    try:
        base = _normalize_api_base(api_base)
        resp = httpx.post(f"{base}/v1/unload_all", timeout=60)
        if resp.status_code != 200:
            logger.warning('[state_ops] unload_all returned status %d', resp.status_code)
            return False
        return True
    except Exception as e:
        logger.warning('[state_ops] unload_all failed for %s: %s', api_base, e)
        return False


def is_autoloader_endpoint(api_base: str) -> bool:
    """Check if endpoint points to llama-autoloader."""
    return ':1234/' in api_base or ':9123/' in api_base


def resolve_held_endpoint(pool, instance: 'AgentInstance'):
    """Resolve the endpoint the instance currently holds a slot on.

    Returns {'api_base', 'model'} for the cursor-aware effective endpoint (the
    same resolution used at slot acquisition), or None if it cannot be resolved.
    Used by state-restore call sites so they only ever load onto an owned
    endpoint — loading a stale _last_endpoint_config on a shared conc=0 autoloader
    would auto-evict a live sibling's resident model.

    ``pool`` is duck-typed (only ``pool.api_router`` is touched), mirroring how
    slot_yield_utils duck-types the pool.
    """
    router = pool.api_router if hasattr(pool, 'api_router') else None
    if not router or not hasattr(router, 'get_effective_slot_info'):
        return None
    slot_info = router.get_effective_slot_info(instance.agent_class, instance_name=instance.instance_name) or {}
    api_base = slot_info.get('api_base') or ''
    # get_effective_slot_info carries no model — take it from the rotated chain head.
    chain = router.get_endpoint_chain(instance.agent_class, instance_name=instance.instance_name) or []
    model = (chain[0].get('model') if chain else '') or ''
    if not api_base or not model:
        return None
    return {'api_base': api_base, 'model': model}


def restore_held_slot_state(pool, instance: 'AgentInstance', inst_name: str) -> bool:
    """Restore KV state to the endpoint the instance currently holds a slot on.

    Defensive by design: any resolution failure skips the restore (a missed
    restore is far better than a wrongful eviction of a sibling's model).
    """
    try:
        held_cfg = resolve_held_endpoint(pool, instance)
        if not held_cfg:
            logger.info('[STATE_RESTORE_SKIP] %s — could not resolve held endpoint', inst_name)
            # Clear the orphaned label so it does NOT re-trigger an expensive
            # state/load on every subsequent turn (a leaked _state_label would keep
            # firing the 3.1GB restore attempt while resolution stays broken). Mirrors
            # the "clear orphaned label" pattern in tool_dispatcher._run_child_sync.
            clear_orphaned_state_label(instance, inst_name)
            return False
        return restore_instance_state(instance, held_endpoint_cfg=held_cfg)
    except Exception as e:
        # Never evict on a resolution error — skip the restore.
        logger.info('[STATE_RESTORE_SKIP] %s — endpoint resolution failed: %s', inst_name, e)
        clear_orphaned_state_label(instance, inst_name)
        return False


def clear_orphaned_state_label(instance: 'AgentInstance', inst_name: str) -> None:
    """Clear a pending _state_label that could not be consumed by a restore.

    Used when endpoint resolution fails before restore_instance_state() runs — in that
    case the label would otherwise leak and re-trigger an expensive state/load on every
    later turn. Best-effort: never raises.
    """
    try:
        with instance._state_lock:
            if instance._state_label is not None:
                instance._state_label = None
                logger.debug('Cleared orphaned state label for %s (restore skipped)', inst_name)
    except Exception as e:
        logger.debug('Failed to clear state label for %s: %s', inst_name, e)


def _cleanup_old_states(api_base_no_v1: str, model: str, instance_name: str):
    """Clean up legacy timestamped state files for this instance (pre-fix artifacts).

    Per-instance tracking: once no legacy files are found for an instance, it's added to a
    set and all future cleanup checks for that instance return immediately — avoids one HTTP
    GET per save forever. If files were deleted, we keep checking in case more appear from
    other models on subsequent saves.
    """
    global _legacy_cleanup_done_instances

    if instance_name in _legacy_cleanup_done_instances:
        return

    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state"
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return

        data = resp.json()
        labels = data.get('labels', [])

        # Only match legacy timestamped format: instance_name_TIMESTAMP
        legacy_pattern = instance_name + '_'
        legacy_states = [l for l in labels if l.startswith(legacy_pattern)]

        # Delete all legacy timestamped states — the current stable label file is kept.
        for label in legacy_states:
            _delete_state(api_base_no_v1, model, label)

        # No legacy files found for this instance — mark it done to avoid perpetual HTTP calls.
        if not legacy_states:
            _legacy_cleanup_done_instances.add(instance_name)

    except Exception as e:
        logger.debug('Legacy state cleanup failed for %s: %s', instance_name, e)


def _delete_state(api_base_no_v1: str, model: str, label: str):
    """Delete a saved state by label via autoloader DELETE endpoint.

    Silently handles missing files and transient errors — cleanup is best-effort.
    """
    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state/{label}"
        httpx.delete(url, timeout=10)
    except Exception as e:
        logger.debug('State delete failed for label %s: %s', label, e)
