# Investigation: "Can't cancel message edits" (web UI)

- **Todo ref:** `todo.md:152` — `- [ ] UI issue: can't cancel message edits`
- **Scope:** `web_ui/app.js` (vanilla JS, ~6889 lines), `web_ui/styles.css`, `web_ui/index.html`. Backend `edit_message` WS handler is out of scope for this bug (frontend never reaches it on cancel).
- **Mode:** Investigative (root cause). No fix written — investigation + plan only.
- **Investigator:** ui-edit-investigator · **Confidence: Confirmed** (primary root cause traced line-by-line against current source)

---

## Executive Summary

`cancelEdit()` restores a bubble's original content by calling `updateBubbleContent(bubble, msg, config)`. But `updateBubbleContent()` has a performance early-exit that skips the re-render when it believes "nothing changed." `startEdit()` replaces the bubble's `.msg-content` innerHTML with the edit UI (textarea + Save/Cancel toolbar) **without updating the bookkeeping fields** (`bubble._prevContent`, `bubble._wasGenerating`) that `updateBubbleContent()` uses to make that decision. So on cancel, `updateBubbleContent()` sees `msg.content === bubble._prevContent` and short-circuits — **leaving the textarea/toolbar stuck in the DOM**. Removing the `.editing` class (the only other thing cancel does) is cosmetic-only (`padding: 0`), so it does not hide the editor.

**The tell-tale discriminator:** `finishEdit()` mutates `msg.content` (optimistic update) before calling `updateBubbleContent()`, which defeats the "nothing changed" guard — so **Save works but Cancel doesn't.** That asymmetry is the fingerprint of this bug and confirms the root cause.

The failure is history-dependent: it reliably reproduces on any bubble that has already been fully re-rendered with `isGenerating=false` (i.e., essentially every completed assistant reply), and does *not* reproduce on a freshly-created bubble that has never been re-rendered (`_wasGenerating` still `undefined`). This explains the intermittent reports.

---

## Failure Modes (ranked by likelihood)

### 1. PRIMARY — "Click Cancel, editor stays stuck" (Confirmed, high likelihood)
- **Symptom:** User opens an edit on a completed message → textarea + Save/Cancel appear → clicks **Cancel** (or presses **Escape**) → the editor UI remains; original content is not restored. Repeated cancels do nothing.
- **Mechanism:** `cancelEdit()` → `updateBubbleContent()` early-exits (L3209–3213) because content is unchanged → `contentDiv.innerHTML` (still the edit UI) is never replaced.
- **Repro (deterministic):**
  1. Send a prompt; wait for the assistant reply to fully complete (streaming done, `isGenerating=false`).
  2. Hover the completed assistant bubble → click ✏️ (or double-click with a selection). Textarea appears.
  3. Click **Cancel** (or press Escape).
  4. **Result:** textarea + toolbar remain; message body not restored.
- **Why it's history-dependent:** requires `bubble._wasGenerating === false` (set during the bubble's final streaming flush). A brand-new user bubble that was never re-rendered has `_wasGenerating === undefined`, so `undefined === false` is false → guard does *not* fire → cancel works. That's why editing a fresh message may "work" while editing an agent reply "fails."

### 2. SECONDARY — "Re-render clobbers the in-progress edit, leaves stale `state.editingIndex`" (Confirmed code path, moderate likelihood)
- **Symptom:** An edit is open; a full panel re-render fires (tab switch, session load, new WS message, generation start/stop, delete). The textarea is destroyed by `scrollContainer.innerHTML = ''`. `state.editingIndex`/`state.editingInstance` are **not** reset anywhere in the render path, so they now point at a detached/wrong bubble. Subsequent Save/Cancel target a stale index (no-op or wrong bubble).
- **Mechanism:** `renderSubAgentPanel()` full-rebuild branches:
  - L4412–4417 (`currentCount < lastCount || lastCount === 0`) → `innerHTML=''` + rebuild.
  - L4435–4440 (`actualChildCount !== lastCount`, the "DOM out of sync" guard) → `innerHTML=''` + rebuild.
  Neither resets `state.editingIndex`. The only place `editingIndex` is consulted in a render path is the skip-guard at **L4393** (`... && state.editingIndex === null && ...`), which *prevents* skipping while an edit is open — but it does not protect the textarea from a full rebuild, nor does it clear stale state afterward.
- **Trigger set for `renderSubAgents()` (→ `renderSubAgentPanel`):** L1084 (session load), L2044 / L2344 (WS message handlers), L4569 (`switchMainTab`), L5873 (streaming/generation flush). Any of these landing while an edit is open can clobber it.

### 3. TERTIARY — "Generation starts while an edit is already open" (Confirmed interaction, lower likelihood)
- **Symptom:** `startEdit()` refuses to *begin* if the agent is generating (L3643–3646). But if generation **starts after** an edit is already open on a *different* (earlier) bubble, streaming deltas only touch the *last* bubble (L4508 / L4470), so the edited earlier bubble's textarea is not directly overwritten by delta appends. However, any full re-render per Failure Mode 2 still clobbers it. Net: generation-during-edit mostly manifests as Mode 2, not a direct textarea kill on the edited bubble.
- **Note:** Because `startEdit` gates on `isAgentGenerating`, you generally cannot open an edit *on* the actively-streaming bubble, which limits this path.

