# API Server Refactoring Plan

**Repository:** AgentCascade Unified  
**Target Files:** `agent_cascade/api_server.py` (2,586 lines), `agent_cascade/api_integration.py` (1,555 lines)  
**Created:** 2026-06-29  
**Author:** RefactorPlanner  
**Revised:** 2026-06-29 — Addressed all reviewer findings from first review cycle  

---

## Section 1: Overview

### Current State

| File | Lines | Key Problems |
|------|-------|-------------|
| `api_server.py` | 2,586 | ws_chat() is a ~1,312-line monolith; 20+ message types in one if/elif chain; ~40 FIX comments; REST endpoints nested inside create_app(); __main__ block duplicates startup logic (132 lines) |
| `api_integration.py` | 1,555 | 5 separate module-level caches with own locks (cache sprawl); build_state_from_pool is 147-line monolith doing 15 things; build_stream_update_from_pool is 168-line monolith with overlapping logic |
| `start_api_server.py` | 192 | Signal handler duplicated in api_server.py __main__ block |
| `run_server.py` | 177 | Legacy/dead code (references `agent_server.schema.GlobalConfig`) |

### Target State After Refactoring

- **api_server.py**: ~1,400 lines (45% reduction) — clean ws_chat with dispatch table pattern
- **api_integration.py**: ~900 lines (42% reduction) — unified cache manager, split build functions
- **New files**: 3 small modules (~750 lines total for handlers + security + config)
- **Total new code**: ~1,050 lines across all files

### Guiding Principles

1. **No behavioral changes** — identical WebSocket protocol, REST endpoints, and frontend output
2. **Backward compatible** — no API contract changes
3. **Incremental** — each phase is independently testable
4. **Single responsibility** — each function does one thing well
5. **DRY** — eliminate duplication via helpers

---

## Section 2: Phase-by-Phase Breakdown

### Phase 0 — Quick Wins (Low Risk, High Impact)

#### 0A. Remove FIX Comments (~37 comments in api_server.py)

All 37 FIX/fix comments are historical annotations from past fixes. They should be removed or converted to concise inline comments where the fix rationale is still relevant.

**Pattern:**
```python
# Before (verbose):
# FIX 1 (Race Condition): Protect session['generating'] check with session_lock
# to prevent two concurrent engine.run() calls for the same instance.
# Without this lock, two WebSocket messages arriving nearly simultaneously
# could both read generating=False and start separate runs.

# After (concise or removed if obvious from context):
is_generating = _is_generating(session)  # Thread-safe guard against concurrent starts
```

**Action:** Remove ~37 FIX comments. Keep only those explaining non-obvious design decisions (e.g., "CRITICAL FIX: Mark activity BEFORE transitioning to IDLE").

#### 0B. Create `_broadcast_state()` Helper

The broadcast pattern `await broadcast({'type': 'state', **build_state(...)})` appears **34 times** in api_server.py with nearly identical structure. Three variants exist, plus the `_sender_loop` dismissal broadcast:

| Variant | Count | Pattern |
|---------|-------|---------|
| State broadcast (default) | ~25 | `{'type': 'state', **build_state()}` |
| State broadcast (generating=True/False) | ~5 | `{'type': 'state', **build_state(generating=True/False)}` |
| Done broadcast | ~3 | `{'type': 'done', **build_state()}` |
| Error broadcast | ~2 | `{'type': 'error', ...}` (not state-based, excluded from helper) |
| Approvals broadcast | ~1 | `{'type': 'approvals', ...}` (not state-based, excluded from helper) |
| `_sender_loop` dismissal | ~1 | `{'type': 'state', **build_state()}` (line 699) |

**New helper in `api_server.py`** (inside `create_app()`):
```python
async def _broadcast_state(
    ws_type: str = 'state',
    generating: Optional[bool] = None,
) -> None:
    """Broadcast state update to all WebSocket clients.
    
    Args:
        ws_type: WebSocket message type ('state', 'done', 'error').
        generating: Override generating flag. None = read from session.
    """
    await broadcast({'type': ws_type, **build_state(generating=generating)})
```

**Impact:** Replaces 34 near-identical call sites (including the `_sender_loop` dismissal broadcast at line 699) with clean `_broadcast_state()` calls. Error and approvals broadcasts are excluded as they use different payload structures.

#### 0C. Deduplicate `_clear_performance_caches()` Calls

The pattern appears **4 times** (lines 863-866, 2255-2258, 2297-2300, 2346-2349) with identical try/except/import structure:
```python
# Current (repeated 4x):
try:
    from agent_cascade.api_integration import _clear_performance_caches
    _clear_performance_caches()
except Exception as e:
    logger.debug(f"Cache clearing failed ...")
```

**New helper in `api_server.py`** (inside `create_app()`):
```python
def _clear_caches(reason: str = "operation") -> None:
    """Clear performance caches with error suppression."""
    try:
        from agent_cascade.api_integration import _clear_performance_caches
        _clear_performance_caches()
    except Exception as e:
        logger.debug(f"Cache clearing failed during {reason} (non-critical): {e}")
```

**Impact:** 4 call sites → 1 helper + 4 clean calls.

#### 0D. Extract System Message Helper

The system message extraction pattern appears **3 times** (lines 367-373, 1128-1135, 2533-2537) with identical logic:
```python
sys_content = None
if hasattr(agent, 'base_system_message') and agent.base_system_message:
    sys_content = str(agent.base_system_message)
elif hasattr(agent, 'system_message') and agent.system_message:
    sys_content = str(agent.system_message)
```

**New helper in `api_server.py`** (module-level):
```python
def _extract_system_message(agent) -> str:
    """Extract system message content from an agent.
    
    Priority: base_system_message > system_message > llm.cfg['system'].
    Returns '' (empty string) if no system message found — consistent with
    how downstream callers check truthiness via `if sys_content:`.
    Never returns None.
    """
    if hasattr(agent, 'base_system_message') and agent.base_system_message:
        return str(agent.base_system_message)
    if hasattr(agent, 'system_message') and agent.system_message:
        return str(agent.system_message)
    if hasattr(agent, 'llm') and hasattr(agent.llm, 'cfg'):
        cfg = agent.llm.cfg
        val = cfg.get('system', '') or cfg.get('system_message', '')
        if val:
            return val
    return ''  # Return empty string (not None) for consistent truthiness checks
```

**Return type note:** Returns `''` (empty string) rather than `None` when no system message is found. This matches the original code pattern where callers use `if sys_content:` — both `None` and `''` are falsy, but returning a consistent string type avoids potential issues with downstream callers that expect str.

**Impact:** 3 duplicated blocks → 1 helper + 3 clean calls.

---

### Phase 0.5 — Import Audit (NEW)

Between Phase 0 quick wins and the main refactoring phases, we perform an explicit import audit to prevent circular dependency issues.

#### 0.5A. Per-File Import Table

Each new/modified file will have a documented import table:

