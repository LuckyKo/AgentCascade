# Slot State Save/Restore for Agent Cascade — Plan v3

## Core Idea

Save parent agent's KV cache state ONLY when delegating via `call_agent`, not after turns. If the model gets evicted and reloaded while the child runs, we restore the parent's state after child completes. Also restore on agent resume if it has a saved label.

State labels are unique per agent instance: `{instance_name}_{timestamp}`. Multiple agents using the same endpoint/model simultaneously don't collide.

## Changes Required

### 1. AgentInstance: Add Single State Tracking Field

In `agent_cascade/agent_instance.py`, add to the dataclass:

```python
@dataclass(slots=True)
class AgentInstance:
    # ... existing fields ...
    
    # Slot state tracking (new)
    _state_label: Optional[str] = None   # Last saved state label for this instance
```

That's it. We already know the endpoint/model from config when we need to restore — no need to track `_last_endpoint_id`, `_last_model`, or `_last_api_base` on the instance.

### 2. Endpoint Config: Add State Toggle

In `api_endpoints.json`, add to each endpoint that uses autoloader:

```json
{
  "id": "...",
  "name": "Autoloader-35B",
  "api_base": "http://127.0.0.1:1234/v1",
  "model": "qwen3.6-35b-a3b",
  "state_save_enabled": true   // NEW: simple toggle
}
```

And in `APIEndpoint` dataclass (`api_router.py` line ~92):

```python
state_save_enabled: bool = False
```

### 3. State Operations Module (NEW: agent_cascade/state_ops.py)

Minimal module for talking to autoloader state endpoints. Uses `instance_name` directly as the label prefix:

```python
import httpx
import time
from typing import Optional

def save_state(api_base: str, model: str, instance_name: str) -> Optional[str]:
    """Save KV cache state. Returns label if successful, None on failure."""
    try:
        base = api_base.rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        
        label = f"{instance_name}_{int(time.time())}"
        url = f"{base}/v1/models/{model}/state/save"
        resp = httpx.post(url, json={"label": label}, timeout=30)
        
        if resp.status_code == 200:
            return label
        return None
    except Exception:
        return None

def restore_state(api_base: str, model: str, label: str) -> bool:
    """Restore KV cache state. Returns True if successful."""
    try:
        base = api_base.rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        
        url = f"{base}/v1/models/{model}/state/load"
        resp = httpx.post(url, json={"label": label}, timeout=30)
        return resp.status_code == 200
    except Exception:
        return False

def is_autoloader_endpoint(api_base: str) -> bool:
    """Check if endpoint points to llama-autoloader."""
    return ':1234/' in api_base or ':9123/' in api_base
```

### 4. ToolDispatcher: Save State Before Delegation, Restore After

In `agent_cascade/tool_dispatcher.py`, in `handle_call_agent()` (line ~151):

**Save parent's state BEFORE spawning child:**

Add a new helper method `_save_parent_state_before_delegation(instance)` and call it early in `handle_call_agent()`, right after argument validation and before slot collision detection. This saves the parent's KV cache so we can restore it if the model gets evicted while the child runs.

```python
from agent_cascade.state_ops import save_state, is_autoloader_endpoint

def _save_parent_state_before_delegation(self, instance: 'AgentInstance') -> bool:
    """Save parent's state before delegating via call_agent. Returns True if saved."""
    router = self.pool.api_router
    if not router:
        return False
    
    # Look up endpoint config fresh — don't track it on the instance
    endpoint_cfg = router.get_llm_config(instance.agent_class)
    if not endpoint_cfg or not endpoint_cfg.get('state_save_enabled'):
        return False
    
    api_base = endpoint_cfg.get('api_base', '')
    model = endpoint_cfg.get('model', '')
    
    if not api_base or not model or not is_autoloader_endpoint(api_base):
        return False
    
    label = save_state(api_base, model, instance.instance_name)
    if label:
        with instance._state_lock:
            instance._state_label = label
        return True
    
    return False

def handle_call_agent(self, args, messages, instance, function_id=None):
    caller_name = instance.instance_name
    
    # ... existing validation (lines 180-209) ...
    
    # NEW: Save parent's state before delegating via call_agent.
    # Best-effort — silent on failure, never blocks execution.
    try:
        self._save_parent_state_before_delegation(instance)
    except Exception:
        pass
    
    # ... rest of existing logic (active instance guard, slot collision, etc.) ...
```

