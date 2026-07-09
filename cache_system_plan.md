# Cache Pool System — Implementation Plan (Revised)

> **Goal**: Upgrade the `__USE_PREV_ARG__` system to a full tool argument + output caching pool.
> Cached entries are referenced via `{USE_CACHED_ENTRY_N}` syntax in tool arguments.
> Backward compatible with existing `__USE_PREV_ARG__`. Toggleable via settings.

---

## 1. Data Structure Design — Rolling Cache Pool

### 1.1 Core Concept

Augment (not replace) the per-instance dict-of-dicts cache (`last_tool_args`) with a rolling buffer that stores **both** tool arguments AND certain tool outputs. Each entry has:
- A sequential index (N) for `{USE_CACHED_ENTRY_N}` references — indices grow monotonically and never wrap, even after eviction
- A category tag: `"arg"` or `"output"`
- The cached value (full for resolution, truncated preview for display)
- Metadata: instance name, source tool, character count

### 1.2 Data Structure Definition

**New classes in `agent_instance.py` (~line 60, before the `AgentInstance` dataclass):**

> **Why here?** `agent_pool.py` already imports from `agent_instance.py` (line 25: `from .agent_instance import AgentInstance, PoolSettings, AgentState`). Placing the classes in `agent_pool.py` would create a circular import. Putting them in `agent_instance.py` avoids any cycle since that module only imports from `llm.schema`, `settings`, and stdlib.

```python
# ── Imports to add at top of agent_instance.py (after line 14) ──
import json                           # NEW: for serializing non-string values in cache preview
from collections import deque         # NEW: rolling buffer for cache pool

# ── New classes inserted ~line 58, before AgentInstance dataclass ──

@dataclass(slots=True)
class CacheEntry:
    """Single entry in the argument/output cache pool."""
    index: int                    # Sequential N for {USE_CACHED_ENTRY_N} (monotonic, never wraps)
    category: str                 # "arg" or "output"
    instance_name: str            # Which agent instance produced this
    source_tool: str              # Tool name that generated this entry
    value: Any                    # Full cached value (for resolution via deep copy)
    preview: str                  # Truncated display string (< 200 chars)
    char_count: int               # Length of full string representation

class ArgumentCachePool:
    """Thread-safe rolling cache pool for tool arguments and outputs.
    
    Per-instance scope with a fixed-size deque that wraps around,
    overwriting oldest entries when the limit is reached.
    
    Indices grow monotonically (never reset). An evicted entry's index
    becomes stale — lookups return None, which signals callers to leave
    placeholders as-is.
    """
    __slots__ = ('_entries', '_lock', '_next_index', 'max_size', 'enabled')
    
    def __init__(self, max_size: int = 50):
        self._entries: deque[CacheEntry] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._next_index = 1          # Monotonically increasing (never wraps/reset)
        self.max_size = max_size
        self.enabled = True           # Toggle on/off
    
    def add(self, category: str, instance_name: str, source_tool: str, 
            value: Any) -> int:
        """Add entry and return its index N. Returns -1 when disabled."""
        if not self.enabled:
            return -1  # Silently skip when cache pool is toggled off
        
        # Serialize to string for preview/length — with error handling
        try:
            val_str = json.dumps(value) if not isinstance(value, str) else value
        except (TypeError, ValueError):
            # Fallback for unserializable objects (e.g., custom types, cycles)
            val_str = str(value)
        
        preview = (val_str[:197] + '...') if len(val_str) > 200 else val_str
        
        with self._lock:
            entry = CacheEntry(
                index=self._next_index,
                category=category,
                instance_name=instance_name,
                source_tool=source_tool,
                value=value,
                preview=preview,
                char_count=len(val_str),
            )
            self._entries.append(entry)  # deque maxlen handles eviction automatically
            idx = self._next_index
            self._next_index += 1
            return idx
    
    def get(self, index: int) -> Optional['CacheEntry']:
        """Look up entry by its N index. Returns None if evicted or not found."""
        with self._lock:
            for entry in reversed(self._entries):
                if entry.index == index:
                    return entry
            return None  # Entry was evicted (too old) — caller should leave placeholder as-is
    
    def get_state_summary(self, max_display: int = 10) -> str:
        """Return truncated state string for system_info display."""
        with self._lock:
            entries = list(self._entries)
        
        if not entries:
            return "  Cache Pool: empty\n"
        
        lines = [f"  Cache Pool: {len(entries)}/{self.max_size} entries (enabled={self.enabled})\n"]
        # Show most recent entries first, up to max_display
        display_entries = reversed(entries[:max_display])
        for e in display_entries:
            marker = "ARG" if e.category == "arg" else "OUT"
            lines.append(f"    [N={e.index:>3}] [{marker}] {e.instance_name}/{e.source_tool} "
                        f"({e.char_count} chars): {e.preview}")
        
        if len(entries) > max_display:
            older = entries[max_display:]
            indices = ", ".join(str(e.index) for e in older[:5])
            lines.append(f"    ... and {len(older)} older entries (oldest: N={indices}...)")
        
        return "\n".join(lines)
```

