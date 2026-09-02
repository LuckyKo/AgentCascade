# Reasoning Effort per Agent Class — Investigation

Date: 2026-08-30 · Investigator: researcher · Todo line 41

## Executive Summary

There is **no existing `reasoning_effort` setting** in AgentCascade. The only references to
`reasoning_effort` today are (a) an **error-string pattern** in `retry_policy.py` (treats a 400
"reasoning_effort not supported" as a fatal, non-retryable error) and (b) a single benchmark
config file that hardcodes `"extra_body": {"reasoning_effort": "high"}` for one GPT model.
"Reasoning" as a *feature* in the codebase refers to **reading back `reasoning_content`** from
responses (DeepSeek/Qwen thinking tokens streamed into the UI) — it does **not** control how much
the model thinks.

Implementing per-agent-class reasoning effort is cleanly achievable by **mirroring two existing
patterns**: the `disabled_tools` per-agent-class dict (for per-class storage + UI "Per Agent"
sub-tab) and the `auto_skill_mode` pulldown (for the pulldown UI + config-handler wiring). The
value flows to the API via `instance._generate_cfg_override` → `_build_merged_cfg` →
`generate_cfg` → the `ALLOWED_LLM_PARAMS` allowlist in `oai.py`. **One required change:** add
`reasoning_effort` to `ALLOWED_LLM_PARAMS` (and/or route it into `extra_body`) so it actually
reaches the API.

---

## 1. Current State (what exists today)

### 1.1 No reasoning-effort control
- `grep reasoning_effort` across the repo finds only:
  - `agent_cascade/retry_policy.py:64,70,127` — `deterministic_client_error_patterns` includes
    the literal string `'reasoning_effort'`. A 400 like
    *"Function tools with reasoning_effort are not supported for gpt-5.6-luna"* is classified
    **fatal** (no retry). This is pure error-handling; it never *sends* the param.
  - `tests/test_retry_policy.py`, `tests/test_router_cascade_breaker.py` — tests for that pattern.
  - `benchmark/deepplanning/models_config.json:31` — `"extra_body": {"reasoning_effort": "high"}`
    for `gpt-5-2025-08-07`. This is the only place the param is actually emitted, and only for a
    benchmark, not the main agent loop.
- No `reasoning_effort`, `thinking`, `enable_thinking`, `reasoning`, or `effort` field exists in
  `PoolSettings` (`agent_instance.py:717`), `constants.py`, `settings.py`, or any `generate_cfg`.

### 1.2 "Reasoning" today = reading thinking tokens back
The system already *consumes* thinking output but never *configures* it:
- `llm/oai.py:501,542` — reads `chunk.choices[0].delta.reasoning_content` and emits it as a
  `Message(reasoning_content=...)`.
- `llm/qwen_dashscope.py:95,137,167` — same, via DashScope `reasoning_content`.
- `llm/schema.py` defines `REASONING_CONTENT` key; `state_builder.py` serializes it for the UI;
  `tokens.py` counts it for token estimation.
- `settings.py:33` `REASONING_ONLY_CONTINUE_ATTEMPTS` / `SOFT_CONTINUE_NUDGE_ENABLED` — these are
  about *re-continuing a stalled reasoning stream*, not effort level. Unrelated.

**Conclusion:** the plumbing to *receive* reasoning is in place; there is no plumbing to *request*
a reasoning effort level.

### 1.3 The single benchmark emission path (proves the mechanism works)
`models_config.json` uses `extra_body` to inject `reasoning_effort`. `oai.py:254-261, 279-287`
already merges unknown sampling params into `kwargs['extra_body']`, and `extra_body` **is** in
`ALLOWED_LLM_PARAMS` (`oai.py:117`). So the transport to carry `reasoning_effort` to an
OpenAI-compatible endpoint already exists — it just is never populated from settings.

---

## 2. Backend support & accepted values

