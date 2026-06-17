# Shell_cmd Auto-Approval Logic Audit Report

**Date:** 2026-06-14  
**Auditor:** ShellAutoApproveAudit (Coder Agent)  
**Issue:** Read operations via `shell_cmd` are too permissive and allow access to arbitrary system paths when auto-approved, unlike `read_file` and `list_dir` which enforce workspace boundaries.

---

## Executive Summary

The `shell_cmd` tool has an auto-approval mechanism for "safe" read-only commands (like `find`, `dir`, `ls`). However, while the **command type** is validated for safety, the **paths referenced within those commands** are NOT validated against workspace boundaries. This allows auto-approved commands to read from arbitrary system directories like `C:\Windows\System32`, creating an inconsistency with other read tools (`read_file`, `list_dir`) that properly enforce path restrictions.

---

## 1. Where Auto-Approval Logic is Defined

### Primary Location
**File:** `agent_cascade/operation_manager.py`  
**Method:** `_is_safe_readonly_shell_command(command: str) -> bool`  
**Lines:** 1764-1861

This static method determines whether a shell command should be auto-approved (bypassing user confirmation) based on the command structure and type.

### Invocation Point
**File:** `agent_cascade/operation_manager.py`  
**Method:** `execute_shell_command()`  
**Lines:** 1863-2102  
**Key Line:** 1883 - `is_safe = self._is_safe_readonly_shell_command(command)`

### Tool Definition
**File:** `agent_cascade/tools/custom/shell_cmd.py`  
**Lines:** 1-76  
This file defines the `ShellCmd` tool class and delegates execution to `operation_manager.execute_shell_command()`.

---

## 2. What Commands/Paths are Whitelisted for Auto-Approval

### Safe Primary Commands (Line 1818-1822)
```python
SAFE_PRIMARY_COMMANDS = {
    'find', 'dir', 'ls', 'tree', 'dir', 'directory',
    'vfd', 'where', 'whereis', 'locate', 'which', 'type',
    'pwd', 'stat', 'file', 'du', 'df',
}
```

### Safe Pipe Commands (Line 1811-1815)
```python
SAFE_PIPE_COMMANDS = {
    'grep', 'egrep', 'fgrep', 'head', 'tail', 'sort', 'uniq', 
    'wc', 'cat', 'more', 'less', 'awk', 'sed', 'cut', 'tr',
    'tee', 'xargs', 'comm', 'diff', 'nl', 'rev', 'fold'
}
```

### Auto-Approval Criteria (Lines 1782-1860)

A command is auto-approved if it:
1. **Uses only safe primary commands** from the whitelist above
2. **Does NOT contain dangerous patterns:**
   - Command chaining: `&&`, `;`, `||` (Line 1886)
   - Subshell execution: `$(`, backticks (Line 1790)
   - File write redirections: `>`, `>>` (except to `/dev/null` or `NUL`) (Lines 1795-1800)
   - Background processes: standalone `&` (Line 1804)
   - `find -exec` or `find -ok` (Lines 1847-1850)
   - `cmd /c` or `powershell` wrappers (Lines 1836-1840)
3. **Pipes only to safe secondary commands** from SAFE_PIPE_COMMANDS (Lines 1853-1859)

### What is NOT Checked (The Bug)

**Path validation within the command itself is NOT performed.** The auto-approval logic checks:
- ✅ Command type safety (is it `dir`, `ls`, `find`?)
- ✅ Command structure safety (no chaining, subshells, etc.)
- ❌ **Path boundaries** (whether paths in the command are within workspace)

---

## 3. Why Reads from Arbitrary System Paths are Auto-Approved

### Root Cause Analysis

#### The Problem
When `execute_shell_command()` is called:

1. **Line 1866:** The `cwd` (current working directory) parameter is validated against workspace boundaries:
   ```python
   resolved_cwd = self._resolve_path(cwd, mode="rw")
   ```

2. **Line 1883:** The command itself is checked for safety:
   ```python
   is_safe = self._is_safe_readonly_shell_command(command)
   ```

3. **Lines 1919-1930:** The command is executed via `subprocess.Popen()` with the resolved cwd, but **the paths WITHIN the command string are NOT validated**.

#### Example Vulnerability

This command would be **auto-approved** (because `dir` is a safe command):
```bash
dir C:\Windows\System32
```

Even though:
- `C:\Windows\System32` is outside the workspace
- The command can read arbitrary system files
- No user approval is requested

