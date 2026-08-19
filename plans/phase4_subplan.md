# Phase 4 Detailed Sub-plan — Selective-extraction refactor

**Date:** 2026-08-19  
**Author:** Maine (orchestrator), drafted by phase4_plan  
**Status:** READY for implementation  
**Scope:** `agent_cascade/api_server.py` (Phase 4a) and `agent_cascade/ws_handlers.py` (Phase 4b)

## 0. Executive Summary

Phase 4 is a **selective-extraction** refactor that moves a small number of pure helper functions from two large modules into dedicated helper modules, while preserving the existing import surface for both production and test code. The refactor follows the same pattern established in Phases 1–3 (facade re-exports, lazy imports, no functional changes).

**Phase 4a (api_server.py):** Extract 4 pure functions into two helper modules:
- `path_security.py`: `_is_path_allowed`, `_get_allowed_file_roots`
- `content_parse.py`: `_parse_multimodal_content`, `_extract_system_message`

**Phase 4b (ws_handlers.py):** Extract 2 pure functions into one helper module:
- `ws_helpers.py`: `_clear_caches_safely`, `_validate_disabled_tools`

All extracted symbols remain importable from their original modules via re-exports. The critical import cycle between `ws_handlers` and `api_server` (which currently works via lazy imports) is preserved by updating `ws_handlers` to import the moved helpers from their new locations, not from `api_server`.

---

## 1. Phase 4a — `api_server.py` Extraction

### 1.1 Symbol Inventory (api_server.py)

#### Module-level functions (to be extracted)
| Symbol | Line | Current Location | Target Location | Reason |
|--------|------|------------------|-----------------|--------|
| `_extract_system_message` | 89 | api_server.py | content_parse.py | Pure function, no closure dependencies |
| `_parse_multimodal_content` | 109 | api_server.py | content_parse.py | Pure function, no closure dependencies |
| `_validate_disabled_tools` | 145 | api_server.py | **KEEP IN PLACE** | Used by ws_handlers.py; moving it would require updating ws_handlers imports (Phase 4b scope) |
| `_is_generating` | 174 | api_server.py | **KEEP IN PLACE** | Module-level, uses `session_lock` |
| `_start_generation` | 180 | api_server.py | **KEEP IN PLACE** | Module-level, uses `session_lock` |
| `_stop_generation` | 189 | api_server.py | **KEEP IN PLACE** | Module-level, uses `session_lock` |
| `_signal_stop` | 195 | api_server.py | **KEEP IN PLACE** | Module-level, uses `session_lock` |
| `_set_generating_true` | 201 | api_server.py | **KEEP IN PLACE** | Module-level, uses `session_lock` |
| `_get_allowed_file_roots` | 212 | api_server.py | path_security.py | Pure function, no closure dependencies |
| `_is_path_allowed` | 229 | api_server.py | path_security.py | Pure function, no closure dependencies |
| `create_app` | 277 | api_server.py | **KEEP IN PLACE** | Main factory function, cohesive |

#### Module-level constants (to be extracted or kept)
| Symbol | Line | Current Location | Target Location | Reason |
|--------|------|------------------|-----------------|--------|
| `session_lock` | 79 | api_server.py | **KEEP IN PLACE** | Used by session helper functions and create_app internals |
| `LLM_CONFIG_KEYS` | 82 | api_server.py | **KEEP IN PLACE** | Used by config_handlers.py; moving would break import chain |
| `_SENSITIVE_FILENAMES` | 209 | api_server.py | path_security.py | Used only by `_is_path_allowed` (moving together) |

