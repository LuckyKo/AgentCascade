# web_ui_unified — Development Sandbox for Tab Unification

## Purpose

This directory is a **development copy** of `web_ui/`, used exclusively for work on the **tab unification** project (TODO item #20). The original `web_ui/` remains untouched as the live production version.

## Branch

All changes in this directory belong to the git branch: **`tab-unification`**

## File Structure

Mirrored from `web_ui/`:

| File | Role |
|------|------|
| `index.html` | Main HTML — references `styles.css` (line 17) and `app.js` (line 612) |
| `app.js` | Primary JavaScript (~132 KB) — rendering, message loop, sub-agent UI |
| `styles.css` | Stylesheet (~47 KB) — `.msg-*` and `.sub-msg-*` class systems to unify |

## How to Switch Between Versions

### Serve the development version (web_ui_unified/)

Update your server configuration or startup script to point to `web_ui_unified/` instead of `web_ui/`. For example, if your server serves static files from a `web_ui` path, change it to `web_ui_unified`.

### Revert to the live version (web_ui/)

Switch the server's static file directory back to `web_ui/`. No code changes are needed — the original is untouched.

### Quick toggle (dev environment)

If you're running locally and want to quickly swap:

1. **Dev mode:** Point your dev server / API server's `static_dir` config to `web_ui_unified/`
2. **Prod mode:** Point it back to `web_ui/`

> ⚠️ **Warning:** Never commit changes from this directory to the main branch. All tab unification work must stay on the `tab-unification` branch.

## Merge Strategy

Once all phases of the tab unification plan are complete and tested, the contents of `web_ui_unified/` will replace `web_ui/` via a merge commit on the `tab-unification` branch before merging to main.