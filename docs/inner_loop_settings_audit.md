# Inner Loop Detection Settings Audit Report
**Date**: 2026-07-12  
**Scope**: Full chain from UI → settings.py → execution_engine.py → inner_loop_detect.py

---

## Executive Summary

The inner loop detection system is **well-designed and mostly complete**, with a clean separation between:
- `InnerLoopSettings` dataclass (low-level detector parameters in `settings.py`)
- Agent pool settings (UI-facing configuration in `agent_instance.py`)
- Config handlers (bridge layer in `config_handlers.py`)

**Issues Found**: 4 gaps identified — 2 are minor, 2 could cause silent misconfiguration.

---

## 1. Settings Inventory

### A. Low-Level Detector Parameters (`InnerLoopSettings` dataclass)
**File**: `N:\work\WD\AgentCascade_unified\agent_cascade\settings.py` (lines 132–170)

| # | Parameter | Type | Default | Env Var Override? | Used In Detector? |
|---|-----------|------|---------|-------------------|--------------------|
| 1 | `max_counter_entries` | int | 200 | ❌ No | ✅ Yes (line 175, `_trim_counter`) |
| 2 | `max_tokens` | int | 1000 | ❌ No | ✅ Yes (line 51, deque maxlen) |
| 3 | `default_min_chars` | int | 4000 | ❌ No | ✅ Yes (via constructor, line 60) |
| 4 | `default_batch_interval` | int | 1 | ❌ No | ✅ Yes (via constructor, line 62) |
| 5 | `ngram_size` | int | 64 | ❌ No | ✅ Yes (via constructor, line 54) |
| 6 | `block_size` | int | 128 | ❌ No | ✅ Yes (via constructor, line 55) |
| 7 | `entropy_window` | int | 128 | ❌ No | ✅ Yes (via constructor, line 56) |
| 8 | `char_run_limit` | int | 70 | ❌ No | ✅ Yes (via constructor, line 57) |
| 9 | `score_threshold` | int | 300 | ❌ No | ✅ Yes (via constructor, line 65) |
| 10 | `sentence_repetition_threshold` | int | 7 | ❌ No | ✅ Yes (line 291) |
| 11 | `ngram_repetition_threshold` | int | 5 | ❌ No | ✅ Yes (line 334) |
| 12 | `block_repetition_threshold` | int | 4 | ❌ No | ✅ Yes (line 361) |
| 13 | `entropy_threshold` | float | 2.0 | ❌ No | ✅ Yes (line 398) |
| 14 | `score_decay_rate` | float | 0.97 | ❌ No | ✅ Yes (line 137, `decay()`) |
| 15 | `max_score` | int | 500 | ❌ No | ✅ Yes (line 143, `add_score()`) |
| 16 | `char_run_enabled` | bool | True | ✅ `AGENT_CASCADE_LOOP_CHAR_RUN` | ✅ Yes (line 261) |
| 17 | `sentence_rep_enabled` | bool | True | ✅ `AGENT_CASCADE_LOOP_SENTENCE_REP` | ✅ Yes (line 287) |
| 18 | `ngram_rep_enabled` | bool | True | ✅ `AGENT_CASCADE_LOOP_NGRAM_REP` | ✅ Yes (line 320) |
| 19 | `block_rep_enabled` | bool | True | ✅ `AGENT_CASCADE_LOOP_BLOCK_REP` | ✅ Yes (line 349) |
| 20 | `entropy_collapse_enabled` | bool | True | ✅ `AGENT_CASCADE_LOOP_ENTROPY` | ✅ Yes (line 389) |

### B. Agent Pool Settings (UI-Facing Configuration)
**File**: `N:\work\WD\AgentCascade_unified\agent_cascade\agent_instance.py` (lines 598–611)

