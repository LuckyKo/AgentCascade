---
name: chrome-devtools
description: Uses Chrome DevTools via MCP for debugging, troubleshooting and browser automation — page targeting, element interaction, efficient data retrieval, and extension testing. Does not apply to `--slim` mode.
triggers:
  - "chrome devtools"
  - "debug web page"
  - "browser automation"
  - "inspect network requests"
  - "take snapshot"
  - "evaluate script in browser"
---

## Core concepts

- **Browser lifecycle:** starts automatically on first tool call using a persistent Chrome profile. Configure via CLI args in the MCP server config: `npx chrome-devtools-mcp@latest --help`. Extra flags: `--categoryExtensions` (extension tooling), `--memoryDebugging` (memory tooling).
- **Page targeting:** page-scoped tools need a `pageId`. Get IDs from `list_pages` or the ID returned by `new_page`. For `evaluate_script`, `pageId` is required when targeting pages — except with `--categoryExtensions`, where you may pass `serviceWorkerId` instead to evaluate inside an extension background service worker.
- **Element interaction:** `take_snapshot` returns page structure with element `uid`s for interaction. If an element isn't found, take a fresh snapshot (it may have been removed or the page changed).

## Workflow patterns

**Before interacting:** 1) navigate (`navigate_page`/`new_page`) → 2) `wait_for` if you know what to wait for → 3) `take_snapshot` with `pageId` → 4) interact using snapshot `uid`s, passing the matching `pageId`.

**Efficient data retrieval:** use `filePath` for large outputs (screenshots/snapshots/traces); use pagination (`pageIdx`, `pageSize`) + filtering (`types`) to minimize data; set `includeSnapshot: false` on input actions unless you need updated state.

**Tool selection:** automation/interaction → `take_snapshot` (text-based, faster); visual inspection → `take_screenshot`; data not in the accessibility tree → `evaluate_script`.

**Parallel execution:** you can batch multiple tool calls, but keep order navigate → wait → snapshot → interact.

## Testing an extension

Extension tools (`install_extension`, `list_extensions`, etc.) exist only when the server started with `--categoryExtensions`. If they're missing from your tool list, stop and ask the user to update MCP config and restart:
```json
{ "mcpServers": { "chrome-devtools": { "command": "npx",
  "args": ["chrome-devtools-mcp@latest", "--categoryExtensions"] } } }
```
1. **Install:** `install_extension` with the unpacked-extension path.
2. **Identify:** get the extension ID from the response or `list_extensions`.
3. **Trigger action:** `trigger_extension_action` to open the popup/side panel if applicable.
4. **Verify service worker:** `evaluate_script` with `serviceWorkerId` (omit `pageId` and `args`) for background state/actions; when evaluating in a page, pass `pageId` (omit `serviceWorkerId`).
5. **Verify page behavior:** navigate to a page where the extension operates; `take_snapshot` to confirm content scripts injected/modified elements correctly.

## Troubleshooting

If `chrome-devtools-mcp` is insufficient, point users to the DevTools UI: https://developer.chrome.com/docs/devtools (and /ai-assistance). For launch errors, see https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/troubleshooting.md.