| File | Imports From | Purpose |
|------|-------------|---------|
| `api_server.py` | `api_integration.py` (build_state, serialize_message, etc.) | State building and serialization |
| `api_server.py` | `ws_handlers.py` [NEW] | WsMessageHandler class for dispatch |
| `api_server.py` | `security_handler.py` [NEW] | SecurityAdvisorHandler class |
| `api_server.py` | `config_handlers.py` [NEW] | ConfigUpdateRouter class |
| `api_server.py` | `shared_init.py` | setup_signal_handler() |
| `ws_handlers.py` | `api_integration.py` (build_state_from_pool, serialize_message) | State building in handlers |
| `ws_handlers.py` | `security_handler.py` [NEW] | Security advisor checks |
| `ws_handlers.py` | `config_handlers.py` [NEW] | Config update routing |
| `security_handler.py` | `api_integration.py` (serialize_message) | Instance serialization for security agent |
| `security_handler.py` | `compression/helpers.py` (extract_instance_output) | Output parsing from engine results |
| `config_handlers.py` | `tools/mcp_manager.py` (init_mcp_tools) | MCP server initialization |
| `api_integration.py` | — (no new imports from new files) | No circular deps possible |

**Circular dependency verification:**
```
Import chain: api_server → ws_handlers → security_handler → compression/helpers ✓
Import chain: api_server → ws_handlers → config_handlers → tools/mcp_manager ✓
Import chain: api_integration → [nothing new] ✓
No cycles detected. All new files import from existing modules only, or from sibling new files with no back-references.
```

#### 0.5B. Import Order Convention

All imports will follow this order within each file:
1. Standard library (sorted alphabetically)
2. Third-party packages (sorted alphabetically)
3. Local package imports (`agent_cascade.*`, sorted by module depth)

---

### Phase 1 — api_integration.py Cleanup (moved before WS handler decomposition)

> **Note:** This phase was previously numbered "Phase 4" but is moved here because the new `ws_handlers.py` will import from cleaned-up `api_integration.py`. Doing this first ensures clean imports.

#### 1A. Cache Manager Design

Currently there are **5 separate caches** with **5 separate locks**:

| Cache | Lock | Purpose | Max Size |
|-------|------|---------|----------|
| `_token_stats_cache` | `_token_stats_lock` | Token stats per conversation identity | 5000 |
| `_last_stream_versions` | `_stream_version_lock` | Version tracking for incremental serialization | Unbounded |
| `_cached_instance_data` | `_stream_version_lock` | Cached serialized instance data | Unbounded |
| `_ui_serialization_cache` | `_ui_cache_lock` | UI message serialization cache | 2000 |
| `_stream_token_stats_cache` | `_stream_token_stats_lock` | Stream token stats per instance | 100 |

**New design:** Single `CacheManager` class in `api_integration.py`:

```python
class CacheManager:
    """Centralized performance cache management for API integration.
    
    Provides thread-safe access to all caches with unified clearing,
    bounded sizes, and FIFO eviction.
    
    PAIRED CACHE EVICTION NOTE:
        The stream_versions and cached_instances caches are paired — they share
        the same lock (_stream_version_lock) in the original code. When evicting
        from one, the corresponding entry in the other should also be removed
        to prevent orphaned data. The evict_if_full method handles this pairing.
    """
    
    def __init__(self):
        self._lock = threading.RLock()  # Single reentrant lock for all caches
        
        # Token stats cache: (msg_count, last_msg_id) -> stats dict
        self.token_stats: Dict[tuple, dict] = {}
        self.token_stats_maxsize = 5000
        
        # Stream version tracking: instance_name -> (msg_count, id, stream_len, content_len)
        self.stream_versions: Dict[str, tuple] = {}
        
        # Cached serialized instance data: instance_name -> dict
        self.cached_instances: Dict[str, dict] = {}
        
        # UI serialization cache: msg_id -> serialized dict
        self.ui_serialization: Dict[int, dict] = {}
        self.ui_maxsize = 2000
        
        # Stream token stats: instance_name -> (h_stats, r_stats)
        self.stream_token_stats: Dict[str, tuple] = {}
        self.stream_token_stats_maxsize = 100
    
    def clear_all(self) -> None:
        """Clear all caches. Called during session reset."""
        with self._lock:
            self.token_stats.clear()
            self.stream_versions.clear()
            self.cached_instances.clear()
            self.ui_serialization.clear()
            self.stream_token_stats.clear()
    
    def evict_if_full(self, cache_name: str, maxsize: int) -> None:
        """Evict oldest entry if cache exceeds max size (FIFO).
        
        Handles paired cache eviction for stream_versions/cached_instances:
        when an instance is evicted from one, its pair in the other is also removed.
        
        Args:
            cache_name: Name of the cache to check ('token_stats', 'ui_serialization',
                        'stream_token_stats', 'stream_versions', or 'cached_instances').
            maxsize: Maximum allowed entries before eviction triggers.
        """
        with self._lock:
            # Determine which cache dict and its pair (if any)
            if cache_name == 'token_stats':
                target = self.token_stats
                paired = None
            elif cache_name == 'ui_serialization':
                target = self.ui_serialization
                paired = None
            elif cache_name == 'stream_token_stats':
                target = self.stream_token_stats
                paired = None
            elif cache_name == 'stream_versions':
                target = self.stream_versions
                paired = ('cached_instances', self.cached_instances)
            elif cache_name == 'cached_instances':
                target = self.cached_instances
                paired = ('stream_versions', self.stream_versions)
            else:
                return
            
            while len(target) >= maxsize:
                oldest_key = next(iter(target))
                target.pop(oldest_key)
                # Evict from paired cache if it exists
                if paired and oldest_key in paired[1]:
                    paired[1].pop(oldest_key, None)

    def evict_instance(self, instance_name: str) -> None:
        """Evict all cached data for a specific instance (paired eviction)."""
        with self._lock:
            self.stream_versions.pop(instance_name, None)
            self.cached_instances.pop(instance_name, None)
            self.stream_token_stats.pop(instance_name, None)
```

**Module-level instance:**
```python
_cache_mgr = CacheManager()

def _clear_performance_caches():
    """Clear all module-level performance caches."""
    _cache_mgr.clear_all()
```

#### 1B. Splitting `build_state_from_pool()` (147 lines → ~6 functions)

Current function does 15 distinct operations. We'll split into focused helpers:

| New Function | Purpose | Lines | Called By |
|-------------|---------|-------|-----------|
| `_get_instance_messages(pool, name, responses)` | Read conversation + extend with responses | 8 | build_state_from_pool, build_stream_update_from_pool |
| `_calc_token_stats(pool, msgs, responses)` | Calculate h_stats and r_stats with error handling | 15 | Both build functions (shared) |
| `_serialize_all_instances(pool, streaming=True/False)` | Serialize all instances in pool snapshot | 20 | Both build functions (shared) |
| `_get_session_metadata(pool, instance_name)` | Derive session_name from root instances | 8 | Both build functions (shared) |
| `_build_state_extras(pool, instance_name)` | Telemetry, API router state, workspace path | 15 | Both build functions (shared) |
| `build_state_from_pool()` | Orchestrates above helpers, assembles result dict | 30 | api_server.py |