### 1.3 Pool Placement & Index Behavior

- **Per-instance scope**: Each `AgentInstance` gets its own `ArgumentCachePool`. Prevents cross-contamination between parallel agents.
- **Initialization**: Created in `AgentInstance` dataclass as a default field (see §5.4).
- **Index behavior**: `_next_index` grows monotonically from 1 and never resets, even after entries are evicted by the deque's maxlen. This means:
  - An old index N may no longer resolve if its entry was evicted → `get(N)` returns `None`
  - The agent sees stale placeholders left as-is (graceful degradation)
  - No risk of index collision across sessions

---

## 2. What Gets Cached — Threshold Logic

### 2.1 Caching Rules (Precise)

| What | When | Threshold | Category |
|------|------|-----------|----------|
| **All tool arguments** | After resolution, before execution | **No threshold** — every resolved arg dict is cached | `"arg"` |
| **Individual string values** > 1000 chars within the args dict | Same time as above | > `cache_threshold_chars` (default 1000) | `"arg"` |
| **Tool outputs** | After execution, before truncation | > `cache_threshold_chars` (default 1000) | `"output"` |

### 2.2 Granular Caching for Large Argument Values

When caching tool arguments, the entire resolved args dict is cached as one entry. Additionally, each string value exceeding the threshold is cached individually so agents can reference large values directly:

```python
# Pseudocode for _cache_tool_args extension:
inst = self.pool.get_instance(instance_name)
if inst and hasattr(inst, 'cache_pool') and inst.cache_pool.enabled:
    # 1. Cache entire args dict as one entry
    idx = inst.cache_pool.add("arg", instance_name, tool_name, tool_args)
    
    # 2. Also cache individual string values > threshold separately
    if isinstance(tool_args, dict):
        for key, val in tool_args.items():
            if isinstance(val, str) and len(val) > threshold:
                inst.cache_pool.add("arg", instance_name, 
                                   f"{tool_name}.{key}", val)
```

### 2.3 Output Caching — Before Truncation

Output caching happens **before** truncation so the full content is preserved in the cache. This means downstream agents can get complete data even if the immediate result was truncated for context window management.

**Exact insertion point**: In `execution_engine.py`, line 2415-2421, the tool result string is available at its full length before `truncate_tool_result()` is called. The output caching call goes between lines 2414 and 2415:

```python
# execution_engine.py ~line 2414 (inside the tool execution loop):
                # Non-string tool results bypass truncation and always report truncated=False.
                _was_truncated = False
                if isinstance(tool_result, str):
                    # NEW: Cache full output BEFORE truncation (if exceeds threshold)
                    self._cache_tool_output(
                        inst_name, tool_name, tool_result, 
                        threshold=self.pool.settings.cache_threshold_chars
                    )
                    
                    _pre_trunc_len = len(tool_result)
                    ...
```

### 2.4 Threshold Configuration

Default: **1000 characters** (as specified in requirements). Configurable via settings.

---

## 3. USE_CACHED_ENTRY_N Resolution System

### 3.1 Syntax

- **Pattern**: `{USE_CACHED_ENTRY_N}` where N is a positive integer (e.g., `{USE_CACHED_ENTRY_42}`)
- **Regex**: `r'\{USE_CACHED_ENTRY_(\d+)\}'`
- **Case-sensitive** to avoid conflicts with existing patterns

### 3.2 Resolution Flow

```
Tool Argument String: "Read the file at {USE_CACHED_ENTRY_5}"
         │
         ▼
┌─────────────────────────────┐
│ 1. Scan for {USE_CACHED     │
│    ENTRY_N} patterns        │
│    (done in parallel with   │
│     __USE_PREV_ARG__ scan)  │
└─────────┬───────────────────┘
          │ found patterns
          ▼
┌─────────────────────────────┐
│ 2. Look up each N in the   │
│    instance's cache pool    │
│    (thread-safe via lock)   │
└─────────┬───────────────────┘
          │ entry found / not found
          ▼
┌─────────────────────────────┐
│ 3. Replace placeholder with │
│    cached value (deep copy) │
│    If evicted: leave as-is, │
│    log debug message        │
└─────────────────────────────┘
```

