# Web UI Lessons & Tips

## Event Listener Leaks in renderFunctions
- **Rule:** When `innerHTML` is replaced on each render, old DOM nodes (and their listeners) die. Scoping `querySelectorAll` to the container (not `document`) prevents accidental matches.
- Example: `sessionsList.querySelectorAll('.session-item')` instead of `document.querySelectorAll('.session-item')`

## Optimistic UI State Changes — Be Careful
- Adding items to a client-side Set before server confirmation creates race conditions if the operation fails.
- The `closedTabs` set was added optimistically when closing tabs, but if termination failed, the tab was permanently hidden with no way to recover it.
- **Fix:** Let server state sync be the single source of truth. Don't mutate client state optimistically for operations that can fail.

## Strict Equality with dataset Values
- `element.dataset.*` always returns strings. Use `=== String(value)` instead of `== value`.
- Example: `b.dataset.index === String(index)` not `b.dataset.index == index`

## CSS Variables — Check Before Using
- `--text-dim` was used in HTML but never defined in styles.css. Replace with existing `--text-muted`.

## Halt Status Display Priority
- When showing status, halt/pause should take priority over "generating" — a paused agent is more critical to highlight than an actively generating one.
- Pattern: `anyHalted ? '⏸ Paused' : (state.generating ? 'Generating...' : '')`