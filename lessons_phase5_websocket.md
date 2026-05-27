# Lessons Learned — Phase 5 WebSocket Handler Migration

## What Was Changed in api_server.py

### Step 1: User Message Handling (msg_type == 'message')
**Before:** `session['history'].append(...)` → sync to pool → `copy.deepcopy()` → thread with history_copy  
**After:** `pool.add_message(instance_name, Message(role=USER, content=...))` → thread with None for history_for_agent

Key insight: The instance may not exist on the first message. Must create it before adding messages.
Added logic to check `pool.get_instance()` and call `create_main_agent_instance()` if needed.

### Step 2: /rollback Command Handler  
**Before:** Loop popping from `session['history']` + manual logger truncation  
**After:** Single call to `pool.surgical_rollback(instance_name, n, soft=True, reason=...)`

### Step 3: Resume Handler
**Before:** Append continuation to `session['history']`, sync via `instance_conversations[name] = session['history']`, deep copy for thread  
**After:** `pool.add_message(target_instance, Message(...))`, thread with None for history_for_agent

Sub-agent pool restoration now reads from `pool.get_instance(sa_name).conversation` instead of `instance_conversations[sa_name]`.

### Step 4: Retry Handler
**Before:** Pop from `session['history']`, deep copy, manual logger truncation  
**After:** Pop from `pool.get_instance(instance_name).conversation`, no deep copy, `surgical_rollback` handles logging

Fallback paths to `session['history']` preserved when pool unavailable or instance not found.

### Step 5: edit_message and delete_messages Handlers
**Before:** Read from `session['history']` for main session  
**After:** Read from `pool.get_instance(target_name).conversation` with fallback to legacy

### Step 6: Reset Handler (both REST and WebSocket)
Added clearing of pool instance conversation alongside the existing `session['history'] = []`.

### Step 7: load_session Handler
**Before:** Read from `instance_conversations[instance_name]`  
**After:** Read from `pool.get_instance(instance_name).conversation`

### Step 8: run_agent_thread Function
Updated to handle `history_for_agent=None` by extracting system message from existing pool instance if available.

## Key Design Decisions

1. **session['history'] is NOT removed** — it's kept as a fallback for backward compatibility during transition. Phase 6 will remove it entirely.
2. **pool.add_message() takes Message objects** — not separate role/content parameters. Important to construct `Message(role=USER, content=...)`.
3. **Instance must exist before add_message** — silently drops messages if instance not found. Must check/create first.
4. **No more copy.deepcopy(session['history'])** — run_agent_thread_unified reads from pool.instances directly. The thread safety is handled by the ExecutionEngine's _setup_turn snapshot.
5. **session['generating'] management unchanged** — it's set to False on reset/load, and generating=True is passed as a parameter to build_state during generation. The 'done' broadcast sets it back to False via the final state from run_agent_thread_unified.

## Remaining Legacy Patterns (for Phase 6)
- `session['history']` still used in: _save_session_history, _load_session_history, get_session_history, get_agent_state, edit_message fallback, delete_messages fallback
- `agent_pool.instance_conversations` still used as a sync target in several places
- `_InstanceConversationMapping` shim still needed

## Reviewer Notes (Follow-up Tasks)
- Issue #3: `reset()` in agent_pool.py only clears `_instance_conversations`, not `self.instances` — stale sub-agent instances after reset. Phase 6 fix.
- Issue #6: `validate_message_pool` import from agent_orchestrator.py may need relocation to `agent_cascade.utils`. Phase 6 fix.
- Edit/delete message handlers have pre-existing thread safety gap (no `_compression_lock`) — not a regression, but worth fixing in Phase 6.