**Restore parent's state AFTER child completes (in `_run_child_sync()`):**

In `_run_child_sync()` around line ~436, right after `run_child_core()` returns and before returning the result:

```python
from agent_cascade.state_ops import restore_state, is_autoloader_endpoint

def _run_child_sync(self, agent_class, instance_name, args, caller_slot_holder, caller_name, child_depth):
    from agent_cascade.child_runner import run_child_core
    
    # ... existing slot release logic (lines 411-420) ...
    
    try:
        result = run_child_core(...)
        
        # NEW: Restore parent's state if it was saved before delegation.
        # Clear label on failure so we don't retry stale state.
        try:
            with caller_slot_holder._state_lock:
                label = caller_slot_holder._state_label
            
            if label:
                router = self.pool.api_router
                if router:
                    endpoint_cfg = router.get_llm_config(caller_slot_holder.agent_class)
                    api_base = endpoint_cfg.get('api_base', '') if endpoint_cfg else ''
                    model = endpoint_cfg.get('model', '') if endpoint_cfg else ''
                    
                    if api_base and model and is_autoloader_endpoint(api_base):
                        success = restore_state(api_base, model, label)
                        if not success:
                            # Restore failed — clear the label to avoid retrying stale state
                            with caller_slot_holder._state_lock:
                                caller_slot_holder._state_label = None
        except Exception:
            pass  # Silent failure
        
        logger.debug(f"[SLOT_SYNC_CHILD_COMPLETE] Sync child '{instance_name}' completed")
        return result
    
    except Exception as e:
        # ... existing error handling (lines 438-442) ...
    
    finally:
        # ... existing slot re-acquisition (lines 444-458) ...
```

For async path (`_run_child_async`), the parent resumes later via `resume_instance()` which triggers `_setup_turn()` — see section 5.

### 5. ExecutionEngine: Restore State on Agent Resume

In `agent_cascade/execution_engine.py`, in `_setup_turn()` (line ~1517):

When resuming an existing agent instance, restore state BEFORE building messages:

```python
from agent_cascade.state_ops import restore_state, is_autoloader_endpoint

def _setup_turn(self, instance: AgentInstance) -> tuple:
    inst_name = instance.instance_name
    
    # NEW: Restore state if agent has a saved label.
    # Clear label on failure so we don't retry stale state.
    try:
        with instance._state_lock:
            label = instance._state_label
        
        if label:
            router = self.pool.api_router
            if router:
                endpoint_cfg = router.get_llm_config(instance.agent_class)
                api_base = endpoint_cfg.get('api_base', '') if endpoint_cfg else ''
                model = endpoint_cfg.get('model', '') if endpoint_cfg else ''
                
                if api_base and model and is_autoloader_endpoint(api_base):
                    success = restore_state(api_base, model, label)
                    if not success:
                        # Restore failed — clear the label to avoid retrying stale state
                        with instance._state_lock:
                            instance._state_label = None
    except Exception:
        pass  # Silent failure
    
    # ... existing setup logic (lines 1529+) ...
```

### 6. Cleanup: Clear State on Termination/Dismissal

In `agent_cascade/agent_pool.py`, in both `terminate_instance()` and `dismiss_instance()`:

Clear the state label when an agent is terminated or dismissed:

```python
def terminate_instance(self, instance_name: str, set_global_stopped: bool = False):
    # ... existing termination logic ...
    
    inst = self.instances.get(instance_name)
    if inst:
        with inst._state_lock:
            inst._state_label = None
    
    # ... rest of existing logic ...

def dismiss_instance(self, instance_name: str):
    # ... existing dismissal logic ...
    
    inst = self.instances.get(instance_name)
    if inst:
        with inst._state_lock:
            inst._state_label = None
    
    # ... rest of existing logic (remove_instance call) ...
```