### 3.3 Resolution Implementation — Precise Line Numbers

**Modified `_resolve_placeholders()` in `execution_engine.py` (lines 3166-3223):**

The method currently has two early-return paths:
- **Line 3204**: Returns `parsed` directly when no `__USE_PREV_ARG__` placeholders are found
- This needs to be extended to also check for `{USE_CACHED_ENTRY_N}` patterns before returning

**Exact changes at lines 3166-3223:**

```python
def _resolve_placeholders(self, tool_args: Any, instance_name: str,
                          tool_name: str) -> Optional[dict]:
    # ── Step 1: ensure we have a dict to work with (lines 3185-3199, UNCHANGED) ──
    if isinstance(tool_args, dict):
        parsed = tool_args
    elif isinstance(tool_args, str):
        try:
            parsed = json.loads(tool_args)
        except json.JSONDecodeError:
            logger.debug("JSON parse failure for %s/%s", instance_name, tool_name)
            return None
        if not isinstance(parsed, dict):
            logger.debug("parsed to non-dict for %s/%s: %s", instance_name, tool_name, type(parsed).__name__)
            return None
    else:
        logger.debug("unexpected type for %s/%s: %s", instance_name, tool_name, type(tool_args).__name__)
        return None

    # ── Step 2: scan for BOTH placeholder types ───────────────────────────────
    placeholders_found = [k for k, v in parsed.items()
                          if isinstance(v, str) and v.strip() == "__USE_PREV_ARG__"]
    
    # NEW: Scan for {USE_CACHED_ENTRY_N} patterns alongside __USE_PREV_ARG__
    _cached_pattern = re.compile(r'\{USE_CACHED_ENTRY_(\d+)\}')
    cached_refs: dict[str, list] = {}  # key -> [(N, entry), ...]
    
    inst = self.pool.get_instance(instance_name)
    cache_pool = getattr(inst, 'cache_pool', None) if inst else None
    
    for key, val in parsed.items():
        if isinstance(val, str):
            matches = list(_cached_pattern.finditer(val))
            if matches:
                for match in matches:
                    n = int(match.group(1))
                    entry = cache_pool.get(n) if cache_pool else None
                    if entry is not None:
                        cached_refs.setdefault(key, []).append((n, entry.value, match.group(0)))
                    else:
                        logger.debug("Cache entry N=%d evicted/unavailable for %s/%s", 
                                   n, instance_name, tool_name)
    
    # Early return only if NO placeholders of either type found
    if not placeholders_found and not cached_refs:
        return parsed  # Nothing to resolve
    
    resolved_args = copy.deepcopy(parsed)

    # ── Step 3a: resolve __USE_PREV_ARG__ (existing logic, lines 3209-3221) ───
    scope_cache = getattr(self.pool, 'last_tool_args', {}).get(instance_name, {})
    prev_args = scope_cache.get(tool_name)
    global_args = scope_cache.get("__GLOBAL__", {})

    for arg_key in placeholders_found:
        if arg_key in global_args:
            resolved_args[arg_key] = copy.deepcopy(global_args[arg_key])
        elif prev_args and arg_key in prev_args:
            resolved_args[arg_key] = copy.deepcopy(prev_args[arg_key])

    # ── Step 3b: resolve {USE_CACHED_ENTRY_N} (NEW) ────────────────────────
    for key, refs in cached_refs.items():
        val = resolved_args[key]
        for n, entry_value, placeholder_str in refs:
            replacement = entry_value
            if not isinstance(replacement, str):
                try:
                    replacement = json.dumps(replacement)
                except (TypeError, ValueError):
                    replacement = str(replacement)
            val = val.replace(placeholder_str, replacement)
        resolved_args[key] = val

    return resolved_args
```

### 3.4 Edge Cases

| Scenario | Behavior |
|----------|----------|
| Index N doesn't exist (evicted) | Leave placeholder as-is, log debug message |
| Multiple references in one arg value | Each replaced independently via `str.replace()` on the same string |
| Mixed `__USE_PREV_ARG__` and `{USE_CACHED_ENTRY_N}` in same call | Both resolved: `__USE_PREV_ARG__` first (Step 3a), then cached entries (Step 3b) |
| Cache disabled but placeholder used | Placeholder left as-is (graceful degradation, no errors) |
| Non-string cached value referenced in string arg | Serialized via `json.dumps()` with fallback to `str()` |

---