The execution flow:
1. `cwd="."` resolves to workspace directory ✓
2. `command="dir C:\Windows\System32"` passes safety check ✓ (it's just `dir`)
3. Command executes and reads from `C:\Windows\System32` ✗ (no path validation)

### Comparison with Other Read Tools

#### `read_file()` - Properly Enforces Boundaries
**File:** `operation_manager.py`  
**Lines:** 442-479  
**Key Line:** 445
```python
resolved = self._resolve_path(path, mode="ro")
```
Every path passed to `read_file()` goes through `_resolve_path()` which validates it's within allowed directories.

#### `list_directory()` - Properly Enforces Boundaries  
**File:** `operation_manager.py`  
**Lines:** 398-440  
**Key Line:** 401
```python
resolved = self._resolve_path(path)
```
Same validation as `read_file()`.

#### `_resolve_path()` - The Validation Mechanism
**File:** `operation_manager.py`  
**Lines:** 359-393

This method:
1. Handles workspace path prefixes (`/workspace/`, `workspace/`)
2. Resolves absolute vs relative paths correctly
3. **Validates against three tiers of allowed directories:**
   - Base workspace directory (always allowed)
   - Extra RW folders (allowed for both RO and RW modes)
   - Extra RO folders (allowed only in "ro" mode)
4. Raises `ValueError` if path is outside allowed directories

**The Inconsistency:** `shell_cmd` uses `_resolve_path()` only for the `cwd` parameter, NOT for paths embedded in the command string itself.

---

## 4. Affected Files and Line Numbers

### Primary Files

| File | Lines | Issue |
|------|-------|-------|
| `agent_cascade/operation_manager.py` | 1764-1861 | `_is_safe_readonly_shell_command()` - Checks command type but not paths |
| `agent_cascade/operation_manager.py` | 1863-2102 | `execute_shell_command()` - Validates `cwd` but not command-embedded paths |
| `agent_cascade/operation_manager.py` | 1866 | Only validates `cwd`, not paths in command string |
| `agent_cascade/operation_manager.py` | 1883 | Auto-approval decision based solely on command structure |
| `agent_cascade/tools/custom/shell_cmd.py` | 1-76 | Tool definition, delegates to operation_manager |

### Supporting Files

| File | Lines | Purpose |
|------|-------|---------|
| `agent_cascade/operation_manager.py` | 359-393 | `_resolve_path()` - The path validation mechanism that should be used |
| `agent_cascade/operation_manager.py` | 398-440 | `list_directory()` - Example of proper path validation |
| `agent_cascade/operation_manager.py` | 442-479 | `read_file()` - Example of proper path validation |
| `tests/test_safe_shell_cmd.py` | 1-150 | Test file showing expected safe/unsafe commands |

---

## 5. Technical Details

### How _resolve_path() Works (The Gold Standard)

```python
def _resolve_path(self, path: str, mode: str = "ro") -> Path:
    # Lines 361-367: Handle workspace prefixes
    clean_path = path
    if clean_path.startswith('/workspace/'):
        clean_path = clean_path[len('/workspace/'):]
    # ...
    
    # Lines 373-376: Resolve absolute vs relative paths
    if Path(clean_path).is_absolute():
        resolved = Path(clean_path).resolve()
    else:
        resolved = (self.base_dir / clean_path).resolve()
    
    # Lines 378-391: Validate against allowed directories
    if self._path_is_contained(resolved, self.base_dir):
        return resolved
    for extra in self.extra_work_folders_rw:
        if self._path_is_contained(resolved, extra):
            return resolved
    if mode == "ro":
        for extra in self.extra_work_folders_ro:
            if self._path_is_contained(resolved, extra):
                return resolved
    
    # Line 393: Raise error if not contained
    raise ValueError(f"Path '{path}' is outside the allowed {mode.upper()} directories")
```

### How shell_cmd Currently Works (The Bug)

```python
def execute_shell_command(self, command: str, justification: str, agent_name: str, cwd: str = ".", ...):
    # Line 1866: Only validates cwd
    resolved_cwd = self._resolve_path(cwd, mode="rw")
    
    # Line 1883: Checks if command TYPE is safe
    is_safe = self._is_safe_readonly_shell_command(command)
    
    # Lines 1919-1930: Executes command without validating paths IN the command
    proc = subprocess.Popen(
        command,  # <-- Paths in here are NOT validated!
        cwd=str(resolved_cwd),  # <-- Only this is validated
        shell=True,
        ...
    )
```

### Example Vulnerable Commands

All of these would be **auto-approved** despite accessing paths outside workspace:

```bash
# Windows examples
dir C:\Windows\System32
find C:\Windows -name "*.dll"
dir C:\Users
tree C:\Program Files

# Linux/macOS examples  
ls /etc/passwd
find /var -name "*.log"
stat /proc/version

# Mixed (cwd is workspace, but command accesses elsewhere)
cd C:\Windows && dir  # This would fail due to '&&' check
dir . ; dir C:\Windows  # This would fail due to ';' check
```

Wait—some of those would fail the chaining checks. Let me refine:

**Actually Vulnerable:**
```bash
dir C:\Windows\System32           # Safe command, absolute path not checked
find C:\Windows -name "*.dll"     # Safe command, absolute path not checked  
ls /etc/passwd                    # Safe command, absolute path not checked
tree C:\ProgramFiles              # Safe command, absolute path not checked
```

**Not Vulnerable (correctly rejected):**
```bash
dir . ; dir C:\Windows            # Rejected: contains ';'
find . && cat C:\secret           # Rejected: contains '&&'
ls | cat C:\secret                # Rejected: 'cat' with path as pipe target is suspicious
```

---

## 6. Impact Assessment

### Security Impact: MEDIUM

**What can happen:**
- Agents can read any file on the system that's accessible to the process user
- System directories, config files, environment variables can be enumerated
- No user notification or approval required for these reads

**What cannot happen (with auto-approved commands only):**
- File writes/deletions (those require approval)
- Arbitrary code execution (subshells, chaining blocked)
- Network access (unless via safe commands like `where curl`)

### Inconsistency Impact: HIGH

**User expectation:** All reads should be restricted to workspace unless explicitly approved  
**Reality:** `read_file` and `list_dir` enforce this, but `shell_cmd` does not

This creates confusion and an uneven security model.

---

## 7. Recommendations

### Immediate Fix Options

#### Option A: Path Extraction and Validation (Most Secure)
Extract all paths from the command string and validate each against workspace boundaries using `_resolve_path()`.

**Pros:**
- Consistent with `read_file` and `list_dir` behavior
- Maximum security
- Clear user expectations

**Cons:**
- Complex parsing required (different syntax for different commands)
- May break legitimate use cases where agents need to check system paths

#### Option B: Whitelist Allowed Absolute Paths (Balanced)
Allow auto-approval only if all paths in the command are within workspace OR a configured whitelist of safe system paths.

**Pros:**
- Balances security with usability
- Configurable via settings

**Cons:**
- Requires configuration
- Still needs path parsing

#### Option C: Require Approval for Absolute Paths (Simplest)
Modify `_is_safe_readonly_shell_command()` to reject any command containing absolute paths outside workspace.

**Pros:**
- Simple implementation
- Consistent security model

**Cons:**
- May require more user approvals
- Agents lose ability to quickly check system state

### Suggested Implementation (Option C)

Add to `_is_safe_readonly_shell_command()` before line 1861:

```python
# Check if command contains absolute paths outside workspace
import re
abs_path_pattern = r'(?:[A-Za-z]:\\|/)(?:[^\s"\'\\]|\\.)+'
matches = re.findall(abs_path_pattern, cmd)
for match in matches:
    # Skip relative paths that start with / but are actually arguments like /s /b
    if not self._is_path_within_workspace(match):
        return False
```

Where `_is_path_within_workspace()` uses the existing `_path_is_contained_cached()` logic.

---

## 8. Test Cases to Verify Fix

### Should Remain Auto-Approved
```bash
dir                          # No path specified, uses cwd
ls -la                       # Uses cwd
find . -name "*.py"          # Relative path
tree /workspace/src          # Workspace-relative (after prefix handling)
```

### Should Require Approval (Currently Auto-Approved - The Bug)
```bash
dir C:\Windows\System32      # Absolute path outside workspace
find C:\ -name "*.dll"       # Absolute path outside workspace
ls /etc/passwd               # Absolute path outside workspace
```

### Should Remain Requiring Approval
```bash
dir . && rm file             # Command chaining
find . -exec rm {} \;        # find -exec
ls > output.txt              # Write redirection
cmd /c dir                   # cmd wrapper
```

---

## 9. Conclusion

The auto-approval logic for `shell_cmd` properly validates **command type safety** but fails to validate **path boundaries**. This creates a security inconsistency where:

- ✅ `read_file("C:\Windows\win.ini")` → Requires approval (path validated)
- ✅ `list_dir("C:\Windows")` → Requires approval (path validated)  
- ❌ `shell_cmd("dir C:\Windows")` → Auto-approved (path NOT validated)

The fix should align `shell_cmd` behavior with the existing path validation mechanism used by other read tools, ensuring consistent security boundaries across all file access operations.

---

## Appendix: Code References

### Key Functions to Review/Modify

1. **`_is_safe_readonly_shell_command()`** (Lines 1764-1861)
   - Add path extraction and validation logic here
   
2. **`execute_shell_command()`** (Lines 1863-2102)
   - Currently validates `cwd` at line 1866
   - Should also validate paths within command string if auto-approving

3. **`_resolve_path()`** (Lines 359-393)
   - Existing validation mechanism to leverage
   
4. **`_path_is_contained_cached()`** (Lines 90-101)
   - Path containment check used by `_resolve_path()`

### Test File

- **`tests/test_safe_shell_cmd.py`** (Lines 1-150)
  - Contains test cases for safe/unsafe commands
  - Should be extended with path boundary test cases