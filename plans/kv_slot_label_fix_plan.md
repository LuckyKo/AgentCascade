# KV Slot Label Fix Plan

**Date:** 2026-08-08  
**Author:** kv_plan_coder  
**Status:** Ready for implementation  

---

## Problem

Every KV cache save generates a new timestamped label (`Maine_1786137856`), causing:
- One agent creates ~N state files over its lifetime (one per delegation)
- Autoloader's per-model max-5 cap evicts live agents' state files
- Parent state gets evicted by child saves → restore failures

**Root cause:** `state_ops.py:138` — `label = f"{instance_name}_{int(time.time())}"`

---

## 1. Core Fix (Required)

### File: `agent_cascade/state_ops.py`

#### Change A: Stable label at line 138

```python
# BEFORE (line 138):
label = f"{instance_name}_{int(time.time())}"

# AFTER:
label = instance_name  # stable per-agent lifetime; autoloader overwrites same file
```

**Rationale:** `instance_name` is already unique per live agent (`agent_pool.py:802–814`). The autoloader overwrites same-label files (no existence check, confirmed at `server.py:966`). This yields exactly one `.bin` file per agent per model.

**Comment to add:**
```python
# Use instance_name as stable label — autoloader overwrites the same file on each save.
# Previous timestamped labels caused per-model eviction of live agent state.
label = instance_name
```

#### Change B: Update `_cleanup_old_states()` (lines 173–204)

The current logic filters by `instance_name_` prefix and sorts by timestamp — this will break with stable labels. Repurpose it as **legacy cleanup only**: purge old `{name}_{ts}` files from before the fix.

```python
def _cleanup_old_states(api_base_no_v1: str, model: str, instance_name: str):
    """Clean up legacy timestamped state files for this instance (pre-fix artifacts)."""
    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state"
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return

        data = resp.json()
        labels = data.get("labels", [])

        # Only match legacy timestamped format: instance_name_TIMESTAMP
        legacy_pattern = instance_name + "_"
        legacy_states = [l for l in labels if l.startswith(legacy_pattern)]

        # Delete all legacy timestamped states — the current stable label file is kept.
        for label in legacy_states:
            _delete_state(api_base_no_v1, model, label)

    except Exception as e:
        logger.debug("Legacy state cleanup failed for %s: %s", instance_name, e)
```

**Rationale:** After the fix, only one file per agent exists (`instance_name`), so there's nothing to "keep last N of." The function becomes a one-time cleanup that purges old timestamped artifacts. It runs harmlessly after each save until legacy files are gone.

#### Change C: Remove unused import (optional)

If `time` is no longer used elsewhere in the file, remove line 9:
```python
import time  # REMOVE if unused
```

Verify with grep first.

---

## 2. Legacy File Cleanup Strategy

### Immediate cleanup via code change

The updated `_cleanup_old_states()` above handles this automatically — it runs after every save and deletes any `{name}_{ts}` files it finds for that agent. No manual intervention needed.

### Manual pre-deploy cleanup (optional, recommended)

Before deploying, manually delete timestamped state files from `llama-autoloader/states/`:
```bash
# Example: remove all timestamped files matching known agent names
rm states/*.bin.<agentname>_<timestamp>
```

Or do a bulk delete of all `.bin` files if you're comfortable — they'll be recreated as needed with the new stable labels.

### `MAX_STATES_PER_INSTANCE` constant

The constant at line 15 (`MAX_STATES_PER_INSTANCE = 3`) becomes obsolete for steady-state but can be kept for documentation purposes or removed. **Recommendation:** remove it and add a comment explaining why per-instance limits are no longer needed.

---

## 3. Orphan Cleanup on Agent Dismiss (Optional Enhancement)

### Current behavior

`_clear_state_label()` at `agent_pool.py:1045–1056` clears `_state_label` and `_last_endpoint_config` but does **not** delete the `.bin` file from disk. With stable labels, this means dismissed agents leave their state files behind indefinitely.

### Proposed enhancement

Add a best-effort DELETE call when clearing state on dismiss/terminate:

```python
def _clear_state_label(self, inst) -> None:
    """Clear state label and optionally delete the saved state file."""
    try:
        with inst._state_lock:
            label = inst._state_label
            endpoint_cfg = inst._last_endpoint_config
            inst._state_label = None
            inst._last_endpoint_config = None

        # Best-effort cleanup of orphaned state file on dismiss
        if label and endpoint_cfg:
            self._delete_instance_state(endpoint_cfg, label)

    except Exception as e:
        logger.debug(f"Clearing state label for {inst.instance_name} failed (non-critical): {e}")


def _delete_instance_state(self, endpoint_cfg: dict, label: str) -> None:
    """Best-effort delete of an instance's saved state file."""
    try:
        api_base = endpoint_cfg.get('api_base', '')
        model = endpoint_cfg.get('model', '')
        if not api_base or not model or not is_autoloader_endpoint(api_base):
            return

        base = _normalize_api_base(api_base)
        url = f"{base}/v1/models/{model}/state/{label}"
        resp = httpx.delete(url, timeout=5)
        if resp.status_code == 200:
            logger.debug(f"Deleted state file for label {label}")
    except Exception as e:
        logger.debug(f"State file delete failed for label {label} (non-critical): {e}")
```

**Tradeoffs:**
- **Pro:** No disk bloat from dismissed agents' state files
- **Con:** Adds HTTP call to dismiss path; DELETE endpoint may not exist in autoloader yet (`state_ops.py:211–212` notes this)
- **Recommendation:** Implement only if the autoloader's DELETE endpoint is confirmed working. Otherwise leave as-is — the per-model max-5 cap still limits disk usage.

---

## 4. Test / Regression Considerations

### Manual verification steps

1. Apply fix, restart AgentCascade
2. Run a session where an agent delegates to 2+ children
3. Check `llama-autoloader/states/`: should see one file per agent (`Maine.bin`, `coder_worker1.bin`, etc.), sizes stable across saves (overwrite behavior)
4. Verify parent state survives child saves — no eviction during normal operation
5. Confirm legacy timestamped files are cleaned up after a save

### Automated checks to consider

- **Unit test:** `save_state()` called twice with same `instance_name` → returns same label both times
- **Integration test:** After N delegations, count `.bin` files for agent X = 1 (not N)
- **Restore test:** Parent saves state → delegates → restores → context is intact

### Existing behavior that must not break

- Best-effort failure semantics: save/restore failures must NOT block execution (`state_ops.py:4`)
- Label clearing on restore success and on restore failure (double-restore prevention)
- Async child completion path (`agent_pool.py:2570–2577`) still works with stable labels

---

## 5. Risk Assessment

### Risk: Low — single line change in a best-effort path

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Wrong label causes restore to load wrong agent's state | Very low | High | `instance_name` is unique per live instance; pool enforces this. Dead agents' files have their own names — no collision. |
| Breaking change for code expecting timestamped labels | Low | Medium | Only `_cleanup_old_states()` parsed timestamps; updated to legacy-only mode. Grep found no other consumers. |
| Autoloader DELETE endpoint missing (if orphan cleanup implemented) | High | Low | Already handled: `state_ops.py:211–212` notes this; wrap in try/except, silent failure. |
| Model switch mid-lifetime causes confusion | None | — | Files are per-model (`{model}.{label}.bin`). Same label across models is isolated. Safe. |
| Concurrent agents with same name collide | None | High | Pool prevents duplicate names; recursive self-calls get `_childN` suffix → distinct labels. |
| Race condition: two saves happen nearly simultaneously, overwrite corrupts file | Very low | Medium | Autoloader's `save_state()` already handles this — it writes to the same path atomically via llama.cpp. Same behavior as auto-save label `auto`. |

### Rollback plan

- Revert the single line change in `state_ops.py:138`
- Restore `_cleanup_old_states()` original logic
- Simple git revert; no data migration needed

---

## 6. Implementation Order

1. Apply Change A (line 138 stable label) — core fix
2. Apply Change B (update `_cleanup_old_states()`) — legacy cleanup
3. Remove `import time` if unused (Change C)
4. Test manually: delegation session, verify single file per agent
5. Optionally implement orphan cleanup on dismiss (Section 3)

**Estimated effort:** ~10 minutes code change, ~15 minutes testing.