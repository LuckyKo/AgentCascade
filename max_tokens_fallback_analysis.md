# Max Tokens Fallback Bug Analysis

## Executive Summary

When an API call fails and the fallback mechanism selects the next endpoint from the priority chain, the `max_input_tokens` value from the new endpoint is **silently overwritten** by the stale value from the original endpoint that was used to initialize the LLM template. This causes agents to use incorrect token limits after fallback switching.

---

## 1. How max_tokens is Initially Set for Agents

### Flow: AgentPool → APIRouter → Template → LLM

1. **AgentPool initialization** (`agent_pool.py:249-252`):
   ```python
   self.api_router = APIRouter(
       default_llm_cfg=llm_cfg,  # Contains max_input_tokens
       config_dir=config_dir
   )
   ```

2. **Template loading** (`agent_factory.py:199-202`):
   ```python
   if agent_pool.api_router is not None:
       agent_llm_cfg = agent_pool.api_router.get_llm_config(agent_name)
   else:
       agent_llm_cfg = llm_cfg or agent_pool.llm_cfg
   ```

3. **LLM object initialization** (`llm/base.py:80-93`):
   ```python
   cfg = cfg or {}
   self.cfg = cfg  # Contains max_input_tokens at top level
   generate_cfg = copy.deepcopy(cfg.get('generate_cfg', {}))
   # Support max_input_tokens at the top level of cfg
   if 'max_input_tokens' in cfg and 'max_input_tokens' not in generate_cfg:
       generate_cfg['max_input_tokens'] = cfg['max_input_tokens']
   self.generate_cfg = generate_cfg  # max_input_tokens stored here
   ```

**Result**: Each agent template's `llm.generate_cfg` contains `max_input_tokens` from the **first endpoint** in the priority chain.

---

## 2. How the API Fallback Mechanism Works

### Flow: ExecutionEngine → APIRouter.call_with_fallback → Endpoint Chain

1. **LLM call initiation** (`execution_engine.py:1563-1620`):
   - `_execute_llm_call()` creates a `_do_call(llm_cfg)` closure
   - Calls `self.pool.api_router.call_with_fallback(agent_type, _do_call, allocated_tokens=allocated_tokens)`

2. **Endpoint chain resolution** (`api_router.py:732-817`):
   - `get_endpoint_chain(agent_type, allocated_tokens)` builds the fallback list:
     - **Tier 1**: Agent-specific endpoints (priority order, enabled only)
     - **Tier 2**: Last successful endpoint (if available)
     - **Tier 3**: General Settings default (always last)
   - Each endpoint config includes `max_input_tokens` from `ep.max_input_tokens` or falls back to `general_limit`

3. **Fallback execution** (`api_router.py:821-1005`):
   ```python
   def call_with_fallback(self, agent_type, call_fn, *args, allocated_tokens=None, **kwargs):
       chain = self.get_endpoint_chain(agent_type, allocated_tokens=allocated_tokens)
       
       for cfg_idx, llm_cfg in enumerate(chain):  # Iterates through endpoints
           for attempt in range(max_retries + 1):
               try:
                   kwargs['llm_cfg'] = llm_cfg
                   result = call_fn(*args, **kwargs)  # Calls _do_call(llm_cfg)
                   return result
               except Exception as e:
                   # Record error, retry or move to next endpoint
                   all_errors.append(error_msg)
       
       raise RuntimeError("All API endpoints exhausted")
   ```

---

## 3. Where max_tokens Should Be Reapplied

The `max_input_tokens` should be properly carried through the fallback chain and applied to each LLM call. The correct flow should be:

1. `get_endpoint_chain()` returns endpoint configs with correct `max_input_tokens` for each endpoint
2. `_do_call(llm_cfg)` receives the endpoint config and uses its `max_input_tokens`
3. The LLM call uses the endpoint-specific `max_input_tokens` for input truncation

### Where it SHOULD happen (correct logic):

In `_do_call()` (`execution_engine.py:1589-1617`):
```python
def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
    # llm_cfg contains the endpoint's max_input_tokens from get_endpoint_chain()
    merged_cfg = dict(llm_cfg)  # Should start with endpoint defaults
    # ... merge with overrides ...
    # merged_cfg should contain the CORRECT max_input_tokens for this endpoint
    return llm.chat(..., extra_generate_cfg=merged_cfg)
```

Then in `llm.chat()` (`llm/base.py:253, 267`):
```python
generate_cfg = merge_generate_cfgs(base_generate_cfg=self.generate_cfg, new_generate_cfg=extra_generate_cfg)
max_input_tokens = generate_cfg.pop('max_input_tokens', DEFAULT_MAX_INPUT_TOKENS)
```

---

## 4. Root Cause: Why max_tokens Isn't Reapplied During Fallback Switching

### The Bug: Config Merge Order in `_do_call()`

**Location**: `execution_engine.py`, lines 1589-1617

```python
def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
    # Start with endpoint config as base, then apply per-instance overrides
    merged_cfg = dict(llm_cfg)  # Endpoint defaults first (includes correct max_input_tokens)
    
    # Per-instance override (set by user via UI) takes precedence over endpoint defaults
    if instance._generate_cfg_override is not None:
        merged_cfg.update(instance._generate_cfg_override)
    elif hasattr(llm, 'generate_cfg'):
        merged_cfg.update(llm.generate_cfg)  # <-- BUG: Overwrites endpoint max_input_tokens!
```

### The Problem Explained

1. **`llm_cfg`** (from fallback chain): Contains the **correct** `max_input_tokens` for the current endpoint (e.g., 32000 for Endpoint B)

2. **`llm.generate_cfg`** (from template initialization): Contains the **stale** `max_input_tokens` from the **first** endpoint that initialized the template (e.g., 128000 for Endpoint A)

