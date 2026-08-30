# AUTO Skill Helper — Implementation Plan (todo.md line 43)

## Overview

When `call_agent` is invoked with `load_skill="AUTO"` in **Advanced** mode, invoke a lightweight **Security agent** with a special advisory prompt that:
1. Reviews available skills and recommends proper matches (semantic, not just keyword overlap)
2. Improves the task prompt by adding relevant notes/context
3. Validates the delegation request — denies useless calls (e.g., lazy parent handing off trivial work it could do itself)

## Architecture

### Insertion Point (CRITICAL: Before Instance Creation)

In `engine/core.py` → `_create_and_run_agent()`, the advisor MUST run **before** `lifecycle.find_or_create_instance()` is called. This ensures that on DENY, no child agent instance is ever allocated.

Current flow:
```python
# Line 2798 — instance creation happens FIRST
inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(...)

# Line 2843 — skill resolution happens AFTER (too late for deny)
if load_skill_mode_upper != LOAD_SKILL_NONE:
    loaded_skills = skill_manager.resolve_load_skill(load_skill_value, task_text, context_text)
```

**New flow (pseudocode for the exact insertion point):**

```python
def _create_and_run_agent(self, agent_class, instance_name, args, caller, nest_depth=0, force_fresh=False):
    ...
    log_file = args.get('log_file')
    
    # ── NEW: Skill Advisor gate (BEFORE find_or_create_instance) ──────
    # Conditions to run the advisor:
    #   1. load_skill resolves to AUTO mode
    #   2. auto_skill_mode == "advanced"  
    #   3. NOT force_fresh (Security/Compressor always get fresh instances)
    #   4. NOT an external load (log_file provided → new session, needs skills)
    #   5. NOT a recall (existing idle instance with valid system message)
    #   6. skill_manager exists and has registered skills
    
    advisor_result = None
    load_skill_value = args.get('load_skill') or getattr(self.pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE)
    load_skill_mode_upper = load_skill_value.strip().upper() if isinstance(load_skill_value, str) else "AUTO"
    auto_skill_mode = getattr(self.pool.settings, 'auto_skill_mode', "basic")
    skill_manager = getattr(self.pool, 'skill_manager', None)
    
    should_run_advisor = (
        load_skill_mode_upper == LOAD_SKILL_AUTO
        and auto_skill_mode == "advanced"
        and not force_fresh
        and log_file is None          # external load → skip advisor (needs fresh skills)
        and skill_manager is not None
        and len(skill_manager.get_skill_names()) > 0  # nothing to recommend
    )
    
    if should_run_advisor:
        # Early recall check — replicate the logic from line 2808 without
        # calling find_or_create_instance(). Locks held briefly, released
        # BEFORE the advisor runs (no lock held during LLM call).
        _is_early_recall = False
        with self.pool._execution._state_lock:
            existing = self.pool.instances.get(instance_name)
        
        if existing is not None:
            with existing._state_lock:
                state = existing.state
            if state in (AgentState.IDLE, AgentState.TERMINATED):
                # Check for valid system message (same as line 2808-2809)
                if existing.conversation and len(existing.conversation) > 0:
                    if getattr(existing.conversation[0], 'role', None) == SYSTEM:
                        _is_early_recall = True
        
        if not _is_early_recall:
            # Run the advisor (NO locks held — LLM call takes seconds)
            task_text = args.get('task', '')
            context_text = args.get('context', '')
            advisor_result = run_skill_advisor(
                engine=self, pool=self.pool, skill_manager=skill_manager,
                task_text=task_text, context_text=context_text,
                agent_class=agent_class, caller_name=caller,
            )
            if advisor_result.verdict == "deny":
                logger.warning("[SKILL-ADVISOR] DENIED delegation to %s: %s", instance_name, advisor_result.reason)
                # Return error — no child instance is created
                return None, [Message(role=FUNCTION, content=(
                    f"Error: Skill Advisor denied this delegation — {advisor_result.reason}. "
                    f"Consider handling this task yourself or rephrasing with more specific context."
                ))]
    
    # ── Existing flow continues UNCHANGED ─────────────────────────────
    inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(
        agent_class, instance_name, caller, nest_depth, force_fresh, log_file=log_file
    )
    ...
```

**Lock ordering and safety:**
- The early recall check acquires `pool._execution._state_lock` (brief dict lookup) then `existing._state_lock` (brief state read), releases both, THEN runs the advisor with no locks held.
- Race condition: if the instance transitions from IDLE→RUNNING between our check and `find_or_create_instance()`, the lifecycle manager will see it's active and create a new instance — which is correct behavior (advisor already approved a new instance).
- `force_fresh=True` (Security/Compressor): advisor skipped entirely. These agents always get fresh instances and don't use AUTO skill mode.
- `log_file` provided (external load): advisor skipped. External loads restore a session from disk and need the full skill resolution path (same as new instance).

