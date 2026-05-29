# Message Duplication Bug Fix — Lessons Learned

## Key Takeaway: `parseInt('Infinity')` returns `NaN`, NOT `Infinity`

This is a critical JavaScript gotcha. When using sentinel values in `dataset.*` attributes that will be parsed with `parseInt()`, always use a large finite integer string like `'999999999'` instead of `'Infinity'`.

- `parseInt('Infinity')` → `NaN` — any comparison with NaN is false
- `parseInt('999999999')` → `999999999` — works correctly in comparisons

## DOM Verification Before Append

When using incremental rendering (append only new items), always verify the container's child count matches expectations before appending. If a tab switch, retry, or other event cleared the DOM without resetting the cached count, appending creates duplicates.

Pattern:
```javascript
const actualChildCount = scrollContainer.children.length;
if (actualChildCount !== lastCount) {
  // Full re-render instead of append
} else {
  // Safe to append
}
```

## Cache Invalidation Consistency

All cache-invalidation sites must use the same pattern and sentinel value. After this fix, all four sites (state/done, stream_update, retry handler, reset handler) use:
- `mainTabPanels.querySelectorAll('.messages').forEach(p => { p.dataset.contentKey = ''; p.dataset.lastRenderedCount = '999999999'; })`

## Root Agent Double-Processing

When the root agent is stored in the same data structure as sub-agents (e.g., `state.subAgents[rootName]`), ensure that loops over `data.agent_instances` skip the root agent. The root agent's messages come from `data.messages`, not from `data.agent_instances`. Use `isRootAgentName(name)` to check.