| Backend | Reasoning control | Accepted values | Notes |
|---|---|---|---|
| **OpenAI o1 / o3 / gpt-5** | `reasoning_effort` (top-level) | `low` / `medium` / `high` | `medium`/`high` only for o1-*; `low`/`medium`/`high` for o3/gpt-5. **`none` and `xhigh` are NOT valid OpenAI values.** |
| **DeepSeek (V3/R1)** | `enable_thinking` (bool) + `reasoning_effort` | `reasoning_effort`: `high` / `max` (per DeepSeek docs); `enable_thinking: true/false` | `reasoning_effort` only meaningful when `enable_thinking=true`. |
| **Qwen (DashScope / QwQ, Qwen3)** | `enable_thinking` (bool) via `extra_body` | `true` / `false` | No graded effort; on/off only. |
| **Others (Grokk, etc.)** | varies | — | Must not break on unknown params. |

**Important value mismatch:** the requested pulldown is `none / low / medium / high / xhigh`.
- `none` and `xhigh` are **not** standard OpenAI values. `none` ≈ "no reasoning" (skip the param
  / `enable_thinking:false`); `xhigh` has no OpenAI equivalent (closest is DeepSeek `max` or just
  `high`). This is a **design decision** — either (a) map non-standard values to the nearest
  supported value per backend, or (b) restrict the pulldown to values the active backend supports.
  Recommend (a) with a documented mapping table and a "pass-through if unknown" fallback.

Because AgentCascade talks to **many endpoints via the API Router** (`api_router_pkg/`), the
implementation must be **defensive**: if the resolved endpoint/model doesn't support
`reasoning_effort`, the param must be dropped (not sent) to avoid the fatal 400 that
`retry_policy.py` is already coded to detect.

---

## 3. How `generate_cfg` flows to the LLM call (the chain to hook into)

```
UI saveSettings()  ──►  ws_handlers / config_handlers (register_config_handler)
                          + state_builder._apply_ui_config(pool, instance_name, ui_cfg)
                          → instance._generate_cfg_override = {...}   (per-instance, template NOT mutated)
                                            │
   engine/llm_call.py  _build_merged_cfg(llm, instance, endpoint_cfg):
        Layer 1: llm.generate_cfg            (template defaults)
        Layer 2: instance._generate_cfg_override   (UI / per-agent override)  ← reasoning_effort lives here
        Layer 3: endpoint_cfg               (API Router per-endpoint sampler params win)
        → merged generate_cfg dict
                                            │
   llm/oai.py  _chat / _chat_stream:
        generate_cfg = {k:v for k,v in generate_cfg.items() if k in ALLOWED_LLM_PARAMS}   ← GATE (oai.py:454,696)
        → _chat_complete_create(model, messages, stream, **generate_cfg)
```

Key files:
- `agent_instance.py:279` — `AgentInstance._generate_cfg_override: Optional[dict]`.
- `api_integration_pkg/state_builder.py:990-1105` — `_apply_ui_config`: sanitizes floats/ints,
  strips `NON_LLM_KEYS` (`constants.py:183-219`), deep-copies template `generate_cfg`, updates
  with UI values, stores on `instance._generate_cfg_override`.
- `engine/llm_call.py:1263-1297` — `_build_merged_cfg` (3-layer merge).
- `llm/oai.py:113-119` — `ALLOWED_LLM_PARAMS` (the hard gate; **`reasoning_effort` is absent**).
- `llm/oai.py:254-261` — merges unknown sampling params into `extra_body`.

### Where `reasoning_effort` fits
1. **Storage:** a **per-agent-class dict** `{'Coder': 'high', 'Researcher': 'medium', ...}` on
   `PoolSettings` (e.g. `reasoning_effort_by_agent: dict`) — mirroring how `disabled_tools`
   is a per-agent dict persisted at top-level of `pool_settings.json`
   (`config_handlers.py:83` `EXTRA_PERSIST_KEYS`).
2. **Resolution at call time:** a new resolver (mirror
   `utils/disabled_tools.py:resolve_disabled_tools_for_agent`) looks up the instance's
   `agent_class` key → returns the effort value (or a default). Inject into
   `instance._generate_cfg_override['reasoning_effort']` (and, if the active endpoint is
   OpenAI-compat, it flows straight through; for Qwen/DeepSeek map to `enable_thinking`).