### New Instance Only — Never on Recall

The advisor fires **exclusively** when building a fresh system message with skill injection. On recall (`_is_recall == True`), the existing system message is preserved byte-for-byte and `load_skill` is ignored by design — the advisor must NOT run there.

### UI Toggle: "AUTO Skill Mode" — Basic | Advanced

- **Basic** (default): Existing keyword match via `resolve_load_skill()` — unchanged behavior, no extra LLM call.
- **Advanced**: Invokes the Skill Advisor (Security agent) for semantic matching + task validation + prompt improvement.

Flow when `load_skill_mode_upper == "AUTO"` AND not a recall:
1. If mode is **Advanced** (`auto_skill_mode == "advanced"`):
   a. Run the **Skill Advisor** (Security agent with special prompt)
   b. Parse its structured output
   c. If DENIED → return error to caller (abort child creation, no instance allocated)
   d. If APPROVED → use advisor's recommended skills as the matched set + append task_notes to context
2. If mode is **Basic** OR advisor fails/times out:
   a. Fall back to existing keyword match (`resolve_load_skill`)

**Integration with Self-Augmentation (unchanged):** After skill resolution (whether from advisor or keyword match), the engine ALWAYS appends Self-Augmentation if skills are globally ON (core.py:2871-2873). The advisor's recommendations are *additional* skills that join Self-Augmentation in the `loaded_skills` list before `_inject_skills_to_system_message()` is called. The final injection looks like:
```python
loaded_skills = [advisor_recommended_skill_1, advisor_recommended_skill_2, ...]  # from advisor or keyword match
# Self-Augmentation always appended (existing logic, unchanged):
self_augmentation_instructions = skill_manager.load_full_instructions("self-augmentation")
if self_augmentation_instructions and self_augmentation_instructions not in loaded_skills:
    loaded_skills.append(self_augmentation_instructions)
_inject_skills_to_system_message(pool, sys_msg, loaded_skills)
```

### Shared Logic Extraction from Security Handler

The current auto-security flow for mutating tools works as follows:
1. Agent calls mutating tool → `request_user_approval()` blocks the agent thread
2. WebUI detects pending approval → sends `ask_security` via WebSocket
3. `SecurityAdvisorHandler.run_check()` spawns a **daemon thread** that:
   - Builds prompt under `security_prompt_lock`
   - Creates Security instance via `_create_system_agent()` + sets turn budget + tool filtering
   - Acquires `security_execution_lock` → yields caller's slot → runs `engine.run()` with streaming + first-yield timeout
   - Extracts output → parses `[YES]`/`[NO]` → auto-applies via `user_approve()`/`user_reject()`
   - Cleans up: cancels timers, reacquires slot, removes instance state

