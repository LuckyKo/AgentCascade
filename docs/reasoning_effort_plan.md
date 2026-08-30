# Reasoning Effort Per Agent Class — Implementation Plan

## Overview
Add a per-agent-class "Reasoning Effort" pulldown setting (none/low/medium/high/xhigh) that controls how much reasoning/thinking the LLM does for each agent class. The value is passed as `reasoning_effort` in the generate_cfg to the LLM API call.

## Design Decisions

### Values
| UI Value | API Value | Meaning |
|----------|-----------|---------|
| none | (not sent) | Don't include reasoning_effort param — model uses default behavior |
| low | "low" | Minimal reasoning (OpenAI o1/o3) |
| medium | "medium" | Balanced (default for OpenAI) |
| high | "high" | Maximum standard reasoning |
| xhigh | "high" | Same as high (future-proofing; some backends may add extended levels) |

### Storage
- **Per-agent-class dict** in pool settings: `reasoning_effort: Dict[str, str]` (e.g., `{"coder": "medium", "researcher": "low"}`)
- Stored alongside `disabled_tools` in the pool's UI config (persisted via `EXTRA_PERSIST_KEYS`)
- Default: empty dict `{}` (no agent has explicit reasoning effort → param not sent)

### Flow to API
1. UI sends `reasoning_effort: {agent_class: value}` in config payload
2. `config_handlers.py` validates and stores on `pool.settings.reasoning_effort` (new PoolSettings field)
3. When building the LLM call, `_build_merged_cfg()` or the instance creation path checks if the current agent class has a reasoning_effort setting
4. If set and not "none", adds `reasoning_effort: <value>` to the generate_cfg dict
5. The OAI backend passes it as a standard kwarg to `client.chat.completions.create()`
6. Backends that don't support it will either ignore it (most) or return 400 (classified as fatal by retry_policy — no retry storm)

### Where to Inject
The cleanest injection point is in the **instance creation / generate_cfg override** path:
- When an agent instance is created, check `pool.settings.reasoning_effort.get(agent_class)`
- If present and not "none", add it to `instance._generate_cfg_override['reasoning_effort']`
- This way it flows through the existing 3-layer merge (template → user override → endpoint) naturally

Alternative: Add to `_build_merged_cfg()` in llm_call.py. But that requires knowing the agent_class there, which isn't currently passed. The instance-level approach is cleaner.

### Backend Compatibility
- **OpenAI (o1/o3/r1):** Accepts `reasoning_effort` directly. ✓
- **OpenAI-compatible (llama.cpp, LM Studio, vLLM):** Most ignore unknown params. Some may 400 → fatal, no retry. Acceptable.
- **DeepSeek:** Uses `thinking_budget` (int) instead. For now, `reasoning_effort` will be ignored or 400. Follow-up: map "low"→small budget, "high"→large budget.
- **Qwen DashScope:** Uses `enable_thinking` (bool). Same as DeepSeek — follow-up mapping.

**Decision for v1:** Only send `reasoning_effort` for backends that are known to support it (OpenAI API). For other backends, the param is simply not added. We detect this by checking if the endpoint's model name matches OpenAI reasoning models (o1, o3, r1) OR if the api_base contains "api.openai.com".

Actually — simpler: just always include it in generate_cfg when set. The OAI backend already handles unknown params gracefully (they go into extra_body for non-standard keys). Let me check...

Looking at oai.py line 254-259: `extra_params = ['top_k', 'repetition_penalty', ...]` — these get moved to `extra_body`. We should add `reasoning_effort` to this list so it goes via `extra_body` (which is the correct way for OpenAI API v1).

Wait — actually `reasoning_effort` IS a standard OpenAI param now (not extra). Let me check the OpenAI SDK... In the latest SDK, `reasoning_effort` is a top-level param on `chat.completions.create()`. So it should NOT go in extra_body. It should be passed directly as a kwarg.

**Final decision:** Pass `reasoning_effort` as a standard kwarg (not in extra_body). The OAI backend's `_chat_complete_create` wrapper already passes all kwargs through to `client.chat.completions.create()`. If the model doesn't support it, OpenAI returns 400 → classified as fatal by retry_policy → no retry storm. For local servers that ignore unknown params, it's silently dropped.

### UI Integration
- **Location:** In the "Target Agent" settings section (index.html line 695), below the agent select pulldown
- **Component:** `<select id="setting-reasoning-effort">` with options: none/low/medium/high/xhigh
- **Behavior:** Changes apply to the currently-selected agent class in the per-agent settings panel
- **Sync:** Follows the same pattern as `disabled_tools` — stored per-agent-class, sent on save

### Files to Modify

| File | Change |
|------|--------|
| `agent_cascade/settings.py` | Add `REASONING_EFFORT_VALUES = ("none", "low", "medium", "high", "xhigh")` constant |
| `agent_cascade/agent_instance.py` | Add `reasoning_effort: Dict[str, str] = field(default_factory=dict)` to PoolSettings |
| `agent_cascade/config_handlers.py` | Register handler for `reasoning_effort` (validate values, store on pool.settings) |
| `agent_cascade/api_integration_pkg/state_builder.py` | Serialize/deserialize `reasoning_effort` in both directions |
| `agent_cascade/engine/llm_call.py` or instance creation path | Inject `reasoning_effort` into generate_cfg when building LLM call for an agent that has it set |
| `web_ui/index.html` | Add pulldown in Target Agent section |
| `web_ui/app.js` | Wire up the pulldown (read/write, sync with server) |

### Testing
- Unit test: config handler validates values (rejects invalid strings)
- Unit test: PoolSettings serialization round-trip
- Unit test: generate_cfg includes reasoning_effort when set for agent class
- Unit test: generate_cfg does NOT include reasoning_effort when "none" or unset
- Integration: full LLM call with mocked API verifies `reasoning_effort` kwarg is passed

### What We DON'T Do (v1)
- No backend-specific mapping (DeepSeek thinking_budget, Qwen enable_thinking) — follow-up
- No per-endpoint override (it's per-agent-class only)
- No dynamic detection of model capability (we trust the user to set appropriate values)
- No "xhigh" distinct from "high" at the API level (both map to "high" for now)