### Ruled out (verified)
- **Selector mismatch:** `startEdit` uses hardcoded `.msg-content` (L3661); `cancelEdit`/`finishEdit` use `'.' + contentClass()` (L3792/L3814). `contentClass()` returns `'msg-content'` (L2631–2633) → **same selector**. Not a bug.
- **Dead Cancel button:** the button is wired (`cancelBtn.onclick = () => cancelEdit(index, instanceName)` L3692; Escape→`cancelEdit` L3725). The handler *runs*; it just fails to restore content. Not a dead button.
- **Missing scrollContainer/bubble in cancelEdit:** guards at L3805/L3810 can early-return (leaving the editor stuck) if the bubble was already detached by a re-render — this is a *symptom amplifier* of Mode 2, not an independent cause.

---

## Root Cause Analysis (verified file:line anchors)

### The invariant that is broken
`updateBubbleContent()` assumes: *"if `msg.content`/`reasoning_content` and the generating-flag are unchanged since my last render, the DOM already reflects `msg`, so I can skip re-rendering."* That invariant holds for streaming **as long as nothing else mutates the bubble's DOM out-of-band.** `startEdit()` violates it: it rewrites `.msg-content.innerHTML` to the edit UI but does **not** touch the fields the guard reads.

### Evidence chain
1. **`createMessageEl()` sets baseline bookkeeping, not `_wasGenerating`.**
   - L2945 `div._prevContent = msg.content || '';`
   - L2946 `div._prevReasoning = msg.reasoning_content || '';`
   - `_wasGenerating` is **never** set here → stays `undefined` until the first full `updateBubbleContent()`.

2. **`startEdit()` mutates DOM without invalidating bookkeeping.**
   - L3648–3649 sets `state.editingIndex`/`state.editingInstance`.
   - L3661 `contentDiv = bubble.querySelector('.msg-content')`.
   - L3664 `contentDiv.innerHTML = '';` → L3665 `contentDiv.classList.add('editing')` → appends gutter+textarea (L3678–3679) and Save/Cancel toolbar (L3694–3698).
   - **No write to `bubble._prevContent`, `bubble._prevReasoning`, or `bubble._wasGenerating`.** So the guard's inputs still describe the *pre-edit* message.

3. **`updateBubbleContent()` early-exits on "unchanged."**
   - L3190–3194 reads `prevContent = bubble._prevContent`, `curContent = msg.content`, `isGenerating`.
   - **L3209:** `if (prevContent === curContent && prevReasoning === curReasoning && bubble._wasGenerating === isGenerating) { applyMsgMeta(...); return; }`
   - For a completed assistant reply: `_prevContent === msg.content` (true), reasoning equal (true), `_wasGenerating(false) === isGenerating(false)` (true) → **returns without re-render** → edit UI persists.

4. **`cancelEdit()` relies entirely on that call to restore content.**
   - L3797–3800 clears `state.editingIndex`/`editingInstance`.
   - L3814 `bubble.querySelector('.' + contentClass()).classList.remove('editing');` → cosmetic only (see CSS below).
   - L3815–3816 `updateBubbleContent(bubble, msgs[index], config)` → **early-exits** → nothing restored.

5. **`finishEdit()` is immune because it changes content first.**
   - L3776 `if (msgs[index]) msgs[index].content = newContent; // Optimistic update` → now `curContent !== prevContent` → guard at L3209 is false → full re-render runs → Save works.

### CSS confirmation (why "remove .editing" doesn't help)
- `styles.css:1528–1530`: `.msg-content.editing { padding: 0; }`. Removing the class only changes padding; it does **not** remove/hide the `.message-edit-container`, `.edit-textarea`, or `.edit-toolbar` nodes. So after a no-op cancel, the user sees the editor still present (with a slight padding shift).

### Why Save ≠ Cancel (the fingerprint)
| | `msg.content` changed? | L3209 guard | Outcome |
|---|---|---|---|
| `finishEdit` (Save) | **Yes** (L3776) | false | re-render → works |
| `cancelEdit` (Cancel) | No | true (on re-rendered bubble) | early-exit → **stuck** |

---

## Recommended Minimal Fix Plan (1–3 changes — NOT implemented here)

Goal: guarantee that leaving edit mode always forces a real re-render of the edited bubble, regardless of the "unchanged" optimization.

**Option A (smallest, most targeted) — invalidate bookkeeping in `cancelEdit` before re-render.**
In `cancelEdit()` (around L3814–3816), before calling `updateBubbleContent()`, reset the guard inputs so it cannot early-exit:
- set `bubble._prevContent = null;` and `bubble._wasGenerating = undefined;` (and optionally `_prevReasoning = null`).
- Then `updateBubbleContent(bubble, msgs[index], config)` will see a mismatch and do a full re-render, restoring the original markdown.
- **Rationale:** one localized change, no new state, mirrors what already makes Save work (a content/flag mismatch).