3. **Transport:** add `reasoning_effort` to `ALLOWED_LLM_PARAMS` in `oai.py` **or** ensure it is
   routed into `extra_body`. Because the OpenAI Python SDK already accepts `reasoning_effort` as
   a real request field for o1/o3/gpt-5, adding it to the allowlist is the lowest-friction path.
   For non-OpenAI endpoints, the param must be dropped (see §5 edge cases).

---

## 4. Per-agent-class settings pattern (what to follow)

### 4.1 `disabled_tools` — the per-agent-class precedent
- **UI storage:** `app.js:4335` `agentDisabledTools` = `{agentName: [tool,...]}` (a dict keyed by
  agent *name*). Persisted to `localStorage` + sent via `cfg.disabled_tools` (`app.js:5394`).
- **Server:** `config_handlers.py:614` `_handle_disabled_tools` → `pool.set_ui_disabled_tools(...)`;
  persisted as top-level key (`config_handlers.py:83`).
- **Live read:** `pool._ui_disabled_tools` (`agent.py:204`), resolved per-instance by
  `utils/disdisabled_tools.py:resolve_disabled_tools_for_agent(...)`.
- **UI:** "Per Agent" sub-tab (`index.html:497, 692`) with `#setting-agent-select`
  (`index.html:700`) + `renderAgentSelect()` / `renderToolsForSelectedAgent()` (`app.js:4337,4373`)
  and delegated `change` handler (`app.js:4403,4418`).

> **This is the exact structure to copy** for reasoning effort: a
> `agentReasoningEffort = {agentName: 'none'|'low'|'medium'|'high'|'xhigh'}` dict, a
> `#setting-reasoning-effort` `<select>` inside the per-agent sub-tab (rendered for the selected
> agent like the tool list), a `resolve_reasoning_effort_for_agent()` helper, and a
> `pool._ui_reasoning_effort` live cache + config handler.

### 4.2 `auto_skill_mode` — the pulldown precedent (UI + handler wiring)
The "new pulldown" to mirror (todo line 43):
- **HTML:** `index.html:568-571` `<select id="setting-auto-skill-mode">` with `<option value="basic">`.
- **JS read:** `app.js:163` binding table entry + `app.js:5325`
  `cfg.auto_skill_mode = $('#setting-auto-skill-mode').value`.
- **PoolSettings field:** `agent_instance.py:801` `auto_skill_mode: str = DEFAULT_AUTO_SKILL_MODE`.
- **Defaults:** `settings.py:413-415` `DEFAULT_AUTO_SKILL_MODE='basic'`, `AUTO_SKILL_MODE_BASIC`/`ADVANCED`.
- **Config handler:** `config_handlers.py:361-373` `_handle_auto_skill_mode` — validates value,
  falls back to default on bad input (a good safety pattern to reuse).
- **Persist key:** `config_handlers.py:43` in `POOL_SETTINGS_KEYS`; serialized in
  `state_builder.py:329`.

> For a **global default** reasoning effort (a single pulldown in the "System" sub-tab), `auto_skill_mode`
> is the exact template. For the **per-agent-class** pulldown (the actual todo), combine the
> `auto_skill_mode` pulldown *markup* with the `disabled_tools` per-agent *data structure*.

---

## 5. Recommended implementation approach

### 5.1 Setting definition (per-agent-class, with a global default)
- **`PoolSettings`** (`agent_instance.py:717`): add
  - `default_reasoning_effort: str = DEFAULT_REASONING_EFFORT`  (global fallback; default `'none'` = current behavior, no change)
  - `reasoning_effort_by_agent: dict = field(default_factory=dict)`  (`{agent_name: value}`)
- **`settings.py`**: define `DEFAULT_REASONING_EFFORT='none'` and the valid set
  `REASONING_EFFORT_VALUES = ('none','low','medium','high','xhigh')`.
- **`constants.py`**: `reasoning_effort` is **not** in `NON_LLM_KEYS` (good — it *should* reach the
  LLM). Ensure it survives `_apply_ui_config`'s sanitize pass (add to a string-passthrough; it is
  already passed through since it's not in `floats`/`ints`/`NON_LLM_KEYS`).