## 4. Settings Integration

### 4.1 PoolSettings Extensions (`agent_instance.py`, lines 433-465)

Add three new fields to the `PoolSettings` dataclass (inserted after line 463, before the trailing blank lines):

```python
@dataclass
class PoolSettings:
    # ... existing fields through line 463 ...
    
    # Cache pool settings (Feature: USE_PREV_ARG → full caching system)
    cache_pool_enabled: bool = True            # Toggle on/off (default: enabled)
    cache_pool_size: int = 50                  # Rolling buffer entries per instance  
    cache_threshold_chars: int = 1000          # Min chars for output & granular arg caching
```

> **No redundant toggle**: The single source of truth is `PoolSettings.cache_pool_enabled`. No separate pool-level default flag in `agent_pool.py`. Each instance's `ArgumentCachePool.enabled` mirrors this setting.

### 4.2 Config Handlers (`config_handlers.py`, appended after line 232)

Register three new handlers following the existing decorator pattern:

```python
@register_config_handler('cache_pool_enabled')
def _handle_cache_pool_enabled(ui_cfg: dict, agent_pool, agents) -> None:
    """Toggle cache pool on/off and propagate to all running instances."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = bool(ui_cfg['cache_pool_enabled'])
        agent_pool.settings.cache_pool_enabled = val
        # Propagate toggle to all existing instance cache pools
        for inst in agent_pool.instance_conversations.values():
            if hasattr(inst, 'cache_pool') and inst.cache_pool is not None:
                inst.cache_pool.enabled = val

@register_config_handler('cache_pool_size')
def _handle_cache_pool_size(ui_cfg: dict, agent_pool, agents) -> None:
    """Update rolling buffer size for cache pools."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(5, int(ui_cfg['cache_pool_size']))  # Min 5 entries to prevent useless pools
        agent_pool.settings.cache_pool_size = val

@register_config_handler('cache_threshold_chars')
def _handle_cache_threshold(ui_cfg: dict, agent_pool, agents) -> None:
    """Update character threshold for output and granular arg caching."""
    if agent_pool is not None and hasattr(agent_pool, 'settings'):
        val = max(100, int(ui_cfg['cache_threshold_chars']))  # Min 100 chars
        agent_pool.settings.cache_threshold_chars = val
```

### 4.3 Settings Propagation Flow

```
UI Config Update → WebSocket Handler → ConfigUpdateRouter.apply() 
    → Registered handler fires → Updates PoolSettings on agent_pool
    → Iterates instance_conversations.values() → Updates each cache_pool.enabled
```

---

## 5. File-by-File Change Map (Precise Line Numbers)

### 5.1 `agent_cascade/agent_instance.py` — Data classes + Settings + Instance init

| Location | Change | Details |
|----------|--------|---------|
| **Line ~14** (imports) | **Add imports** | `import json`, `from collections import deque` |
| **Line ~58** (before `AgentInstance`) | **New classes** | Insert `CacheEntry` dataclass + `ArgumentCachePool` class (~90 lines total) |
| **Line 433-465** (`PoolSettings`) | **Add fields** | Append `cache_pool_enabled`, `cache_pool_size`, `cache_threshold_chars` after line 463 |
| **Line ~128+** (AgentInstance defaults) | **Add field** | Add `_cache_pool: Optional[ArgumentCachePool] = None` as a default field in the dataclass. Initialized lazily by execution engine on first access to avoid issues with dataclass `default_factory` and threading. |

### 5.2 `agent_cascade/execution_engine.py` — Caching + Resolution

| Line Range | Change | Details |
|------------|--------|---------|
| **3139-3164** (`_cache_tool_args`) | **Extend** | After existing `last_tool_args` caching (lines 3152-3164), add cache pool entry for the full args dict AND individual string values > threshold. Insert after line 3164, before the closing of the method. |
| **3166-3223** (`_resolve_placeholders`) | **Extend** | Replace the early-return at line 3204 to also check for cached entry patterns. Add Step 3b (cached entry resolution) after Step 3a (existing `__USE_PREV_ARG__` logic). See §3.3 for exact code. |
| **~line 2414** (tool execution loop) | **Add output caching hook** | Insert `_cache_tool_output()` call between lines 2414-2415, BEFORE `truncate_tool_result()`. This caches the full-length result. |
| **After line 3223** | **New method** | Add `_cache_tool_output()` method (see §5.2 code below) |

**`_cache_tool_args` extension — inserted after line 3164:**