**Refactored structure:**
```python
def build_state_from_pool(pool, instance_name, responses=None, generating=False):
    """Build a full state snapshot for the frontend."""
    instance = pool.get_instance(instance_name)
    if instance is None:
        return None
    
    msgs = _get_instance_messages(pool, instance_name, responses)
    h_stats, r_stats = _calc_token_stats(pool, msgs, responses)
    all_instances = _serialize_all_instances(pool, streaming=False)
    session_name = _get_session_metadata(pool, instance_name)[0]
    
    max_tokens = _resolve_max_tokens(pool, instance)
    telemetry_data = _safe_get_telemetry(pool, instance_name)
    api_router_state = _safe_get_api_router_state(pool)
    default_workspace = _get_default_workspace(pool)
    is_waiting = _check_is_waiting(pool, instance_name)
    
    pending_approvals = _get_approvals(pool)
    
    return {
        'messages': [serialize_message(m, i) for i, m in enumerate(msgs)],
        'instances': all_instances,
        'agent_instances': all_instances,
        'active_stack': list(pool._execution.active_stack) if hasattr(pool, '_execution') else [],
        **( {'approvals': pending_approvals} if pending_approvals else {} ),
        'generating': generating,
        'session_name': session_name,
        'instance_name': instance_name,
        'total_tokens': h_stats['tokens'] + r_stats['tokens'],
        'total_words': h_stats['words'] + r_stats['words'],
        'max_tokens': max_tokens,
        'summary': instance.compression_summary or "",
        'has_queued_messages': pool.has_messages(instance_name),
        'stopped': pool.stopped,
        'agents': _build_agents_list(pool),
        'current_model': _get_current_model(pool, instance),
        'telemetry': telemetry_data,
        'default_workspace': default_workspace,
        'is_waiting': is_waiting,
        'api_router': api_router_state,
    }
```

#### 1C. Consolidating Duplicate Logic Between Build Functions

`build_state_from_pool()` and `build_stream_update_from_pool()` share these operations:
- Token stats calculation (with caching)
- Instance serialization loop
- Max tokens resolution
- Current model lookup
- Telemetry fetch
- API router state retrieval
- Pending approvals check

**Consolidation strategy:** The shared helper functions from 1B are reused by both build functions. The main difference is:
- `build_state_from_pool()` always does full serialization (streaming=False)
- `build_stream_update_from_pool()` uses incremental serialization with version checking

The incremental serialization logic in `build_stream_update_from_pool()` will be extracted to `_serialize_instances_incremental()`, keeping the version-tracking cache management isolated.

---

### Phase 2 — WebSocket Handler Decomposition (Highest Impact)

> **Note:** Previously numbered "Phase 1". Renumbered after Phase 1 moved above.

#### 2A. Design: `WsMessageHandler` Class

The ws_chat() function handles **20 message types** in one if/elif chain spanning ~1,312 lines. We'll extract a handler class with a dispatch table pattern.

**New file:** `agent_cascade/ws_handlers.py` (~650-750 lines)

```python
"""WebSocket message handlers for the AgentCascade API server."""

from typing import Any, Callable, Dict, Optional
import threading


class WsMessageHandler:
    """Dispatches WebSocket messages to appropriate handler methods.
    
    Each message type has its own method with a consistent signature:
        async def handle_<type>(self, data: dict) -> None
    
    The handler maintains references to shared state (session, agent_pool, etc.)
    and provides helper methods for common operations.
    
    Constructor uses start_gen_fn pattern instead of raw run_agent_thread
    to provide a stable callable interface that encapsulates thread creation
    with all necessary context (history extraction, session lock, pool access).
    """
    
    def __init__(
        self,
        session: Dict[str, Any],
        agent_pool,          # AgentPool instance
        agents: list,        # List of agent objects
        send_queue,          # asyncio.Queue
        broadcast: Callable,  # async broadcast(data) -> None
        build_state: Callable,  # build_state(responses=None, generating=None) -> dict
        start_gen_fn: Callable,  # Thread entry point: (history, runner, gen_id, loop, target_name) -> None
        session_lock: threading.Lock,  # Session lock for thread-safe access to shared state
    ):
        self.session = session
        self.agent_pool = agent_pool
        self.agents = agents
        self.send_queue = send_queue
        self.broadcast = broadcast
        self.build_state = build_state
        
        # Session helpers (thin wrappers for thread-safe access)
        self._session_lock = session_lock
        self._start_gen_fn = start_gen_fn
        
        # Dispatch table: message_type -> handler method
        # Includes 'error' type and unknown fallback via handle_unknown.
        self._dispatch: Dict[str, Callable] = {
            'message': self.handle_message,
            'continue': self.handle_continue,
            'stop': self.handle_stop,
            'pause': self.handle_pause,
            'resume_all': self.handle_resume_all,
            'resume': self.handle_resume,
            'terminate_agent_instance': self.handle_terminate,
            'terminate_sub_agent': self.handle_terminate,  # Alias
            'retry': self.handle_retry,
            'reset': self.handle_reset,
            'refresh_souls': self.handle_refresh_souls,
            'restart_server': self.handle_restart_server,
            'update_config': self.handle_update_config,
            'update_endpoints': self.handle_update_endpoints,
            'update_api_priorities': self.handle_update_api_priorities,
            'approve': self.handle_approve,
            'reject': self.handle_reject,
            'ask_security': self.handle_ask_security,
            'set_auto_security': self.handle_set_auto_security,
            'edit_message': self.handle_edit_message,
            'delete_messages': self.handle_delete_messages,
            'select_agent': self.handle_select_agent,
            'set_session_name': self.handle_set_session_name,
            'load_session': self.handle_load_session,
            'inject': self.handle_inject,
            # Error type for consistency (sent by handlers, but also accepted from clients)
            'error': self.handle_error,
        }
    
    async def dispatch(self, data: dict) -> None:
        """Route a parsed WebSocket message to the appropriate handler."""
        msg_type = data.get('type', '')
        handler = self._dispatch.get(msg_type)
        if handler is not None:
            await handler(data)
        else:
            # Fallback for unknown types — log and silently ignore
            await self.handle_unknown(msg_type, data)
    
    async def handle_unknown(self, msg_type: str, data: dict) -> None:
        """Handle unrecognized message types."""
        logger.debug(f"Unknown WebSocket message type: {msg_type!r} "
                     f"(data keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'})")
    
    # ── Handler Methods (one per message type) ──
    async def handle_message(self, data: dict) -> None: ...
    async def handle_continue(self, data: dict) -> None: ...
    async def handle_error(self, data: dict) -> None: ...  # Client-side error echo
    # ... etc.
```

#### 2B. Message Type Inventory and New Method Names

