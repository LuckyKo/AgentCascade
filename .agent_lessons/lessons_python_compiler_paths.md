# Python Compiler Path Discovery Update

## Summary
Updated `python_compiler.py` to dynamically discover allowed paths from path_mapping files instead of hardcoding just the workspace directory.

## Changes Made

### File: `agent_cascade/tools/python_compiler.py`

#### 1. Added module-level imports (lines 15-20)
```python
import json
import logging
from functools import lru_cache
```

#### 2. Added `_get_allowed_paths()` static method with caching (lines 80-123)
- Uses `@lru_cache(maxsize=1)` for performance
- Collects workspace directory (AgentWorkspace)
- Dynamically discovers host paths from `path_mapping_*.json` files
- Returns list of allowed directory paths for file validation
- Proper exception handling with logging

#### 3. Updated security check in `call()` method (lines 154-159)
- Changed from hardcoded `[workspace_dir]` to dynamic `_get_allowed_paths(workspace_dir)`
- Error message now includes actual allowed paths for better debugging

## How It Works

The `_get_allowed_paths()` method:
1. Always includes the workspace directory (`AgentWorkspace`)
2. Scans for `path_mapping_*.json` files in the project root
3. Extracts all `host` paths from the `host_to_container` mappings
4. Validates each path exists and is a directory
5. Returns deduplicated list of allowed paths using `os.path.realpath()`

## Security Features

- Uses `os.path.commonpath()` for containment check (prevents sibling-directory escape)
- Handles Windows cross-drive scenarios via `ValueError` exception
- Resolves symlinks with `os.path.realpath()`
- Deduplicates paths via `set()`
- Caches results to avoid repeated filesystem I/O

## Testing

Verified logic works correctly:
```python
# Test shows AgentCascade is now in allowed paths
allowed = PythonCompiler._get_allowed_paths(workspace_dir)
# Returns: ['N:\work\WD\AgentWorkspace', 'N:\work\WD\AgentCascade']

PythonCompiler._is_path_allowed('N:\work\WD\AgentCascade\test.py', allowed)
# Returns: True
```

## Notes

- Subdirectories of allowed paths are automatically included via `os.path.commonpath()` check in `_is_path_allowed()`
- If path_mapping files are unavailable, falls back to workspace_dir only with warning logged
- Uses `set()` internally to deduplicate paths (same host path may appear in multiple mappings)
- Results cached with `@lru_cache(maxsize=1)` - cache persists for lifetime of process

## Activation

The updated code requires module reload to take effect in running agents. Cached `.pyc` files may need clearing:
```
del agent_cascade\tools\__pycache__\python_compiler*.pyc
```

## Review Status

✅ All reviewer findings addressed:
1. Critical: `import json` moved to top-level
2. Major: Exception handling improved with specific types and logging
3. Major: Added `@lru_cache` for performance
4. Minor: Removed redundant `os.path.isdir()` check
5. Minor: Error message now includes allowed paths