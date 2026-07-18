---
name: debug_editable_install_cross_contamination
description: Diagnose and fix import conflicts when two related Python projects share an editable pip install, causing one branch's code to leak into another
source: auto-skill
extracted_at: '2026-06-28T09:44:47.563Z'
---

## Problem Pattern

Two sibling directories contain related Python projects (e.g., `AgentCascade` and `AgentCascade_unified`). One project was installed via `pip install -e .` pointing at the wrong directory. When running code from the "main" branch, imports of shared packages resolve to the unified branch's code instead — causing unexpected behavior that appears as if changes in one branch affect another unrelated instance.

## Symptoms

- Changes to Branch B cause failures in Branch A (which hasn't been modified)
- Agents or services fail to start, messages queue up without being drained
- Behavior differs between interactive terminal and background processes
- `pip show <package>` reveals editable install pointing at the wrong directory

## Diagnostic Procedure

1. **Check editable installs:**
   ```cmd
   pip show -f <package-name> | findstr "Editable"
   # or on macOS/Linux:
   pip show -f <package-name> | grep "Editable"
   ```
   Verify the `Editable project location` points to the correct directory.

2. **Verify import resolution:**
   ```python
   python -c "import agent_cascade; print(agent_cascade.__file__)"
   # Should resolve to the local branch's package, not a sibling
   ```

3. **Check sys.path order:**
   ```python
   python -c "import sys; [print(i, p) for i, p in enumerate(sys.path)]"
   # cwd (index 0 or '') shadows site-packages — explains why interactive runs work but background processes don't
   ```

4. **Confirm the running process's working directory:**
   ```cmd
   powershell -c "$wmi = Get-WmiObject Win32_Process -Filter 'ProcessId=<PID>'; Write-Host $wmi.CurrentDirectory"
   ```

## Fix

Reinstall editable mode from the correct project root:
```cmd
cd <correct-project-root> && pip install -e .
# Verify:
pip show <package-name> | findstr "Editable"
```

Then restart the affected services.

## Prevention

- Use separate virtual environments per project to avoid global editable installs
- Or use `pip install -e .` only from within the intended project directory and verify with `pip show` after each install
- Consider using `uv` or `poetry` for isolated per-project dependency management