## Integration Points Summary

| Operation | Location | When |
|-----------|----------|------|
| Save state before delegation | `tool_dispatcher.py:handle_call_agent()` | BEFORE spawning child agent via call_agent |
| Restore after sync child | `tool_dispatcher.py:_run_child_sync()` | After child completes, before returning to parent |
| Restore on resume | `execution_engine.py:_setup_turn()` | When resuming an agent instance (async path or manual resume) |
| Cleanup on terminate | `agent_pool.py:terminate_instance()` and `dismiss_instance()` | When agent is terminated/dismissed |

## Thread Safety

All reads/writes of `_state_label` use `instance._state_lock` (existing RLock):
- Save state: write label under lock after save succeeds
- Restore state: read under lock, clear label under lock on failure
- Cleanup: write under lock

### 7. UI Changes: Add "Enable State Save" Checkbox to Endpoint Config

Add a checkbox toggle in the endpoint configuration panel so users can enable/disable state save per endpoint without editing JSON directly.

**File:** `web_ui/app.js` — function `renderApiEndpoints()` (line ~4734)

Add checkbox alongside existing toggles ("Vision Enabled", "Custom Sampling"). Follow the same pattern:

In the HTML template, after the "Custom Sampling" toggle (around line 4827), add:

```html
<label class="setting-field toggle-field" style="margin:4px 0 0 0;font-size:12px;cursor:pointer;" title="Enable KV cache state save/restore for this endpoint (autoloader only)">
  <span>💾 State Save</span>
  <input type="checkbox" class="ep-input-state-save" ${epStateSave ? 'checked' : ''}>
</label>
```

And add the default extraction above the template (around line 4761):

```js
const epStateSave = !!ep.state_save_enabled;  // default False
```

**File:** `web_ui/app.js` — function `handleApiEndpointToggle()` (line ~4954)

Add handler for the new checkbox, following the vision toggle pattern:

```js
// State save toggle — just save state immediately
const stateSaveToggle = e.target.closest('.ep-input-state-save');
if (stateSaveToggle) {
  const card = stateSaveToggle.closest('.api-endpoint-card');
  const endpoints = state.api_router?.endpoints || [];
  const ep = endpoints.find(ep => ep.id === card.dataset.id);
  if (ep) {
    ep.state_save_enabled = e.target.checked;
    sendApiRouterUpdate();
  }
}
```

**File:** `web_ui/app.js` — function `handleApiEndpointBlur()` (line ~5007)

Add checkbox read in blur handler, alongside vision/custom-sampling:

```js
const stateSaveCb = card.querySelector('.ep-input-state-save');
if (stateSaveCb) ep.state_save_enabled = stateSaveCb.checked;
```

**File:** `agent_cascade/api_router.py` — `APIEndpoint` dataclass (line ~92)

Already covered in section 2 (`state_save_enabled: bool = False`). Ensure JSON serialization includes it.

### 8. Autoloader State Cleanup: Prune Old States on Save

When saving a new state, clean up older states for the same agent instance to avoid disk bloat from forced process interruptions or abandoned saves. Keep last N (default 3) states per instance_name prefix.

**File:** `agent_cascade/state_ops.py` — update `save_state()` function

After successfully saving a new state, query existing states via autoloader's `/v1/models/{model}/state` endpoint, filter by this instance's label prefix, and delete oldest if count exceeds N.