| # | Setting | Type | Default | UI Control Exists? | Config Handler Exists? |
|---|---------|------|---------|---------------------|------------------------|
| 1 | `inner_loop_detect_enabled` | bool | False | ✅ `setting-inner-loop-detect` checkbox | ✅ `_handle_inner_loop_detect` |
| 2 | `loop_min_chars` | int | 4000 | ✅ `setting-loop-min-chars` number input | ✅ `_handle_loop_min_chars` |
| 3 | `loop_score_threshold` | int | 300 | ✅ `setting-loop-score-threshold` number input | ✅ `_handle_loop_score_threshold` |
| 4 | `loop_char_run_enabled` | bool | True | ✅ `setting-loop-char-run` checkbox | ✅ `_handle_loop_char_run` |
| 5 | `loop_sentence_rep_enabled` | bool | True | ✅ `setting-loop-sentence-rep` checkbox | ✅ `_handle_loop_sentence_rep` |
| 6 | `loop_ngram_rep_enabled` | bool | True | ✅ `setting-loop-ngram-rep` checkbox | ✅ `_handle_loop_ngram_rep` |
| 7 | `loop_block_rep_enabled` | bool | True | ✅ `setting-loop-block-rep` checkbox | ✅ `_handle_loop_block_rep` |
| 8 | `loop_entropy_enabled` | bool | True | ✅ `setting-loop-entropy` checkbox | ✅ `_handle_loop_entropy` |
| 9 | `loop_max_retries` | int | 2 | ✅ `setting-loop-max-retries` number input | ✅ (handler exists, see below) |

---

## 2. Settings Flow Chain Analysis

### Step 1: UI → Config Handlers
**UI File**: `N:\work\WD\AgentCascade_unified\web_ui/index.html` (lines 504–541)  
**JS Collection**: `N:\work\WD\AgentCascade_unified\web_ui/app.js` (lines 4023–4032)

All 9 UI settings are properly collected in JS and sent to the server. ✅

### Step 2: Config Handlers → Agent Pool Settings
**File**: `N:\work\WD\AgentCascade_unified\agent_cascade/config_handlers.py` (lines 154–213)

Each of the 8 primary settings has a registered config handler that writes to `agent_pool.settings`. ✅

### Step 3: Agent Pool Settings → InnerLoopSettings Constructor
**File**: `N:\work\WD\AgentCascade_unified\agent_cascade/execution_engine.py` (lines 1796–1807)

```python
_inner_settings = _InnerLoopSettings(
    default_min_chars=getattr(_ps, 'loop_min_chars', 4000),
    score_threshold=getattr(_ps, 'loop_score_threshold', 300),
    char_run_enabled=getattr(_ps, 'loop_char_run_enabled', True),
    sentence_rep_enabled=getattr(_ps, 'loop_sentence_rep_enabled', True),
    ngram_rep_enabled=getattr(_ps, 'loop_ngram_rep_enabled', True),
    block_rep_enabled=getattr(_ps, 'loop_block_rep_enabled', True),
    entropy_collapse_enabled=getattr(_ps, 'loop_entropy_enabled', True),
)
_inner_detector = InnerLoopDetector(settings=_inner_settings)
```

### Step 4: InnerLoopSettings → Detection Logic
**File**: `N:\work\WD\AgentCascade_unified\agent_cascade/inner_loop_detect.py` (lines 26–411)

All settings are properly consumed in the detector. ✅

---

## 3. Issues Found

### 🔴 ISSUE #1: Settings Passed to `_InnerLoopSettings()` Are Incomplete
**Severity**: Medium  
**Location**: `execution_engine.py`, lines 1798–1806

The execution engine creates an `_InnerLoopSettings` instance passing only **7 parameters**, but the dataclass has **20 fields**. The following are NOT passed through from agent pool settings and silently use hardcoded defaults:

| Parameter | Default in Dataclass | UI-Tunable? | Impact |
|-----------|---------------------|-------------|--------|
| `max_counter_entries` | 200 | No (internal) | Low — memory bound |
| `max_tokens` | 1000 | No (internal) | Low — memory bound |
| `default_batch_interval` | 1 | No (internal) | Low — performance tuning |
| `ngram_size` | 64 | No (UI lacks control) | **Medium** — affects detection sensitivity |
| `block_size` | 128 | No (UI lacks control) | **Medium** — affects detection sensitivity |
| `entropy_window` | 128 | No (UI lacks control) | Low — internal tuning |
| `char_run_limit` | 70 | No (UI lacks control) | **Low-Medium** — char run sensitivity |
| `sentence_repetition_threshold` | 7 | No (internal) | Medium — detection threshold |
| `ngram_repetition_threshold` | 5 | No (internal) | Medium — detection threshold |
| `block_repetition_threshold` | 4 | No (internal) | Medium — detection threshold |
| `entropy_threshold` | 2.0 | No (internal) | Medium — entropy sensitivity |
| `score_decay_rate` | 0.97 | No (internal) | Low — scoring dynamics |
| `max_score` | 500 | No (internal) | Low — safety cap |

