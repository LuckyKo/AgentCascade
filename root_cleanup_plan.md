# Root Cleanup Plan — AgentCascade_unified

**Generated:** 2026-05-28  
**Goal:** Move root-level .py files into `agent_cascade/`, delete legacy duplicates, and keep only startup/config on root.

---

## 1. FILES TO MIGRATE INTO agent_cascade/

### 1.1 **api_router.py** (~30KB) — `agent_cascade/api_router.py`
- **Status:** ACTIVE — imported by root `agent_pool.py:27` as `from api_router import APIRouter`
- **Imports from:** Pure stdlib + logging (no agent_cascade imports, no root imports)
- **Who imports it:** `agent_pool.py` (root), new `agent_cascade/agent_pool.py` references it via injection
- **Confidence to move:** HIGH — standalone module with no external deps on root files
- **Note:** The new architecture injects APIRouter rather than importing it directly, so both the old and new pool can coexist during transition

### 1.2 **telemetry.py** (~20KB) — `agent_cascade/telemetry.py` or `agent_cascade/utils/telemetry.py`
- **Status:** ACTIVE — imported by root `agent_pool.py:25` as `from telemetry import TelemetryCollector`
- **Imports from:** Pure stdlib (no agent_cascade imports, no root imports)
- **Who imports it:** `agent_pool.py` (root), new pool takes it via injection parameter
- **Confidence to move:** HIGH — standalone module with zero external deps

### 1.3 **soul_loader.py** (~8KB) — `agent_cascade/soul_loader.py` or `agent_cascade/utils/soul_loader.py`
- **Status:** ACTIVE — imported by `agent_factory.py:19`, `demo_soul_webui.py:11`
- **Imports from:** stdlib + yaml only (no agent_cascade, no root)
- **Who imports it:** `agent_factory.py` (root), `demo_soul_webui.py` (demo)
- **Confidence to move:** HIGH — small, self-contained utility

### 1.4 **agent_logger.py** (~21KB) — **DO NOT MIGRATE, DELETE** (see §3)
- **Status:** ACTIVE in root pool, but REPLACED by `agent_cascade/logger/agent_instance_logger.py`
- See DUPLICATE FILES section below

### 1.5 **api_server.py** (~130KB) — `agent_cascade/api_server.py`
- **Status:** ACTIVE — main FastAPI app, imported by `start_api_server.py:236`
- **Imports from agent_cascade/:** YES — 8 imports from agent_cascade subpackages (llm, utils, prompts, log, settings)
- **Imports from root:** Only `operation_manager` (line 56: security timeout constants)
- **Who imports it:** `start_api_server.py`, `start_multi_agent.py`
- **Confidence to move:** MEDIUM — the file is huge; needs careful import path updates. The new architecture already uses `agent_cascade.api_integration` as a bridge, so api_server should be in the package
- **Note:** When run as `__main__`, it imports from NEW `agent_cascade.agent_pool`. When called via `start_api_server.py`, the pool is created externally (old path)

### 1.6 **operation_manager.py** (~91KB) — `agent_cascade/operation_manager.py` or `agent_cascade/tools/operation_manager.py`
- **Status:** ACTIVE — imported by `api_server.py:56` (timeout constants), `agent_pool.py:53` (OperationManager class)
- **Imports from agent_cascade/:** YES — `settings`, `log`
- **Imports from root:** None
- **Who imports it:** `api_server.py`, `agent_pool.py` (root), test files
- **Confidence to move:** HIGH — already uses new arch imports, zero root file deps

### 1.7 **agent_factory.py** (~11KB) — `agent_cascade/agent_factory.py`
- **Status:** ACTIVE — imported by `start_api_server.py:38`, `start_multi_agent.py:43`
- **Imports from agent_cascade/:** YES — agents, log, tools (multiple)
- **Imports from root:** `soul_loader` (line 19)
- **Who imports it:** `start_api_server.py`, `start_multi_agent.py`
- **Confidence to move:** MEDIUM — must be moved AFTER soul_loader is in place