```python
    def _cache_tool_args(self, instance_name: str, tool_name: str, 
                         tool_args: Any) -> None:
        # ... existing lines 3152-3164 unchanged ...
        
        # ── NEW: Also add to rolling cache pool ─────────────────────────────
        if not isinstance(tool_args, dict):
            return
        
        inst = self.pool.get_instance(instance_name)
        if inst is None or not hasattr(inst, 'cache_pool') or inst.cache_pool is None:
            return
        
        cp = inst.cache_pool
        if not cp.enabled:
            return
        
        threshold = self.pool.settings.cache_threshold_chars
        
        # 1. Cache entire args dict as one entry
        try:
            cp.add("arg", instance_name, tool_name, copy.deepcopy(tool_args))
        except (TypeError, AttributeError):
            pass
        
        # 2. Also cache individual string values > threshold separately
        for key, val in tool_args.items():
            if isinstance(val, str) and len(val) > threshold:
                try:
                    cp.add("arg", instance_name, f"{tool_name}.{key}", val)
                except (TypeError, AttributeError):
                    pass
```

**`_cache_tool_output()` — new method after line 3223:**

```python
    def _cache_tool_output(self, instance_name: str, tool_name: str, 
                           output: str, threshold: int = 1000) -> None:
        """Cache tool output in the rolling pool if it exceeds the threshold.
        
        Called BEFORE truncation so the full content is preserved.
        
        Args:
            instance_name: Agent instance name (scope key).
            tool_name: Name of the tool that produced this output.
            output: The tool result string (full, pre-truncation).
            threshold: Minimum character count to trigger caching.
        """
        if not isinstance(output, str) or len(output) <= threshold:
            return
        
        inst = self.pool.get_instance(instance_name)
        if inst is None or not hasattr(inst, 'cache_pool') or inst.cache_pool is None:
            return
        
        cp = inst.cache_pool
        if not cp.enabled:
            return
        
        try:
            cp.add("output", instance_name, tool_name, output)
        except (TypeError, AttributeError):
            pass
```

### 5.3 `agent_cascade/tool_dispatcher.py` — Output caching hooks

| Line | Change | Details |
|------|--------|---------|
| **Line 124** (`call_agent` path) | **Add output caching** | After `_cache_tool_args()` at line 125, add `_cache_tool_output()` call before `return result` |
| **Line 130** (`dismiss_agent` path) | **Add output caching** | Same pattern after `_cache_tool_args()` |
| **Line 135** (`compress_context` path) | **Add output caching** | Same pattern after `_cache_tool_args()` |
| **Line 154** (generic tool path) | **Add output caching** | Same pattern after `_cache_tool_args()` |

**Exact change at line 124-126 (`call_agent` path):**

```python
            result = self.handle_call_agent(resolved, llm_messages, instance, function_id=function_id)
            logger.debug("handle_call_agent returned type=%s", type(result).__name__)
            self.engine._cache_tool_args(instance.instance_name, tool_name, resolved)
            # NEW: Cache output before returning (full content preserved pre-truncation)
            self.engine._cache_tool_output(
                instance.instance_name, tool_name, result,
                threshold=self.pool.settings.cache_threshold_chars
            )
            return result
```

**Same pattern at lines 130, 135, and 154 for all four dispatch paths.**

> **Note**: Output caching is also done in the execution engine loop (line ~2414) as a safety net. The dispatcher-level calls handle the case where results are returned directly without going through truncation. The execution-engine-level call handles ALL tool results uniformly before truncation.

### 5.4 `agent_cascade/tool_utils.py` — Shared resolution utility

| Line | Change | Details |
|------|--------|---------|
| **Line ~138** (inside `resolve_prev_arg_placeholders`) | **Extend** | After scanning for `__USE_PREV_ARG__` at line 138, add `{USE_CACHED_ENTRY_N}` scan. Need access to instance's cache pool — pass it as an additional parameter or look it up via agent_pool reference. |

