# Bug Investigation: "UI wont start properly if internet is down" (todo.md line 211)

**Investigated:** 2026-09-11 · **Investigator:** ui-offline-research (researcher)
**Target repo:** `N:\work\WD\AgentCascade`

## Executive Summary

**Verdict: STILL AN ISSUE (unfixed).** The WebUI still loads all four external dependencies (Google Fonts, marked, DOMPurify, highlight.js + github-dark theme) from public CDNs, and `app.js` crashes at line 9 (`marked is not defined`) when offline — exactly the reported failure. No assets are vendored locally and no boot-level fallback exists. Only partial graceful degradation exists (system font stacks, plain-text fallback inside `renderMarkdown`), which is unreachable when offline because the top-level `marked.setOptions` call kills the entire script.

## Key Findings

### 1. Frontend served from `web_ui/` via FastAPI

`agent_cascade/api_server.py`:
- L1295: `web_ui_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web_ui'))`
- L1341: `GET /` → `FileResponse(os.path.join(web_ui_dir, 'index.html'))`
- L1343-1352: catch-all `GET /{path:path}` serves files from `web_ui/` (with path-traversal guard) and SPA-falls back to `index.html`

So the live UI is exactly `web_ui/index.html` (loads `app.js` at `index.html:930`). The root-level `baseline.html` has identical CDN refs (L9-16) but is NOT served — it's a snapshot, not part of the bug.

### 2. External CDN dependencies unchanged — `web_ui/index.html:9-16`

```html
9:   <link rel="preconnect" href="https://fonts.googleapis.com">
11:     href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
13:   <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
14:   <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>
15:   <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
16:   <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
```

Note: **`marked` is unpinned** (no version in the jsdelivr URL) — a supply-chain/availability concern beyond offline.

### 3. No vendored copies exist

Repo-wide search (excluding `.git`, `node_modules`) for `marked*.js`, `purify*.js`, `highlight*.js`, `github-dark*.css`, `*.woff`, `*.woff2`, `*.ttf` returned **zero files** (npm dependency *metadata* in `agent-cascade-docs/website/package-lock.json` was noted and excluded — no `node_modules/`, no runtime copies). There is no `vendor/` or local assets directory under `web_ui/` (only `app.js`, `index.html`, `styles.css`, `streaming_demo.html`, and dev check scripts).

### 4. The crash — `web_ui/app.js:9` (unguarded)

```js
8: // ── Markdown setup ───────────────────────────────────────────────────────────
9: marked.setOptions({
10:   breaks: true,
11:   gfm: true,
12:   highlight: (code, lang) => {
13:     if (lang && hljs.getLanguage(lang)) {
14:       try { return hljs.highlight(code, { language: lang }).value; } catch { }
15:     }
16:     return hljs.highlightAuto(code).value;   // ← also unguarded
17:   },
18: });
```

Offline ⇒ `marked` undefined ⇒ `ReferenceError` at **app.js:9** (matches the captured console error `app.js:9 Uncaught ReferenceError: marked is not defined`) ⇒ the entire `app.js` script aborts ⇒ UI never boots (no WebSocket connect, no message rendering). `hljs` (L16) is likewise unguarded.

### 5. Partial fallbacks exist but do not save the boot path

- `app.js:22` — `if (typeof DOMPurify !== 'undefined') { DOMPurify.setConfig(...) }` (guarded *config* only).
- `app.js:3272` `renderMarkdown()` — L3283-3286 and L3333-3336 wrap `DOMPurify.sanitize(marked.parse(text))` in try/catch with `<p>${escapeHtml(text)}</p>` fallback. **Unreachable offline** because L9 kills the script before these functions are ever defined/used.
- Fonts degrade gracefully: `styles.css:42-43` — `--font-sans` carries true system fallbacks (`'Inter', -apple-system, BlinkMacSystemFont, sans-serif`); `--font-mono` falls back to `'Fira Code'` then generic `monospace`. So the Google Fonts failure is cosmetic only.

## Git History Check (since 2026-08-31)

- `git log --oneline --since=2026-08-31 -- web_ui/ baseline.html` — ~30 commits, all streaming/perf/settings UI work; none touch external dependencies or offline handling.
- `git log --since=2026-08-31 -S"fonts.googleapis.com" -- web_ui/index.html` — **no output** (CDN lines untouched).
- Commit-message search (`offline`, `vendor`, `cdn`, `font`, `marked`, `fallback`) — only 2 hits, both unrelated (`compression fallback`, `no-copy behavior` test).

## Confidence Level

**Confirmed.** Direct file inspection of the served files plus git-history cross-check; evidence is from primary sources (the code itself).

## Open Questions / Assumptions

- Not reproduced live (no offline browser test run); the failure mode is inferred from code, which is deterministic (`ReferenceError` on top-level undefined identifier).
- Assumption: the running server serves from this checkout's `web_ui/` (standard deployment of `start_api_server.py`).

## Minimal Fix Plan (NOT implemented)

1. **Vendor assets** into `web_ui/vendor/`:
   - `marked.min.js` (pin a version, e.g. `marked@12.x` — currently unpinned on jsdelivr)
   - `purify.min.js` (DOMPurify 3.1.7, matching the existing pin)
   - `highlight.min.js` + `github-dark.min.css` (highlight.js 11.9.0, matching existing pin)
   - Optional: self-host Inter / JetBrains Mono woff2 files for pixel-identical fonts (system fallback stack already exists, so this is cosmetic).
2. **Update `web_ui/index.html:9-16`** to reference local paths (`vendor/...`) served by the existing catch-all static route (L1343-1352) — no server changes needed.
3. **Hardening (defense in depth):**
   - `app.js`: guard L9 with `if (typeof marked !== 'undefined') { marked.setOptions(...); }` and add a plain-text `renderMarkdown` path when `marked` is missing (the try/catch in `renderMarkdown` already provides the fallback shape).
   - Guard `hljs` usage (L13/L16) with `typeof hljs !== 'undefined'`.
   - Keep the Google Fonts `<link>` (it degrades gracefully via `styles.css:42-43`) or drop it.
4. **Update `baseline.html`** if it is kept in sync with `web_ui/index.html`.

## Suggested Next Actions

- Implement the vendor + guard fix (coder agent); verify by loading the UI with network disabled.
- Add a `vendor/` entry to `MANIFEST.in` / packaging if the package is distributed (`MANIFEST.in` currently 93 B — check whether `web_ui/*` is included).

## Memory Saved

`.agent_lessons/webui-cdn-offline-bug-still-present.md`
