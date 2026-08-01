"""State save/restore operations for llama-autoloader endpoints.

Talks to autoloader state endpoints for KV cache slot state management.
All operations are best-effort — failures are logged but never block execution.
"""

import httpx
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Module constant — could be made configurable later via settings or endpoint config.
MAX_STATES_PER_INSTANCE = 3   # Keep last 3 states per agent instance


def _normalize_api_base(api_base: str) -> str:
    """Normalize api_base to base URL without trailing /v1."""
    base = api_base.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    return base


def save_instance_state(instance: 'AgentInstance', pool: 'AgentPool') -> bool:
    """Save KV cache state for an instance.

    Looks up endpoint config from pool's API router, saves state via autoloader,
    and stores the label on the instance under lock.

    Args:
        instance: AgentInstance to save state for.
        pool: AgentPool for API router access.

    Returns:
        True if state was saved and label stored, False otherwise.
    """
    try:
        router = pool.api_router
        if not router:
            logger.debug("No API router available for state save on %s", instance.instance_name)
            return False

        endpoint_cfg = router.get_llm_config(instance.agent_class)
        if not endpoint_cfg or not endpoint_cfg.get('state_save_enabled'):
            logger.debug("State save not enabled for %s (%s)", instance.instance_name, instance.agent_class)
            return False

        api_base = endpoint_cfg.get('api_base', '')
        model = endpoint_cfg.get('model', '')

        if not api_base or not model or not is_autoloader_endpoint(api_base):
            logger.debug("Not an autoloader endpoint for state save on %s", instance.instance_name)
            return False

        label = save_state(api_base, model, instance.instance_name)
        if label:
            with instance._state_lock:
                instance._state_label = label
            logger.debug("Saved state %s for instance %s", label, instance.instance_name)
            return True

        logger.warning("State save returned no label for instance %s", instance.instance_name)
        return False

    except Exception as e:
        logger.warning("Unexpected error during state save for %s: %s", instance.instance_name, e)
        return False


def restore_instance_state(instance: 'AgentInstance', pool: 'AgentPool') -> bool:
    """Restore KV cache state for an instance.

    Reads the label from the instance under lock, restores state via autoloader,
    and clears the label on failure to avoid retrying stale state.

    Args:
        instance: AgentInstance to restore state for.
        pool: AgentPool for API router access.

    Returns:
        True if state was restored successfully, False otherwise.
    """
    try:
        with instance._state_lock:
            label = instance._state_label

        if not label:
            logger.debug("No state label to restore for %s", instance.instance_name)
            return False

        router = pool.api_router
        if not router:
            logger.debug("No API router available for state restore on %s", instance.instance_name)
            return False

        endpoint_cfg = router.get_llm_config(instance.agent_class)
        api_base = endpoint_cfg.get('api_base', '') if endpoint_cfg else ''
        model = endpoint_cfg.get('model', '') if endpoint_cfg else ''

        if not api_base or not model or not is_autoloader_endpoint(api_base):
            logger.debug("Not an autoloader endpoint for state restore on %s", instance.instance_name)
            return False

        success = restore_state(api_base, model, label)
        if not success:
            # Restore failed — clear the label to avoid retrying stale state
            with instance._state_lock:
                instance._state_label = None
            logger.warning("State restore failed for %s (label=%s), cleared label", instance.instance_name, label)
            return False

        logger.debug("Restored state %s for instance %s", label, instance.instance_name)
        return True

    except Exception as e:
        # Clear label on unexpected errors to avoid retrying stale state
        try:
            with instance._state_lock:
                instance._state_label = None
        except Exception:
            pass
        logger.warning("Unexpected error during state restore for %s: %s", instance.instance_name, e)
        return False


def save_state(api_base: str, model: str, instance_name: str) -> Optional[str]:
    """Save KV cache state. Returns label if successful, None on failure."""
    try:
        base = _normalize_api_base(api_base)

        label = f"{instance_name}_{int(time.time())}"
        url = f"{base}/v1/models/{model}/state/save"
        resp = httpx.post(url, json={"label": label}, timeout=30)

        if resp.status_code == 200:
            # Cleanup old states for this instance after successful save
            _cleanup_old_states(base, model, instance_name)
            return label
        logger.debug("State save returned status %d for %s", resp.status_code, instance_name)
        return None
    except Exception as e:
        logger.debug("State save failed for %s: %s", instance_name, e)
        return None


def restore_state(api_base: str, model: str, label: str) -> bool:
    """Restore KV cache state. Returns True if successful."""
    try:
        base = _normalize_api_base(api_base)

        url = f"{base}/v1/models/{model}/state/load"
        resp = httpx.post(url, json={"label": label}, timeout=30)
        if resp.status_code != 200:
            logger.debug("State restore returned status %d for label %s", resp.status_code, label)
        return resp.status_code == 200
    except Exception as e:
        logger.debug("State restore failed for label %s: %s", label, e)
        return False


def is_autoloader_endpoint(api_base: str) -> bool:
    """Check if endpoint points to llama-autoloader."""
    return ':1234/' in api_base or ':9123/' in api_base


def _cleanup_old_states(api_base_no_v1: str, model: str, instance_name: str):
    """Delete oldest states for this instance if count > MAX_STATES_PER_INSTANCE."""
    try:
        # List all saved states for this model
        url = f"{api_base_no_v1}/v1/models/{model}/state"
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return

        data = resp.json()
        labels = data.get("labels", [])

        # Filter to states belonging to this instance (label starts with instance_name_)
        my_states = [l for l in labels if l.startswith(instance_name + "_")]

        # Sort by timestamp portion (label format: instance_name_TIMESTAMP)
        def extract_ts(label: str):
            try:
                return int(label.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                return 0

        my_states.sort(key=extract_ts)

        # Delete oldest if we exceed the limit
        while len(my_states) > MAX_STATES_PER_INSTANCE:
            oldest = my_states.pop(0)
            _delete_state(api_base_no_v1, model, oldest)

    except Exception as e:
        logger.debug("State cleanup failed for %s: %s", instance_name, e)


def _delete_state(api_base_no_v1: str, model: str, label: str):
    """Delete a saved state by label. Uses DELETE endpoint if available."""
    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state/{label}"
        resp = httpx.delete(url, timeout=10)
        # If no DELETE endpoint exists yet (500/405), fall through silently.
        # Future: add DELETE /v1/models/{model_id}/state/{label} to autoloader.
    except Exception as e:
        logger.debug("State delete failed for label %s: %s", label, e)