3. **`merged_cfg.update(llm.generate_cfg)`**: This overwrites `merged_cfg['max_input_tokens']` (32000) with `llm.generate_cfg['max_input_tokens']` (128000)

4. **Result**: The LLM call uses 128000 instead of 32000, even though Endpoint B only supports 32000 tokens

### Why This Happens

The `llm` object is created once during template loading with the **first endpoint's** config. Its `generate_cfg` is populated at that time and never updated. When fallback switches to a different endpoint, the `llm` object still has the old endpoint's `max_input_tokens` in `generate_cfg`.

The `merged_cfg.update(llm.generate_cfg)` call was intended to apply template-level settings (like `parallel_function_calls`, `stop`, etc.) but it inadvertently overwrites **all** keys from `llm.generate_cfg`, including `max_input_tokens`.

### Visual Representation

```
First API call (Endpoint A - 128K tokens):
  llm_cfg = {'api_base': 'http://endpoint-a', 'max_input_tokens': 128000}
  llm.generate_cfg = {'max_input_tokens': 128000, 'parallel_function_calls': True}
  merged_cfg = llm_cfg.copy()                    # max_input_tokens = 128000 ✓
  merged_cfg.update(llm.generate_cfg)            # max_input_tokens = 128000 ✓
  Result: Uses 128000 tokens ✓

API call fails → Fallback to Endpoint B (32K tokens):
  llm_cfg = {'api_base': 'http://endpoint-b', 'max_input_tokens': 32000}
  llm.generate_cfg = {'max_input_tokens': 128000, 'parallel_function_calls': True}  # STALE!
  merged_cfg = llm_cfg.copy()                    # max_input_tokens = 32000 ✓
  merged_cfg.update(llm.generate_cfg)            # max_input_tokens = 128000 ✗ OVERWRITTEN!
  Result: Uses 128000 tokens (WRONG! Endpoint B only supports 32K) ✗
```

### Additional Impact

The stale `max_input_tokens` also affects:
- **Input message truncation** (`llm/base.py:281-288`): Messages are truncated to the wrong limit
- **Instance tracking** (`execution_engine.py:1603-1606`): `instance._allocated_max_input_tokens` is set to the wrong value
- **Compression checks** (`execution_engine.py:1127-1156`): Token usage percentage is calculated against the wrong limit

---

## 5. Recommended Fix

### Option A: Reverse Merge Order (Simplest)

In `_do_call()`, apply endpoint config AFTER template config so endpoint values take precedence:

```python
def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
    # Start with template config as base
    merged_cfg = {}
    if hasattr(llm, 'generate_cfg'):
        merged_cfg.update(llm.generate_cfg)
    
    # Endpoint config takes precedence (includes correct max_input_tokens for this endpoint)
    merged_cfg.update(llm_cfg)
    
    # Per-instance override takes final precedence
    if instance._generate_cfg_override is not None:
        merged_cfg.update(instance._generate_cfg_override)
```

### Option B: Selective Key Merge (More Precise)

Only merge non-token-related keys from `llm.generate_cfg`:

```python
def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
    merged_cfg = dict(llm_cfg)  # Endpoint defaults first
    
    # Apply template settings, but preserve endpoint's max_input_tokens
    if hasattr(llm, 'generate_cfg'):
        for k, v in llm.generate_cfg.items():
            if k != 'max_input_tokens':  # Don't overwrite endpoint's token limit
                merged_cfg[k] = v
    
    if instance._generate_cfg_override is not None:
        merged_cfg.update(instance._generate_cfg_override)
```

### Option C: Use Endpoint Config for Token Limit Only

```python
def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
    # Start with template config as base
    merged_cfg = {}
    if hasattr(llm, 'generate_cfg'):
        merged_cfg.update(llm.generate_cfg)
    
    # Apply endpoint config for transport settings, but preserve template's max_input_tokens
    for k, v in llm_cfg.items():
        if k != 'max_input_tokens':  # Keep template's max_input_tokens
            merged_cfg[k] = v
    
    # Per-instance override takes final precedence
    if instance._generate_cfg_override is not None:
        merged_cfg.update(instance._generate_cfg_override)
```

**Recommendation**: Option A is the simplest and most intuitive — endpoint config should always override template defaults for the same keys. This matches the expected "fallback" semantics where the next endpoint in the chain should fully replace the previous one.

---

## 6. Files Involved

| File | Line(s) | Role |
|------|---------|------|
| `agent_cascade/execution_engine.py` | 1589-1617 | **BUG LOCATION**: `_do_call()` merge order |
| `agent_cascade/api_router.py` | 732-817 | `get_endpoint_chain()` - builds fallback list |
| `agent_cascade/api_router.py` | 821-1005 | `call_with_fallback()` - executes fallback chain |
| `agent_cascade/api_router.py` | 65-74 | `APIEndpoint.to_llm_cfg()` - converts endpoint to config |
| `agent_cascade/llm/base.py` | 80-93 | LLM init - stores `max_input_tokens` in `generate_cfg` |
| `agent_cascade/llm/base.py` | 253, 267 | `chat()` - merges configs and extracts `max_input_tokens` |
| `agent_cascade/agent_factory.py` | 199-202 | Template loading - gets initial endpoint config |
| `agent_cascade/agent_pool.py` | 249-252 | Pool init - creates APIRouter with default config |

---

## 7. Verification Steps

To verify the fix:

1. Configure two API endpoints with different `max_input_tokens` values (e.g., Endpoint A: 128K, Endpoint B: 32K)
2. Set Endpoint A as primary for an agent type
3. Make an API call that fails on Endpoint A
4. Verify the fallback to Endpoint B uses 32K tokens (not 128K)
5. Check `instance._allocated_max_input_tokens` after the call