# Lessons Learned — Docker Mount Auto-Configuration Bug Fixes

## Issues Fixed

### 1. 🔴 Watchdog Type Incompatibility (Critical)
**Root cause:** Lines 309 and 696 of `code_interpreter.py` were overwriting `_KERNEL_ACTIVITY[kernel_id]` with a bare `time.time()` float, but the watchdog thread reads it as a dict via `.get('work_dir')`.

**Fix:** Always update the dict's `last_active` key instead of replacing the value:
```python
if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
    _KERNEL_ACTIVITY[kernel_id]['last_active'] = time.time()
else:
    _KERNEL_ACTIVITY[kernel_id] = {'last_active': time.time(), 'work_dir': self.work_dir}
```

**Lesson:** When a global data structure is shared across threads, always use the same type consistently. The `isinstance()` guard is important because old corrupted entries could still exist if code was restarted mid-flight.

### 2. 🟠 Stale Extra-Folder Config
**Root cause:** CodeInterpreter instances were created at server startup with empty extra folder lists. When users added extra work folders via UI, existing instances never picked them up.

**Fix:** Added `_operation_manager` reference and `_resolve_extra_folders()` method that reads dynamically from the operation manager at kernel start time, falling back to config-set defaults for standalone/testing use.

**Lesson:** For runtime-configurable settings, prefer reading from a shared mutable source (like an operation manager) at point of use rather than storing static copies at init time. The `list()` copy prevents shared mutable state issues when falling back to stored defaults.

### 3. 🟠 Path Security Validation
**Root cause:** Extra paths were mounted without checking if they're safe.

**Two sub-bugs discovered during review:**
- **Bug A:** Used `self.base_dir` which doesn't exist on CodeInterpreter → crash on every `_start_kernel()` call
- **Bug B:** Used `.startswith()` for path containment check → sibling-directory escape (e.g., `/workspace_extra` passes `startswith('/workspace')`)

**Fix:** 
```python
# Use self.work_dir (which exists) and os.path.commonpath() for proper containment
allowed_prefixes = [os.path.realpath(self.work_dir)] if self.work_dir else []

def _is_path_allowed(self, abs_path: str, allowed_prefixes: List[str]) -> bool:
    for prefix in allowed_prefixes:
        try:
            if os.path.commonpath([abs_path, prefix]) == prefix:
                return True
        except ValueError:
            # Different drive letters on Windows
            continue
    return False
```

**Lesson:** 
- Always use `os.path.realpath()` (not `os.path.abspath()`) for security checks — resolves symlinks.
- Never use `.startswith()` for path containment — it allows sibling-directory escapes. Use `os.path.commonpath([path, prefix]) == prefix` instead.
- Test security code thoroughly — the reviewer caught both bugs that would have made the feature crash or be bypassable.

### 4. 🟡 Orphaned path_mapping Files
**Root cause:** The path_mapping JSON file was written BEFORE the Docker container start check, so on failure it was left behind.

**Fix:** Moved the file write to after `result.returncode != 0` check passes.

**Lesson:** Write side-effect files only after confirming the primary operation succeeded.

### 5. 🟡 Dead Code (Double Assignment)
**Root cause:** Two consecutive assignments to `self.work_dir`, the first immediately overwritten by the second.

**Fix:** Merged into single priority chain: `config > env var > default`.

### 6. 🟡 Test Refactoring
**Root cause:** Tests duplicated implementation logic instead of testing actual CodeInterpreter methods.

**Fix:** Extracted `_build_path_mapping()` and `_resolve_extra_folders()` as proper methods on CodeInterpreter so tests call them directly.

**Lesson:** If test helpers duplicate production code, extract the logic into production methods and test those instead. This way if the real implementation changes, tests still catch it.

### 7. 🔵 Debug Logging
**Fix:** Added debug log when no extra folders are configured in agent_factory.py.

## Key Takeaways
1. **Security checks need their own security review** — even simple path validation has subtle edge cases (sibling escape via startswith)
2. **Never assume attributes exist** — `self.base_dir` wasn't defined, causing a crash that would affect every kernel start
3. **Test dynamically** — refactoring tests to use actual methods instead of duplicated logic means fewer bugs go unnoticed when implementations change