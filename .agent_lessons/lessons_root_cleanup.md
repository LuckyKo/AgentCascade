# Lessons — AgentCascade Root Cleanup

## Key Discoveries

### Dual-Path Architecture (Critical)
The system runs TWO code paths simultaneously:
1. **Old path:** `start_api_server.py` → root `agent_pool.AgentPool` (god-object, 53KB)
2. **New path:** `api_server.py --main__` → `agent_cascade.agent_pool.AgentPool` (lean, 60KB)

This means you can't just delete the old pool — start scripts still depend on it.

### Import Chain (Old Path)
```
start_api_server.py / start_multi_agent.py
  → agent_pool (root)
    → agent_logger (root)       [replaced by agent_cascade/logger/agent_instance_logger]
    → telemetry (root)          [no replacement in agent_cascade]
    → api_router (root)         [no replacement in agent_cascade]
    → operation_manager (root)  [no replacement in agent_cascade]
  → agent_factory (root)
    → soul_loader (root)        [no replacement in agent_cascade]
```

### Orphaned Files (Safe to Delete Immediately)
- `file_manager.py` — never imported anywhere, verified by grep
- `message_client.py` — never imported anywhere, verified by grep

### api_server.py Import Status
- Already imports 8 things from `agent_cascade/` subpackages (llm, utils, prompts, log, settings)
- Only imports ONE thing from root: `operation_manager` (security timeout constants)
- When run as `__main__`, uses NEW pool (`agent_cascade.agent_pool`)
- When called via `create_app()` from start scripts, receives pool as parameter

### agent_logger.py is Truly Legacy
- Root version used only by old `agent_pool.py:24`
- New version at `agent_cascade/logger/agent_instance_logger.py` (explicitly says "Ported from agent_logger.py")
- Delete root version when old pool goes away

## Migration Rules

1. **Move shared modules FIRST** — telemetry, api_router, soul_loader have zero root-file dependencies
2. **Move operation_manager BEFORE api_server** — api_server imports from it
3. **Move api_server LAST among core files** — it's the biggest (130KB) and most connections
4. **Only after start scripts are updated to new pool → delete old pool + old logger**

## File Sizes for Reference
- `api_server.py`: 130KB (2369 lines) — biggest, highest risk
- `operation_manager.py`: 91KB (1844 lines) — big but self-contained
- `agent_pool.py` (root): 53KB (1105 lines) — old, will be deleted eventually
- `agent_cascade/agent_pool.py`: 60KB (1318 lines) — new, the replacement
- `api_router.py`: 30KB (644 lines) — standalone
- `telemetry.py`: 20KB (497 lines) — standalone
- `agent_logger.py` (root): 21KB (428 lines) — legacy
- `agent_factory.py`: 11KB (263 lines) — depends on soul_loader