### 5.2 Per-agent resolution (new module, mirror `utils/disabled_tools.py`)
- `utils/reasoning_effort.py`:
  - `resolve_reasoning_effort_for_agent(instance, pool) -> Optional[str]` — look up
    `pool._ui_reasoning_effort` (live) / `pool.settings.reasoning_effort_by_agent` by
    `instance.agent_class`; fall back to `pool.settings.default_reasoning_effort`; return `None`
    if value is `'none'` (meaning "do not request reasoning").
  - A **backend value mapper** `to_backend_reasoning(model_type, value)` that translates the 5
    UI values into per-backend params (see §6 mapping).

### 5.3 Inject into the LLM call
- In the call path (where `disabled_tools` is resolved, e.g. `agent.py:_get_active_functions` and
  `engine/core.py`), resolve the agent's effort and, when non-`none`, set
  `instance._generate_cfg_override['reasoning_effort'] = mapped_value`
  (and `enable_thinking` for Qwen/DeepSeek). This lands in Layer 2 of `_build_merged_cfg`.
- **`oai.py`**: add `'reasoning_effort'` (and optionally `'enable_thinking'`) to
  `ALLOWED_LLM_PARAMS` (`oai.py:113-119`) so it passes the allowlist gate (`oai.py:454,696`).
  For models where the SDK field is rejected, rely on the existing `extra_body` fallback
  (`oai.py:254-261`) — i.e., if `reasoning_effort` is set and the endpoint is not o1/o3/gpt-5,
  move it into `extra_body` or drop it.

### 5.4 Config handler + persistence
- `config_handlers.py`: add a `_handle_reasoning_effort` (mirror `_handle_auto_skill_mode`
  at line 361) that validates the per-agent dict values against `REASONING_EFFORT_VALUES` and
  stores into `pool.settings.reasoning_effort_by_agent` + a live `pool._ui_reasoning_effort`.
- Add `'reasoning_effort'` (per-agent) to `EXTRA_PERSIST_KEYS` (`config_handlers.py:82`) and
  `'default_reasoning_effort'` to `POOL_SETTINGS_KEYS` (`config_handlers.py:29`).
- `state_builder.py`: serialize both back to the UI (`_add_pool_runtime_settings` / pool_settings
  block at line ~307).

### 5.5 UI
- **Per-agent sub-tab** (`index.html:692`): add a "Reasoning Effort" section with a
  `<select id="setting-reasoning-effort">` (options none/low/medium/high/xhigh), rendered per
  selected agent via the `renderToolsForSelectedAgent()` pattern; store in
  `agentReasoningEffort` dict (`app.js`), sent as `cfg.reasoning_effort` in `getGenerateCfg()`
  (`app.js:5303`). Add a binding-table row like `app.js:163`.
- Optionally a **global default** pulldown in the "System" sub-tab mirroring `setting-auto-skill-mode`.

---

## 6. Backend value mapping (design decision — needs confirmation)

| UI value | OpenAI (o1/o3/gpt-5) | DeepSeek | Qwen (DashScope) |
|---|---|---|---|
| `none` | *(omit param / no reasoning)* | `enable_thinking: false` | `enable_thinking: false` |
| `low` | `reasoning_effort: "low"` | `enable_thinking: true, reasoning_effort: "high"`* | `enable_thinking: true` |
| `medium` | `reasoning_effort: "medium"` | `enable_thinking: true` | `enable_thinking: true` |
| `high` | `reasoning_effort: "high"` | `reasoning_effort: "high"` | `enable_thinking: true` |
| `xhigh` | *(no equivalent → use "high")* | `reasoning_effort: "max"` | `enable_thinking: true` |

\* DeepSeek does not expose a "low" graded effort; mapping `low`→`enable_thinking:true` is a
placeholder. **This table is a proposal, not a verified spec** — each backend's accepted values
should be re-checked against current vendor docs before coding (see Open Questions). The safe
default behavior is: if the active endpoint/model is unknown or the value is unsupported, **drop
the param entirely** rather than send it (avoids the fatal 400 that `retry_policy.py` handles).