#### Nested functions (inside `create_app`)
These are defined inside `create_app` and are **NOT** candidates for extraction (they rely on the factory's closure variables):
- `_load_session_history` (line 339)
- `_get_send_queue` (line 454)
- `get_active_stack` (line 476)
- `get_approvals` (line 481)
- `_safe_get_telemetry` (line 486)
- `_broadcast_state` (line 495)
- `_clear_caches_safely` (line 506) — **NOTE:** This is a nested function, not the module-level one in ws_handlers.py
- `build_state` (line 514)
- `build_stream_update` (line 599)
- `broadcast` (line 663)
- `run_agent_thread` (line 684)
- `startup` (line 749)
- `_sender_loop` (line 782)
- `_approval_loop` (line 801)
- `api_get_keys` (line 824)
- `api_handshake` (line 832)
- `api_inject_message` (line 858)
- `api_get_status` (line 924)
- `api_list_agents` (line 945)
- `api_get_state` (line 958)
- `api_reset` (line 966)
- `api_approve` (line 994)
- `api_reject` (line 1001)
- `api_resume_all` (line 1008)
- `api_list_sessions` (line 1016)
- `api_serve_file` (line 1056)
- `api_telemetry` (line 1081)
- `api_telemetry_export` (line 1092)
- `api_list_endpoints` (line 1103)
- `api_add_endpoint` (line 1110)
- `api_update_endpoint` (line 1133)
- `api_delete_endpoint` (line 1145)
- `api_set_priorities` (line 1157)
- `api_bulk_update_endpoints` (line 1172)
- `start_gen` (line 1185)
- `ws_chat` (line 1191)
- `find_file` (line 1243)
- `serve_index` (line 1277)
- `serve_static` (line 1281)
- `parse_document` (line 1292)

#### Main block
- `if __name__ == "__main__":` (line 1330) — **KEEP IN PLACE**

---

### 1.2 Dependency Check for Extracted Symbols

#### `_extract_system_message` (line 89)
**Current dependencies:**
- Module-level imports: None directly used in function body
- Function uses only: `hasattr`, `str`, `agent` parameter
- **No closure dependencies** (not nested in `create_app`)
- **No module-global dependencies** (no references to `session_lock`, `LLM_CONFIG_KEYS`, etc.)

**Analysis:**
✅ **SELF-CONTAINED** — Can be safely moved to `content_parse.py`

#### `_parse_multimodal_content` (line 109)
**Current dependencies:**
- Module-level imports: 
  - `_IMAGE_DATA_PATTERN` (imported from `agent_cascade.utils.thinking_block` at line 63)
  - `save_image_from_data_uri`, `MediaStorageError` (imported from `agent_cascade.utils.media_utils` at line 58)
- Function uses: `_IMAGE_DATA_PATTERN`, `save_image_from_data_uri`, `MediaStorageError`
- **No closure dependencies** (not nested in `create_app`)
- **No module-global dependencies**

**Analysis:**
✅ **SELF-CONTAINED** — Can be safely moved to `content_parse.py`
- **REQUIRES:** Import statements for `_IMAGE_DATA_PATTERN`, `save_image_from_data_uri`, `MediaStorageError` in `content_parse.py`

#### `_get_allowed_file_roots` (line 212)
**Current dependencies:**
- Module-level imports:
  - `DEFAULT_WORKSPACE` (imported from `agent_cascade.settings` at line 52)
- Function uses: `Path`, `DEFAULT_WORKSPACE`, lazy imports for `make_instance_dir` and `DEFAULT_WORKSPACE` (redundant)
- **No closure dependencies** (not nested in `create_app`)
- **No module-global dependencies**

**Analysis:**
✅ **SELF-CONTAINED** — Can be safely moved to `path_security.py`
- **REQUIRES:** Import statement for `DEFAULT_WORKSPACE` in `path_security.py`

#### `_is_path_allowed` (line 229)
**Current dependencies:**
- Module-level imports:
  - `os` (standard library, already imported)
  - `Path` (from pathlib, already imported)
- Function uses: `unquote` (lazy import), `Path`, `os.sep`, `_SENSITIVE_FILENAMES`, `_get_allowed_file_roots` (the function above)
- **No closure dependencies** (not nested in `create_app`)
- **Module-global dependency:** `_SENSITIVE_FILENAMES` (defined at line 209)

**Analysis:**
✅ **SELF-CONTAINED** — Can be safely moved to `path_security.py`
- **REQUIRES:** `_SENSITIVE_FILENAMES` constant (moving together)
- **REQUIRES:** `_get_allowed_file_roots` function (moving together)

---

### 1.3 New Helper Modules

#### `agent_cascade/content_parse.py`
```python
"""Content parsing utilities for multimodal messages and system message extraction."""

from agent_cascade.utils.thinking_block import _IMAGE_DATA_PATTERN
from agent_cascade.utils.media_utils import save_image_from_data_uri, MediaStorageError


def _extract_system_message(agent) -> str:
    """Extract system message content from an agent.
    
    Priority: base_system_message > system_message > llm.cfg['system'].
    Returns '' (empty string) if no system message found.
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
    return ''


def _parse_multimodal_content(text):
    """
    Parse markdown images ![alt](data:...) and return a list of content items.
    If no images are found, returns the original text.

    Saves base64 data URIs to media storage as paths; falls back to inline
    base64 if media storage fails.
    """
    from agent_cascade.log import logger

    parts = []
    last_end = 0
    for match in _IMAGE_DATA_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_end:
            parts.append({'text': text[last_end:start]})
        alt, url = match.groups()
        try:
            media_path = save_image_from_data_uri(url)
            parts.append({'image': media_path})  # Path instead of base64
        except MediaStorageError as e:
            # Fallback to inline base64 if media storage fails
            logger.warning(f"Media storage failed for user image, keeping inline base64: {e}")
            parts.append({'image': url})
        last_end = end
    
    if last_end < len(text):
        parts.append({'text': text[last_end:]})

    if not parts:
        return text
    if len(parts) == 1 and 'text' in parts[0]:
        return parts[0]['text']
    return parts
```

#### `agent_cascade/path_security.py`
```python
"""Path security utilities for /api/file endpoint."""

import os
from pathlib import Path
from typing import List

from agent_cascade.settings import DEFAULT_WORKSPACE

# Sensitive filenames that should never be served
_SENSITIVE_FILENAMES = {".env", ".gitconfig", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}


def _get_allowed_file_roots() -> List[Path]:
    """Return the list of directories that /api/file is allowed to serve from.

    Includes:
      - Media directory (<workspace>/logs/media/ or <workspace>/logs_<instance>/media/)
      - Workspace root (DEFAULT_WORKSPACE)
    """
    from agent_cascade.instance_id import make_instance_dir
    from agent_cascade.settings import DEFAULT_WORKSPACE

    workspace_root = Path(DEFAULT_WORKSPACE)
    base_logs = str(workspace_root / "logs")
    instance_logs = make_instance_dir(base_logs)
    media_dir = Path(instance_logs) / "media"
    return [media_dir, workspace_root]


def _is_path_allowed(path: str) -> bool:
    """Check if a file path is allowed to be served via /api/file.

    URL-decodes and resolves the path, then verifies it falls under an allowed root
    using prefix matching with os.sep to avoid partial directory name matches.

    Args:
        path: The raw path string from the request (may be URL-encoded).

    Returns:
        True if the path is safe to serve, False otherwise.
    """
    from urllib.parse import unquote
    from agent_cascade.log import logger

    # URL-decode the path
    decoded = unquote(path)

    try:
        resolved = Path(decoded).resolve()
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to resolve path for /api/file: {decoded} ({e})")
        return False

    # Check filename-level restrictions
    basename = resolved.name.lower()

    # Block hidden files/dirs (starting with dot)
    if basename.startswith("."):
        return False

    # Block known sensitive filenames
    if basename in _SENSITIVE_FILENAMES:
        return False

    # Check that path is under an allowed root
    allowed_roots = _get_allowed_file_roots()
    resolved_str = str(resolved) + os.sep

    for root in allowed_roots:
        root_str = str(root.resolve()) + os.sep
        if resolved_str.startswith(root_str):
            return True

    logger.warning(f"Path outside allowed roots for /api/file: {decoded} (resolved: {resolved})")
    return False
```

---

### 1.4 Facade Re-exports (api_server.py)

After moving the functions, add these imports at the top of `api_server.py` (after the existing imports):

```python
# Re-exports for backward compatibility (Phase 4a refactor)
from agent_cascade.content_parse import (
    _extract_system_message,
    _parse_multimodal_content,
)
from agent_cascade.path_security import (
    _is_path_allowed,
    _get_allowed_file_roots,
    _SENSITIVE_FILENAMES,
)
```

**Remove** from `api_server.py`:
- Lines 89–107: `def _extract_system_message(agent) -> str:`
- Lines 109–142: `def _parse_multimodal_content(text):`
- Lines 209: `_SENSITIVE_FILENAMES = {...}`
- Lines 212–226: `def _get_allowed_file_roots() -> List[Path]:`
- Lines 229–274: `def _is_path_allowed(path: str) -> bool:`

**Keep** in `api_server.py`:
- Line 79: `session_lock = threading.Lock()`
- Line 82: `LLM_CONFIG_KEYS = frozenset({...})`
- Line 145: `def _validate_disabled_tools(ui_cfg: Dict[str, Any]) -> None:`
- Lines 174–204: Session helper functions (`_is_generating`, `_start_generation`, `_stop_generation`, `_signal_stop`, `_set_generating_true`)
- Line 277: `def create_app(agents, agent_pool, config=None, auto_security=True):`
- All nested functions and main block

---

## 2. Phase 4b — `ws_handlers.py` Extraction

### 2.1 Symbol Inventory (ws_handlers.py)

#### Module-level functions (to be extracted)
| Symbol | Line | Current Location | Target Location | Reason |
|--------|------|------------------|-----------------|--------|
| `_clear_caches_safely` | 16 | ws_handlers.py | ws_helpers.py | Pure function, no closure dependencies |
| `_validate_disabled_tools` | 26 | ws_handlers.py | ws_helpers.py | Pure function, no closure dependencies |

#### Class (to be kept)
| Symbol | Line | Current Location | Target Location | Reason |
|--------|------|------------------|-----------------|--------|
| `WsMessageHandler` | 42 | ws_handlers.py | **KEEP IN PLACE** | Main handler class, 31+ methods |

**Note:** `WsMessageHandler` is a cohesive dispatch-table class. Splitting it into mixins would be high-risk for little gain. All 31+ handler methods stay in `WsMessageHandler`.

---

### 2.2 Dependency Check for Extracted Symbols

#### `_clear_caches_safely` (line 16)
**Current dependencies:**
- Module-level imports: None directly used in function body
- Function uses: `from agent_cascade.api_integration import _clear_performance_caches` (lazy import), `from agent_cascade.log import logger` (lazy import)
- **No closure dependencies** (not nested in any function)
- **No module-global dependencies**

**Analysis:**
✅ **SELF-CONTAINED** — Can be safely moved to `ws_helpers.py`

#### `_validate_disabled_tools` (line 26)
**Current dependencies:**
- Module-level imports: None directly used in function body
- Function uses: `normalize_disabled_tools`, `validate_tool_names` (lazy imports), `TOOL_REGISTRY` (lazy import), `RUNTIME_REGISTERED_TOOLS` (lazy import)
- **No closure dependencies** (not nested in any function)
- **No module-global dependencies**

**Analysis:**
✅ **SELF-CONTAINED** — Can be safely moved to `ws_helpers.py`

---

### 2.3 New Helper Module

#### `agent_cascade/ws_helpers.py`
```python
"""WebSocket handler helper utilities."""


def _clear_caches_safely() -> None:
    """Clear performance caches with error suppression."""
    try:
        from agent_cascade.api_integration import _clear_performance_caches
        _clear_performance_caches()
    except Exception as e:
        from agent_cascade.log import logger
        logger.debug(f"Cache clearing failed (non-critical): {e}")


def _validate_disabled_tools(ui_cfg: dict) -> None:
    """Validate disabled_tools in a generate_cfg dict against the tool registry."""
    from agent_cascade.utils.disabled_tools import normalize_disabled_tools, validate_tool_names
    from agent_cascade.tools.base import TOOL_REGISTRY
    from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS

    if 'disabled_tools' in ui_cfg and ui_cfg['disabled_tools']:
        dt = ui_cfg['disabled_tools']
        known = set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS
        if isinstance(dt, dict):
            for tools in dt.values():
                validate_tool_names(normalize_disabled_tools(tools), known_tools=known)
        else:
            validate_tool_names(normalize_disabled_tools(dt), known_tools=known)
```

---

### 2.4 Facade Re-exports (ws_handlers.py)

After moving the functions, add these imports at the top of `ws_handlers.py` (after the existing imports):

```python
# Re-exports for backward compatibility (Phase 4b refactor)
from agent_cascade.ws_helpers import (
    _clear_caches_safely,
    _validate_disabled_tools,
)
```

**Remove** from `ws_handlers.py`:
- Lines 16–23: `def _clear_caches_safely() -> None:`
- Lines 26–39: `def _validate_disabled_tools(ui_cfg: dict) -> None:`

**Keep** in `ws_handlers.py`:
- Line 42: `class WsMessageHandler:` and all its methods

---

## 3. Import Cycle Analysis (ws_handlers ↔ api_server)

### 3.1 Current Import Structure

#### api_server.py imports from ws_handlers.py
**Line 323 (inside `create_app` function body, lazy import):**
```python
from agent_cascade.ws_handlers import WsMessageHandler
```

**Context:** This is a lazy import inside the `create_app` function, so it's only executed when `create_app` is called, not at module load time.

#### ws_handlers.py imports from api_server.py
**Three locations (all lazy imports inside function bodies):**

1. **Line 233 (inside `handle_message` method):**
```python
from agent_cascade.api_server import _parse_multimodal_content
```

2. **Line 241 (inside `handle_message` method):**
```python
from agent_cascade.api_server import _parse_multimodal_content, _extract_system_message
```

3. **Line 1021 (inside `handle_edit_message` method):**
```python
from agent_cascade.api_server import _parse_multimodal_content, COMPRESSION_MARKER, _CONTEXT_SUMMARY_RE
```

**Context:** All three are lazy imports inside method bodies, so they're only executed when those handlers are called.

### 3.2 How the Cycle Works Today

The import cycle is **safe** because:
1. **api_server.py** loads first (it's the entry point or imported by start scripts)
2. When `api_server.py` loads, it defines all module-level functions and constants
3. When `create_app()` is called, it imports `WsMessageHandler` from `ws_handlers.py`
4. When `WsMessageHandler` methods execute, they import symbols from `api_server.py` (which is already fully loaded)

**Key insight:** The lazy imports inside function bodies break the circular dependency at module load time.

### 3.3 Impact of Phase 4a on the Cycle

**After Phase 4a:**
- `_parse_multimodal_content`, `_extract_system_message` move to `content_parse.py`
- api_server.py re-exports them via `from agent_cascade.content_parse import _parse_multimodal_content, _extract_system_message`

**Impact on ws_handlers.py imports:**
- The lazy imports in ws_handlers.py (lines 233, 241, 1021) still work unchanged
- They import from `agent_cascade.api_server`, which now re-exports these symbols
- **NO CHANGE NEEDED in ws_handlers.py for Phase 4a**

### 3.4 Impact of Phase 4b on the Cycle

**After Phase 4b:**
- `_clear_caches_safely`, `_validate_disabled_tools` move to `ws_helpers.py`
- ws_handlers.py re-exports them via `from agent_cascade.ws_helpers import _clear_caches_safely, _validate_disabled_tools`

**Impact on api_server.py:**
- api_server.py does NOT import these functions from ws_handlers.py
- **NO CHANGE NEEDED in api_server.py for Phase 4b**

### 3.5 Conclusion: No New Cycle Introduced

✅ **The existing lazy-import cycle is preserved and remains safe.**

The refactor moves symbols to new modules but maintains the re-export pattern, so all existing imports continue to work without modification.

---

## 4. Production Code Import Inventory

### 4.1 Imports from `agent_cascade.api_server`

#### Production code (agent_cascade/)
| File | Line | Import | Purpose |
|------|------|--------|---------|
| `ws_handlers.py` | 233 | `_parse_multimodal_content` | Parse multimodal content in message handler |
| `ws_handlers.py` | 241 | `_parse_multimodal_content`, `_extract_system_message` | Parse content and extract system message |
| `ws_handlers.py` | 1021 | `_parse_multimodal_content`, `COMPRESSION_MARKER`, `_CONTEXT_SUMMARY_RE` | Parse content and check compression markers |
| `config_handlers.py` | 7 | `LLM_CONFIG_KEYS` (mentioned in docstring) | Documentation reference |

**Note:** `COMPRESSION_MARKER` and `_CONTEXT_SUMMARY_RE` are imported from `agent_cascade.prompts.dna` and `agent_cascade.utils.thinking_block` respectively, but they are re-exported via api_server.py's module-level imports (lines 55, 62).

#### Start scripts
| File | Line | Import | Purpose |
|------|------|--------|---------|
| `start_api_server.py` | 140 | `create_app` | Create FastAPI app |
| `start_multi_agent.py` | 122 | `create_app` | Create FastAPI app |

### 4.2 Imports from `agent_cascade.ws_handlers`

#### Production code (agent_cascade/)
| File | Line | Import | Purpose |
|------|------|--------|---------|
| `api_server.py` | 323 | `WsMessageHandler` | WebSocket message handler (lazy import) |

**Note:** No other production code imports from ws_handlers.py.

### 4.3 Symbols That Must Remain Importable

#### From `agent_cascade.api_server`
**Functions:**
- `create_app` (main factory)
- `_extract_system_message` (re-exported from content_parse)
- `_parse_multimodal_content` (re-exported from content_parse)
- `_validate_disabled_tools` (stays in api_server)
- `_is_path_allowed` (re-exported from path_security)
- `_get_allowed_file_roots` (re-exported from path_security)
- `_is_generating`, `_start_generation`, `_stop_generation`, `_signal_stop`, `_set_generating_true` (session helpers)

**Constants:**
- `session_lock`
- `LLM_CONFIG_KEYS`
- `COMPRESSION_MARKER` (re-exported via api_server's imports)
- `_CONTEXT_SUMMARY_RE` (re-exported via api_server's imports)
- `_SENSITIVE_FILENAMES` (re-exported from path_security)

#### From `agent_cascade.ws_handlers`
**Classes:**
- `WsMessageHandler`

**Functions (re-exported from ws_helpers):**
- `_clear_caches_safely`
- `_validate_disabled_tools`

---

## 5. Test Code Import Inventory

### 5.1 Imports from `agent_cascade.api_server`

| File | Line | Import | Purpose |
|------|------|--------|---------|
| `test_media_storage.py` | 359, 374, 388, 396, 403, 422, 436 | `_is_path_allowed`, `_get_allowed_file_roots` | Test path security functions |
| `test_media_storage.py` | 465, 499, 550 | `_parse_multimodal_content` | Test multimodal parsing |
| `test_media_storage.py` | 521 | `api_server` (module) | Access module for testing |
| `test_media_storage.py` | 529 | `api_server._parse_multimodal_content` | Call via module attribute |
| `test_api_endpoints.py` | 148 | `create_app` | Create test app |
| `test_e2e_agent_calls.py` | 333 | `create_app` | Create test app |
| `test_startup_integration.py` | 115 | `create_app` | Create test app |
| `test_unified_system.py` | 33, 324 | `create_app` | Create test app |
| `test_security_handler_deadlock_fixes.py` | 143 | `api_server` (module) | Inspect module source |
| `test_imports.py` | 37 | `_validate_disabled_tools` | Test import works |

### 5.2 Imports from `agent_cascade.ws_handlers`

**None found** — No test files directly import from ws_handlers.py.

### 5.3 Test Changes Required

**✅ NO TEST CHANGES REQUIRED for Phase 4a or 4b.**

**Reasoning:**
1. All test imports use the original module paths (`agent_cascade.api_server`, `agent_cascade.ws_handlers`)
2. The facade re-exports maintain backward compatibility
3. Tests import symbols, not their internal locations
4. The lazy-import cycle is preserved, so all imports resolve correctly

**Verification:**
- Tests importing `_is_path_allowed` → still works (re-exported from path_security)
- Tests importing `_parse_multimodal_content` → still works (re-exported from content_parse)
- Tests importing `create_app` → still works (stays in api_server)
- Tests importing `WsMessageHandler` → still works (stays in ws_handlers)

---

## 6. Circular-Import / Ordering Risks

### 6.1 Module Load Order Analysis

#### New module dependencies:

**content_parse.py:**
- Imports: `agent_cascade.utils.thinking_block`, `agent_cascade.utils.media_utils`
- **No circular dependencies** (these are utility modules with no reverse imports)

**path_security.py:**
- Imports: `agent_cascade.settings` (for DEFAULT_WORKSPACE)
- **No circular dependencies** (settings is a config module)

**ws_helpers.py:**
- Imports: None at module level (all lazy)
- **No circular dependencies**

#### Updated module dependencies:

**api_server.py (after refactor):**
- New imports: `agent_cascade.content_parse`, `agent_cascade.path_security`
- Existing imports: `agent_cascade.ws_handlers` (lazy, inside create_app)
- **No circular dependencies** (content_parse and path_security don't import api_server)

**ws_handlers.py (after refactor):**
- New imports: `agent_cascade.ws_helpers`
- Existing imports: `agent_cascade.api_server` (lazy, inside methods)
- **No circular dependencies** (ws_helpers doesn't import ws_handlers or api_server)

### 6.2 Import Order Requirements

**Safe import order:**
1. `agent_cascade.utils.thinking_block` (loaded by content_parse)
2. `agent_cascade.utils.media_utils` (loaded by content_parse)
3. `agent_cascade.settings` (loaded by path_security)
4. `agent_cascade.content_parse` (loaded by api_server)
5. `agent_cascade.path_security` (loaded by api_server)
6. `agent_cascade.ws_helpers` (loaded by ws_handlers)
7. `agent_cascade.api_server` (loaded by start scripts)
8. `agent_cascade.ws_handlers` (loaded lazily by api_server.create_app)

**No ordering constraints** — All new modules are leaf nodes in the dependency graph.

### 6.3 Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Circular import at module load | Low | High | All new modules are leaf nodes; verified no reverse imports |
| Broken re-export chain | Low | High | Facade re-exports tested in test suite; import smoke test |
| Lazy import timing issue | Very Low | Medium | Cycle already works with lazy imports; refactor doesn't change this |
| Test failures from moved symbols | Very Low | Medium | All symbols remain importable from original paths; full test suite gate |

---

## 7. Implementation Checklist

### Phase 4a (api_server.py)

#### Step 1: Create helper modules
- [ ] Create `agent_cascade/content_parse.py` with `_extract_system_message`, `_parse_multimodal_content`
- [ ] Create `agent_cascade/path_security.py` with `_get_allowed_file_roots`, `_is_path_allowed`, `_SENSITIVE_FILENAMES`

#### Step 2: Update api_server.py
- [ ] Add re-export imports at top of api_server.py
- [ ] Remove `_extract_system_message` function (lines 89–107)
- [ ] Remove `_parse_multimodal_content` function (lines 109–142)
- [ ] Remove `_SENSITIVE_FILENAMES` constant (line 209)
- [ ] Remove `_get_allowed_file_roots` function (lines 212–226)
- [ ] Remove `_is_path_allowed` function (lines 229–274)

#### Step 3: Verify
- [ ] Run import smoke test: `python -c "from agent_cascade.api_server import _parse_multimodal_content, _is_path_allowed, create_app"`
- [ ] Run test suite: `pytest tests/test_media_storage.py -v`
- [ ] Run full test suite: `pytest -v`

### Phase 4b (ws_handlers.py)

#### Step 1: Create helper module
- [ ] Create `agent_cascade/ws_helpers.py` with `_clear_caches_safely`, `_validate_disabled_tools`

#### Step 2: Update ws_handlers.py
- [ ] Add re-export imports at top of ws_handlers.py
- [ ] Remove `_clear_caches_safely` function (lines 16–23)
- [ ] Remove `_validate_disabled_tools` function (lines 26–39)

#### Step 3: Verify
- [ ] Run import smoke test: `python -c "from agent_cascade.ws_handlers import WsMessageHandler, _clear_caches_safely"`
- [ ] Run test suite: `pytest tests/test_ws_message_queue.py -v`
- [ ] Run full test suite: `pytest -v`

### Final Verification

- [ ] Run full test suite: `pytest -v` (expect 1619+ passed, 1 env failure, 1 skipped)
- [ ] Verify no circular imports: `python -c "import agent_cascade.api_server; import agent_cascade.ws_handlers; print('OK')"`
- [ ] Review diff for pure-move compliance (no logic changes)

---

## 8. Reviewer Checklist

### Pre-implementation review
- [ ] Verify symbol inventory matches actual code
- [ ] Confirm dependency analysis (no hidden closure/module-global deps)
- [ ] Verify import cycle analysis is correct
- [ ] Confirm no test changes needed

### Post-implementation review
- [ ] Verify all symbols moved verbatim (no logic changes)
- [ ] Verify facade re-exports are correct
- [ ] Verify import smoke tests pass
- [ ] Verify test suite passes at parity
- [ ] Verify no new circular imports introduced

---

## 9. Summary

**Phase 4 is a low-risk selective-extraction refactor** that:

1. **Extracts 6 pure functions** from api_server.py and ws_handlers.py into 3 new helper modules
2. **Preserves all import surfaces** via facade re-exports
3. **Maintains the existing lazy-import cycle** between ws_handlers and api_server
4. **Requires no test changes** (all tests use original module paths)
5. **Introduces no new circular dependencies** (all new modules are leaf nodes)

**Estimated effort:**
- Implementation: 30–60 minutes (straightforward moves)
- Testing: 15–30 minutes (full suite run)
- Review: 30–45 minutes (verify pure-move compliance)

**Total: ~1.5–2.5 hours** (including review)

This refactor improves code organization and maintainability while maintaining 100% backward compatibility.