```python
# In resolve_prev_arg_placeholders() starting ~line 138:

    # Existing placeholder scan (unchanged):
    placeholders_found = [key for key, val in tool_args.items() if val == "__USE_PREV_ARG__"]
    
    # NEW: Scan for {USE_CACHED_ENTRY_N} patterns
    _cached_pattern = re.compile(r'\{USE_CACHED_ENTRY_(\d+)\}')
    cached_refs: dict[str, list] = {}
    
    for key, val in tool_args.items():
        if isinstance(val, str):
            matches = list(_cached_pattern.finditer(val))
            if matches:
                # Look up instance's cache pool via agent_pool
                inst = None
                for name, i in getattr(agent_pool, 'instance_conversations', {}).items():
                    if name == instance_scope:
                        inst = i
                        break
                cp = getattr(inst, 'cache_pool', None) if inst else None
                
                for match in matches:
                    n = int(match.group(1))
                    entry = cp.get(n) if cp else None
                    if entry is not None:
                        cached_refs.setdefault(key, []).append((n, entry.value, match.group(0)))
    
    # Combine both scan results for early return check:
    if not placeholders_found and not cached_refs:
        return tool_args, None
    
    resolved_args = copy.deepcopy(tool_args)
    
    # ... existing __USE_PREV_ARG__ resolution (lines 145-176) unchanged ...
    
    # NEW: Apply cached entry replacements after __USE_PREV_ARG__ resolution
    for key, refs in cached_refs.items():
        val = resolved_args[key]
        for n, entry_value, placeholder_str in refs:
            replacement = entry_value if isinstance(entry_value, str) else json.dumps(entry_value)
            val = val.replace(placeholder_str, replacement)
        resolved_args[key] = val
    
    return resolved_args, None
```

### 5.5 `agent_cascade/config_handlers.py` — Settings handlers

| Line | Change | Details |
|------|--------|---------|
| **After line 232** (end of file) | **Append 3 handlers** | Register `cache_pool_enabled`, `cache_pool_size`, `cache_threshold_chars` handlers. See §4.2 for exact code. |

### 5.6 `agent_cascade/tools/custom/system_info.py` — Cache pool display

| Line | Change | Details |
|------|--------|---------|
| **Line ~101-102** (getting instance) | **Reuse existing lookup** | The instance is already looked up at lines 76-80. Reuse that reference instead of doing a second lookup. |
| **Line ~201-214** (building info string) | **Add cache pool section** | Insert between "Session Stats" and "Tool Policy" sections |

```python
# In call() method — modify the info assembly at lines 201-215:

        # NEW: Cache pool state (inserted before building final string)
        cache_pool_str = ""
        if inst is not None and hasattr(inst, 'cache_pool') and inst.cache_pool is not None:
            cache_pool_str = f"\n{inst.cache_pool.get_state_summary(max_display=10)}\n"

        info = (
            f"--- System Information ---\n"
            f"OS: {os_info}\n"
            f"Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Python Version: {py_version}\n"
            f"API Endpoint: {api_base}\n"
            f"Model Used: {model}\n"
            f"\n--- Workspace & Permissions ---\n"
            f"{folders_info}"
            f"\n--- Session Stats ---\n"
            f"{stats_str}\n"
            f"\n--- Cache Pool State ---"     # NEW SECTION HEADER (no extra newline, body has its own)
            f"{cache_pool_str if cache_pool_str else '  (not initialized)'}"
            f"\n--- Tool Policy ---\n"
            f"{tools_str}"
        )
```

### 5.7 `agent_cascade/agent_pool.py` — Minimal changes

| Line | Change | Details |
|------|--------|---------|
| **Line ~287** | **Add import** | Add `from collections import deque` to imports (line ~16) if needed for any pool-level operations. Otherwise no data class definitions here — they live in `agent_instance.py`. |
| **No new toggle field** | The single source of truth is `PoolSettings.cache_pool_enabled` on the settings object at line 287. No redundant `cache_pool_enabled_default` added. |

---

## 6. Backward Compatibility with `__USE_PREV_ARG__`

### 6.1 Strategy: Dual-Mode Resolution

Both systems coexist and are resolved in a single pass through `_resolve_placeholders()`:
1. Parse JSON arguments → dict (lines 3185-3199, unchanged)
2. Scan for BOTH `__USE_PREV_ARG__` AND `{USE_CACHED_ENTRY_N}` patterns simultaneously
3. Resolve `__USE_PREV_ARG__` first (Step 3a, existing logic preserved)
4. Resolve `{USE_CACHED_ENTRY_N}` second (Step 3b, new logic)
5. Return resolved dict

### 6.2 Preservation Rules

| What | Where | Status |
|------|-------|--------|
| `last_tool_args` dict on pool | `agent_pool.py:287` | **KEPT** — still written by `_cache_tool_args()` (lines 3152-3164) |
| Per-tool + global cache structure | Same as above | **UNCHANGED** |
| `__USE_PREV_ARG__` resolution logic | `execution_engine.py:3209-3221` | **KEPT** — extended with additional scan, not replaced |
| `resolve_prev_arg_placeholders()` utility | `tool_utils.py:100-178` | **EXTENDED** — adds cached entry resolution alongside existing |

### 6.3 Migration Path

