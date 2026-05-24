# Branch Merge Strategy for Tab Unification Project

**Date**: 2026-05-23
**Status**: Planning only — no git operations performed

---

## Current State Summary

| Repo | Path | Branch | HEAD Commit | Description |
|------|------|--------|-------------|-------------|
| Main | `n:\work\WD\AgentCascade\` | master/main | `f291d51` (latest) | 15 commits ahead of fork point |
| Unified | `n:\work\WD\AgentCascade_unified\` | tab-unification | `103691f` | Forked from `9c6a92a`, has 1 commit + uncommitted changes |

**Fork Point**: `9c6a92a` (chore: incremental updates)
**Main HEAD**: `f291d51` (fix: forced compression pool corruption and sub-agent resume recovery)

---

## Conflict Analysis

### Files Changed on Main Since Fork (9c6a92a → f291d51): **68 files**

### Uncommitted Changes on tab-unification: **5 files**
1. `agent_cascade/tools/custom/compression_tools.py` — ⚠️ CONFLICT RISK
2. `agent_orchestrator.py` — ⚠️ CONFLICT RISK (HIGH)
3. `web_ui/app.js` — ⚠️ CONFLICT RISK (HIGH)
4. `README.md` — ✅ No conflict (not changed on main since fork)
5. `tab_unification_plan.md` — ✅ No conflict (not changed on main since fork)

### Detailed Overlap Assessment

#### 1. `agent_orchestrator.py` — ⚠️⚠️ HIGH CONFLICT RISK
- **Main changes**: +456/-146 lines across 28 hunks spanning the entire file (lines 37→1953)
- **Tab-unification changes**: +102/-93 lines across 5 hunks (lines 37, 595, 863, 1718)
- **Direct overlap in regions**:
  - Lines ~37-48: Import statements — both branches modified
  - Lines ~595-706: `OrchestratorAgent` core methods — both heavily modified (main: +112/-133; tab: +117/-112)
  - Lines ~1718-1734: Both branches modified this region
- **Assessment**: High risk. The main branch added ~456 lines of new features (idle agent dismissal, stop/resume, grep spillover, security timeout). Tab-unification also restructured the same core methods. Git will struggle to auto-resolve these overlaps.

#### 2. `web_ui/app.js` — ⚠️⚠️ HIGH CONFLICT RISK
- **Main changes**: +433/-63 lines across 29 hunks (lines 53→2837)
- **Tab-unification changes**: +160/-176 lines across 11 hunks (lines 1152, 1181, 1189, 1205, 1219, 1261, 1614, 1636, 2000, 2027, 3275)
- **Direct overlap in regions**:
  - Lines ~1152-1294: Message rendering functions (`fullRender`, `createMessageEl`, `updateBubbleContent`) — both modified heavily
  - Lines ~2039-2063: `renderSubAgentPanel` — both modified (main added new features; tab removed/refactored sub-agent panel code)
  - Tab-unification adds **123 new lines** at the end of the file (Phase 1 tab unification helpers: `msgClass`, `headerClass`, `contentClass`, etc.)
- **Assessment**: High risk. Main added significant new frontend features (streaming fixes, markdown rendering, tool results, sub-bubble content). Tab-unification refactored message rendering and sub-agent panels AND added the tab unification foundation layer. These are deeply intertwined changes.

#### 3. `agent_cascade/tools/custom/compression_tools.py` — ⚠️ MODERATE CONFLICT RISK
- **Main changes**: +59/-265 lines (reduced from 338 to ~114 lines)
- **Tab-unification changes**: +50/-265 lines (reduced to ~105 lines)
- **Both branches converged** on the same approach: delegating to `compress_context()` from `agent_cascade.compression`
- **Assessment**: Moderate risk but likely resolvable. Both branches independently reached a similar conclusion (thin wrapper). The main version is slightly more fleshed out with better docstrings.

---

## Strategy Evaluation

### Option A: Rebase tab-unification onto latest main ❌ NOT RECOMMENDED

```bash
# What this would involve:
cd n:\work\WD\AgentCascade_unified
git stash                    # Save uncommitted changes
git rebase f291d51           # Replay 103691f on top of f291d51
git stash pop                # Apply uncommitted changes (likely more conflicts)
```

**Pros**:
- Preserves the commit history cleanly
- Proper git workflow

**Cons**:
- **Guaranteed conflicts in at least 3 files**, possibly all requiring manual resolution
- The `agent_orchestrator.py` and `web_ui/app.js` conflicts would be deep — both branches restructured the same core functions
- Stash pop after rebase adds another layer of potential conflicts with uncommitted changes
- High risk of losing work during conflict resolution in a 1500+ line file

**Verdict**: Too risky. The overlap is too significant and the changes are too structural.

---

### Option B: Reset tab-unification to latest main, then re-apply frontend work ✅ RECOMMENDED

```bash
# Step-by-step execution plan:

# 1. Save current tab-unification work as reference
cd n:\work\WD\AgentCascade_unified
git stash -m "tab-unification work before reset"

# 2. Hard reset to latest main
git fetch origin              # if there's a remote
git reset --hard f291d51      # or: git pull origin master first, then reset to HEAD

# 3. Apply the uncommitted changes selectively (they should mostly apply cleanly)
#    The stash contains changes to these files:
#    - README.md              → should apply cleanly (no main changes)
#    - tab_unification_plan.md → should apply cleanly (no main changes)
#    - agent_orchestrator.py  → will need manual merge
#    - web_ui/app.js          → will need manual merge  
#    - compression_tools.py   → will likely conflict but easy to resolve

git stash pop                 # Try applying — expect conflicts in 3 files

# 4. Resolve conflicts file by file:

# For compression_tools.py: Accept main's version (it's more complete)
# Then manually add any tab-unification-specific changes if needed

# For agent_orchestrator.py: Manual merge
#   - Main added: idle agent dismissal, stop/resume, grep spillover, security timeout
#   - Tab changed: core method restructuring around line 595-706 and 1718-1734
#   - Strategy: Keep main's new features, integrate tab's structural changes

# For web_ui/app.js: Manual merge  
#   - Main added: streaming fixes, markdown rendering, tool results (lines 53-2837)
#   - Tab changed: message rendering refactor (lines 1152-1294) + sub-agent panel cleanup (lines 2039-2063)
#   - Tab added: Phase 1 tab unification helpers at end of file (123 new lines)
#   - Strategy: Keep main's additions, integrate tab's refactoring and new tab helpers

# 5. Commit the merged work
git add -A
git commit -m "Merge tab-unification changes onto latest main with manual conflict resolution"

# 6. Verify everything works
python agent_orchestrator.py  # Test backend
# Open web UI in browser      # Test frontend
```

**Pros**:
- Clean starting point — you get all of main's latest features for free
- Conflicts are resolved one file at a time with full context
- The main branch version of `compression_tools.py` is more complete — easy to prefer it
- Tab-unification changes to `web_ui/app.js` are mostly refactoring + new additions at the end of file — manageable

**Cons**:
- Requires manual conflict resolution (but this is unavoidable)
- Stash pop may show as conflicts even where there aren't real ones

**Verdict**: Best approach. Clean slate with full visibility into what needs merging.

---

### Option C: Cherry-pick specific commits from main ❌ NOT RECOMMENDED

```bash
# What this would involve:
cd n:\work\WD\AgentCascade_unified
git fetch /path/to/main/repo master  # or use a remote
# Cherry-pick each of the 15 commits from main individually
git cherry-pick 9c6a92a..f291d51
```

**Pros**:
- Granular control over which changes come in
- Can skip problematic commits

**Cons**:
- **15 commits to cherry-pick**, each potentially conflicting with the tab-unification work
- Main's commits are interdependent (e.g., grep tool depends on compression improvements which depend on endpoint scheduler) — cherry-picking them individually breaks this chain
- The `compression_tools.py` conflict would hit early and cascade
- High risk of breaking things mid-sequence

**Verdict**: Too many commits, too much interdependence. Cherry-picking is designed for isolated changes, not a 15-commit feature branch.

---

## Recommended Strategy: Option B with Detailed Execution Plan

### Phase 0: Preparation (Before Any Changes)

```bash
# Create safety backups
cd n:\work\WD\AgentCascade_unified
git stash -m "PRE-MERGE: tab-unification work as of 2026-05-23"
# Also copy the entire directory as a filesystem backup:
# xcopy /E /I n:\work\WD\AgentCascade_unified n:\work\WD\AgentCascade_unified_backup
```

### Phase 1: Reset to Latest Main

```bash
cd n:\work\WD\AgentCascade_unified