**Assessment**: This is by design — not all parameters need to be UI-tunable. However, the most impactful ones (`ngram_size`, `block_size`, `char_run_limit`) could benefit from being exposed as advanced settings.

### 🔴 ISSUE #2: Default Value Mismatch Between Layers
**Severity**: Low  
**Location**: Cross-file comparison

| Setting | `InnerLoopSettings` default | Agent pool default | Match? |
|---------|---------------------------|--------------------|--------|
| `default_min_chars` / `loop_min_chars` | 4000 | 4000 | ✅ |
| `score_threshold` / `loop_score_threshold` | 300 | 300 | ✅ |
| Per-mode toggles (all) | True | True | ✅ |

**All defaults are consistent.** ✅ No mismatch found.

### 🟡 ISSUE #3: Env Vars in `InnerLoopSettings` Are Bypassed by UI Flow
**Severity**: Low  
**Location**: `settings.py` lines 166–170 vs. `execution_engine.py` line 1798

The per-mode toggle env vars (`AGENT_CASCADE_LOOP_CHAR_RUN`, etc.) are evaluated at class definition time in `InnerLoopSettings`. However, when execution_engine constructs `_InnerLoopSettings()`, it explicitly overrides these with values from the agent pool settings:

```python
_inner_settings = _InnerLoopSettings(
    char_run_enabled=getattr(_ps, 'loop_char_run_enabled', True),  # ← Always overrides env var
    ...
)
```

**Effect**: If no UI config is loaded (e.g., CLI mode or first-run before settings are saved), the env vars ARE used. But once any agent pool settings exist, they always win — even if they're at their defaults. This means **env vars can never override UI settings**, which is correct behavior but worth noting for debugging.

### 🟡 ISSUE #4: `inner_loop_detect_enabled` Toggle Is Checked Twice
**Severity**: Very Low (cosmetic)  
**Location**: `execution_engine.py` line 1807 vs. line 1885

The detector is **always instantiated** at line 1807 regardless of whether detection is enabled:
```python
_inner_detector = InnerLoopDetector(settings=_inner_settings)
```

Then the toggle check happens at feed time (line 1885):
```python
if getattr(self.pool.settings, 'inner_loop_detect_enabled', False):
    _ev = _inner_detector.feed(_delta_text)
```

**Effect**: A detector object is created every retry attempt even when detection is disabled. This wastes a small amount of memory (the deque + counters). Minor optimization opportunity — could wrap the constructor in the same `if` guard.

---

## 4. Hardcoded Values Check

### Detection Logic (`inner_loop_detect.py`)
All hardcoded values were checked against their settings counterparts:

| Line | Hardcoded Value | Source Setting | Match? |
|------|----------------|----------------|--------|
| 261 | `self.char_run_limit` | `settings.char_run_limit` via constructor ✅ | ✅ |
| 291 | `self._settings.sentence_repetition_threshold` | Direct from settings ✅ | ✅ |
| 334 | `self._settings.ngram_repetition_threshold` | Direct from settings ✅ | ✅ |
| 361 | `self._settings.block_repetition_threshold` | Direct from settings ✅ | ✅ |
| 398 | `self._settings.entropy_threshold` | Direct from settings ✅ | ✅ |
| 137 | `self._settings.score_decay_rate` | Direct from settings ✅ | ✅ |
| 143 | `self._settings.max_score` | Direct from settings ✅ | ✅ |

**No hardcoded values overriding settings.** ✅

### Score Constants in Detection Logic
Lines 295, 337, 364, 403 use hardcoded score amounts:
- Sentence repetition: **+100** (line 295) — NOT configurable
- N-gram repetition: **+90** (line 337) — NOT configurable  
- Block repetition: **+100** (line 364) — NOT configurable
- Entropy collapse: **+50** (line 403) — NOT configurable
- Char run: **+100** added to score display (line 266) — NOT configurable

These are internal scoring weights and are **not exposed as settings**. This is acceptable for a detection system but worth noting.

---

## 5. Constructor Parameter Coverage