| Message Type | Lines in ws_chat() | Complexity | New Handler Method | Notes |
|-------------|-------------------|------------|-------------------|-------|
| `message` | ~83 (1087-1170) | Medium | `handle_message()` | Starts agent thread, creates instance if needed |
| `continue` | ~63 (1172-1234) | Medium | `handle_continue()` | Pops trailing assistant msg, starts thread |
| `stop` | ~69 (1236-1306) | High | `handle_stop()` | Transitions all agents to IDLE, cleans up stacks |
| `pause` | ~13 (1308-1320) | Low | `handle_pause()` | Sets global pause flag |
| `resume_all` | ~14 (1322-1336) | Low | `handle_resume_all()` | Clears pause flag, starts generation |
| `resume` | ~121 (1338-1459) | High | `handle_resume()` | Restores agent pools from logs, complex logic |
| `terminate_*` | ~46 (1460-1506) | Medium | `handle_terminate()` | Dismisses instance with cascade termination |
| `retry` | ~73 (1508-1611) | High | `handle_retry()` | Trims tail, rolls back snapshots, re-enqueues |
| `reset` | ~23 (1613-1635) | Low | `handle_reset()` | Clears conversation, resets session |
| `refresh_souls` | ~10 (1637-1647) | Low | `handle_refresh_souls()` | Refreshes agent templates |
| `restart_server` | ~3 (1649-1652) | Trivial | `handle_restart_server()` | os.execl call |
| `update_config` | ~89 (1654-1744) | High | `handle_update_config()` | 12+ config key checks |
| `update_endpoints` | ~8 (1746-1753) | Low | `handle_update_endpoints()` | Bulk endpoint update |
| `update_api_priorities` | ~9 (1755-1763) | Low | `handle_update_api_priorities()` | Priority mapping update |
| `approve` | ~8 (1765-1772) | Low | `handle_approve()` | Approves request, broadcasts state |
| `reject` | ~9 (1774-1782) | Low | `handle_reject()` | Rejects with reason, broadcasts state |
| `ask_security` | ~100 (1784-2185) | Very High | `handle_ask_security()` | Creates Security agent instance, runs engine |
| `set_auto_security` | ~3 (2187-2190) | Trivial | `handle_set_auto_security()` | Stores toggle state |
| `edit_message` | ~68 (2192-2260) | Medium | `handle_edit_message()` | Edits conversation, rebuilds instance |
| `delete_messages` | ~41 (2262-2302) | Medium | `handle_delete_messages()` | Prunes messages from conversation |
| `select_agent` | ~3 (2304-2306) | Trivial | `handle_select_agent()` | Updates agent_index |
| `set_session_name` | ~9 (2308-2317) | Low | `handle_set_session_name()` | Renames session, migrates summaries |
| `load_session` | ~30 (2321-2350) | Medium | `handle_load_session()` | Loads from log file |
| `inject` | ~4 (2352-2356) | Trivial | `handle_inject()` | Enqueues message to agent queue |

#### 2C. Dispatch Table Pattern with WebSocketDisconnect Handling

The dispatch table in `_dispatch` dict maps message types to handler methods. This eliminates the 20-level if/elif chain:

```python
# Before (in ws_chat):
if msg_type == 'message':
    ...
elif msg_type == 'continue':
    ...
# ... 18 more elif branches

# After (in ws_chat):
handler = WsMessageHandler(
    session, agent_pool, agents, send_queue, broadcast, build_state, start_gen_fn, session_lock
)

try:
    while True:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        await handler.dispatch(data)
except WebSocketDisconnect:
    pass  # Normal client disconnect
except Exception as e:
    logger.error(f"WebSocket handler error for client: {e}")
finally:
    ws_connections.discard(websocket)
```

**Key difference from original:** The `while True` loop is wrapped in a try/except/finally block that catches `WebSocketDisconnect`, logs errors, and cleans up connections. This matches the current behavior exactly (lines 2358-2364).

#### 2D. Broadcast Pattern in Each Handler

Each handler method uses the `_broadcast_state()` helper from Phase 0B. Handlers that need to broadcast after their operation call:

```python
await self._broadcast('state')   # Default state broadcast
await self._broadcast('done')    # Done broadcast (after stop/reset)
```

The `WsMessageHandler` provides a convenience method:
```python
async def _broadcast(self, ws_type: str = 'state', generating: Optional[bool] = None) -> None:
    await self.broadcast({'type': ws_type, **self.build_state(generating=generating)})
```

#### 2E. start_gen_fn Callable Pattern

Instead of passing the raw `run_agent_thread` function directly, we use a factory pattern where `start_gen_fn` is a callable that encapsulates thread creation with all necessary context:

```python
# In create_app(), after defining run_agent_thread:
def start_gen(history_for_agent, agent_runner, gen_id, loop, target_instance_name=None):
    """Start an agent generation thread.
    
    Wraps run_agent_thread to provide a stable callable interface.
    This abstraction allows the handler to start threads without knowing
    about session_lock internals or history extraction logic.
    """
    with session_lock:
        session['generation_id'] = gen_id
    
    # ... (same body as current run_agent_thread, but exposed via this wrapper)
    
# Then pass to WsMessageHandler:
handler = WsMessageHandler(
    ..., start_gen_fn=start_gen
)

# Inside handler methods:
thread = threading.Thread(
    target=self._start_gen_fn,
    args=(history_copy, agent_runner, gen_id, loop, instance_name),
    daemon=True,
).start()
```

This is more robust than passing `run_agent_thread` directly because:
1. The callable signature is stable and documented
2. Session lock handling is encapsulated within the factory
3. Future changes to thread creation (e.g., adding tracing) don't require handler updates

---

### Phase 3 — Security Handler Extraction

The `ask_security` handler is ~400 lines (1784-2185), handling the entire security advisor lifecycle. This deserves its own class.

**New file:** `agent_cascade/security_handler.py` (~180 lines)

```python
"""Security advisor handler for tool approval checks."""

from typing import Any, Dict, Optional


class SecurityAdvisorHandler:
    """Manages security advisor checks for pending tool approvals.
    
    Lifecycle per check:
      1. Create unique Security agent instance (keyed by request_id)
      2. Run ExecutionEngine with the formatted prompt
      3. Stream updates via WebSocket during execution
      4. Parse [YES]/[NO] verdict from output
      5. Auto-approve or auto-reject based on verdict
      6. Clean up instance state
    
    Thread-safe: uses semaphore to limit concurrency to 1 active check.
    """
    
    def __init__(self, agent_pool, session, send_queue):
        self.agent_pool = agent_pool
        self.session = session
        self.send_queue = send_queue
        
        # Concurrency control (initialized lazily)
        self._semaphore: Optional[threading.Semaphore] = None
        self._lock: Optional[threading.Lock] = None
        self._active_checks: set = set()
        self._checks_lock: threading.Lock = threading.Lock()
    
    def ensure_initialized(self, app_state: Dict[str, Any]) -> None:
        """Initialize concurrency primitives from app state."""
        if self._semaphore is None:
            self._semaphore = app_state.get('security_check_semaphore')
            self._lock = app_state.get('security_check_lock', threading.Lock())
    
    async def check(
        self, request_id: str, auto_apply: bool, approval: dict, loop
    ) -> None:
        """Execute a security advisor check for the given request.
        
        Args:
            request_id: Unique identifier for the pending approval.
            auto_apply: If True, auto-approve/reject based on verdict.
            approval: The approval dict with tool_name, description, tool_args.
            loop: Running asyncio event loop for queue dispatch.
        """
```

