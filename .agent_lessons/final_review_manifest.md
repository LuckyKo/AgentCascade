# Tab Unification Project — Final Review Change Manifest (Phases 0–3)

**Branch:** `tab-unification`
**Date:** 2026-05-24
**Purpose:** Complete, precise change manifest for all phases of the tab unification project. Every file changed or created is listed with exact line numbers and code excerpts for reviewer navigation.

---

## Table of Contents

1. [Phase 0 — Feature Flags](#phase-0--feature-flags)
2. [Phase 1 — Unified Token Cache](#phase-1--unified-token-cache)
3. [Phase 2 — Dual-Read State Accessors](#phase-2--dual-read-state-accessors)
4. [Phase 3 — Shared Resolver & Lock Protection](#phase-3--shared-resolver--lock-protection)
5. [Frontend — Tab Bar UI (app.js)](#frontend--tab-bar-ui-appjs)
6. [Frontend — Tab Bar Styles (styles.css)](#frontend--tab-bar-styles-stylescss)

---

## Phase 0 — Feature Flags

### NEW FILE: `config/unified.py` (Lines 1–16, 16 lines total)

**What changed:** Created feature flag module gating all unified behavior via environment variables. Defaults to legacy mode (False).

```python
"""
Feature flags for tab unification project.
All default to False (legacy mode). Set environment variables to enable.
"""
import os

__all__ = ['USE_UNIFIED_ARCHITECTURE', 'USE_UNIFIED_STATE', 'USE_UNIFIED_LOOP']

# Master toggle - gates all unified behavior
USE_UNIFIED_ARCHITECTURE = os.environ.get('AC_USE_UNIFIED_ARCHITECTURE', '0') == '1'

# Gates state read/write path (Phase 2)
USE_UNIFIED_STATE = os.environ.get('AC_USE_UNIFIED_STATE', '0') == '1'

# Gates message loop unification (Phase 3)
USE_UNIFIED_LOOP = os.environ.get('AC_USE_UNIFIED_LOOP', '0') == '1'
```

### NEW FILE: `config/__init__.py` (Line 1, 1 line total)

**What changed:** Re-exports all three flags for convenience imports.

```python
from config.unified import USE_UNIFIED_ARCHITECTURE, USE_UNIFIED_STATE, USE_UNIFIED_LOOP
```

---

## Phase 1 — Unified Token Cache

### NEW FILE: `config/token_cache.py` (Lines 1–75, 75 lines total)

**What changed:** Created thread-safe, TTL-based token cache class replacing the split caching pattern (`_cached_hist_stats` for root + `_sa_stats_{name}` for sub-agents).

```python
"""
Unified token cache for all agent instances.
Replaces the split caching: _cached_hist_stats (root) + _sa_stats_{name} (sub-agents).
"""
import threading
import time


class AgentTokenCache:
    """Thread-safe, TTL-based cache mapping agent instance names to token count stats."""
    
    def __init__(self, ttl=300):
        self._cache = {}       # instance_name -> {'count': int, 'tokens': int, 'timestamp': float}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._cleanup_timer = None
        self._start_cleanup_timer()
    
    def _start_cleanup_timer(self):
        self._cleanup_timer = threading.Timer(300, self.cleanup_expired)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()
    
    def get(self, instance_name):
        """Get cached stats for an instance. Returns None if not found or expired."""
        with self._lock:
            entry = self._cache.get(instance_name)
            if entry is None:
                return None
            if time.time() - entry['timestamp'] > self._ttl:
                del self._cache[instance_name]
                return None
            return {'count': entry['count'], 'tokens': entry['tokens']}
    
    def set(self, instance_name, count, tokens):
        """Set cached stats for an instance."""
        with self._lock:
            self._cache[instance_name] = {
                'count': count,
                'tokens': tokens,
                'timestamp': time.time(),
            }
    
    def invalidate(self, instance_name):
        """Remove cache entry for a specific instance (e.g., after compression)."""
        with self._lock:
            self._cache.pop(instance_name, None)
    
    def clear_all(self):
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
    
    def cleanup_expired(self):
        """Remove all expired entries. Call periodically."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._cache.items() if now - v['timestamp'] > self._ttl]
            for k in expired:
                del self._cache[k]
    
    def size(self):
        """Number of cached instances."""
        with self._lock:
            return len(self._cache)
    
    def __repr__(self):
        """String representation showing cache size and TTL."""
        return f'AgentTokenCache(size={self.size()}, ttl={self._ttl})'
```

### MODIFIED: `api_server.py` — Token Cache Initialization (Lines 553–555)

**What changed:** Added import of `AgentTokenCache` and instantiation of the unified cache at module level.

```python
    # ── Unified token cache (coexists with old _cached_hist_stats during transition) ──
    from config.token_cache import AgentTokenCache
    unified_token_cache = AgentTokenCache(ttl=300)
```

### MODIFIED: `api_server.py` — Token Cache Write in build_state() (Lines 792–794)

**What changed:** Added write to the unified token cache when in unified mode, mirroring the legacy `_cached_hist_stats`.

```python
        # Write to unified token cache when in unified mode (mirrors the legacy _cached_hist_stats)
        if USE_UNIFIED_STATE:
            unified_token_cache.set('root', len(active_h), h_stats['tokens'])
```

---

## Phase 2 — Dual-Read State Accessors

### MODIFIED: `api_server.py` — Feature Flag Import (Lines 58–59)

**What changed:** Added import of `USE_UNIFIED_STATE` and `USE_UNIFIED_ARCHITECTURE` from the config module at module level.

```python
# Unified architecture feature flags (module-level to avoid repeated local imports)
from config.unified import USE_UNIFIED_STATE, USE_UNIFIED_ARCHITECTURE
```

### MODIFIED: `api_server.py` — New Function `get_session_history()` (Lines 465–503, 39 lines)

**What changed:** Created a dual-read wrapper for session history that supports both legacy (`session['history']`) and unified (`agent_pool.sub_agent_state`) state stores based on the feature flag.

```python
    def get_session_history(session, instance_name='root', use_unified=None):
        """Dual-read wrapper for session history — supports both legacy and unified state stores.
        
        Returns messages from either session['history'] (legacy) or agent_pool.sub_agent_state
        (unified), depending on the feature flag.
        
        Args:
            session: Flask session dict
            instance_name: Agent instance name ('root' for main chat, or agent name for sub-agents)
            use_unified: Override the global flag for this call. None = use global flag.
        
        Returns:
            list of message objects
        """
        # Priority check: explicit argument > global flag > default (False)
        effective_unified = use_unified if use_unified is not None else USE_UNIFIED_STATE
        
        if effective_unified:
            # Read from unified store
            if instance_name == 'root':
                # Root agent history lives in sub_agent_state['root']['messages']
                store = agent_pool.sub_agent_state.get('root', {}) if agent_pool else {}
                msgs = store.get('messages', [])
                # Edge case: unified store not populated yet, fall back to legacy for root only
                if not msgs and session.get('history'):
                    logger.debug(f"Unified store empty for root, falling back to legacy session['history']")
                    msgs = list(session['history'])
                return msgs
            else:
                # Sub-agent history from unified store
                state = agent_pool.sub_agent_state.get(instance_name, {}) if agent_pool else {}
                return state.get('messages', [])
        else:
            # Legacy read path
            if instance_name == 'root':
                return list(session.get('history', []))
            else:
                # Sub-agent history from legacy store (instance_conversations)
                return agent_pool.instance_conversations.get(instance_name, []) if agent_pool else []
```

### MODIFIED: `api_server.py` — New Function `get_agent_state()` (Lines 505–534, 30 lines)

**What changed:** Created a unified state accessor replacing the dual-track where root used `session['history']` and sub-agents used `agent_pool.get_sub_agent_state()`. In unified mode, both come from `agent_pool.sub_agent_state`.

```python
    def get_agent_state(instance_name):
        """Get state for any agent instance, including root.
        
        Replaces the dual-track where root used session['history'] and sub-agents
        used agent_pool.get_sub_agent_state(). In unified mode, both come from
        agent_pool.sub_agent_state.
        
        Args:
            instance_name: Agent instance name ('root' for main chat, or agent name)
        
        Returns:
            dict with 'messages', 'active', etc. or None if not found
        """
        if USE_UNIFIED_STATE:
            # Unified path: all agents including root in sub_agent_state
            if not agent_pool:
                return None
            state = agent_pool.sub_agent_state.get(instance_name)
            return state.copy() if state else None
        else:
            # Legacy path: root is special-cased
            if instance_name == 'root':
                msgs = list(session.get('history', []))
                return {
                    'messages': msgs,
                    'active': True,
                    'agent_name': 'Maine (OrchestratorAgent)',
                }
            else:
                return agent_pool.sub_agent_state.get(instance_name)
```

### MODIFIED: `api_server.py` — Modified `build_state()` — Unified Read (Lines 729–736)

**What changed:** Added unified state read path at the top of `build_state()`. When `USE_UNIFIED_STATE` is True, reads root messages from `get_agent_state('root')` instead of `session['history']`.

```python
    def build_state(responses=None, generating=None):
        """Build a full state snapshot for the frontend."""
        if USE_UNIFIED_STATE:
            root_state = get_agent_state('root')
            msgs = root_state['messages'] if root_state else []
        else:
            msgs = list(session.get('history', []))
```

### MODIFIED: `api_server.py` — Modified `build_state()` — Raw History Source (Lines 745–748)

**What changed:** In unified mode, uses the already-unified messages as raw_history instead of `session['history']`.

```python
        # Calculate tokens for the active 'working set' (after compression)
        if USE_UNIFIED_STATE and root_state:
            raw_history = msgs  # already from unified store
        else:
            raw_history = session.get('history', [])
```

### MODIFIED: `api_server.py` — Modified `build_state()` — Summary Scan Path (Lines 805–806)

**What changed:** In unified mode, scans the unified messages for compression markers instead of `session['history']`.

```python
        # Sync session summary from history if it was just compressed
        current_summary = session.get('summary', '')
        # In unified mode, scan the unified messages; otherwise scan legacy session['history']
        hist_to_scan = msgs if USE_UNIFIED_STATE else session['history']
```

---

## Phase 3 — Shared Resolver & Lock Protection

### NEW FILE: `agent_cascade/tool_utils.py` (Lines 1–89, 89 lines total)

**What changed:** Extracted the inline `__USE_PREV_ARG__` placeholder resolution logic from `agent_orchestrator.py` into a shared, reusable utility function. Supports both streaming and non-streaming tool paths. Thread-safe when lock is provided; parameterized to avoid deadlock when caller already holds the lock.

```python
"""Shared utility functions for tool execution.

Import via: from agent_cascade.tool_utils import resolve_prev_arg_placeholders
"""

import copy
import threading
from typing import Any, Optional, Tuple


def resolve_prev_arg_placeholders(
    tool_args: Any,
    instance_scope: str,
    tool_name: str,
    agent_pool: Any,
    lock: Optional[threading.Lock] = None,
) -> Tuple[Any, Optional[str]]:
    """Resolves __USE_PREV_ARG__ placeholders from the last tool call.

    This is a shared utility that replaces the inline resolution in agent_orchestrator.py.
    Works for both streaming and non-streaming tool paths.

    Thread-safety note: When *lock* is provided, cache reads are protected by it.
    Callers that already hold *lock* must pass ``None`` to avoid deadlock.
    When *lock* is ``None``, this function is NOT thread-safe for the read path.

    Args:
        tool_args: Tool arguments (typically a dict after JSON parsing).
                   Non-dict inputs pass through unchanged with no error.
        instance_scope: The instance name scope (e.g., session_name).
        tool_name: Name of the tool being called.
        agent_pool: Reference to the AgentPool for accessing last_tool_args cache.
        lock: Optional threading.Lock to guard cache reads. Pass ``None`` if the
              caller already holds the relevant lock, or when thread-safety is not
              needed (e.g., tests).

    Returns:
        tuple: (resolved_args, error_message)
            - resolved_args: dict with placeholders replaced, or original args if no
              placeholders were found. On error, returns the UNMODIFIED original
              tool_args; callers MUST NOT use it for execution.
            - error_message: None on success, error string if resolution failed.
    """
    if not isinstance(tool_args, dict):
        return tool_args, None

    # Scan for placeholders
    placeholders_found = [key for key, val in tool_args.items() if val == "__USE_PREV_ARG__"]

    if not placeholders_found:
        return tool_args, None

    resolved_args = copy.deepcopy(tool_args)

    try:
        if lock is not None:
            with lock:
                scope_cache = agent_pool.last_tool_args.get(instance_scope, {})
                prev_args = scope_cache.get(tool_name)
                global_args = scope_cache.get("__GLOBAL__", {})
        else:
            scope_cache = agent_pool.last_tool_args.get(instance_scope, {})
            prev_args = scope_cache.get(tool_name)
            global_args = scope_cache.get("__GLOBAL__", {})
    except AttributeError:
        # Defensive: agent_pool may not have last_tool_args in unusual setups.
        return tool_args, None

    if not prev_args and not global_args:
        return tool_args, (
            f"Error: Cannot use __USE_PREV_ARG__ for '{tool_name}' because no previous "
            f"call to this tool was recorded for instance '{instance_scope}'."
        )

    for arg_key in placeholders_found:
        if prev_args and arg_key in prev_args:
            # Deepcopy resolved values to prevent cache mutation via shared refs.
            resolved_args[arg_key] = copy.deepcopy(prev_args[arg_key])
        elif arg_key in global_args:
            resolved_args[arg_key] = copy.deepcopy(global_args[arg_key])
        else:
            return tool_args, (
                f"Error: Cannot use __USE_PREV_ARG__ for argument '{arg_key}' because "
                f"it was not found in previous calls (neither specific to '{tool_name}' "
                f"nor globally)."
            )

    return resolved_args, None
```

### MODIFIED: `agent_orchestrator.py` — Feature Flag Import (Line 56)

**What changed:** Added import of `USE_UNIFIED_LOOP` from the config module.

```python
from config.unified import USE_UNIFIED_LOOP
```

### MODIFIED: `agent_orchestrator.py` — Streaming Tool Path: Shared Resolver (Lines 1387–1407)

**What changed:** In the streaming tool path (sub-agent calls), replaced inline `__USE_PREV_ARG__` resolution with a call to the shared resolver `resolve_prev_arg_placeholders` when `USE_UNIFIED_LOOP=True`. Added comment noting this prevents double-resolution if both paths were active simultaneously.

```python
                        # NOTE: This branch replaces the inline __USE_PREV_ARG__ resolution
                        # that exists in the else (non-streaming) path below. The shared
                        # resolver is called here when USE_UNIFIED_LOOP=True, preventing
                        # double-resolution if both paths were active simultaneously.
                        parsed_args = tool_args
                        if isinstance(tool_args, str):
                            try:
                                parsed_args = json_loads(tool_args)
                            except Exception:
                                parsed_args = {}

                        # ── Resolve __USE_PREV_ARG__ placeholders (Phase 3 unification) ──
                        if USE_UNIFIED_LOOP and isinstance(parsed_args, dict):
                            from agent_cascade.tool_utils import resolve_prev_arg_placeholders

                            instance_scope = self.session_name if hasattr(self, 'session_name') else 'root'
                            parsed_args, prev_arg_error = resolve_prev_arg_placeholders(
                                parsed_args, instance_scope, tool_name, self.agent_pool
                            )
                        else:
                            prev_arg_error = None
```

### MODIFIED: `agent_orchestrator.py` — Non-Streaming Tool Path: Shared Resolver (Lines 1455–1479)

**What changed:** In the non-streaming tool path, replaced inline `__USE_PREV_ARG__` resolution with a call to the shared resolver. Added `skip_execution` guard so that when resolution fails, tool execution is skipped and the error is returned directly.

```python
                        # --- Handle __USE_PREV_ARG__ Placeholder Replacement (shared resolver) ---
                        if isinstance(tool_args, str):
                            tool_args = tool_args.strip()
                            if tool_args:
                                try:
                                    tool_args = json_loads(tool_args)
                                except Exception:
                                    pass  # Let _call_tool handle standard verification
                            else:
                                tool_args = {}  # Guard against empty string arguments

                        if isinstance(tool_args, dict):
                            instance_scope = self.session_name if hasattr(self, 'session_name') else 'root'
                            from agent_cascade.tool_utils import resolve_prev_arg_placeholders

                            tool_args, prev_arg_error = resolve_prev_arg_placeholders(
                                tool_args, instance_scope, tool_name, self.agent_pool
                            )
                            if prev_arg_error:
                                # Resolution failed — skip tool execution, return error
                                tool_result = prev_arg_error
                                skip_execution = True
                            else:
                                skip_execution = False

                            if not skip_execution:
```

### MODIFIED: `agent_orchestrator.py` — Lock Protection: State Write at Initialization (Lines 1962–1964)

**What changed:** Wrapped the write to `sub_agent_state[instance_name]` with `_state_lock` protection when initializing streaming state for a sub-agent call.

```python
        # Overwrite any existing state for this instance (protected by _state_lock)
        with self.agent_pool._state_lock:
            self.agent_pool.sub_agent_state[instance_name] = state
```

### MODIFIED: `agent_orchestrator.py` — Lock Protection: State Write After Loop Rollback (Lines 2175–2176)

**What changed:** Wrapped the write to `sub_agent_state[instance_name]` with `_state_lock` after injecting a corrective hint following loop detection and rollback.

```python
                    with self.agent_pool._state_lock:
                        self.agent_pool.sub_agent_state[instance_name] = state
```

### MODIFIED: `agent_orchestrator.py` — Lock Protection: State Write + Active Stack Cleanup in Finally Block (Lines 2222–2233)

**What changed:** Wrapped both the `sub_agent_state` write and `active_stack` removal in the `finally` block under `_state_lock`, ensuring atomicity of both operations. Added comment referencing `agent_pool.py:114` for lock documentation.

```python
            # Protect both sub_agent_state and active_stack writes under _state_lock
            # (_state_lock protects both of these shared data structures per agent_pool.py:114)
            with self.agent_pool._state_lock:
                self.agent_pool.sub_agent_state[instance_name] = state
                
                # Remove from active stack (pop the most recent occurrence)
                removed = False
                for i in range(len(self.agent_pool.active_stack) - 1, -1, -1):
                    if self.agent_pool.active_stack[i] == instance_name:
                        self.agent_pool.active_stack.pop(i)
                        removed = True
                        break
```

### MODIFIED: `agent_pool.py` — Added `_state_lock` (Line 114)

**What changed:** Added a new threading lock `_state_lock` that protects `sub_agent_state` and `active_stack` for thread-safe parallel agent execution.

```python
        # Thread safety locks for parallel agent execution
        self._state_lock = threading.Lock()           # Protects sub_agent_state, active_stack
        self._conversation_lock = threading.Lock()    # Protects instance_conversations writes
```

---

## Frontend — Tab Bar UI (app.js)

### MODIFIED: `web_ui/app.js` — New DOM References (Lines 92–94)

**What changed:** Added new DOM element references for the main tab bar system.

```javascript
const mainTabBar = $('#mainTabBar');
const mainTabChat = $('#mainTabChat');
const mainTabPanels = document.querySelector('.main-tab-panels');
```

### MODIFIED: `web_ui/app.js` — Auto-Switch to Sub-Agent Tab on Activity (Lines 992–1005)

**What changed:** When the active agent stack changes, auto-switch the tab to show the new sub-agent. When the stack empties, auto-switch back to chat only if the user isn't already viewing a sub-agent tab (preserves their view).

```javascript
        if (stackChanged) {
          if (state.activeStack.length > 0) {
            const topAgent = state.activeStack[state.activeStack.length - 1];
            // Only auto-switch if the sub-agent panel has actually been created
            if (state.subAgents && state.subAgents[topAgent] && state.activeSubTab !== 'sub-' + topAgent) {
              switchMainTab('sub-' + topAgent);
            }
          } else {
            // Only auto-switch back to chat if the user isn't already looking at a sub-agent tab.
            // This allows them to keep reading the sub-agent output even after it finishes.
            if (!state.activeSubTab || !state.activeSubTab.startsWith('sub-')) {
              switchMainTab('chat');
            }
          }
        }
```

### MODIFIED: `web_ui/app.js` — Chat Tab Icon Update in updateMainActivityBar (Lines 1304–1308)

**What changed:** Added logic to ensure the Chat tab label always shows the correct icon (💬 for idle, pulse for generating).

```javascript
  // Only update tab label if needed
  const newTabLabel = `<span class="main-tab-icon">💬</span> Chat`;
  if (chatTab && chatTab.innerHTML !== newTabLabel) {
    chatTab.innerHTML = newTabLabel;
  }
```

### NEW: `web_ui/app.js` — Agent Config Helpers (Lines 1372–1380, 9 lines)

**What changed:** Added `getRootAgentConfig()` and `getSubAgentConfig(name)` helper functions for the unified rendering path.

```javascript
/** Get config object for root-agent rendering */
function getRootAgentConfig() {
    return { isRoot: true };
}

/** Get config object for sub-agent rendering (name = instance name) */
function getSubAgentConfig(name) {
    return { isRoot: false, instanceName: name };
}
```

### NEW: `web_ui/app.js` — Unified Rendering Function `renderAgentConversation()` (Lines 1382–1421, 40 lines)

**What changed:** Created a unified rendering entry point that handles both root and sub-agent conversations through a single code path using `createMessageEl` with config. Supports nesting depth indentation and index mapping for incremental rendering.

```javascript
/**
 * Render a complete agent conversation as a DOM document fragment.
 * This is the unified rendering entry point — uses createMessageEl with config
 * to handle both root and sub-agent conversations through a single code path.
 * 
 * @param {string} instanceName - "root" for main chat, or agent name (e.g., "coder")
 * @param {Array}  messages     - array of message objects
 * @param {number} depth        - nesting level (0=root, 1=direct sub-agent, etc.)
 * @param {Array}  [indexMap]   - optional mapping from filtered-index → original-index
 *                                (needed when messages have been pre-filtered, e.g., system msgs removed)
 * @returns {DocumentFragment}  fragment containing all rendered message elements
 */
function renderAgentConversation(instanceName, messages, depth, indexMap) {
    if (!messages || messages.length === 0) return document.createDocumentFragment();

    const isRoot = (instanceName === 'root');
    const config = isRoot ? getRootAgentConfig() : getSubAgentConfig(instanceName);

    const fragment = document.createDocumentFragment();

    // Apply indentation for nested agents (16px per nesting level)
    const indentPx = depth * 16;

    for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];
        // Use original index from indexMap if provided, otherwise use the loop index
        const origIndex = indexMap ? indexMap[i] : i;
        const el = createMessageEl(msg, origIndex, config);

        if (!isRoot) {
            // Visual indicators for nested sub-agent conversations
            el.style.marginLeft = indentPx + 'px';
            el.style.borderLeft = isRoot ? '' : '2px solid var(--border-color, #333)';
        }

        fragment.appendChild(el);
    }

    return fragment;
}
```

### MODIFIED: `web_ui/app.js` — Stale Tab Cleanup in renderSubAgents (Lines 2262–2270)

**What changed:** Added logic to remove stale sub-agent tabs and panels for agents that no longer exist in the state.

```javascript
  // Remove stale sub-agent tabs and panels for agents that no longer exist
  mainTabBar.querySelectorAll('.main-tab[data-tab^="sub-"]').forEach(tab => {
    const agentName = tab.dataset.tab.substring(4);
    if (!names.includes(agentName)) {
      tab.remove();
      const panel = document.getElementById('panelSub-' + agentName);
      if (panel) panel.remove();
    }
  });
```

### MODIFIED: `web_ui/app.js` — Dynamic Tab Button Creation (Lines 2281–2308)

**What changed:** Dynamically create sub-agent tab buttons with click handlers, icons, labels, and close/terminate buttons. Appended to the main tab bar.

```javascript
    // Create tab button if it doesn't exist
    let tabBtn = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
    if (!tabBtn) {
      tabBtn = document.createElement('button');
      tabBtn.className = 'main-tab';
      tabBtn.dataset.tab = tabId;
      tabBtn.onclick = () => switchMainTab(tabId);

      const iconSpan = document.createElement('span');
      iconSpan.className = 'tab-icon-container';
      tabBtn.appendChild(iconSpan);

      const labelSpan = document.createElement('span');
      labelSpan.className = 'tab-label';
      tabBtn.appendChild(labelSpan);

      const closeBtn = document.createElement('span');
      closeBtn.className = 'close-tab';
      closeBtn.title = 'Terminate Agent';
      closeBtn.textContent = '\u00d7';
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        send({ type: 'terminate_sub_agent', instance_name: name });
        switchMainTab('chat');
      };
      tabBtn.appendChild(closeBtn);

      mainTabBar.appendChild(tabBtn);
    }
```

### MODIFIED: `web_ui/app.js` — Tab Icon Update (Lines 2312–2314)

**What changed:** Update the tab icon to show a pulse animation when active, or a robot emoji when idle.

```javascript
    const iconSpan = tabBtn.querySelector('.tab-icon-container');
    if (iconSpan) {
      iconSpan.innerHTML = isActive ? '<span class="sub-tab-pulse"></span>' : '<span class="main-tab-icon">🤖</span>';
    }
```

### MODIFIED: `web_ui/app.js` — Dynamic Panel Creation (Lines 2336–2350)

**What changed:** Create sub-agent panels with context bars and append them to the main tab panels container.

```javascript
    let panel = document.getElementById('panelSub-' + name);
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'main-tab-panel sub-agent-panel';
      panel.id = 'panelSub-' + name;

      const contextBar = document.createElement('div');
      contextBar.className = 'context-bar';
      contextBar.title = 'Context Usage';
      const contextFill = document.createElement('div');
      contextFill.className = 'context-bar-fill';
      contextFill.id = 'subContextFill-' + name;
      contextBar.appendChild(contextFill);
      panel.appendChild(contextBar);

      mainTabPanels.appendChild(panel);
    }
```

### MODIFIED: `web_ui/app.js` — Unified Rendering in renderSubAgentPanel (Lines 2486–2507)

**What changed:** Replaced the old sub-agent rendering path with calls to `renderAgentConversation()` for both full re-render and incremental append. Added TODO comment for future streaming update unification.

```javascript
    // Use unified rendering path for sub-agent full re-render
    scrollContainer.appendChild(renderAgentConversation(name, msgs, 1));
    const fillEl = document.getElementById('subContextFill-' + name);
    if (fillEl) updateContextBar(fillEl, msgs, agentData.total_tokens, agentData.max_tokens);
  } else {
    // Append new messages using unified rendering with indexMap to preserve indices
    const newMsgs = [];
    const newIndexMap = [];
    for (let i = lastCount; i < currentCount; i++) {
      newMsgs.push(msgs[i]);
      newIndexMap.push(i);
    }
    scrollContainer.appendChild(renderAgentConversation(name, newMsgs, 1, newIndexMap));
    const fillEl = document.getElementById('subContextFill-' + name);
    if (fillEl) updateContextBar(fillEl, msgs, agentData.total_tokens, agentData.max_tokens);
    // TODO(Phase1-StepL): Replace updateSubBubbleContent with unified streaming update.
```

### NEW: `web_ui/app.js` — Tab Switching Function `switchMainTab()` (Lines 2666–2701, 36 lines)

**What changed:** Created the tab switching function that updates tab buttons and panel visibility, triggers re-renders on tab switch, and resets `lastRenderedCount` to prevent duplicate messages.

```javascript
function switchMainTab(tabId) {
  // Update tab buttons
  mainTabBar.querySelectorAll('.main-tab').forEach(t => t.classList.remove('active'));
  const activeTab = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
  if (activeTab) activeTab.classList.add('active');

  // Update panels
  mainTabPanels.querySelectorAll('.main-tab-panel').forEach(p => p.classList.remove('active'));
  if (tabId === 'chat') {
    const chatPanel = document.getElementById('panelChat');
    chatPanel.classList.add('active');
    const scroll = chatPanel.querySelector('.messages-scroll');
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
  } else {
    const name = tabId.substring(4); // strip 'sub-'
    const panel = document.getElementById('panelSub-' + name);
    if (panel) {
      panel.classList.add('active');
      const scroll = panel.querySelector('.messages-scroll');
      if (scroll) scroll.scrollTop = scroll.scrollHeight;
    }
  }
  
  state.activeSubTab = tabId;
  
  // Trigger immediate render of the newly visible content
  if (tabId === 'chat') {
    // Reset lastRenderedCount to force a full re-render sync on visibility return.
    // Without this, switching away during streaming and back causes duplicate messages
    // because lastRenderedCount was stale from before the user switched tabs.
    lastRenderedCount = Infinity;
    renderMessages();
  } else {
    renderSubAgents();
  }
}
```

### MODIFIED: `web_ui/app.js` — Static Chat Tab Click Handler (Lines 2703–2706)

**What changed:** Wired up the static Chat tab button to call `switchMainTab('chat')`.

```javascript
// Wire up the static Chat tab
if (mainTabChat) {
  mainTabChat.addEventListener('click', () => switchMainTab('chat'));
}
```

### MODIFIED: `web_ui/app.js` — Generate Mode Tab Icon Update in updateControls (Lines 2816 & 2822)

**What changed:** When generating, replace the Chat tab icon with a pulse animation. When idle, restore the 💬 emoji.

```javascript
    if (mainTabChat) mainTabChat.innerHTML = '<span class="sub-tab-pulse"></span> Chat';
```
and
```javascript
    if (mainTabChat) mainTabChat.innerHTML = '<span class="main-tab-icon">💬</span> Chat';
```

---

## Frontend — Tab Bar Styles (styles.css)

### NEW: `web_ui/styles.css` — Main Tab Bar Section (Lines 424–562, ~139 lines)

**What changed:** Added the complete CSS for the main tab bar system including tab buttons, icons, animations, panels, and responsive behavior.

Key rules added:
- `.main-tab-bar` (Line 424): Flex container with scroll overflow
- `.main-tab-bar::-webkit-scrollbar` (Line 438): Hidden scrollbar
- `.main-tab` (Line 442): Tab button base styles with active/inactive states
- `.main-tab .activity-dot` (Line 460): Activity indicator dot on tabs
- `.main-tab.agent-active .activity-dot` (Line 472): Pulsing activity dot animation
- `.main-tab:hover` (Line 477): Hover state
- `.main-tab.active` (Line 482): Active tab with accent border-bottom
- `.main-tab.has-activity` (Line 487): Accent-colored text for active agents
- `.main-tab-icon` (Line 491): Icon sizing
- `.close-tab` (Line 496): × button on sub-agent tabs with hover danger color
- `.sub-tab-pulse` (Line 521): Pulse animation for generating state
- `.main-tab-panels` (Line 547): Container for tab panels
- `.main-tab-panel` (Line 556): Panel base styles
- `.main-tab-panel.active` (Line 564): Active panel visibility
- `.sub-agent-panel` (Line 569): Sub-agent panel specific styles
- `.sub-agent-activity-bar` / `.main-activity-bar` (Lines 580–581): Activity bar alignment

### MODIFIED: `web_ui/styles.css` — Responsive Tab Bar (Lines 1823–1825)

**What changed:** Added responsive rule for the main tab bar in the mobile breakpoint.

```css
  .main-tab-bar {
    padding: 0 6px;
  }
```

---

## Summary of Change Counts

| File | Lines Changed/Added | Type |
|------|-------------------|------|
| `config/unified.py` | 16 | NEW |
| `config/__init__.py` | 1 | NEW |
| `config/token_cache.py` | 75 | NEW |
| `agent_cascade/tool_utils.py` | 89 | NEW |
| `api_server.py` | ~200 (Lines 58-59, 465-534, 553-555, 729-748, 792-794, 805-806) | MODIFIED |
| `agent_orchestrator.py` | ~60 (Lines 56, 1387-1407, 1455-1479, 1962-1964, 2175-2176, 2222-2233) | MODIFIED |
| `agent_pool.py` | 1 (Line 114) | MODIFIED |
| `web_ui/app.js` | ~200 (Lines 92-94, 992-1005, 1304-1308, 1372-1421, 2262-2350, 2486-2507, 2666-2706, 2816, 2822) | MODIFIED |
| `web_ui/styles.css` | ~140 (Lines 424-562, 1823-1825) | MODIFIED |

**Total: 4 new files, 7 modified files, approximately 782 lines of changes across all phases.**