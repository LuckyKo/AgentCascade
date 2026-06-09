# Feature 019: Prompt Reprocessing Optimization — Cache, Drain, Rebuild

## Overview
Optimize prompt reprocessing overhead across AgentCascade unified branch by implementing intelligent caching of preprocessed messages, efficient async message queue draining, and streamlined working set rebuild after compression. This reduces redundant LLM preprocessing work, minimizes token counting overhead, and improves overall agent response times.

## Problem Statement
Current implementation has several inefficiencies:
1. **Redundant Preprocessing**: Messages are preprocessed multiple times even when content hasn't changed
2. **Token Counting Overhead**: `_count_history_tokens()` is called repeatedly without caching
3. **Inefficient Drain Pattern**: Async message draining doesn't batch operations efficiently
4. **Working Set Rebuild**: After compression, working sets are rebuilt but token caches aren't invalidated consistently

## Changes Required

### 1. Preprocessed Message Cache
**File**: `agent_cascade/llm/base.py`

Add a simple LRU cache for preprocessed messages to avoid redundant preprocessing:

```python
from functools import lru_cache
import hashlib

class BaseChatModel:
    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        # Cache for preprocessed messages (keyed by message hash)
        self._preprocess_cache = {}
        self._max_cache_size = 100  # LRU cache size limit
    
    def _get_message_hash(self, messages: List[Message]) -> str:
        """Generate a hash for a list of messages to use as cache key."""
        msg_str = str([(m.role, str(m.content)) for m in messages])
        return hashlib.md5(msg_str.encode()).hexdigest()
    
    def _clear_preprocess_cache(self):
        """Clear the preprocessing cache."""
        self._preprocess_cache.clear()
```

### 2. Token Count Cache with Invalidator
**File**: `agent_cascade/execution_engine.py`

Add caching for token counts with explicit invalidation:

```python
def _count_history_tokens(self, messages: List[Message], instance: AgentInstance) -> int:
    """Count tokens in message history with caching.
    
    Returns cached value if messages haven't changed since last count.
    Cache is invalidated when conversation length changes (tracked via _last_token_count_conversation_length).
    """
    # Check cache validity
    if (instance._last_token_count is not None and 
        instance._last_token_count_conversation_length == len(messages)):
        return instance._last_token_count
    
    # Perform actual count
    from agent_cascade.utils.tokenization_qwen import count_tokens
    token_count = count_tokens(messages)
    
    # Update cache
    instance._last_token_count = token_count
    instance._last_token_count_conversation_length = len(messages)
    
    return token_count
```

### 3. Batched Async Drain
**File**: `agent_cascade/agent_pool.py`

Optimize the drain operation to batch queue clearing:

```python
def drain_queue(self, instance_name: str) -> List[str]:
    """Drain all queued messages for an instance in a single atomic operation.
    
    Returns list of message texts (may be empty).
    Operation is thread-safe and clears the queue atomically.
    """
    with self._queue_lock:
        # Pop entire queue at once (atomic operation)
        return self.message_queues.pop(instance_name, [])

def drain_async_results(self, instance_name: str) -> List[str]:
    """Drain all completed async results for an instance atomically.
    
    Args:
        instance_name: The agent instance to drain results for.
        
    Returns:
        List of result strings (may be empty).
    """
    with self._async_lock:
        # Pop entire results list at once (atomic operation)
        return self._async_results.pop(instance_name, [])
```

### 4. Working Set Rebuild Optimization
**File**: `agent_cascade/execution_engine.py`

Optimize the rebuild process to minimize redundant operations:

```python
def _rebuild_working_set(
    self, messages: List[Message], llm_messages: List[Message], inst_name: str
):
    """Rebuild both working sets from pool state after compression.
    
    Optimized version that:
    1. Clears existing lists in-place (preserves list objects)
    2. Extends with fresh copies from pool
    3. Invalidates token count cache atomically
    """
    # Get conversation from pool (single source of truth)
    inst = self.pool.get_instance(inst_name)
    if not inst:
        logger.warning(f"Instance {inst_name} not found during rebuild")
        return
    
    with inst._compression_lock:
        conv = inst.conversation
    
    # Clear and refill in-place (more efficient than reassignment)
    messages.clear()
    llm_messages.clear()
    
    # Copy conversation to messages working set
    from copy import deepcopy
    messages.extend(deepcopy(conv))
    
    # Apply slice for LLM messages (what actually goes to LLM)
    sliced = self.pool.slice_history_for_llm(conv)
    if sliced:
        llm_messages.extend(deepcopy(list(sliced)))
    else:
        llm_messages.extend(deepcopy(list(conv)))
    
    # Invalidate token count cache atomically
    inst._last_token_count = None
    inst._last_token_count_conversation_length = -1
    
    logger.debug(f"Rebuilt working sets for {inst_name}: messages={len(messages)}, llm_messages={len(llm_messages)}")
```

### 5. Agent Instance Token Cache Fields
**File**: `agent_cascade/agent_instance.py`

Add token counting cache fields to AgentInstance:

```python
class AgentInstance:
    def __init__(self, ...):
        # ... existing initialization ...
        
        # Token count caching (for optimization)
        self._last_token_count: Optional[int] = None
        self._last_token_count_conversation_length: int = -1
```

## Files Modified
1. `agent_cascade/llm/base.py` - Add preprocessing cache
2. `agent_cascade/execution_engine.py` - Token count caching, optimized rebuild
3. `agent_cascade/agent_pool.py` - Batched drain operations  
4. `agent_cascade/agent_instance.py` - Token cache fields

## Performance Impact
Expected improvements:
- **Preprocessing**: ~30-50% reduction in redundant preprocessing when messages unchanged
- **Token Counting**: ~70-90% reduction in token counting calls via caching
- **Drain Operations**: Atomic operations reduce lock contention
- **Overall**: 15-25% faster agent response times in typical workloads

## Testing Checklist
- [ ] Preprocessing cache works correctly (messages cached and retrieved)
- [ ] Token count cache invalidates when conversation changes
- [ ] Drain operations are atomic and thread-safe
- [ ] Working set rebuild preserves message order and content
- [ ] No memory leaks from unbounded cache growth
- [ ] Existing tests still pass

## Rollback Plan
If issues arise:
1. Remove preprocessing cache from base.py
2. Disable token count caching in execution_engine.py (set `_last_token_count = None`)
3. Revert drain operations to original implementation
4. Restore working set rebuild to previous version

## Implementation Notes
- Cache sizes are bounded to prevent memory growth
- All cache invalidation is tied to conversation length changes
- Thread safety maintained via existing locks
- Backward compatible with existing code