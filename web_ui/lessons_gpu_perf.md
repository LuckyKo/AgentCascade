# GPU Performance Fixes - Agent Cascade Unified

## Problem
4 infinite CSS @keyframes animations running at 60fps during idle/tool-wait states, plus persistent box-shadow on activity bars forcing GPU compositor layers.

## Solution Summary

### Fix A: Pause All Infinite Animations via Body Class (CSS Safety Net)
- Added `body:not(.is-generating)` rule that pauses all infinite animations when not generating
- Covers: `.sub-tab-pulse`, `.activity-dot`, `.activity-queued`, `.waiting-spin`
- Uses `animation-play-state: paused !important` as a fallback safety net

### Fix B: Replace Infinite CSS Animations with JS-Driven Transitions
- Removed @keyframes: `subtab-pulse`, `activity-pulse`, `pulse` (3 animations eliminated)
- Replaced with `transition: opacity 0.3s ease-in-out` + `.dimmed` modifier classes
- Added `togglePulseElements()` in app.js that toggles `.dimmed` class at ~1.25Hz (800ms throttle)
- Called from `renderSubAgents()` and `updateMainActivityBar()` — already throttled by SSE event rate
- Elements covered: `.sub-tab-pulse`, `.activity-dot`, `.activity-queued`, `.btn-stop`, `.send-btn.inject-mode`

### Fix C: Conditional box-shadow on Activity Bars
- Removed persistent `box-shadow: 0 -2px 8px rgba(0,0,0,0.2)` from base activity bar styles
- Now only applied when `.active` class is present (only during generation)
- Also moved `.main-tab .activity-dot` box-shadow to the `.agent-active` variant

### Fix D: Pause waiting-spin when not generating
- Included in Fix A's `body:not(.is-generating)` rule block

## Remaining Animations (Intentionally Kept)
- `@keyframes spin` — loading spinner for waiting state (paused by Fix A/D safety net)
- `@keyframes slide-down` — one-shot animation for approval dropdown (not infinite)

## GPU Savings Estimate
- 3 infinite @keyframes removed = ~3 compositor layers eliminated at idle
- Transition-based approach runs at ~1.25Hz vs 60fps = ~98% reduction in animation frames
- box-shadow only on active elements = fewer forced GPU compositing layers

## Key Technical Details
- `state.generating` drives `body.is-generating` via `updateControls()` (line ~2712)
- `togglePulseElements()` uses `performance.now()` for throttling (not setInterval)
- The CSS safety net ensures animations are paused even if JS fails or runs late