```python
import httpx
import time
from typing import Optional, List

MAX_STATES_PER_INSTANCE = 3   # Keep last 3 states per agent instance

def save_state(api_base: str, model: str, instance_name: str) -> Optional[str]:
    """Save KV cache state. Returns label if successful, None on failure."""
    try:
        base = api_base.rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        
        label = f"{instance_name}_{int(time.time())}"
        url = f"{base}/v1/models/{model}/state/save"
        resp = httpx.post(url, json={"label": label}, timeout=30)
        
        if resp.status_code == 200:
            # Cleanup old states for this instance after successful save
            _cleanup_old_states(base, model, instance_name)
            return label
        return None
    except Exception:
        return None

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
            
    except Exception:
        pass  # Best-effort cleanup, silent failure

def _delete_state(api_base_no_v1: str, model: str, label: str):
    """Delete a saved state by label. Uses DELETE endpoint if available."""
    try:
        url = f"{api_base_no_v1}/v1/models/{model}/state/{label}"
        resp = httpx.delete(url, timeout=10)
        # If no DELETE endpoint exists yet (500/405), fall through silently.
        # Future: add DELETE /v1/models/{model_id}/state/{label} to autoloader.
    except Exception:
        pass  # Silent failure — state save succeeds even if cleanup fails
```

**Note on DELETE endpoint:** The current autoloader (`llama-autoloader/server.py`) has GET `/v1/models/{model_id}/state` (list), POST `.../state/save`, and POST `.../state/load`, but no DELETE. Options:
- **Preferred:** Add `DELETE /v1/models/{model_id}/state/{label}` to autoloader — maps directly to `Path.unlink()` on the state file.
- **Alternative:** Have `_delete_state()` construct the filesystem path directly (`save_state_dir/model.label.bin`) and delete via OS call from state_ops.py, but this couples us to autoloader's internal directory layout.

For now, `_delete_state()` is a no-op if DELETE isn't available — cleanup can be added once the endpoint exists.

## Integration Points Summary

| Operation | Location | When |
|-----------|----------|------|
| Save state before delegation | `tool_dispatcher.py:handle_call_agent()` | BEFORE spawning child agent via call_agent |
| Restore after sync child | `tool_dispatcher.py:_run_child_sync()` | After child completes, before returning to parent |
| Restore on resume | `execution_engine.py:_setup_turn()` | When resuming an agent instance (async path or manual resume) |
| Cleanup on terminate | `agent_pool.py:terminate_instance()` and `dismiss_instance()` | When agent is terminated/dismissed |
| Prune old states | `state_ops.py:save_state()` → `_cleanup_old_states()` | After each successful state save |

## Thread Safety

All reads/writes of `_state_label` use `instance._state_lock` (existing RLock):
- Save state: write label under lock after save succeeds
- Restore state: read under lock, clear label under lock on failure
- Cleanup: write under lock

State cleanup in `_cleanup_old_states()` operates asynchronously via HTTP calls and does not hold the instance lock — it's independent filesystem/autoloader operation.

## Key Design Decisions

1. **Save only before delegation, not after turns** — saves are expensive HTTP calls; we only need them when the parent is paused while a child runs.
2. **No endpoint tracking on instance** — look up endpoint config fresh from `api_router.get_llm_config(agent_class)` when needed. Simpler, no stale references.
3. **Unique labels per instance** — `{instance_name}_{timestamp}` format prevents collisions between agents using the same model.
4. **Clear label on restore failure** — prevents retrying stale state labels that may have been cleaned up by autoloader.
5. **All operations are best-effort** — silent failures, never block execution or affect user experience.
6. **UI checkbox follows existing patterns** — same toggle-field structure as "Vision Enabled" and "Custom Sampling", immediate save via `sendApiRouterUpdate()`.
7. **State cleanup is optional/future-ready** — `_cleanup_old_states()` queries list endpoint now; actual deletion awaits DELETE endpoint in autoloader. No breaking changes if cleanup silently fails.

## That's It

- Save state before call_agent delegation (best effort)
- Restore state after sync child completes OR on agent resume
- Clear stale state labels on restore failure
- Clean up label on termination/dismissal
- No tracking of endpoint/model on instance — look up fresh from config
- UI checkbox for per-endpoint enable/disable
- Prune old states (last N per instance) after each save (future: needs autoloader DELETE endpoint)

**Total code changes:** ~60 lines across 4 files + state_ops.py (~55 lines with cleanup) + app.js UI (~15 lines) = ~130 lines total