**Methods needed:**
| Method | Purpose | Lines |
|--------|---------|-------|
| `__init__()` | Store refs to pool, session, send_queue | 10 |
| `_create_security_instance(request_id)` | Create unique Security agent instance | 25 |
| `_build_prompt(approval)` | Format SECURITY_ADVISOR_PROMPT with args | 15 |
| `_run_engine(sec_instance)` | Execute engine.run() with streaming | 40 |
| `_parse_verdict(text)` | Extract [YES]/[NO] from output text | 30 |
| `_apply_verdict(request_id, verdict, justification)` | Approve/reject + notify UI | 25 |
| `_cleanup(sec_state_key)` | Remove instance state, release semaphore | 15 |
| `check(request_id, auto_apply, approval, loop)` | Main entry point (orchestrates above) | 20 |

**Integration with WsMessageHandler:**
```python
async def handle_ask_security(self, data: dict) -> None:
    rid = data.get('request_id')
    auto_apply = data.get('auto_apply', False)
    
    pending = self.agent_pool.operation_manager.list_pending_approvals()
    ap = next((a for a in pending if a['request_id'] == rid), None)
    if not ap:
        return
    
    sec_handler = SecurityAdvisorHandler(
        self.agent_pool, self.session, self.send_queue
    )
    
    loop = asyncio.get_running_loop()
    # Run check in thread (same as current behavior)
    threading.Thread(
        target=lambda: sec_handler.check(rid, auto_apply, ap, loop),
        daemon=True,
    ).start()
```

---

### Phase 4 — Config Handler Extraction

The `update_config` handler is ~90 lines checking 12+ config keys. We'll use a router pattern.

**New file:** `agent_cascade/config_handlers.py` (~120 lines)

```python
"""Configuration update handlers for the API server."""

from typing import Any, Dict, Optional


class ConfigUpdateRouter:
    """Routes config key updates to their respective setters.
    
    Each config key has a handler function that checks if the value changed
    and applies it only when necessary (defense-in-depth optimization).
    
    Usage:
        router = ConfigUpdateRouter(agent_pool)
        for key, handler in router._handlers.items():
            if key in ui_cfg:
                handler(ui_cfg[key])
    """
    
    def __init__(self, agent_pool):
        self.pool = agent_pool
        self._handlers: Dict[str, callable] = {
            'work_access_folders_ro': self._handle_work_folders,
            'work_access_folders_rw': self._handle_work_folders,
            'default_workspace': self._handle_workspace,
            'idle_timeout_seconds': self._handle_idle_timeout,
            'approval_timeout_seconds': self._handle_approval_timeout,
            'enable_approval_timeout': self._handle_enable_timeout,
            'max_parallel_agents': self._handle_max_workers,
            'auto_continue': self._handle_auto_continue,
        }
    
    def apply(self, ui_cfg: Dict[str, Any]) -> None:
        """Apply all config keys present in ui_cfg."""
        for key, handler in self._handlers.items():
            if key in ui_cfg:
                try:
                    handler(ui_cfg)
                except Exception as e:
                    logger.warning(f"Config update failed for '{key}': {e}")
    
    def _handle_work_folders(self, ui_cfg: dict) -> None: ...
    # etc.
```

**Config Key Inventory:**

| Config Key | Handler Method | Current Lines | Action |
|------------|---------------|---------------|--------|
| `mcpServers` | `_handle_mcp_servers()` | 8 lines | Init MCP tools for all agents |
| `work_access_folders_ro/rw` | `_handle_work_folders()` | 17 lines | Compare + set extra work folders |
| `default_workspace` | `_handle_workspace()` | 9 lines | Set base_dir if changed |
| `idle_timeout_seconds` | `_handle_idle_timeout()` | 2 lines | Update pool settings |
| `approval_timeout_seconds` | `_handle_approval_timeout()` | 3 lines | Set approval timeout |
| `enable_approval_timeout` | `_handle_enable_timeout()` | 3 lines | Toggle timeout enablement |
| `max_parallel_agents` | `_handle_max_workers()` | 5 lines | Resize ThreadPoolExecutor |
| `auto_continue` | `_handle_auto_continue()` | 1 line | Update pool settings |
| LLM keys (model, api_base, etc.) | `_handle_llm_config()` | 8 lines | Update default_llm_cfg if changed |

**Integration with WsMessageHandler:**
```python
async def handle_update_config(self, data: dict) -> None:
    ui_cfg = data.get('generate_cfg', {})
    
    # Handle MCP servers (special case — needs agent list)
    if 'mcpServers' in ui_cfg:
        self._init_mcp_tools(ui_cfg['mcpServers'])
    
    # Route remaining config keys
    router = ConfigUpdateRouter(self.agent_pool)
    router.apply(ui_cfg)
    
    await self._broadcast('state')
```

---

### Phase 5 — Dead Code Removal

#### 5A. `__main__` Block in api_server.py (Lines 2455-2586)

The __main__ block (**132 lines**, not 131: lines 2455 through 2586 inclusive = 132 lines) duplicates startup logic from `start_api_server.py`:
- Same agent initialization pattern
- Same signal handler structure  
- Different default port (12345 vs 8765) and host (0.0.0.0 vs 127.0.0.1)

**Action:** Keep the __main__ block but simplify it to delegate to `start_api_server.py`'s initialization:
```python
if __name__ == "__main__":
    # Delegate to shared startup logic, with CLI overrides for direct invocation
    ...
```

Or alternatively, extract common startup into `agent_cascade/shared_startup.py`.

#### 5B. `run_server.py` (177 lines)

References `agent_server.schema.GlobalConfig` — this is legacy code from the original Qwen project. It's not imported anywhere in the active codebase. Only referenced in documentation files (browser_agent_cn.md, browser_agent.md).

**Action:** Delete `run_server.py`. Update doc references to point to `start_api_server.py`.

#### 5C. Duplicate Signal Handler

Signal handler exists in both:
- `start_api_server.py` lines 164-177
- `api_server.py` __main__ block lines 2560-2574

**Action:** Extract to shared function in `agent_cascade/shared_init.py`:
```python
def setup_signal_handler(agent_pool, server=None):
    """Set up graceful shutdown signal handlers."""
    def handle_shutdown(signum, frame):
        logger.info("\n[INFO] Initiating graceful shutdown...")
        agent_pool.stopped = True
        if hasattr(agent_pool, 'operation_manager') and agent_pool.operation_manager:
            try:
                agent_pool.operation_manager.cleanup_backups()
            except Exception as e:
                logger.warning(f"Cleanup backups failed during shutdown: {e}")
        if server:
            server.should_exit = True
    
    signal.signal(signal.SIGINT, handle_shutdown)
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, handle_shutdown)
    
    return handle_shutdown
```

---