# Option 1: If you have the main repo as a remote
git fetch origin master
git reset --hard origin/master

# Option 2: Direct commit reference (more reliable)
git reset --hard f291d51

# Verify we're at latest main
git log --oneline -3
# Should show: f291d51 fix: forced compression pool corruption...
```

### Phase 2: Apply Tab-Unification Changes

```bash
# Apply the stashed changes — expect conflicts in 3 files
git stash pop

# If conflicts arise (expected), resolve them one at a time
```

### Phase 3: Conflict Resolution Strategy per File

#### File 1: `compression_tools.py`
- **Resolution**: Accept main's version entirely. It has better docstrings and the same delegation pattern.
- If tab-unification added any unique lines, manually copy them in after accepting main's version.

#### File 2: `agent_orchestrator.py`  
- **Resolution**: Manual merge with this priority:
  1. Keep ALL of main's new features (idle dismissal, stop/resume, grep spillover, security timeout)
  2. Integrate tab-unification's structural changes to the core methods
  3. The overlapping region at lines ~595-706 needs careful attention — this is where both branches restructured `OrchestratorAgent` methods

#### File 3: `web_ui/app.js`
- **Resolution**: Manual merge with this priority:
  1. Keep ALL of main's new features (streaming fixes, markdown rendering, tool results)
  2. Integrate tab-unification's message rendering refactor (`createMessageEl`, `updateBubbleContent`)
  3. Integrate tab-unification's sub-agent panel cleanup
  4. **Preserve the Phase 1 Tab Unification helpers** at the end of the file — this is new code unique to tab-unification

#### Files 4-5: `README.md` and `tab_unification_plan.md`
- Should apply cleanly with no conflicts

### Phase 4: Verification

```bash
# Check for Python syntax errors
python -m py_compile agent_orchestrator.py
python -m py_compile agent_cascade/tools/custom/compression_tools.py

# Run any existing tests
python -m pytest tests/  # if tests exist

# Commit the result
git add -A
git commit -m "Merge tab-unification work onto latest main (f291d51)

- Integrated 15 commits from main: compression fixes, grep tool, idle agent dismissal, stop/resume, streaming fixes
- Preserved tab-unification frontend refactoring (message rendering, sub-agent panels)
- Added Phase 1 Tab Unification foundation layer (msgClass, headerClass helpers)
- Resolved conflicts in: agent_orchestrator.py, web_ui/app.js, compression_tools.py"
```

---

## Risk Mitigation

1. **Always have a stash** — never work without `git stash` as a safety net
2. **Resolve one file at a time** — don't try to resolve all conflicts simultaneously
3. **Test after each conflict resolution** — verify Python syntax after fixing `agent_orchestrator.py`, test the web UI after fixing `app.js`
4. **Use git mergetool** if available: `git mergetool --tool=vscode` (or your preferred merge tool)
5. **Keep the original stash**: After successful merge, keep the stash as a reference point for undoing any problematic changes

---

## Quick Reference: Conflict Probability Summary

| File | Overlap Type | Conflict Severity | Resolution Difficulty |
|------|-------------|------------------|----------------------|
| `agent_orchestrator.py` | Structural + feature overlap | 🔴 HIGH | Medium — need to preserve both sets of features |
| `web_ui/app.js` | Refactoring + feature overlap | 🔴 HIGH | Medium — main's additions are additive, tab's refactoring is structural |
| `compression_tools.py` | Convergent changes (same approach) | 🟡 MODERATE | Low — accept main's version |
| `README.md` | No overlap | 🟢 NONE | None |
| `tab_unification_plan.md` | No overlap | 🟢 NONE | None |

---

## Conclusion

**Use Option B (Reset + Re-apply)** with the detailed execution plan above. The conflict surface is manageable (3 files) and well-understood. The main branch's changes are largely additive (new features), while tab-unification's changes are structural (refactoring). These can be merged systematically by keeping main's features and integrating tab-unification's refactoring on top.

Estimated time to resolve: 1-2 hours for careful manual merge with testing.