### `InnerLoopDetector.__init__()` accepts:
| Param | Purpose | Passed From execution_engine? |
|-------|---------|------------------------------|
| `ngram_size` | N-gram window size | ❌ Not passed (uses settings default) |
| `block_size` | Block window size | ❌ Not passed (uses settings default) |
| `entropy_window` | Entropy calc window | ❌ Not passed (uses settings default) |
| `char_run_limit` | Char run threshold | ❌ Not passed (uses settings default) |
| `score_threshold` | Detection trigger score | ✅ Via `_InnerLoopSettings(score_threshold=...)` |
| `min_chars` | Min chars for activation | ✅ Via `_InnerLoopSettings(default_min_chars=...)` |
| `batch_interval` | Heavy check frequency | ❌ Not passed (uses settings default) |
| `settings` | Full settings object | ✅ Passed as keyword arg |

**Coverage**: All 8 constructor params can be influenced through the `settings` object. The 5 structural params (`ngram_size`, `block_size`, etc.) use dataclass defaults, which is correct behavior — they're internal tuning parameters not meant for UI exposure.

---

## 6. Toggle Respect Check

### `inner_loop_detect_enabled` Flow:
1. **UI** (index.html line 505): Checkbox → JS collects it (app.js line 4023) ✅
2. **Config handler** (config_handlers.py line 158): Writes to `agent_pool.settings.inner_loop_detect_enabled` ✅
3. **Execution engine** (execution_engine.py line 1885): Checks toggle before calling `_inner_detector.feed()` ✅

**Toggle is fully respected throughout the chain.** ✅

---

## 7. Summary Table: Complete Settings Matrix

| Setting | Defined In | UI Control | Config Handler | Passed to Engine | Used in Detector | Status |
|---------|-----------|------------|----------------|------------------|-----------------|--------|
| `inner_loop_detect_enabled` | agent_instance.py | ✅ | ✅ | ✅ (line 1885) | N/A (gate only) | ✅ OK |
| `loop_min_chars` → `default_min_chars` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1799) | ✅ (line 60) | ✅ OK |
| `loop_score_threshold` → `score_threshold` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1800) | ✅ (line 65) | ✅ OK |
| `loop_char_run_enabled` → `char_run_enabled` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1801) | ✅ (line 261) | ✅ OK |
| `loop_sentence_rep_enabled` → `sentence_rep_enabled` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1802) | ✅ (line 287) | ✅ OK |
| `loop_ngram_rep_enabled` → `ngram_rep_enabled` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1803) | ✅ (line 320) | ✅ OK |
| `loop_block_rep_enabled` → `block_rep_enabled` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1804) | ✅ (line 349) | ✅ OK |
| `loop_entropy_enabled` → `entropy_collapse_enabled` | settings.py / agent_instance.py | ✅ | ✅ | ✅ (line 1805) | ✅ (line 389) | ✅ OK |

---

## 8. Recommendations

### Priority 1: None Required — System Is Sound
The core chain is complete and consistent. All UI-exposed settings flow correctly to the detector.

### Priority 2: Optional Enhancements
1. **Move detector construction inside the toggle guard** (Issue #4) — minor optimization:
   ```python
   _inner_detector = None
   if getattr(self.pool.settings, 'inner_loop_detect_enabled', False):
       _inner_settings = _InnerLoopSettings(...)
       _inner_detector = InnerLoopDetector(settings=_inner_settings)
   ```

2. **Consider exposing advanced settings** for power users: `ngram_size`, `block_size`, `char_run_limit` — these affect detection sensitivity and could help tune false positive rates.

3. **Add env var fallback chain**: Currently if UI config is missing, defaults are used. Consider adding explicit env var support at the agent pool level so non-UI deployments can still configure via environment.

---

## Files Audited (Absolute Paths)
1. `N:\work\WD\AgentCascade_unified\agent_cascade\settings.py` — InnerLoopSettings dataclass definition
2. `N:\work\WD\AgentCascade_unified\agent_cascade\inner_loop_detect.py` — Detector implementation
3. `N:\work\WD\AgentCascade_unified\agent_cascade\execution_engine.py` — Engine instantiation (lines 1796–1807, 1885)
4. `N:\work\WD\AgentCascade_unified\agent_cascade\agent_instance.py` — Agent pool settings defaults (lines 598–611)
5. `N:\work\WD\AgentCascade_unified\agent_cascade\config_handlers.py` — Config handlers (lines 154–213)
6. `N:\work\WD\AgentCascade_unified\web_ui\index.html` — UI controls (lines 504–541)
7. `N:\work\WD\AgentCascade_unified\web_ui\app.js` — JS config collection (lines 4023–4032)