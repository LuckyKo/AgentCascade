# ActivityBar Performance & Correctness Audit

**Target:** `web_ui/app.js` — the `ActivityBar` object + its call sites (vanilla-JS web UI)
**Requested by:** Maine (todo.md line 145: "audit the activity bar logic - currently kinna sus, might have some performance bugs")
**Auditor:** activity-bar-auditor
**Date:** 2026-09-20
**Scope:** Audit + report only. No code changes made. All line anchors verified against current HEAD (app.js = 6908 lines / 305 KB).

**Independent review:** All structural/perf/correctness claims (P1–P3, C1–C4) were re-verified against source by an independent reviewer — **all CONFIRMED**. The reviewer also caught that the original P1 fix proposal was not output-equivalent for the both-fields-present / content<300 case; this has been corrected to a fully-equivalent O(300) version (see P1).

---

## 1. Implementation Map (verified file:line)

### Throttle constants — `THROTTLE` object, app.js:53–66
| Constant | Value | Line | Notes |
|---|---|---|---|
| `PUSH_IMMEDIATE_MS` | **30 ms** | 54 | gates `pushImmediate()` DOM writes (~33 Hz max) |
| `ACTIVITY_BAR_RENDER_MS` | **200 ms** | 58 | gates `render()` full re-render (~5 Hz max) |

> ⚠️ The comment at app.js:120 (`lastUiUpdate: 0 // For activity bar throttling (~1Hz)`) is **stale/misleading**. `lastUiUpdate` is declared (120) and reset (5016, in `resetGenStats`) but **never read** to gate anything. There is **no ~1 Hz activity-bar throttle in use**; the real cadence during streaming is up to ~33 Hz via `pushImmediate`. See finding C4.

### The `ActivityBar` object — app.js:580–766
| Member | Line | Kind | DOM touch? |
|---|---|---|---|
| `el` / `fifoEl` / `queuedEl` | 581–583 | refs (`#globalActivityBar`, `.activity-fifo`, `.activity-queued`) | — |
| `queueBanner` | **584** and **590** (DUPLICATE) | refs (`#queueBanner`) | — |
| `queueMessageList` | 591 | ref (`#queueMessageList`) | — |
| `lastRenderTime` / `_lastPushTime` / `_initialized` | 585–587 | throttle timers + init guard | — |
| `_dedupInstance/_dedupPreview/_dedupWaiting/_dedupTokens` | 594–597 | fixed-size dedup cache (4 scalars) | — |

### Methods
| Method | Line | What it does | Call frequency | DOM writes |
|---|---|---|---|---|
| `init()` | 599–632 | One-time: grab refs, attach **delegated** click listener on `queueMessageList` + `clearAllQueueBtn`. Guarded by `_initialized`. | Once (app.js:5866) | none (only registers listeners) |
| `push(instanceName, text)` | 634–637 | Filter-check then `render(text)`. **NEVER CALLED — dead code.** | 0 | via render() |
| `_buildActivityStatus(agentData, opts)` | 645–669 | Pure string builder (status + word/token suffix). No DOM. Reads last msg via `getActivityPreview` only in its `else` branch. | per pushImmediate/render that passes gates | none |
| `pushImmediate(instance, preview, isWaiting, tokenCount)` | 671–702 | **Hot path.** filter-check → null-guard → dedup (4 fields) → 30 ms throttle → update cache → DOM writes + `renderQueueBanner`. | **Every stream tick** (feed path 2242–2250), DOM gated to ≤ ~33 Hz | `classList.toggle`, `fifoEl.textContent`, `queuedEl.style.display`, queue-banner rebuild |
| `getFilterInstance()` | 704–706 | → `getActiveInstanceName()` (app.js:560) → `getActiveAgentName()` (534). O(1), reads `state.activeSubTab`. | per pushImmediate/render | none |
| `setActiveTab(tabId)` | 708–710 | **Ignores `tabId`**, just calls `render()`. | on agent activation (4314) + user tab switch (4573) | via render() |
| `render(streamingText)` | 712–732 | 200 ms throttle → DOM writes + `renderQueueBanner`. Called with **no arg** at 4357 ⇒ `streamingText` undefined ⇒ `_buildActivityStatus` falls to `else` ⇒ recomputes `getActivityPreview(lastMsg)`. | from `renderSubAgentPanel` when visible (4357), and via `setActiveTab` | same as pushImmediate |
| `renderQueueBanner(queuedMessages)` | 735–765 | If empty → `display:none` (cheap). Else → `queueMessageList.innerHTML=''` then `createElement`+`appendChild` per message. **No dirty check.** | every throttled pushImmediate (701) + every render (731) | O(queue-length) rebuild when non-empty |

