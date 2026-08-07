# Investigation: Why `[CACHE_REBUILD]` fires and causes full context reprocessing

**Date:** 2026-08-06
**Investigator:** researcher (investigate_cache_rebuild_cause)
**Status:** Expected behavior — rebuilds correlate with new-agent first turns and config changes. Cache reuse works on subsequent turns of a persisted instance.

---

## Executive Summary

The `[CACHE_REBUILD] Rebuilding working set` message is **not a bug**. It only fires when
(1) an agent instance has never rebuilt its working set yet (cache is empty → first turn),
(2) the pool's global `_config_version` changed since the instance last rebuilt, or
(3) a conversation length regression is detected (cache longer than conversation — forced resync).

The example in the user's report — `reviewer_async_kill_fix` at `console.log:8756` with system prompt `6094→6627` — is **the instance's very first turn** (`conv_len=2`, right after `_create_and_run_agent ENTRY` at line 8744 and `starting engine.run()` at 8755). Its first rebuild is fully expected. On its **second invocation** (~13 min later, line 8955) the same instance got a clean `[CACHE_HIT] Reusing cached messages=28` — proving the cache works across turns of the same instance.

There is **no evidence of anomalous repeated rebuilds on same-instance subsequent turns**. The one pattern worth attention is the root `Maine` metadata ping-pong described in `investigation_report_prompt_drift.md` (live workspace config churn), which is config-triggered, not a defect — and it is driven by LLM-visible prefix content.

---

## 1. When does `_setup_turn()` produce a REBUILD vs HIT?

**File:** `agent_cascade/execution_engine.py`, method `ExecutionEngine._setup_turn` (lines 1567–1795).

**Cache-hit condition (lines 1594–1599):**
```python
can_use_cache = (
    instance._last_config_version == self.pool._config_version and
    instance._cached_messages and
    instance._cached_llm_messages
)
```
On a hit it extends the cached list with any new appended messages (`[CACHE_EXTEND]`, lines 1610–1622) and returns at line 1636 (`[CACHE_HIT] Reusing cached messages=...`).