**The Skill Advisor shares the same launch pattern** (create instance → set config → run engine → extract output → cleanup) but differs in execution context (synchronous in caller's thread vs. daemon thread) and result handling (skill recommendations vs. approval verdict).

Extract a **shared lightweight advisor runner** into `agent_cascade/advisor_runner.py`:

```python
@dataclass
class AdvisorResult:
    """Structured result from an advisor agent invocation."""
    output_text: str          # Raw text output from the agent
    was_timeout: bool         # True if first-yield timeout fired
    was_error: bool           # True if engine.run() raised
    error_msg: str            # Exception message if was_error
    latency_ms: float         # Wall-clock time for the advisor call

def run_lightweight_advisor(
    pool: AgentPool,
    agent_class: str,          # e.g., 'Security' — determines turn limit + tool restrictions
    instance_name: str,        # e.g., f'Security_op_{uuid4().hex[:8]}' (same pattern as security_handler:428)
    task: str,                 # The formatted prompt
    caller: str,               # Parent agent name
    max_turns: Optional[int] = None,  # None = use SECURITY_AGENT_MAX_TURNS (20) for Security class
    first_yield_timeout: float = 30.0,
) -> AdvisorResult:
    """Run a lightweight advisor agent synchronously and return structured result.

    SHARED between Security checks (follow-up refactor) and Skill Advisor.
    Encapsulates the common launch pattern from security_handler.py:

    1. Create fresh ExecutionEngine(pool) — NOT shared, one per call
    2. Create instance via engine._create_system_agent(agent_class, instance_name, task, caller)
    3. Set turn budget:
       - If max_turns is None: use SECURITY_AGENT_MAX_TURNS (20) for 'Security' class
       - Otherwise: use provided value
    4. Apply tool filtering config (SAME as security_handler:445-464):
       - copy session generate_cfg, strip NON_LLM_KEYS
       - merge_disabled_tools_for_auto_agent(existing, agent_class, DEFAULT_SECURITY_DISABLED_TOOLS)
       - Set inst._generate_cfg_override with merged config
    5. Run engine.run() with:
       - First-yield timeout guard (threading.Timer + Event, same as security_handler:570-583)
       - Simplified loop: no streaming ticks, no WebSocket broadcast
       - Break on pool.stopped
    6. Extract output via compression.helpers.extract_instance_output()
    7. Record telemetry (pool.telemetry.record_agent_instance_call)
    8. Cleanup: mark instance inactive in instance_state, remove from active_stack

    Does NOT handle (caller's responsibility):
    - WebSocket streaming (Security handler does this in its own loop)
    - Slot yielding/reacquiring (Security runs in daemon thread; Skill Advisor is sync)
    - Global serialization locks (security_prompt_lock / security_execution_lock)
    - Approval/verdict infrastructure (operation_manager.user_approve/reject)
    - Warning timer injection (nagging the agent to hurry)
    - Active checks tracking set (duplicate prevention for concurrent approvals)
    """
```

**What's shared vs. Security handler (detailed):**

| Concern | Shared? | Where in security_handler.py | Notes |
|---------|---------|------------------------------|-------|
| `_create_system_agent()` | ✅ Already shared | Line 435 | In engine/core.py, used by both |
| Turn budget + tool filtering config | ✅ Extract to runner | Lines 443-464 | `SECURITY_AGENT_MAX_TURNS`, `merge_disabled_tools_for_auto_agent`, `NON_LLM_KEYS` |
| First-yield timeout guard | ✅ Extract to runner | Lines 570-583 | threading.Timer + Event pattern, identical logic |
| Engine run loop (simplified) | ✅ Extract to runner | Lines 600-651 | Security adds streaming ticks; runner skips those |
| Output extraction | ✅ Already shared | Line 670-671 | `compression.helpers.extract_instance_output()` |
| Instance cleanup | ✅ Extract to runner | Lines 967-990 (`_cleanup`) | Mark inactive, remove from active_stack |
| Telemetry recording | ✅ Extract to runner | Lines 655-663 | `record_agent_instance_call()` |
| WebSocket streaming | ❌ Security only | Lines 632-649 | `broadcast_stream_update()` — Skill Advisor invisible to user |
| Slot yielding/reacquiring | ❌ Security only | Lines 560-565, 712-716 | `yield_caller_slot()` / `reacquire_for()` — daemon thread pattern |
| Global serialization locks | ❌ Security only | Lines 400-545 | `security_prompt_lock` + `security_execution_lock` + ResettableRLock |
| Approval/verdict parsing | ❌ Security only | Lines 727-813 (`_parse_verdict`) | `[YES]`/`[NO]` — Skill Advisor uses `[SKILLS]`/`[NOTES]`/`[VERDICT]` |
| Warning timer injection | ❌ Security only | Lines 477-489 | Nags agent to hurry before approval timeout |
| Active checks tracking | ❌ Security only | Lines 298-303, 691 | Duplicate prevention for concurrent approval requests |

**Refactoring the Security handler (FOLLOW-UP, not in this PR):** After `run_lightweight_advisor` is proven with the Skill Advisor, refactor `security_handler.py._execute_check()` to use it for the engine-run phase. The Security handler would:
- Keep its own prompt building (under `security_prompt_lock`)
- Keep its own slot yielding/reacquiring
- Call `run_lightweight_advisor()` for the run+extract+cleanup phase
- Add its own streaming loop around the runner (or pass a callback)
- Keep its own verdict parsing and approval application

This is explicitly deferred to avoid risk to the production Security path during initial Skill Advisor development.

### New Module: `agent_cascade/skills/advisor.py`

The Skill Advisor-specific logic (prompt construction, output parsing, result interpretation):

```python
@dataclass
class SkillAdvisorResult:
    """Parsed result from the Skill Advisor."""
    verdict: str              # "approve" | "deny" | "ambiguous"
    reason: str               # Advisor's justification
    recommended_skills: List[str]  # Validated skill names
    task_notes: str           # Improved task notes (empty if none)

def run_skill_advisor(
    pool: AgentPool,
    skill_manager: SkillManager,
    task_text: str,
    context_text: str,
    agent_class: str,
    caller_name: str,
) -> SkillAdvisorResult:
    """Run the Skill Advisor and return parsed result.

    Follows the SAME rules as the Security advisor (security_handler.py):
    - Agent class: 'Security' (same template, same soul/prompt base)
    - Turn limit: SECURITY_AGENT_MAX_TURNS (20) — same as security_handler:443
    - Tool restrictions: DEFAULT_SECURITY_DISABLED_TOOLS + merge_disabled_tools_for_auto_agent()
      (blocks call_agent, shell_cmd, write_file, edit_file, etc. — read-only analysis only)
    - Instance naming: f'Security_op_{uuid4().hex[:8]}' — same pattern as security_handler:428
      (e.g., 'Security_op_a3f1b2c9') to avoid collision with tool-approval checks

    Flow:
    1. Builds prompt with skills metadata + task + context
    2. Calls run_lightweight_advisor() (shared runner) with Security class config
    3. Parses [SKILLS], [NOTES], [VERDICT] markers
    4. Validates recommended skill names against registry
    5. Returns structured result

    On timeout/error: returns SkillAdvisorResult with verdict="ambiguous"
    so the caller falls back to basic keyword match.
    """
```

### Prompt Design

**Important:** The Self-Augmentation skill is ALWAYS injected into new agent instances when skills are globally ON (independent of this advisor). The advisor only recommends *additional* skills to add on top. It should NOT recommend "self-augmentation" — that's handled automatically by the engine (core.py:2871-2873).

```
You are a skill advisor. A parent agent is about to delegate a task to a sub-agent.
The sub-agent will ALWAYS have the Self-Augmentation meta-skill injected automatically.
Your job is to recommend ADDITIONAL specialized skills that would help with this specific task.

1. RECOMMEND SKILLS: From the available skills list below, pick the most relevant ones 
   for this task. These will be ADDED to the sub-agent's skill set alongside Self-Augmentation.
   Do NOT include "self-augmentation" — it is always present automatically.
2. IMPROVE TASK: Add any missing context, constraints, or notes that would help the sub-agent succeed.
3. VALIDATE: Is this delegation justified? Deny if the parent could trivially do this itself 
   (e.g., reading a single file, a one-line grep, simple arithmetic, a task already in progress).

Available Skills (excluding self-augmentation which is always present):
{skills_metadata}

Task: {task_text}
Context: {context_text}
Target Agent Class: {agent_class}
Caller: {caller_name}

Respond in EXACTLY this format (no extra text before or after):
[SKILLS] skill1, skill2, ...   (or [SKILLS] none)
[NOTES] <improved task notes or "none">
[VERDICT] APPROVE — <reason>
OR
[VERDICT] DENY — <reason>
```

**Skills metadata construction:** When building `{skills_metadata}`, exclude `self-augmentation` from the list (it's always injected separately). Format each skill as:
```
- {skill_name}: {skill_description}
```

### Output Parsing

Parse three markers (case-insensitive, whitespace-tolerant):
- `[SKILLS]` → comma-separated skill names (validate each against `skill_manager.get_skill_names()`, skip unknowns)
- `[NOTES]` → task improvement text (append to context_text for the child agent)
- `[VERDICT]` → APPROVE/DENY + reason

**Malformed output policy:** If NO valid `[VERDICT]` marker is found (ambiguous), default to **APPROVE** with empty skills/notes and fall back to basic keyword match. Rationale: the advisor is an optimization, not a gate — failing closed would break legitimate delegations on LLM hiccups.

### Settings

New pool setting: `auto_skill_mode: str = "basic"` — values: `"basic"` | `"advanced"`

- **Basic** (default): Keyword-only matching (existing behavior)
- **Advanced**: Invokes the Skill Advisor LLM call for semantic matching + validation

**Migration:** The existing `default_load_skill_mode` setting (AUTO/NONE) is UNCHANGED. The new `auto_skill_mode` is a sub-setting that only applies when `default_load_skill_mode == "AUTO"`. No migration needed — old configs simply don't have the new field, which defaults to `"basic"` (existing behavior).

Also add to:
- `config_handlers.py` — handler for UI pulldown (Basic / Advanced)
- `state_builder.py` — serialize/deserialize (both directions)
- `agent_instance.py` PoolSettings dataclass
- Frontend: pulldown component in settings panel (next to existing "Enable skills" toggle)

### Deny Path

If the advisor returns DENY:
- Log the reason at WARNING level
- Return an error string to the calling agent: `"Error: Skill Advisor denied this delegation — {reason}. Consider handling this task yourself or rephrasing with more specific context."`
- The child agent is NOT created (advisor runs before `find_or_create_instance`)
- Telemetry: record the denial event

### Fallback / Error Handling

- If the advisor times out (>30s first-yield) → fall back to basic keyword match, log WARNING
- If the advisor crashes (exception in engine.run) → fall back to basic keyword match, log ERROR
- If no skills are recommended → proceed with no matched skills (Self-Augmentation still injected)
- If `skill_manager` has zero registered skills → skip advisor entirely (nothing to recommend), use basic path
- The advisor call must NOT block for more than ~30s (turn budget of 3 + first-yield timeout of 30s)

### Concurrency / Thread Safety

The advisor runs **synchronously in the caller's thread** (same as the existing `resolve_load_skill` call). No additional locking is needed because:
- `_create_and_run_agent()` is already called from a single thread per agent invocation
- The Security handler's complex locking (`security_prompt_lock`, `security_execution_lock`) exists to serialize *user-triggered* security checks across multiple concurrent approval requests — the Skill Advisor doesn't have that problem since it's tied to a single call_agent flow
- `_create_system_agent()` is already thread-safe (uses pool lifecycle manager with its own locks)

**Concurrent advisors:** If two different parent agents both call `call_agent` with AUTO+Advanced simultaneously, each runs its own advisor in its own thread. This is safe because:
- Each advisor creates its own unique instance name (`Security_op_{uuid4().hex[:8]}`)
- Each calls `run_lightweight_advisor()` which creates a **fresh `ExecutionEngine(self.pool)`** per invocation (not shared)
- The pool's endpoint slot system naturally serializes LLM calls via the FIFO queue
- No global lock is needed — the worst case is two advisors waiting for slots, which is bounded by the 30s first-yield timeout

### Telemetry

Record via existing `pool.telemetry`:
- `record_agent_instance_call()` for the advisor instance (latency, class="Security")
- Custom event: `skill_advisor_decision` with fields: verdict, skill_count, latency_ms, was_fallback
- Track in session stats: `skill_advisor_calls`, `skill_advisor_denials`, `skill_advisor_fallbacks`

### What We DON'T Do

- **No advisor on recall** — when reusing an existing idle agent (`_is_recall == True`), the system prompt is preserved verbatim. The advisor only runs when building a fresh system message with skill injection.
- No WebSocket streaming for the advisor (it's a background advisory, not user-visible)
- No slot yielding (the caller is already waiting; the advisor acquires its own endpoint slot via normal engine.run flow)
- No global serialization lock (unlike Security handler which serializes all checks)
- No approval/verdict infrastructure from security_handler — different output format, simpler parsing
- We do NOT refactor security_handler.py in this PR — that's a follow-up to reduce duplication after the shared runner is proven

## Files to Modify/Create

| File | Action |
|------|--------|
| `agent_cascade/advisor_runner.py` | **NEW** — Shared lightweight advisor runner (extracted pattern) |
| `agent_cascade/skills/advisor.py` | **NEW** — Skill Advisor prompt + parsing logic |
| `agent_cascade/prompts/dna.py` | Add `SKILL_ADVISOR_PROMPT` template |
| `agent_cascade/engine/core.py` | Hook into `_create_and_run_agent()` BEFORE instance creation |
| `agent_cascade/agent_instance.py` | Add `auto_skill_mode: str = "basic"` to PoolSettings |
| `agent_cascade/config_handlers.py` | Register UI handler for pulldown (Basic / Advanced) |
| `agent_cascade/api_integration_pkg/state_builder.py` | Serialize/deserialize new setting |
| `agent_cascade/settings.py` | Add `DEFAULT_AUTO_SKILL_MODE = "basic"` constant |
| Frontend settings component | Add pulldown for AUTO Skill Mode (Basic/Advanced) |

## Testing

- Unit test: advisor prompt construction (various skill counts, empty registry)
- Unit test: output parsing — valid markers, malformed, missing verdict, extra text, case variations
- Unit test: deny path returns proper error string, no instance created
- Unit test: fallback on timeout (mock engine that never yields)
- Unit test: fallback on exception (mock engine that raises)
- Unit test: zero skills registered → advisor skipped
- Unit test: recommended skill names validated against registry (unknowns filtered)
- Integration: end-to-end call_agent with AUTO + Advanced mode (mock LLM response)
- Integration: concurrent call_agent calls with Advanced mode (no deadlock)
- Regression: Basic mode unchanged (existing tests pass)

## Follow-up (NOT in this PR)

- Refactor `security_handler.py._execute_check()` to use `run_lightweight_advisor()` for its engine-run phase
- Performance benchmarking (target: <5s median advisor latency)
- Consider caching advisor decisions for identical task+skills combinations (TTL-based)