---

## 2. FILES TO DELETE

### 2.1 **file_manager.py** (~13KB) — ORPHANED, never imported
- **Status:** LEGACY — no imports anywhere in the codebase
- **Confidence to delete:** HIGH — verified with grep across all .py files

### 2.2 **message_client.py** (~5KB) — ORPHANED, never imported
- **Status:** LEGACY — no imports anywhere in the codebase
- **Confidence to delete:** HIGH — verified with grep across all .py files

---

## 3. FILES TO KEEP ON ROOT

### 3.1 **Startup Scripts** (entry points)
| File | Size | Purpose |
|------|------|---------|
| `start_api_server.py` | ~11KB | Main entry point for API server |
| `start_multi_agent.py` | ~14KB | Multi-agent Gradio entry point |
| `run_server.py` | ~6KB | Legacy server runner (imports from agent_server, not root) |

### 3.2 **Demo Scripts**
| File | Size | Purpose |
|------|------|---------|
| `demo_webui.py` | ~6KB | Quick WebUI demo with LM Studio |
| `demo_soul_webui.py` | ~5KB | Soul-based WebUI demo |

### 3.3 **Configuration / Build**
| File | Size | Purpose |
|------|------|---------|
| `setup.py` | ~3KB | Package build script |
| `requirements.txt` | ~2KB | Dependencies |
| `MANIFEST.in` | ~100B | Package manifest |

### 3.4 **Profiling / Debug Utilities** (dev tools)
| File | Size | Purpose |
|------|------|---------|
| `profile_grep_ops.py` | ~17KB | Grep performance profiler |
| `profile_streaming.py` | ~4KB | Streaming performance profiler |
| `profile_server.bat` | ~1.3KB | Windows server profiler |
| `check_lm_studio.py` | ~0.5KB | LM Studio connectivity check |
| `check_quotes.py` | ~0.7KB | Quote counting utility |

### 3.5 **Documentation / Planning** (non-Python, already categorized)
- All `.md` files: DESIGN_REWRITE.md, README*.md, phase*.md, etc. — keep on root

---

## 4. DUPLICATE FILES

### 4.1 **agent_pool.py** (~53KB root vs ~60KB in agent_cascade/)
| | Root `agent_pool.py` | `agent_cascade/agent_pool.py` |
|--|---------------------|------------------------------|
| Size | 53KB (1105 lines) | 60KB (1318 lines) |
| Class | `AgentPool` (god-object, ~25 attrs) | `AgentPool` (lean coordinator, delegates to focused managers) |
| Logger used | Root `agent_logger.AgentInstanceLogger` | `agent_cascade.logger.agent_instance_logger.AgentInstanceLogger` |
| Architecture | Old monolithic | New Phase 1 rewrite |
| Used by | `start_api_server.py`, `start_multi_agent.py` (old path) | `api_server.py --main__` (new path) |

**Verdict:** The root version is the OLD code. During transition, both are needed. After `start_api_server.py` and `start_multi_agent.py` are updated to use the new pool, root `agent_pool.py` can be DELETED.

### 4.2 **agent_logger.py** (~21KB root vs ~21KB in agent_cascade/logger/)
| | Root `agent_logger.py` | `agent_cascade/logger/agent_instance_logger.py` |
|--|----------------------|------------------------------------------------|
| Class | `AgentInstanceLogger` | `AgentInstanceLogger` |
| Used by | Root `agent_pool.py:24` | New `agent_cascade/agent_pool.py:1192` |
| Comment in new | — | "Ported from agent_logger.py for the new unified architecture" |