**Rebuild path (line 1640):**
```python
logger.info(f"[CACHE_REBUILD] Rebuilding working set for {inst_name} (conv_len={len(conv)})")
```
This is reached if **any** of:
- `_last_config_version != pool._config_version` (config changed since instance's last rebuild),
- `_cached_messages` is empty (never rebuilt / cache cleared),
- `_cached_llm_messages` is empty,
- `_cached_len != current_len` and `cached_len > current_len` → forced resync rebuild (`[CACHE_MISMATCH]`, lines 1623–1632).

**After a rebuild**, caches are synced (lines 1786–1788):
```python
instance._cached_messages = conv
instance._cached_llm_messages = llm_messages
instance._last_config_version = self.pool._config_version
```

---

## 2. Is every new agent's first turn a rebuild? — **Yes, by design.**

`_cached_messages`/`_cached_llm_messages` default to empty lists (`agent_instance.py:280,285`) and
`_last_config_version` defaults to `-1` (`agent_instance.py:286`). A fresh/reused-but-never-set-up
instance will therefore fail the `can_use_cache` check and take the rebuild path on its first turn.
The `conv_len=2` signatures in the log (Security_op_*, investigator_prompt_drift, fix_log_level_prompt_changed,
async_shell_final_reviewer, investigate_cache_rebuild_cause) are all first-turn rebuilds. Expected.

## 3. Subsequent turns of the SAME instance → do we get hits? **Yes.**

Verified in `logs/console.log`:
- `reviewer_async_kill_fix`: first build at line 8756 (rebuild, conv_len=2); second invocation at
  line 8944 (`[INSTANCE REUSE]`), then line 8955 → **`[CACHE_HIT] Reusing cached messages=28`**. Correct behavior.
- `Maine`: rebuild at 9455 (conv_len=186, post-restart), then `[CACHE_HIT] Reusing cached messages=201` at line 9591, `[CACHE_HIT] ...=221` at 9640. Multiple subsequent hits.
- Rebuilds at `conv_len=2` for `Security_op_*` are distinct fresh instances each with their own first turn — not repeated rebuilds of one instance.

Across the whole log: **194** `[CACHE_REBUILD]` lines vs **34** `[CACHE_HIT]` lines. Instances showing 2+ rebuilds
(Maine=10, Compressor_1=4, async_shell_fixer=2, async_shell_reviewer=2) are explained by config-version bumps
and process restarts, not a same-turn churn defect (details in §6).

## 4. What invalidates the cache? (all paths that clear `_cached_messages`/`_cached_llm_messages` or bump `_config_version`)

Every mutation to the instance's conversation is routed through centralized APIs in `agent_instance.py`
that keep `_cached_messages/_cached_llm_messages` in sync and clear them when index shifts make them stale:

| Trigger | File:line | Effect on cache |
|---|---|---|
| `append_message` (add) | `agent_instance.py:349-350` | EXTEND (preserved — no rebuild) |
| `extend_messages` (add) | `agent_instance.py:365-366` | EXTEND (preserved) |
| `replace_at` (user history edit) | `agent_instance.py:383-386` | REPLACE contents (partially invalidated) |
| `insert_message_at_head` (P7 system inj.) | `agent_instance.py:407-410` | CLEAR both cached lists (indices shifted → forces rebuild) |
| `trim_tail` (rollback/retry) | `agent_instance.py:435-436` | TRIM to match |
| `retry_resume` | `agent_instance.py:469-470` | CLEAR + reset token cache |
| `_replace_all_conversation` (compression/load/reset) | `agent_instance.py:496-498` | REPLACE entirely |
| `clear` (new session) | `agent_instance.py:520-522` | CLEAR |
| `_invalidate_token_cache`-adjacent clear (line 541-543) | `agent_instance.py:539-544` | CLEAR |
| Pool `stop_session` (interrupted turns) | `agent_pool.py:1457-1462` | `_cached_messages=[]`, `_cached_llm_messages=[]`, `_last_config_version=0` → force rebuild |
| Compression pool trim | `agent_pool.py:2666-2668` | CLEAR both + reset token cache (conversation trimmed → stale) |
| Compression apply | `compression/handler.py:687-689`, `989-991` | `_invalidate_token_cache` + rebuild |
| **Global config change** | `agent_pool.py:2024` (`notify_config_changed`) | `_config_version += 1` → invalidates ALL instances' `_last_config_version` match |
| `refresh_agents` (content changed on disk) | `agent_pool.py:2015` | calls `notify_config_changed` → global bump |

So a rebuild is triggered by: any history-mutation API that clears/truncates the cache, a pool-wide trim
(compression of *another* agent clears all — note pool trim loop), `stop_session`, and any global config bump.

## 4. System prompt: rebuilt or memoized?

**Rebuilt from scratch on cache rebuild** — there is **no system-prompt memoization cache**. On every
`_setup_turn` rebuild path, `execution_engine.py:1642` loads the template and lines 1648–1726 rebuild the system
message by mutating `conv[0]`:
1. Identity rename (`You are {inst}`), line 1682.
2. `## Session Metadata` refresh via `_build_session_metadata` (line 1685), which reads **live** workspace
   folders from `operation_manager.extra_work_folders_ro/rw` (lines 695–696).
3. `## AVAILABLE AGENTS` block refresh via `_build_resources_block` (line 1703; builder at 428), reading
   live `disabled_tools`.

There IS a **byte-identical guard** for KV-cache preservation (line 1729): `if m0_content != original_content:`
only mutates when changed, logging `[CACHE_REBUILD] System prompt content CHANGED ... (len 6094→6627)` at line 1751
or `textually identical — skipping pool update` (line 1778) when unchanged. So the system prompt string is *not*
re-sent unless it changed; but it **is recomputed/compared on every rebuild**.

**Implication:** the `6094→6627` delta in the report is the expected **one-time first-run injection** of the
`## AVAILABLE AGENTS` block (skills are injected at creation at `execution_engine.py:4519`, but the AVAILABLE
AGENTS block is only injected on the first rebuild — see existing `investigation_report_prompt_drift.md` §2a). It
happens once per instance and the log confirms the instance's later turn was a clean CACHE_HIT.

## 5. The `6094→6627` / `reviewer_async_kill_fix` example — confirmed first turn

From `logs/console.log`:
- Line 8744 `[CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=reviewer_async_kill_fix ... force_fresh=False`
- Line 8745 `new instance registered in pool for reviewer_async_kill_fix`
- Line 8755 `starting engine.run()`
- Line 8756 `[CACHE_REBUILD] Rebuilding working set for reviewer_async_kill_fix (conv_len=2)` ← first turn
- Line 8757 `[CACHE_REBUILD] System prompt content CHANGED ... (len 6094→6627, first_diff@2845 ... AVAILABLE AGENTS ...)`
- Line 8800 run completed, `conv_len=2`
- ~13 min later: **Line 8943 `_create_and_run_agent ENTRY` again** → 8944 `[INSTANCE REUSE]` → 8955 `[CACHE_HIT] Reusing cached messages=28` → successful reuse.

Conclusion: the CHANGED entry is the one-time AVAILABLE AGENTS injection; the rebuild is the first turn; reuse
thereafter correctly hits cache. Matches the prior prompt-drift report (stability confirmed at console.log:8269–8282).

## 6. Global config bumps in the log (drives `Maine`/`Compressor` repeated rebuilds)

Every observed `notify_config_changed` call occurred at server (re)start — `[CONFIG] Global configuration version
incremented to 1` appears right after workspace tiered-folders init at process start
(console.log lines 71, 4478, 4782, 5259, 5339, 6838, 8451, 9419, 9737). `_config_version` restarts at 0 per
process (`agent_pool.py:330`), so every restart bumps it 0→1 and triggers a rebuild of any instance whose
`_last_config_version` was the previous process's value. The Maine rebuild at line 9756 (`conv_len=1`) is exactly a
fresh `Created main agent instance: Maine` (line 9744) — a new process/instance, expected rebuild.

The 10 rebuilds for Maine split across multiple process restarts / first-turns; no same-turn repeat anomaly found.

---

## Verdict & Recommendations

**Core finding:** CACHE_REBUILD is firing for the correct, expected reasons. There is no defect causing full
context reprocessing on subsequent same-instance turns; those turns get CACHE_HIT. The user's concern stems
primarily from **log noise**: rebuilds and the "CHANGED" prompt-injection entry are routine first-turn events,
but are logged at INFO and thus look alarming.

Recommendations (matching the prior prompt-drift investigation's log-level conclusions):

1. **Demote first-turn/normal rebuild noise to DEBUG.** Line 1640 `[CACHE_REBUILD] Rebuilding working set for
   {inst}` appears on 3–5 refactor / new-agent turns here — it's cheap to add just `force_fresh` awareness since
   it appears only after `starting engine.run()` anyway.
   - Even better signal: treat `conv_len <= 1` as "expected / no-op cached"? Check whether this early path actually
     masks the real `len > 1` case which is the actual cacheable one.

2. **Out-of-scope optional debounce:** Make these `CACHE_REBUILD`-with-CHANGED entries quieter by categorizing vs.
   config-level vs. first-run cosmetic.

3. **Re-confirmed the only real prompt-drift risk:** `Maine`'s `## Session Metadata` availability paths read *live*
   workspace folders every rebuild. If `operation_manager` reports transient/alternating folder states, the root
   agent's prefix cache is invalidated each rebuild. This is the one path worth a byte-stability guard
   (`_build_session_metadata` determinism), as noted in `investigation_report_prompt_drift.md` §4.

### Confidence
**High.** Both code paths (execution_engine.py `_setup_turn` cache logic) and runtime log evidence confirm the
behavior. The reuse-then-cache-hit for `reviewer_async_kill_fix` is directly observed.

### Open Questions
1. Is the `conv_len=2` first-turn rebuild necessary, or could we build the cache at `_create_and_run_agent` creation
   time to eliminate the first-turn rebuild entirely? (Would improve LLM prefix preservation on turn 1.)
2. Confirm whether `Maine`'s metadata ping-pong is UI-driven workspace edits vs. a transient read-order issue.