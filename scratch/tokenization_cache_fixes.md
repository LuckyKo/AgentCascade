# Tokenization Caching Fixes - 2026-05-18

## Fix #1 — Critical: Sub-agent index mismatch (api_server.py, get_sub_agent_state)
**Problem:** Code tracked `msg_count = len(msgs)` (full unsliced count) but indexed into `active_msgs` 
(which may be shorter after `slice_history_for_llm` compression). This caused IndexError or wrong 
incremental token counts when compression reduced the active set.

**Fix:** Track `active_count = len(active_msgs)` consistently. Compute `active_msgs` once at the top,
use `active_count` for all comparisons and indexing. Session key renamed from `_count` to track
the active count instead of the raw message count.

## Fix #2 — Major: History cache broken by compression (api_server.py, both sub-agent and main)
**Problem:** The cache assumed monotonic growth (`if count > cached_count`). But `slice_history_for_llm()` 
can reduce the active set when a `<context_summary>` is found, dropping older messages. The stale cache
would then overcount tokens.

**Fix:** Added an `elif hist_count < cached_hist_count` branch that recomputes stats from scratch and
updates the cached count. Applied to both sub-agent state (`get_sub_agent_state`) and main history 
(`build_state`).

## Fix #3 — Minor: Internal cache keys leak to frontend (api_server.py, serialize_message)
**Problem:** `get_history_stats()` injects `_tokens` and `_words` keys into message dicts for caching.
These keys then leak through `serialize_message()` into the JSON sent to the frontend.

**Fix:** Added `d.pop('_tokens', None)` and `d.pop('_words', None)` in `serialize_message()` right 
after the None-value stripping, before the UI cache is stored. This ensures the internal bookkeeping
keys never reach the wire.