**Verdict:** The root version is LEGACY. Delete after root `agent_pool.py` is removed (it's the only consumer).

---

## 5. RECOMMENDED MIGRATION ORDER

### Phase A: Clean Orphans (Zero Risk)
1. **Delete `file_manager.py`** — never imported, zero impact
2. **Delete `message_client.py`** — never imported, zero impact

### Phase B: Move Standalone Modules (Low Risk)
3. **Move `api_router.py` → `agent_cascade/api_router.py`**
   - Update import in root `agent_pool.py:27`: `from api_router import APIRouter` → `from agent_cascade.api_router import APIRouter`
   - The new pool injects it, so no change needed there

4. **Move `telemetry.py` → `agent_cascade/telemetry.py`** (or `agent_cascade/utils/`)
   - Update import in root `agent_pool.py:25`: `from telemetry import TelemetryCollector` → `from agent_cascade.telemetry import TelemetryCollector`

5. **Move `soul_loader.py` → `agent_cascade/soul_loader.py`** (or `agent_cascade/utils/`)
   - Update import in root `agent_factory.py:19`: `from soul_loader import create_agent_from_soul` → `from agent_cascade.soul_loader import create_agent_from_soul`

### Phase C: Move Core Infrastructure (Medium Risk)
6. **Move `operation_manager.py` → `agent_cascade/operation_manager.py`**
   - Update import in `api_server.py:56`: `from operation_manager import ...` → `from agent_cascade.operation_manager import ...`
   - Update import in root `agent_pool.py:53`: `from operation_manager import OperationManager` → `from agent_cascade.operation_manager import OperationManager`

7. **Move `api_router.py`** (already done in step 3) — ensure both pools can find it

### Phase D: Move the Big File (Higher Risk)
8. **Move `api_server.py` → `agent_cascade/api_server.py`**
   - Update imports in `start_api_server.py:236`: `from api_server import create_app` → `from agent_cascade.api_server import create_app`
   - This is the biggest change — 130KB file, many internal references

9. **Move `agent_factory.py` → `agent_cascade/agent_factory.py`**
   - Must be done AFTER soul_loader migration (step 5)
   - Update imports in `start_api_server.py:38`, `start_multi_agent.py:43`

### Phase E: Kill the Old Pool (Highest Risk — Requires Testing)
10. **Update `start_api_server.py` and `start_multi_agent.py`** to use new pool:
    - Change `from agent_pool import AgentPool` → `from agent_cascade.agent_pool import AgentPool`
    - Change `from agent_factory import load_orchestrator_agent` → `from agent_cascade.agent_factory import load_orchestrator_agent`
    - Test thoroughly — the new pool has a different constructor signature

11. **Delete root `agent_pool.py`** — old god-object, replaced by new lean pool

12. **Delete root `agent_logger.py`** — only consumer was old agent_pool

---

## 6. DEPENDENCY GRAPH (Current State)

```
start_api_server.py ──→ agent_pool (root) ──┬── agent_logger (root) ← DELETE with pool
                                            ├── telemetry (root)     ← MOVE first
                                            ├── api_router (root)    ← MOVE first
                                            └── operation_manager (root) ← MOVE before api_server

start_multi_agent.py  ──→ same as above

api_server.py ────────→ operation_manager (root) ← MOVE before api_server
                   ──→ agent_cascade.* (8 imports) ← already on new path

agent_factory.py ────→ soul_loader (root) ← MOVE first
                   ──→ agent_cascade.* (multiple) ← already on new path

api_server.__main__ → agent_cascade.agent_pool (new) ← already on new path
```

---

## 7. IMPORTANT NOTES

- **Dual-path reality:** The system currently has TWO code paths:
  - **Old path:** `start_api_server.py` → root `agent_pool` → root `agent_logger` + root `telemetry` + root `api_router` + root `operation_manager`
  - **New path:** `api_server.py --main__` → `agent_cascade.agent_pool` → `agent_cascade.logger.agent_instance_logger`

- **Migration strategy:** Move shared modules first (telemetry, api_router, soul_loader, operation_manager), then move the big files (api_server, agent_factory), and ONLY THEN switch start scripts to use new pool. This keeps both paths working during transition.

- **Test coverage:** The test files in `tests/` import from root `operation_manager`. They'll need updates too when operation_manager moves.

- **agent_cascade/__init__.py** should be updated to export the moved modules so existing imports continue to work as compatibility aliases if needed.