## Section 3: New File Structure

### Directory Layout After Refactoring

```
agent_cascade/
├── api_server.py              (~1,400 lines, down from 2,586)
│   ├── create_app() with nested helpers
│   │   ├── _broadcast_state()          [NEW - Phase 0B]
│   │   ├── _clear_caches()             [NEW - Phase 0C]
│   │   └── WsMessageHandler dispatch   [PHASE 2]
│   └── __main__ block (simplified)     [~40 lines, down from 132]
│
├── api_integration.py         (~900 lines, down from 1,555)
│   ├── CacheManager class            [NEW - Phase 1A]
│   │   └── evict_if_full() with paired eviction support
│   ├── build_state_from_pool()       [SPLIT - Phase 1B]
│   │   ├── _get_instance_messages()    [NEW helper]
│   │   ├── _calc_token_stats()         [NEW helper]
│   │   ├── _serialize_all_instances()  [NEW helper]
│   │   └── _build_state_extras()       [NEW helper]
│   └── build_stream_update_from_pool() [CONSOLIDATED - Phase 1C]
│
├── ws_handlers.py             (~650-750 lines, NEW FILE)     [Phase 2]
│   ├── WsMessageHandler class
│   │   ├── dispatch(data) -> None
│   │   ├── handle_unknown(type, data) -> None    [NEW - unknown fallback]
│   │   └── handle_<type>(data) -> None           [25 methods + error handler]
│
├── security_handler.py        (~180 lines, NEW FILE)         [Phase 3]
│   └── SecurityAdvisorHandler class
│       ├── check(request_id, auto_apply, approval, loop)
│       ├── _create_security_instance()
│       ├── _run_engine()
│       └── _parse_verdict() / _apply_verdict()
│
├── config_handlers.py         (~120 lines, NEW FILE)         [Phase 4]
│   └── ConfigUpdateRouter class
│       ├── apply(ui_cfg) -> None
│       └── _handle_<key>(ui_cfg) -> None           [9 methods]
│
├── shared_init.py             (updated, +50 lines)           [Phase 5C]
│   └── setup_signal_handler() [NEW function]
│
└── run_server.py              (DELETED - Phase 5B)

Total: ~1,400 + ~900 + 700 + 180 + 120 = ~3,300 lines across 5 files
(vs. current 2,586 + 1,555 = 4,141 lines in 2 files)
```

### Line Count Summary

| File | Before | After | Delta |
|------|--------|-------|-------|
| `api_server.py` | 2,586 | ~1,400 | -1,186 |
| `api_integration.py` | 1,555 | ~900 | -655 |
| `ws_handlers.py` | 0 | ~700 (650-750) | +700 (new) |
| `security_handler.py` | 0 | ~180 | +180 (new) |
| `config_handlers.py` | 0 | ~120 | +120 (new) |
| **Total** | **4,141** | **~3,300** | **-841 (-20%)** |

### Import Dependency Graph (Updated)

```
api_server.py
├── api_integration.py  (build_state_from_pool, serialize_message, etc.)
│   ├── agent_instance.py
│   ├── execution_engine.py
│   └── loop_detection.py
│   [NO imports from new files — circular dep free]
│
├── ws_handlers.py     [NEW - Phase 2]
│   ├── api_integration.py (build_state_from_pool, serialize_message)
│   ├── security_handler.py  [NEW - Phase 3]
│   │   └── compression/helpers.py (extract_instance_output)
│   └── config_handlers.py   [NEW - Phase 4]
│       └── tools/mcp_manager.py
│
├── shared_init.py     (setup_signal_handler)
│   [no dependencies on new files]

Import order: api_integration → security_handler/config_handlers → ws_handlers → api_server
No circular dependencies. Verified by Phase 0.5 import audit.
```

---

## Section 4: Migration Strategy

### Execution Order

```
Phase 0  → Phase 0.5 → Phase 1  → Phase 2  → Phase 3  → Phase 4  → Phase 5
(Quick   (Import     (api_     (WS       (Security (Config    (Dead
 Wins)    Audit)      integration) Handlers) Handler) Router)   Code)
```

### Rationale for Order

1. **Phase 0 first** — Quick wins reduce noise and make subsequent phases easier. Removing FIX comments clarifies the code. Helper functions (_broadcast_state, _clear_caches, _extract_system_message) are immediately useful in later phases.

2. **Phase 0.5 next (Import Audit)** — Before creating any new files, verify the import structure to prevent circular dependencies. This is a documentation-only step that informs all subsequent phases.

3. **Phase 1 before Phase 2** — Cleaning up `api_integration.py` first ensures that `ws_handlers.py` imports from clean, well-structured code. The CacheManager and split build functions are foundational for the handler decomposition.

4. **Phase 2 next** — The ws_chat() monolith is the biggest source of complexity. Extracting handlers makes the codebase much more navigable for subsequent work. With Phase 1 complete, all imports from `api_integration.py` are clean.

5. **Phases 3-4 after** — Security and config handlers are extracted from within ws_chat(), so they depend on Phase 2 being complete (or at least the handler decomposition pattern established).

6. **Phase 5 last** — Dead code removal is safest to do after everything else is verified working.

### Testing Strategy Per Phase

| Phase | Test Method | What to Verify |
|-------|------------|----------------|
| Phase 0 | Manual test: send message, verify state broadcast works identically | `_broadcast_state()` produces same output as original pattern; `_extract_system_message` returns `''` (not None) when no system msg found |
| Phase 0.5 | Static analysis: run import order check | No circular dependencies in new file structure |
| Phase 1 | Run multi-agent session with streaming | Token stats accurate; incremental serialization works; cache invalidation correct; paired eviction works for stream_versions/cached_instances |
| Phase 2 | Send all 25 message types via WebSocket client script | Each handler responds correctly; unknown type logged and ignored; error type handled; no behavior change |
| Phase 3 | Trigger tool approval → security check → verify verdict parsing | Security advisor runs, streams updates, approves/rejects correctly |
| Phase 4 | Change each config setting via UI | Each config key is applied only when changed (defense-in-depth) |
| Phase 5 | Delete files, restart server | Server starts normally; no import errors |

### Handler Method Test Strategy

#### Unit Tests — Per-Handler Methods

Each handler method in `WsMessageHandler` will be tested in isolation using mocked dependencies:

| Target | Mock Dependencies | Verification |
|--------|------------------|--------------|
| `handle_message()` | mock agent_pool (empty), session, broadcast | Creates new instance if none exists; starts generation thread; broadcasts state |
| `handle_continue()` | mock agent_pool with 1 instance + trailing assistant msg, session, broadcast | Pops last assistant message; starts generation thread; broadcasts state |
| `handle_stop()` | mock agent_pool with running agents, session, broadcast | Transitions all agents to IDLE; cleans up execution stacks; broadcasts done then state |
| `handle_pause()` | mock agent_pool, session, broadcast | Sets global pause flag; broadcasts state |
| `handle_resume_all()` | mock agent_pool, session, broadcast | Clears pause flag; starts generation for paused instances; broadcasts state |
| `handle_resume()` | mock agent_pool with log files, session, broadcast | Restores agent pools from logs; handles missing log gracefully; broadcasts state |
| `handle_terminate()` | mock agent_pool with active instance, session, broadcast | Dismisses instance; cascades termination to sub-agents; broadcasts done then state |
| `handle_retry()` | mock agent_pool with conversation history + snapshots, session, broadcast | Trims tail messages; rolls back snapshot; re-enqueues original message; broadcasts state |
| `handle_reset()` | mock agent_pool, session, broadcast | Clears conversation; resets session metadata; clears caches; broadcasts done then state |
| `handle_refresh_souls()` | mock agents list with template attrs, session, broadcast | Refreshes all agent templates; broadcasts state |
| `handle_restart_server()` | no mocks needed (os.execl) | Calls os.execl with correct args (test via patching) |
| `handle_update_config()` | mock agent_pool with settings, session, broadcast | Routes each config key to correct handler; applies only changed values; broadcasts state |
| `handle_ask_security()` | mock agent_pool with pending approvals, session, send_queue | Creates Security instance; runs engine in thread; parses verdict; auto-applies result |
| All trivial handlers (`select_agent`, `set_session_name`, `load_session`, `inject`, etc.) | Minimal mocks | Correct state mutation + broadcast call |

#### Integration Test — Full Message Sweep Script

A dedicated test script will exercise the complete WebSocket protocol:

```python
# test_message_sweep.py — send all 25+ message types and compare outputs
async def test_all_message_types():
    """Send every supported message type via WebSocket and verify broadcast payloads."""
    ws = await connect_ws("ws://localhost:8765/ws")
    
    baselines = load_baselines("test_data/broadcast_baselines.json")
    
    for msg_type, payload in test_messages:
        await ws.send_json(payload)
        responses = collect_broadcasts(ws, timeout=5.0)
        
        baseline_key = f"{msg_type}_broadcast"
        assert responses[-1]['type'] == baselines[baseline_key]['expected_type'], \
            f"{msg_type}: expected {baselines[baseline_key]['expected_type']}, got {responses[-1]['type']}"
        # Compare key fields (instance_name, generating, session_name, etc.)
        compare_state_fields(responses[-1], baselines[baseline_key])
```

**Scope:** Sends all 25+ message types in sequence and compares broadcast payloads against pre-recorded baseline outputs. This ensures the refactored handlers produce identical WebSocket output to the original monolithic ws_chat().

#### Minimum Integration Tests — High-Complexity Handlers

At minimum, these high-complexity handlers require full integration tests (not just unit tests with mocks):

| Handler | Test Scenario | What to Verify |
|---------|-------------|----------------|
| `handle_message` | Send message → multi-agent conversation with sub-agents | Full state chain is correct; generation threads start and complete; token stats accumulate |
| `handle_continue` | Continue after agent finishes first response | Assistant message appended correctly; no duplicate messages in stream |
| `handle_stop` | Stop mid-generation during active streaming | All agents transition to IDLE; execution stack cleared; done broadcast sent |
| `handle_resume` | Resume from log file after server restart | Agent pool restored from logs; conversation history intact; generation resumes correctly |
| `handle_retry` | Retry last message in multi-turn conversation | Tail trimmed correctly; snapshot rolled back; original message re-processed with fresh output |
| `handle_ask_security` | Tool approval → security check → verdict parsing → auto-apply | Security agent created and run; [YES]/[NO] parsed from output; approval applied to correct request_id |

#### Test Execution Order

```
1. Unit tests (mocked) — run after Phase 2 completion, before integration tests
2. Message sweep script — run after all phases complete, against live server
3. Integration tests (high-complexity handlers) — run in parallel with message sweep
4. Edge case matrix (E1-E12) — manual verification checklist
```

### Edge Case Test Matrix (NEW)

| # | Edge Case | Phase Affected | Verification Method | Expected Behavior |
|---|-----------|---------------|---------------------|-------------------|
| E1 | No system message on agent | 0D | `_extract_system_message(plain_agent)` | Returns `''` (empty string), not None |
| E2 | Agent with only llm.cfg system msg | 0D | `_extract_system_message(cfg_agent)` | Returns cfg value as str |
| E3 | Unknown WebSocket message type | 2A | Send `{"type": "unknown_type", ...}` | Logged at debug level, no error |
| E4 | Empty message type in dispatch | 2C | Send `{"type": ""}` or `{}` | Falls through to handle_unknown gracefully |
| E5 | Concurrent WebSocket disconnect during handler execution | 2C | Kill connection mid-handlers | Clean disconnect, finally block runs |
| E6 | Cache eviction with paired entries | 1A | Fill stream_versions + cached_instances beyond limit | Both caches evict the same instance key together |
| E7 | Multiple simultaneous security checks | 3 | Send overlapping ask_security messages | Semaphore limits to 1 active check, others queue |
| E8 | Config update during active generation | 4 | Change LLM model mid-stream | No crash; config applied atomically |
| E9 | Session load with no log file | 2 | Send `load_session` with invalid path | Error message returned to client |
| E10 | Build state for non-existent instance | 1B | Call build_state_from_pool(pool, "nonexistent") | Returns None (not empty dict) |
| E11 | Broadcast during __main__ startup | 0B | Verify _sender_loop dismissal broadcast works | State correctly built and sent |
| E12 | Thread creation failure in handler | 2E | Exhaust thread pool, send message | Error logged, session state preserved |

### What NOT to Change

- **WebSocket protocol** — message types and field names stay identical
- **REST endpoint URLs** — all paths remain the same
- **Frontend output format** — state dict keys and structure unchanged
- **Thread safety model** — session_lock, compression_lock usage preserved
- **Cache behavior** — cache sizes, eviction policies, invalidation triggers unchanged
- **Signal handling semantics** — graceful shutdown behavior identical

---

## Section 5: Risk Assessment

### High-Risk Areas

| Area | Risk | Mitigation |
|------|------|-----------|
| Thread safety in handlers | New handler methods might miss session_lock usage | Audit all handler methods for shared state access; copy lock patterns from original code |
| Cache manager consolidation | Single RLock might cause contention if caches are accessed concurrently | Test with concurrent multi-agent sessions; profile if needed |
| Security handler extraction | Complex lifecycle (create → run → parse → cleanup) | Keep the same thread-based execution model; test timeout scenarios |
| Build function splitting | Shared helpers must handle all edge cases from both original functions | Ensure None handling, error fallbacks, and empty-state behavior are preserved |

### Medium-Risk Areas

| Area | Risk | Mitigation |
|------|------|-----------|
| Config router | Missing a config key handler | Test each of the 12+ config keys individually |
| Signal handler extraction | Server instance reference might be None in some paths | Add defensive checks; keep original behavior as fallback |
| Import ordering | Circular dependency if ws_handlers imports from api_integration before it's cleaned up | Phase 0.5 audit + Phase 1-first execution order prevents this |

### Low-Risk Areas