### Call sites (all verified by grep — 7 total references)
- **Feed path** app.js:2242–2250 — `ActivityBar.pushImmediate(activeInstance, getActivityPreview(lastMsg), …)` on **every** `stream_update` tick. Note: `getActivityPreview(lastMsg)` is an **argument**, so it is evaluated **before** the 30 ms throttle inside pushImmediate.
- `setActiveTab` app.js:4314 (agent becomes active) and 4573 (`switchMainTab`).
- `render()` app.js:4357 — inside `renderSubAgentPanel`, when the tab is visible. **This line runs BEFORE the content-key "nothing changed" early-exit at app.js:4404**, so it fires on every visible-panel render pass even when panel content is unchanged.
- `init()` app.js:5866.

### DOM / CSS structure (verified)
- `index.html:160–177`: `#globalActivityBar` (`.main-activity-bar`) → `.activity-status` (dot + label), `.activity-fifo` (line 166, the status text target), `.activity-queued` (line 167). Separate `#queueBanner` (171) with `#queueMessageList` (176).
- `styles.css:689–724`: `.main-activity-bar` is a small fixed flex bar (`min-height:40px`). No layout-affecting properties that would make a `textContent` write expensive.

---

## 2. Findings

### PERFORMANCE (ranked)

#### 🔴 P1 — HIGH: O(n) preview string concatenation on **every** stream tick, unthrottled
- **Where:** `getActivityPreview()` app.js:5123 — `const text = ((msg.reasoning_content || '') + (msg.content || '')).slice(-300);`
- **Why it's a problem:** This concatenates the **entire** `reasoning_content + content` string, then slices the last 300 chars. It is called from the feed path app.js:2247 as an argument to `pushImmediate`, i.e. **before** the 30 ms throttle — so it runs on **every tick regardless of throttling**. Cost is O(total message length) per tick. During a long thinking/streaming response (tens of KB of `reasoning_content`), this allocates a full-size string every ~tick → sustained transient garbage + GC pressure that grows with message size. It is also recomputed inside `render()` (via `_buildActivityStatus`'s `else` branch) on each ~200 ms panel render, so there are **two** independent O(n) preview computations during streaming.
- **Severity:** HIGH (on the hottest path; scales with message length; trivially fixable). Magnitude is small for short messages but real for long thinking/responses.
- **Fix (plan only):** Bound the work to O(300) while staying **exactly** equivalent to the original `((r||'')+(c||'')).slice(-300)`:
  ```js
  const c = msg.content || '';
  const r = msg.reasoning_content || '';
  let text;
  if (c.length >= 300) {
    text = c.slice(-300);                  // content tail alone fills the 300 window
  } else {
    text = r.slice(-(300 - c.length)) + c;  // pull only the needed reasoning tail, then all content
  }
  return getLastWords(text, 20) || 'Streaming...';
  ```
  **Equivalence (verified by independent review):** matches the original in every case — content-only, reasoning-only, both-present with `content ≥ 300` (last-300 lies entirely in content's tail), and both-present with `content < 300` (last-300 = last `(300−len(c))` of reasoning + all content). Crucially the else-branch is O(300) too, so the common *long-reasoning / empty-content* streaming case (the main cost driver) is fixed.
  ⚠️ **Rejected alternative:** the naive `(c ? c.slice(-300) : r.slice(-300))` is **NOT** equivalent — when both fields are present and `content < 300` it drops the reasoning tail (e.g. `r='A'×250, c='B'×100` → original = 250 A's + 100 B's; naive = only 100 B's). Do not use it.
  This single change also defangs the redundant O(n) recompute in `render()` (see P2), and since preview output is byte-identical, `pushImmediate`'s string dedup (app.js:676–679) behaves exactly as before.

#### 🟠 P2 — MED: Redundant double-update of the bar (`pushImmediate` + `render`)
- **Where:** `pushImmediate` app.js:671–702 **and** `render` app.js:712–732, invoked from `renderSubAgentPanel` app.js:4357.
- **Why it's a problem:** Both write the **same DOM** (`fifoEl.textContent`, `classList.toggle`, `queuedEl.style.display`, queue banner) from the **same source** (`state.subAgents[getFilterInstance()]`) and produce the same status string (both end up computing `getActivityPreview(lastMsg)`). During streaming the bar is updated ~33 Hz by `pushImmediate` *and* ~5 Hz by `render()` — the latter is pure redundancy. Compounding it, `render()` at 4357 runs **before** the content-key early-exit at 4404, so it re-renders even when panel content hasn't changed (no dirty flag ties it to actual data change).
- **Severity:** MED. Each `render()` write is cheap; the main cost is the redundant O(n) preview recompute — which **P1 largely eliminates**. Residual value of fixing P2 is modest once P1 lands.
- **Fix (plan only, OPTIONAL):** Make `pushImmediate` the single live writer and drop/gate the 4357 `render()` call during streaming; keep `render()` for the non-streaming/tab-switch paths (via `setActiveTab`). ⚠️ **Caution:** do NOT simply delete 4357 — when idle there are no ticks, so `pushImmediate` never fires and a panel re-render without a tick or tab switch would leave the bar stale. A safe variant is a dirty flag: `render()` no-ops if `pushImmediate` already wrote identical state this cycle. Given P1 removes most of the cost, treat this as low-priority hygiene.

#### 🟠 P3 — MED: `renderQueueBanner` rebuilds O(queue-length) DOM with no dirty check
- **Where:** app.js:735–765 (`queueMessageList.innerHTML = ''` then `createElement`+`appendChild` per message), called from `pushImmediate` (701) and `render()` (731).
- **Why it's a problem:** Whenever there are queued messages, the entire queue list is torn down and rebuilt on **every** throttled `pushImmediate` (~33 Hz during streaming) + every `render()`. That is O(queue-length) DOM churn per tick. When the queue is empty it's just a cheap `display:none` (no problem).
- **Severity:** MED — only bites when the queue is non-empty, but then it's real churn.
- **Fix (plan only):** Add a dirty check — cache a signature of the rendered queue (e.g. `this._queueSig = queuedMessages.join('\n')`) and early-return when unchanged *and* the banner visibility is already correct. Skips the `innerHTML=''` + rebuild for a static queue.

> **Not a finding (verified clean):** No layout/reflow **thrash** in the ActivityBar path — the object performs writes only; it contains no `offsetHeight`/`getBoundingClientRect`/`getComputedStyle` reads. The single forced reflow in `renderSubAgentPanel` (scroll read at app.js:4538) is one read after a batch of writes (normal auto-scroll), not write→read→write thrash, and is unrelated to the bar.

### CORRECTNESS (ranked)

#### ⚪ C1 — LOW: `ActivityBar.push()` is dead code
- **Where:** app.js:634–637. Never called anywhere (verified by grep — only `pushImmediate`/`render`/`setActiveTab`/`init` are referenced). Misleading leftover.
- **Fix:** Delete it (or document intent). Zero risk.

#### ⚪ C2 — LOW: Duplicate `queueBanner` property in the object literal
- **Where:** declared at app.js:584 **and** app.js:590 (`queueBanner: null` twice). The second silently wins; the first is dead. Refactoring leftover / suspicious.
- **Fix:** Remove the duplicate declaration. Zero risk.

#### ⚪ C3 — LOW: `setActiveTab(tabId)` ignores its parameter
- **Where:** app.js:708–710 — body is just `this.render()`. Works correctly only because `state.activeSubTab` is set at every call site before the call, but the unused param + name imply per-tab behavior that doesn't exist.
- **Fix:** Drop the param or actually use it. Zero risk.

#### ⚪ C4 — LOW: Dead `lastUiUpdate` field + stale "~1 Hz activity bar" comment
- **Where:** app.js:120 (declared, comment claims ~1 Hz throttle) and 5016 (reset in `resetGenStats`). Never read to gate anything.
- **Why it matters:** Misleads future readers into believing a ~1 Hz throttle exists; the real streaming cadence is up to ~33 Hz. This is likely why the bar "feels sus."
- **Fix:** Remove the field + fix/remove the comment. Zero risk.

> **Checked and clean (no bug):** No unbounded JS buffer (dedup = 4 fixed scalars; queue DOM is rebuilt via `innerHTML=''`, not accumulated — bounded by current server queue length). No repeated listener registration (dismiss uses delegation set up once in `init()`, guarded by `_initialized`). No stale-instance-after-tab-switch (bar always shows `getActiveInstanceName()` = active tab, which is set before every call site).

---

## 3. Overall Assessment — is "sus" justified?

**Partially.** The bar is structurally sound: no memory leaks, no unbounded growth, throttles reset consistently, correct instance shown after tab switches, no reflow thrash inside the object. So it's not broken.

But there **are** legitimate inefficiencies on the streaming hot path that justify the "kinna sus" gut feeling:
1. A genuine per-tick **O(n) allocation** (`getActivityPreview` full-string concat) that is *not* throttled — the single most defensible real cost (P1).
2. **Redundant double-updates** (`pushImmediate` + `render`) with no dirty flag, including a redundant O(n) preview recompute (P2).
3. **O(queue-length) DOM rebuild** on every tick whenever a queue exists (P3).

**Top 1–3 things actually worth fixing:**
1. **P1** — make `getActivityPreview` tail-only (O(300)). Highest value, lowest risk, one-line-class change; also defangs P2's cost.
2. **P3** — add a dirty check to `renderQueueBanner`. Low risk, removes real churn when queues are used.
3. **C4 + C1/C2/C3 cleanup** — dead field/comment + dead method + duplicate property + unused param. Zero-risk hygiene that also fixes the misleading "~1 Hz" story.

P2 is worth a light touch (dirty flag) but is low priority once P1 lands.

---

## 4. Recommended Minimal-Fix Plan (plan only — NOT implemented)

Ordered by value/risk. Each preserves behavior; none touches wire format or other panels.

**Step 1 — P1 (do first, biggest win):** In `getActivityPreview` (app.js:5123), replace the full-string concat with the O(300) equivalent shown in the P1 fix block above (`if (c.length >= 300) c.slice(-300) else r.slice(-(300-c.length)) + c`). Do **not** use the naive `(c ? c.slice(-300) : r.slice(-300))` — it is not equivalent when both fields are present and content < 300 (see rejected-alternative note). Verify by diffing old vs new preview strings across: content-only, reasoning-only, both-present with content ≥ 300, and both-present with content < 300.

**Step 2 — P3:** Add `this._queueSig` cache to `renderQueueBanner`; early-return when the joined queue signature is unchanged and banner visibility already matches. Keeps first render + any real change working; skips static-queue rebuilds.

**Step 3 — Hygiene (C1–C4):** delete dead `push()`; remove duplicate `queueBanner` property; drop/unused `setActiveTab` param; remove dead `lastUiUpdate` field and correct the stale comment. All zero-risk.

**Step 4 — P2 (optional, only if still worth it after Step 1):** add a dirty flag so `render()` no-ops when `pushImmediate` already wrote identical state this cycle; do **not** blindly delete the 4357 call (idle-no-tick case would go stale).

**Verification approach (no browser harness exists — web_ui tests are bare-Node pure-expression scripts):**
- `node --check web_ui/app.js` for syntax after each step.
- For P1, a small Node snippet asserting old/new `getActivityPreview` outputs match on representative messages.
- Independent reviewer pass focused on: (a) P1 output-equivalence across the 3 message shapes, (b) P3 dirty-flag not suppressing real queue changes, (c) Step 4 not breaking the idle no-tick path.

**Explicitly NOT recommended:** removing `render()` at 4357 outright (can't prove no visible change missed in the idle-no-tick case — see safe-optimization gate).
