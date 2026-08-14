# Grep Path Safety Investigation

**Investigator**: grep_path_safety_investigator
**Date**: 2026-08-11
**Scope**: Can the grep tool search outside allowed directories (workspace + extra paths) given wide inputs like `"."`, `".."`, `"/workspace/../.."`, or unusual `include` patterns?
**Verdict**: ⚠️ **UNSAFE** — one confirmed escape vector via the `include` argument (Python fallback `rglob`).

---

## 1. Architecture / Call Chain

```
Grep tool (agent_cascade/tools/custom/file_ops.py:942-1036)
  └─ Grep.call() → agent_pool.operation_manager.grep(...)      (file_ops.py:1026)
       └─ OperationManager.grep()                              (operation_manager/grep.py:394)
            ├─ self._resolve_path(path)   ← PathSecurityMixin (path_security.py:150)  VALIDATES path
            ├─ _try_subprocess_grep()     (grep.py:66)         ripgrep / GNU grep fast path
            └─ Python fallback            (grep.py:452-633)    os.walk / rglob
```

- `OperationManager(ApprovalMixin, PathSecurityMixin, FileOpsMixin, GrepMixin, ShellMixin)` — confirmed at `agent_cascade/operation_manager/__init__.py:42`.
- The `Grep` tool class (**file_ops.py:943**) does **NOT** inherit `PathResolutionMixin`; it passes `path` straight through to `operation_manager.grep()`. `operation_manager.grep()` calls `self._resolve_path(path)` at **grep.py:405**, which is `PathSecurityMixin._resolve_path` (path_security.py:150).
- `resolve_tool_path()` (agent_cascade/utils/tool_path_resolver.py:21-75) is the tool-level resolver used by other tools (read_file etc.); it delegates to `om._resolve_path` when an agent_pool exists. It is not in the grep path.

---

## 2. Files & Lines Reviewed

| File | Lines | Content |
|---|---|---|
| `agent_cascade/tools/custom/file_ops.py` | 942-1036 | `Grep` tool class; `call()` reads `path` (default `.`) and `include` (default `*`) from params, forwards to `om.grep()` |
| `agent_cascade/operation_manager/grep.py` | 394-413 | `grep()`: `resolved = self._resolve_path(path)`; `if not resolved.exists()` → "Directory not found"; file vs dir branch |
| `agent_cascade/operation_manager/grep.py` | 66-296 | `_try_subprocess_grep()`: builds `rg` cmd (**109-114**: `--glob {include}`) or GNU grep cmd; `subprocess.run(cwd=str(path))` (**146-154**); rc==2 → returns None (**288-291**) |
| `agent_cascade/operation_manager/grep.py` | 452-633 | Python fallback; **486**: `if include == '*'` → `os.walk`; **494-495**: `else: file_iter_gen = resolved.rglob(include)` |
| `agent_cascade/operation_manager/path_security.py` | 16-28 | `_path_is_contained_cached()` — `os.path.commonpath` containment check |
| `agent_cascade/operation_manager/path_security.py` | 150-252 | `_resolve_path()` — virtual prefix mapping, absolute path handling, extra-folder fallback, containment enforcement (raises `ValueError` if outside) |
| `agent_cascade/operation_manager/__init__.py` | 42-121 | `OperationManager` class + `base_dir`/`extra_work_folders_ro/rw` setup |
| `agent_cascade/utils/tool_path_resolver.py` | 21-75 | Tool-level resolver (fallback mode) — not used by grep |
| `tests/test_shell_cmd_cwd_resolution.py` | 86-110 | Existing traversal tests (pass) |

---

## 3. How the `path` Argument Is Resolved (SAFE)

`path_security.py:150-252` (`_resolve_path`):

1. **Virtual prefixes** (`:184-204`): `/workspace/` → stripped; `workspace/` → stripped; `/extra_rw_N` → mapped to `extra_work_folders_rw[N]`; `/extra_ro_N` → mapped to `extra_work_folders_ro[N]` (RO mode only).
2. **Absolute paths** (`:210-211`): used directly with `.resolve()` (symlink-dereferencing).
3. **Relative paths** (`:215-231`): joined to `base_dir` first, then falls back to extra RW/RO folders if not found.
4. **Containment enforcement** (`:233-252`): `_path_is_contained` (commonpath equality, case-insensitive) against `base_dir`, extra RW, extra RO. Any path outside → `ValueError: Path '...' is outside the allowed RO/RW directories`.

### Behavior for the asked inputs (empirically verified, both Linux container and Windows host)

| Input | Result |
|---|---|
| `.` | → base_dir (allowed) |
| `..` | → **BLOCKED** (ValueError) |
| `./..`, `../..`, `../../..` | → **BLOCKED** |
| `/workspace/..`, `/workspace/../..`, `/workspace/../` | → **BLOCKED** |
| `/workspace/../outside` | → **BLOCKED** |
| `workspace/..`, `workspace/../..` | → **BLOCKED** |
| absolute outside path (e.g. `C:\Windows\System32`, `/etc/passwd`) | → **BLOCKED** |
| `/extra_rw_0/..`, `/extra_rw_0/../..` | → **BLOCKED** |
| symlink path pointing outside | → `.resolve()` dereferences; containment check → **BLOCKED** |