- FIX comment removal (pure text change)
- Helper function extraction (_broadcast_state, _clear_caches, _extract_system_message)
- Dead code deletion (run_server.py)
- Import audit (documentation only)

### Rollback Strategy

Each phase is committed separately:
```
Phase 0 + 0.5: git commit -m "Phase 0+0.5: Quick wins — remove FIX comments, add helpers, import audit"
Phase 1:      git commit -m "Phase 1: Clean up api_integration.py — CacheManager, split build functions"
Phase 2:      git commit -m "Phase 2: Extract WsMessageHandler class from ws_chat()"
Phase 3:      git commit -m "Phase 3: Extract SecurityAdvisorHandler for ask_security"
Phase 4:      git commit -m "Phase 4: Extract ConfigUpdateRouter for update_config"
Phase 5:      git commit -m "Phase 5: Dead code removal — run_server.py, signal handler dedup"
```

If a regression is found, revert the specific phase commit. The phases are designed to be independent (each builds on working code).

### Things Requiring Careful Testing

1. **Concurrent WebSocket connections** — multiple clients connecting simultaneously while agents are running
2. **Security advisor timeout** — verify timeout detection still works after handler extraction
3. **Session load/restore** — loading from log files and resuming should work identically
4. **Multi-agent streaming** — concurrent sub-agents generating simultaneously with incremental serialization
5. **Config updates during active generation** — changing LLM config mid-stream should not cause issues

---

## Appendix A: Message Type Handler Complexity Matrix

| Priority | Message Types | Lines Each | Total | Extraction Difficulty |
|----------|--------------|-----------|-------|---------------------|
| P0 (Critical) | `message`, `continue`, `stop` | 83, 63, 69 | 215 | Medium — thread creation logic |
| P1 (High) | `resume`, `retry`, `ask_security` | 121, 73, 400 | 594 | High — complex state management |
| P2 (Medium) | `edit_message`, `delete_messages`, `terminate_*` | 68, 41, 46 | 155 | Medium — conversation manipulation |
| P3 (Low) | All others (14 types + error) | ~5-17 each | ~130 | Low — straightforward operations |

---

## Appendix B: Import Dependency Graph

```
api_server.py
├── api_integration.py  (build_state_from_pool, serialize_message, etc.)
│   ├── agent_instance.py
│   ├── execution_engine.py
│   └── loop_detection.py
├── ws_handlers.py     [NEW]
│   ├── security_handler.py  [NEW]
│   │   └── compression/helpers.py (extract_instance_output)
│   └── config_handlers.py   [NEW]
│       └── tools/mcp_manager.py
└── shared_init.py     (setup_signal_handler)

No circular dependencies introduced. All new files import from existing modules only.
Verified by Phase 0.5 import audit table.
```

---

## Appendix C: Handler Signature Standard (NEW)

All handler methods in `WsMessageHandler` follow this standard signature:

```python
async def handle_<type>(self, data: dict) -> None:
    """Handle '<type>' WebSocket message.
    
    Args:
        data: Parsed JSON payload from websocket.receive_text().
              Contains at minimum a 'type' field matching the handler name.
    
    Returns:
        None. Side effects include session state changes, agent pool mutations,
        and broadcast calls to update all connected clients.
    """
```

### Signature Conventions

1. **Async only** — All handlers are `async def` to allow awaiting broadcasts and async operations
2. **Single parameter** — Accepts the parsed message dict; no additional parameters needed (all shared state is on `self`)
3. **No return value** — Handlers communicate results via `_broadcast()` calls, not return values
4. **Error handling** — Each handler catches exceptions internally and logs them; dispatch() never raises
5. **Broadcast responsibility** — Each handler is responsible for broadcasting the final state after its operation completes

### Handler Method Naming Convention

| Message Type | Handler Method | Notes |
|-------------|---------------|-------|
| `message` | `handle_message()` | Primary entry point |
| `continue` | `handle_continue()` | Continue generation |
| `stop` | `handle_stop()` | Stop all agents |
| `pause` | `handle_pause()` | Pause global flag |
| `resume_all` | `handle_resume_all()` | Resume from pause |
| `resume` | `handle_resume()` | Full resume with log restore |
| `terminate_agent_instance` / `terminate_sub_agent` | `handle_terminate()` | Shared handler for both types |
| `retry` | `handle_retry()` | Retry last generation |
| `reset` | `handle_reset()` | Clear conversation |
| `refresh_souls` | `handle_refresh_souls()` | Refresh agent templates |
| `restart_server` | `handle_restart_server()` | Server restart via os.execl |
| `update_config` | `handle_update_config()` | 12+ config key updates |
| `update_endpoints` | `handle_update_endpoints()` | API endpoint update |
| `update_api_priorities` | `handle_update_api_priorities()` | Priority mapping update |
| `approve` | `handle_approve()` | Approve pending request |
| `reject` | `handle_reject()` | Reject with reason |
| `ask_security` | `handle_ask_security()` | Security advisor check |
| `set_auto_security` | `handle_set_auto_security()` | Toggle auto-security |
| `edit_message` | `handle_edit_message()` | Edit conversation messages |
| `delete_messages` | `handle_delete_messages()` | Prune messages |
| `select_agent` | `handle_select_agent()` | Change active agent |
| `set_session_name` | `handle_set_session_name()` | Rename session |
| `load_session` | `handle_load_session()` | Load from log file |
| `inject` | `handle_inject()` | Enqueue message |
| `error` | `handle_error()` | Client-side error echo (for completeness) |

---

## Appendix D: Broadcast Pattern Reference (NEW)

Complete inventory of all 34 broadcast occurrences in api_server.py:

### State broadcasts (25 occurrences)
```python
# Lines with {'type': 'state', **build_state()}:
699   # _sender_loop dismissal handler
1002  # After message send
1015  # After continue
1027  # After retry state update
1043  # After pause
1056  # After resume_all
1234  # After continue with generating=True
1318  # After stop (generating=False)
1334  # After resume_all (generating=True)
1454  # During resume (generating=True)
1457  # During resume (default)
1487  # Done after terminate (actually 'done')
1506  # After terminate state update
1549  # After retry state update
1611  # After retry with generating=True
1635  # Done after reset (actually 'done')
1647  # After refresh_souls
1744  # After update_config
1753  # After update_endpoints
1763  # After update_api_priorities
1772  # After approve
1782  # After reject
2260  # After edit_message
2302  # After delete_messages
2306  # After select_agent
2317  # After set_session_name
2350  # After load_session
```

### Done broadcasts (3 occurrences)
```python
875   # /api/reset endpoint
1306  # After stop completion
1635  # After reset completion
```

### Error broadcasts (2 occurrences)
```python
1207  # "No agent instance found to continue"
1651  # "Server is restarting..."
```

### Approvals broadcast (1 occurrence)
```python
719   # _approval_loop polling
```

**Total: 34 occurrences.** Of these, ~30 use the standard state/done pattern that `_broadcast_state()` can replace. Error and approvals broadcasts are excluded as they use different payload structures.

---

*End of Refactoring Plan (Revised)*