---

## 7. Edge cases

1. **Backend doesn't support the param** → drop it (do not send). This is the single most
   important guard; the codebase already treats "reasoning_effort not supported" 400s as fatal
   (`retry_policy.py:64`), so a bad send wastes the whole retry budget.
2. **Per-class vs global** → per-agent dict wins; fall back to `default_reasoning_effort`;
   `'none'` (or empty dict entry) = no reasoning (today's behavior, zero regression).
3. **`extra_body` collisions** → if the template already sets `extra_body` (e.g. a benchmark-style
   config), merge `reasoning_effort` into the existing dict rather than overwrite
   (`oai.py:258` already deep-copies before merging).
4. **Child-agent propagation** → `lifecycle_manager.py:739-754` builds a child's
   `_generate_cfg_override` from the caller's; decide whether per-agent reasoning effort propagates
   to children or each child uses its own class value (recommend: each child uses its own class
   value, mirroring how `disabled_tools` is re-resolved per child at `lifecycle_manager.py:768-815`).
5. **`xhigh` / `none` are non-standard** → mapping table (§6) + drop-on-unknown.
6. **Streaming** → reasoning effort only affects the request; response `reasoning_content` already
   streams correctly (`oai.py:501,542`), so no UI change needed for display.
7. **Sanitization** → ensure `reasoning_effort` (a string) is not accidentally coerced; it is
   passed through `_apply_ui_config` as-is (not in `floats`/`ints` lists), but confirm it isn't in
   `NON_LLM_KEYS` (it is not).

---

## 8. Files to touch (checklist)

| File | Change |
|---|---|
| `agent_cascade/settings.py` | `DEFAULT_REASONING_EFFORT`, `REASONING_EFFORT_VALUES` |
| `agent_cascade/agent_instance.py` (`PoolSettings`) | `default_reasoning_effort`, `reasoning_effort_by_agent` (+ `to_dict`/`from_dict` already generic) |
| `agent_cascade/utils/reasoning_effort.py` (new) | `resolve_reasoning_effort_for_agent`, `to_backend_reasoning` |
| `agent_cascade/config_handlers.py` | `_handle_reasoning_effort`, `POOL_SETTINGS_KEYS`/`EXTRA_PERSIST_KEYS` |
| `agent_cascade/api_integration_pkg/state_builder.py` | serialize `reasoning_effort` back to UI |
| `agent_cascade/llm/oai.py` | add `reasoning_effort` (+ `enable_thinking`) to `ALLOWED_LLM_PARAMS`; drop-on-unknown guard |
| `agent_cascade/agent.py` / `engine/core.py` | resolve per-agent effort → `instance._generate_cfg_override` |
| `web_ui/index.html` | per-agent sub-tab `<select id="setting-reasoning-effort">` |
| `web_ui/app.js` | `agentReasoningEffort` dict, binding row, `getGenerateCfg()` entry, render fn |

---

## 9. Confidence & open questions

**Confidence: High** on the *mechanism* (chain traced end-to-end with file/line evidence; the
benchmark proves `extra_body`/`reasoning_effort` already reaches the API). **Confidence:
Moderate** on the *exact backend value sets* (mapping table §6 is a proposal; vendor specs for
DeepSeek `max` and the `xhigh` mapping should be verified against current docs before coding).

Open questions to resolve with the user / before implementation:
1. Should the pulldown keep `none` and `xhigh` (non-OpenAI values), or restrict to
   `low/medium/high`? (Design decision — affects the mapping table.)
2. Per-agent-class only, or per-agent-class **plus** a global default pulldown? (Recommend both.)
3. Does reasoning effort propagate to child agents, or does each child use its own class value?
4. Which specific endpoints/models is this targeted at (to prioritize the mapping table)?

**Suggested next action:** confirm Q1–Q4, then hand the §5 checklist to the `coder` agent (with the
`auto-skill-helper-advanced` and `[[auto-skill-mode]]` lessons as reference for the UI wiring).