```
Phase 1 (this implementation): Both systems active simultaneously
    ↓
Phase 2 (future, optional): Deprecation warnings for __USE_PREV_ARG__ in logs
    ↓
Phase 3 (optional): Agents updated to prefer {USE_CACHED_ENTRY_N} in prompts
```

### 6.4 Prompt Template Updates

Agent system prompts should be updated to mention the new syntax:

**In DNA prompt templates**, add to tool usage instructions:
```
- Tool arguments can reference cached entries using {USE_CACHED_ENTRY_N} 
  where N is the cache index shown in system_info output.
- The legacy __USE_PREV_ARG__ placeholder still works for reusing previous 
  arguments from the same tool.
```

---

## 7. System Info Extension — Cache Pool Display

### 7.1 Output Format

When `system_info` is called, a new section appears:

```
--- Cache Pool State ---
  Cache Pool: 12/50 entries (enabled=True)
    [N= 48] [ARG] worker1/read_file (127 chars): {"path": "src/main.py", "start_line": 1}
    [N= 47] [OUT] worker1/call_agent (3421 chars): Agent 'researcher1' Completed: The implementation plan includes...
    [N= 46] [ARG] worker1/edit_file (89 chars): {"path": "src/main.py", "old_content": "..."}
    [N= 45] [OUT] orchestrator/call_agent (2105 chars): Agent 'coder1' Completed: Here is the working code...
    [N= 44] [ARG] orchestrator/read_file (68 chars): {"path": "docs/design.md"}
    [N= 43] [OUT] worker1/call_agent (4892 chars): Agent 'reviewer1' Completed: Code review findings:\n1. The main...
    ... and 7 older entries (oldest: N=36, 37, 38, 39, 40...)

--- Cache Pool State ---
  Cache Pool: empty
```

### 7.2 Display Rules

- Show **most recent** entries first (reverse chronological)
- Max **10 entries** displayed in detail
- Truncate preview to **~200 chars** per entry
- Show category tag: `[ARG]` or `[OUT]` for quick scanning
- Include character count so agents know if content is substantial
- When pool is empty, show "Cache Pool: empty" (not cluttered)
- When disabled, show "(enabled=False)" in header
- When not initialized yet, show "(not initialized)"

---

## 8. Implementation Order & Dependencies

### Phase A: Foundation (no behavior change yet)
1. **`agent_instance.py`** — Add `CacheEntry` + `ArgumentCachePool` classes (~line 58), add imports, extend `PoolSettings`, add `_cache_pool` field to `AgentInstance`
2. Verify no circular imports (`agent_instance.py` only imports from `llm.schema`, `settings`, stdlib)

### Phase B: Caching Logic
3. **`execution_engine.py`** — Extend `_cache_tool_args()` (~line 3158) with cache pool writes + granular caching
4. **`execution_engine.py`** — Add `_cache_tool_output()` method (after line 3223)
5. **`execution_engine.py`** — Insert output caching call at ~line 2414 (before truncation)

### Phase C: Resolution Logic
6. **`execution_engine.py`** — Extend `_resolve_placeholders()` (~lines 3201-3223) with `{USE_CACHED_ENTRY_N}` support
7. **`tool_utils.py`** — Extend `resolve_prev_arg_placeholders()` for streaming path parity

### Phase D: Settings & Display
8. **`config_handlers.py`** — Register toggle/size/threshold handlers (after line 232)
9. **`system_info.py`** — Add cache pool state section to output (~line 201)

### Phase E: Testing & Validation
10. Run test suite (see §9 below)

---

## 9. Thread Safety Considerations

| Resource | Protection Level | Implementation |
|----------|-----------------|----------------|
| Cache pool deque | `threading.Lock` in `ArgumentCachePool` | Per-instance lock (each instance has its own pool) |
| `_next_index` counter | Protected by same lock | Atomic increment within lock scope |
| `last_tool_args` dict | Existing: no explicit lock | Acceptable — single-writer-per-instance pattern |
| Settings fields | Read-after-write, no races | Config handlers update during idle moments |

---

## 10. Test Plan

### Unit Tests (to be implemented in a test file or verified manually)

