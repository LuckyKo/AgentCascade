# Context Bar Frontend Fix — Feature #6 (Fix 2e)

**Date:** 2026-06-09  
**Feature:** Token Calculation & Context Usage Bar Fix — Frontend (Step 2)  
**Fix ID:** 2e  
**Files Modified:** `web_ui/app.js`, `web_ui/styles.css`

---

## Summary

Implemented the numeric token counter display on the right side of each agent tab's context bar, as specified in Feature Plan #6. This provides users with immediate visual feedback about their context usage without needing to hover over the bar for tooltip information.

---

## Changes Made

### 1. `web_ui/app.js`

#### Added `formatTokenCount()` Helper Function
```javascript
/**
 * Format token count with K/M suffixes for compact display.
 * @param {number} count - Token count to format
 * @returns {string} Formatted string (e.g., "1.5K", "2.3M")
 */
function formatTokenCount(count) {
  if (count >= 1000000) {
    return (count / 1000000).toFixed(1) + 'M';
  } else if (count >= 1000) {
    return (count / 1000).toFixed(1) + 'K';
  }
  return String(count);
}
```

**Purpose:** Formats large token counts with K (thousands) and M (millions) suffixes for compact, readable display. This prevents cluttered displays like "1234567 tokens" and instead shows "1.2M".

#### Enhanced `updateContextBar()` Function

Added numeric counter display logic:
```javascript
// Update or create the numeric counter display on the right side of the context bar
const contextBarContainer = barEl.parentElement;
if (contextBarContainer) {
  let counterEl = contextBarContainer.querySelector('.context-bar-counter');
  if (!counterEl) {
    counterEl = document.createElement('span');
    counterEl.className = 'context-bar-counter';
    contextBarContainer.appendChild(counterEl);
  }
  // Display: "used / max (percentage%)" with formatted token counts
  counterEl.textContent = `${formatTokenCount(tokens)} / ${formatTokenCount(maxContext)} (${Math.round(pct)}%)`;
}
```

**Purpose:** 
- Creates a `.context-bar-counter` element if it doesn't exist
- Updates the counter text with formatted display: "used / max (percentage%)"
- Uses `formatTokenCount()` for compact token representation
- Rounds percentage to integer for cleaner display

### 2. `web_ui/styles.css`

#### Updated `.context-bar` Styling
```css
.context-bar {
  height: 3px;
  background: var(--bg-elevated);
  width: 100%;
  position: relative;
  z-index: 10;
  flex-shrink: 0;
  display: flex;          /* ADDED: Enable flex layout */
  align-items: center;    /* ADDED: Center items vertically */
}
```

**Purpose:** Added flex properties to enable proper positioning of the counter element within the bar container.

#### Updated `.context-bar-fill` Styling
```css
.context-bar-fill {
  height: 100%;
  background: var(--accent);
  width: 0%;
  transition: width 0.3s ease, background-color 0.3s ease;
  position: absolute;    /* ADDED: Position absolutely for fill bar */
  left: 0;               /* ADDED: Anchor to left edge */
  top: 0;                /* ADDED: Anchor to top edge */
}
```

**Purpose:** Made the fill bar absolutely positioned so it doesn't interfere with the counter element's layout. The fill bar now renders underneath the counter.

#### Added `.context-bar-counter` Styling
```css
/* Numeric counter display on the right side of context bar */
.context-bar-counter {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 10px;
  color: var(--text-muted);
  background: var(--bg-tertiary);
  padding: 2px 6px;
  border-radius: 3px;
  white-space: nowrap;
  z-index: 11;
}
```

**Purpose:** 
- Positions counter on the right side of the bar with absolute positioning
- Vertically centers the counter using `transform: translateY(-50%)`
- Uses small font size (10px) and muted color for subtle appearance
- Adds background and border-radius for readability against the fill bar
- Sets higher z-index (11) to ensure it appears above the fill bar (z-index 10)

---

## Design Decisions

### 1. Format with K/M Suffixes
**Rationale:** Token counts can reach millions in long conversations. Displaying "1234567 / 1280000 (96%)" is harder to read quickly than "1.2M / 1.3M (96%)". The K/M formatting provides immediate scale recognition.

### 2. Absolute Positioning for Counter
**Rationale:** As specified in Feature Plan #6 Section 3.4, the counter is absolutely positioned on the right side rather than being part of the flex flow. This ensures:
- The fill bar renders normally with percentage-based width
- The counter sits flush-right without affecting layout
- Clean separation between visual (fill bar) and numeric (counter) information

### 3. Dynamic Element Creation
**Rationale:** The counter element is created dynamically on first update rather than being statically present in the DOM. This:
- Keeps initial HTML clean
- Ensures counter only appears when context bar updates
- Prevents stale counters on tabs that may be dismissed/recreated

### 4. Percentage Rounding
**Rationale:** Using `Math.round(pct)` instead of decimal percentages (e.g., "96%" vs "96.37%") provides cleaner display and matches the visual precision of the fill bar itself.

---

## Testing Recommendations

1. **Visual Verification:** Confirm "used / max (percentage%)" text is visible on the right side of each agent tab's context bar
2. **Format Scaling:** Test with various token counts:
   - Small (< 1000): Should show raw number (e.g., "523 / 32K (1%)")
   - Medium (1000-999999): Should show K suffix (e.g., "1.5K / 32K (5%)")
   - Large (> 1000000): Should show M suffix (e.g., "1.2M / 2M (60%)")
3. **Color States:** Verify counter remains visible in all bar states:
   - Normal (green/accent)
   - Warning (yellow, >75%)
   - Danger (red, >90%)
4. **Sub-Agent Tabs:** Confirm all sub-agent tabs show the counter, not just the main agent
5. **Streaming Updates:** During active generation, verify counter updates smoothly without flicker
6. **Session Load:** After loading an old session, confirm counters display correct values immediately

---

## Related Files

- `feature_plan_006.md` — Complete specification for Feature #6
- `web_ui/app.js` — Main frontend application logic
- `web_ui/styles.css` — Styling for UI components

---

*Generated during implementation of Feature #6, Fix 2e (Context Bar Numeric Display)*