**Conclusion for `path`**: the argument is properly contained. No escape via `..`, absolute paths, virtual prefixes, or symlink-to-outside.

---

## 4. The `include` Argument — CONFIRMED ESCAPE VECTOR ⚠️

`include` is **never validated**. It flows directly into:

- **rg fast path**: `cmd.extend(['--glob', include])` — grep.py:109-110
- **Python fallback**: `file_iter_gen = resolved.rglob(include)` — grep.py:494-495 (used whenever `include != '*'`)

### Exploit: `include='../*.txt'`

`Path.rglob(pattern)` resolves `..` segments upward from the search root (verified on Python 3.12.6/3.12.12). With `path='.'` (resolves to base_dir, passes the path check), the Python fallback walks **the parent of base_dir**:

**End-to-end proof, real tool, zero monkeypatching, Windows host (deployment platform):**
```
om = OperationManager(base_dir=<temp dir>)
om.grep(pattern='E2E_LEAK_TOKEN', path='.', include='../*.txt', timeout=10)
→ "Found 1 matches for 'E2E_LEAK_TOKEN':\n\n../E2E_SENTINEL.txt:1: E2E_LEAK_TOKEN"
```
The file `E2E_SENTINEL.txt` was created in `base_dir.parent` — **outside** the allowed directory.

### Why the subprocess path doesn't stop it (it actually enables the leak)

- ripgrep with `--glob '../*.txt'` **correctly refuses** to search outside cwd: rc=2, "No files were searched" (verified on Windows with real `C:\Python312\Scripts\rg.exe`).
- BUT grep.py:288-291 treats rc==2 as "fall back to Python" → grep.py:452 Python fallback runs → **leak**.
- `rg` **is** installed in this deployment (`C:\Python312\Scripts\rg.exe`), so the subprocess path is the *default*; the rc=2 fallback makes the leak reliably reachable.

### Notes on symlinks

- `os.walk` (used when `include == '*'`) does **not** follow directory symlinks → safe for `include='*'`.
- `rglob` through a **directory symlink inside base** does **not** escape (rglob uses scandir, doesn't resolve the symlink) — verified.
- A **path argument** that is itself a symlink to outside is blocked by `.resolve()` + containment check.

---

## 5. Impact Assessment

- **Confidentiality**: an agent (or prompt-injected content) can read arbitrary files **outside** the allowed workspace/extra folders by supplying `include='../<glob>'` (or deeper `../../../...` traversal), limited only by the OS user's read permissions.
- **Trigger**: any grep call with `include != '*'` containing `..` (or absolute path segments). The `path` remains inside the sandbox, so the leak is non-obvious.
- **Other tools**: `list_dir`/`read_file`/etc. use `resolve_tool_path`/`PathResolutionMixin` for their main path but should be audited separately for similar unvalidated secondary arguments (out of scope here).

---

## 6. Recommendations

1. **Validate/sanitize `include`** in `OperationManager.grep()` (grep.py:394) before use:
   - Reject `..` path segments, leading `/` or `\`, and drive-letter/UNC prefixes.
   - Or: resolve rglob results against `resolved` and skip anything where `file_path.relative_to(resolved)` raises `ValueError` — note the current code already has such a `relative_to` at grep.py:506-512 but it only gates the `ignore_vcs` skip and is skipped when `ignore_vcs=False`; it must become a hard containment filter applied to every yielded file.
2. **Apply the same filter in the Python fallback loop** (grep.py:497-542) — files outside `resolved` should be skipped, not searched.
3. **Optionally**: in `_try_subprocess_grep`, treat rc==2 with a *path-related* stderr as a hard error instead of falling back silently, or pre-validate the glob.
4. **Add regression tests**: `include='../*.txt'`, `include='../../*.txt'`, `include='/*.txt'` must return no matches / be rejected, with a sentinel file placed outside base_dir.

---

## 7. Conclusion

| Aspect | Verdict |
|---|---|
| `path` argument containment | ✅ Safe — `_resolve_path` + commonpath containment blocks all traversal attempts |
| `include='*'` default | ✅ Safe — `os.walk` doesn't follow symlinks |
| `include` with `..` (Python fallback) | ❌ **LEAKS** — `rglob(include)` searches outside base_dir |
| Subprocess `--glob ../*` | ✅ rg refuses, BUT the resulting rc=2 fallback routes to the vulnerable Python path |
| **Overall** | ⚠️ **UNSAFE** — confirmed end-to-end escape on the deployment platform |

**Confidence: Confirmed** (static analysis + two independent runtime reproductions — Linux container and Windows host — with the real tool, no mocking).

**Severity**: High for confidentiality in multi-tenant/agent contexts. Fix is localized to `include` validation in `operation_manager/grep.py`.

**Memory saved**: `.agent_lessons/grep-include-rglob-escape.md`