| # | Test Name | What It Verifies | Expected Outcome |
|---|-----------|-----------------|------------------|
| T1 | `test_cache_entry_creation` | Creating a `CacheEntry` with all fields | Entry has correct index, category, preview truncated at 200 chars |
| T2 | `test_pool_add_and_get` | Add entry → get by same index | Returns identical value (deep copy safe) |
| T3 | `test_pool_rolling_eviction` | Fill pool to max_size + 1, check oldest evicted | Oldest index returns None from `get()`, newest still accessible |
| T4 | `test_pool_index_monotonicity` | Add entries, evict some, add more | New indices are always > all previously assigned indices |
| T5 | `test_pool_disabled_skip` | Set `enabled=False`, add entry | Returns -1, no entry stored |
| T6 | `test_cache_tool_args_basic` | Call `_cache_tool_args()` with a dict | Entry appears in pool's deque under category "arg" |
| T7 | `test_cache_tool_args_granular` | Call with args containing string > 1000 chars | Two entries created: one for full dict, one for the large value |
| T8 | `test_cache_tool_output_threshold` | Call `_cache_tool_output()` with 500-char and 2000-char strings | Only the 2000-char string is cached |
| T9 | `test_resolve_cached_entry` | Arg dict has `{USE_CACHED_ENTRY_1}` value | Placeholder replaced with cached content |
| T10 | `test_resolve_evicted_entry` | Reference index that was evicted | Placeholder left as-is, debug log emitted |
| T11 | `test_resolve_mixed_placeholders` | Same arg dict has both `__USE_PREV_ARG__` and `{USE_CACHED_ENTRY_N}` | Both resolved correctly in order |
| T12 | `test_backward_compat_prev_arg` | Use only `__USE_PREV_ARG__` (no cached entries) | Works identically to pre-change behavior |
| T13 | `test_cache_before_truncation` | Tool returns 5000 chars, threshold=1000, truncation at 2000 | Cache stores full 5000-char version |
| T14 | `test_json_dumps_fallback` | Cache a non-serializable value (e.g., dict with custom objects) | Falls back to `str()`, no exception raised |
| T15 | `test_settings_toggle_propagation` | Toggle `cache_pool_enabled` via config handler | All instance cache pools reflect new state |

### Integration Tests

| # | Test Name | What It Verifies | Expected Outcome |
|---|-----------|-----------------|------------------|
| I1 | End-to-end: call_agent → cache → reuse | Agent calls another agent, result cached, third tool references it via `{USE_CACHED_ENTRY_N}` | Third tool receives the full child agent output |
| I2 | system_info displays pool state | Call `system_info` after several tool executions | Cache Pool State section shows recent entries with correct format |
| I3 | Toggle off mid-session | Disable cache pool, verify new tools don't add entries | No new entries appear in pool |

---

## 11. Estimated Impact Summary

| Metric | Estimate | Notes |
|--------|----------|-------|
| New lines of code | ~220-270 | Spread across 6 files |
| Modified functions | 4 core + 3 new methods + 3 config handlers | `_cache_tool_args`, `_resolve_placeholders`, `execute_tool` loop, `system_info.call` + `_cache_tool_output`, config handlers |
| Performance overhead | Negligible | Cache ops are O(1) deque append; resolution adds one compiled regex scan per arg dict |
| Memory overhead | ~50 entries × instance | Each entry stores value + preview string; typical usage < 2KB/instance at steady state |
| Backward compat | Full | `__USE_PREV_ARG__` unchanged, both systems coexist in single resolution pass |

---

## Appendix: Key File References (Verified Line Numbers)

| File | Lines | Purpose |
|------|-------|---------|
| `agent_instance.py` | 1-25 | Imports section — add `json`, `deque` |
| `agent_instance.py` | ~58 | Insert `CacheEntry` + `ArgumentCachePool` classes |
| `agent_instance.py` | 433-465 | `PoolSettings` dataclass — extend with cache fields |
| `agent_pool.py` | 25 | Import from `agent_instance` (confirms no circular import if we add there) |
| `agent_pool.py` | 287 | Current `last_tool_args` storage — keep as-is |
| `execution_engine.py` | 16-20 | Imports (`copy`, `json`, `re` already present) |
| `execution_engine.py` | 3139-3164 | `_cache_tool_args()` — extend with pool writes + granular caching |
| `execution_engine.py` | 3166-3223 | `_resolve_placeholders()` — add `{USE_CACHED_ENTRY_N}` resolution |
| `execution_engine.py` | ~2414 | Tool execution loop — insert output caching before truncation |
| `tool_dispatcher.py` | 119-155 | Four dispatch paths — each needs `_cache_tool_output()` call after `_cache_tool_args()` |
| `tool_utils.py` | 100-178 | Shared resolution utility — extend with cached entry support |
| `config_handlers.py` | 27+ | Config handler registration pattern — append 3 new handlers |
| `tools/custom/system_info.py` | 201-215 | System info output assembly — add cache pool section |