**Option B (more robust, recommended if you want a single source of truth) — an explicit "dirty/editing" flag.**
- In `startEdit()`, after mutating the DOM, set `bubble._editing = true;`.
- In `updateBubbleContent()`'s early-exit condition (L3209), add `&& !bubble._editing` so it never skips while a bubble is in edit mode.
- In both `cancelEdit()` and `finishEdit()`, clear `bubble._editing = false;` before calling `updateBubbleContent()`.
- **Rationale:** makes the invariant explicit ("a bubble being edited is always dirty"), protects *both* Save and Cancel, and is self-documenting. Slightly larger blast radius (touches 3 functions) but removes the latent coupling to "did content change."

**Option C (defense-in-depth for Failure Mode 2) — reset edit state on full re-render.**
In `renderSubAgentPanel()`'s full-rebuild branches (L4412–4417 and L4435–4440), after `scrollContainer.innerHTML = ''`, clear `state.editingIndex = null; state.editingInstance = null;` (the textarea is destroyed by the wipe, so the stale indices are otherwise left dangling). This prevents a later Save/Cancel from targeting a detached/wrong bubble.
- **Rationale:** independent of A/B; fixes the stale-state amplifier. Low risk (only clears state when the DOM was actually wiped).

**Suggested scope for a minimal patch:** **Option A + Option C.** A fixes the primary "can't cancel"; C prevents the stale-index clobber. Option B is a cleaner long-term alternative to A if the maintainers prefer an explicit flag over field-resetting.

### Verification approach
- **There is no DOM/browser test harness for this path.** `web_ui/` contains only standalone Node scripts that replicate *pure expressions* from `app.js`:
  - `test_content_key.js` (replicates the `contentKey` expression, L4337–4359) — `node web_ui/test_content_key.js`.
  - `test_send_message.js`.
  Neither exercises `startEdit`/`cancelEdit`/`updateBubbleContent`, which are DOM-bound (`querySelector`, `innerHTML`) and not unit-testable in bare Node without a DOM shim.
- **Primary verification = manual (browser):**
  1. Repro steps for Failure Mode 1 (edit a completed assistant reply → Cancel) must now restore the original content and remove the editor.
  2. Confirm Save still works (no regression to the working path).
  3. Confirm Escape cancels identically to the button.
  4. For Mode 2: open an edit, then switch tabs / trigger a re-render; confirm no stale `editingIndex` causes a later mis-targeted Save/Cancel.
- **Optional automated:** if a regression test is desired, introduce a jsdom-based Node test that loads `app.js`, builds one bubble via the same fields (`_prevContent`, `_wasGenerating=false`), invokes `startEdit`-equivalent DOM mutation + `cancelEdit`, and asserts `.msg-content` no longer contains `.edit-textarea`. This is new infrastructure (jsdom) not currently present — treat as optional, not required.

---

## Regression Risks

1. **Option A field-resetting:** resetting `_prevContent`/`_wasGenerating` only affects the *edited* bubble and forces one extra re-render on cancel — negligible perf cost (single bubble). No cross-bubble effect. Low risk.
2. **Option B flag:** adding `!bubble._editing` to the L3209 guard means an "editing" bubble will always full-render while flagged. Must ensure `_editing` is reliably cleared in *both* Save and Cancel (and ideally on any full re-render that destroys the editor) — otherwise a leaked flag would force redundant re-renders every tick on that bubble. Medium risk if cleanup is missed; mitigated by clearing it in the same spots as Option A.
3. **Option C state-clear:** clearing `editingIndex` on full rebuild is safe *only* because the textarea is genuinely destroyed by `innerHTML=''`. Do not clear it on the incremental (append-only) path where the editor may still be live. Low risk if scoped to the two `innerHTML=''` branches.
4. **General:** any change touching `updateBubbleContent()`'s early-exit must preserve the streaming fast paths (L3219–3296, L4508) — those depend on the guard's normal behavior for non-editing bubbles. Keep the edit-specific bypass narrow so streaming performance is unaffected.

---

## Open Questions / Remaining Unknowns
- Whether the maintainers prefer an explicit `_editing` flag (Option B) over field-resetting (Option A) — a style decision, not a correctness one.
- Exact frequency of Failure Mode 2 in practice depends on how often full re-renders fire mid-edit (tab switches, WS bursts); not measured here. Code path is confirmed; incidence is unquantified.

## Suggested Next Actions
1. Apply Option A (+C) as the minimal patch.
2. Manual-verify Failure Mode 1 repro now passes and Save is unregressed.
3. Optionally add a jsdom regression test for cancel-restores-content.
4. Update `todo.md:152` to `[x]` with a one-line root-cause note referencing this plan.
