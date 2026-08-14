# Investigation: propose_skill → Security Agent Review — Full Skill Text Truncation

**Date**: 2026-08-11
**Investigator**: researcher (investigation and evidence specialist)
**Status**: Root cause CONFIRMED — truncation occurs at the source in `propose_skill.py`

---

## Executive Summary

`propose_skill` **does** route the skill through the Security agent review (via the standard approval → `ask_security` → `SecurityAdvisorHandler` pipeline), **but the full skill text never reaches the Security agent**: `propose_skill.py` truncates `skill_content` to **500 characters** before placing it into the approval `tool_args`, and that truncated dict is what gets JSON-serialized into the Security advisor's prompt. **No downstream code re-fetches the full skill content.** The Security agent reviews at most the frontmatter + ~500 chars of the body — a real security gap for injected content later in the SKILL.md.

---

## Key Findings

### 1. Where propose_skill is implemented

| File | What |
|---|---|
| `agent_cascade/tools/custom/propose_skill.py` (181 lines) | Full `ProposeSkill` tool — `call(self, params)` entry at line 55 |
| `agent_cascade/tools/custom/__init__.py` (line 13) | Registers `ProposeSkill` |
| `agent_cascade/agent_factory.py` (line 132) | Tool → class wiring (`elif tool_name == 'propose_skill'`) |
| `agent_cascade/prompts/dna.py` (lines 50, 534) | Tool listed in `AVAILABLE_TOOLS` and `TOOL_METADATA` |

### 2. Security review is triggered via the standard user-approval pipeline

`propose_skill` is **always** routed through a user approval gate (no auto-approval bypass):

1. **`propose_skill.py` lines 148-158** — calls `operation_manager.request_user_approval(agent_name=..., tool_name='propose_skill', tool_args={...}, description=...)`. This blocks until approved/rejected. There is **no** `_is_auto_approved` shortcut for propose_skill (that method only applies to file ops; `propose_skill` has no path-based exemption).
2. **`operation_manager/approval.py` lines 84-153** — `request_user_approval` stores `tool_args` verbatim into `PendingApproval` (lines 106-113) and blocks the calling thread.
3. **Security agent spawn (manual "Ask Security" click OR Auto-Ask toggle)** — the WebUI sends `ask_security` → `ws_handlers.py` `handle_ask_security` (lines 977-992) → `SecurityAdvisorHandler.run_check` (spawns daemon thread) → `_run_check_worker` → `_execute_check` (all in `agent_cascade/security_handler.py`).
4. **`security_handler.py` `_execute_check`, lines 255-261** — builds the prompt:
   ```python
   prompt = prompt_template.format(
       tool_name=ap.get('tool_name', 'unknown'),
       description=ap.get('description', ''),
       arguments=json.dumps(ap.get('tool_args', {})),   # ← truncated skill_content lives here
       ...
   )
   ```
5. **`prompts/dna.py` lines 110-123** — `SECURITY_ADVISOR_PROMPT` template interpolates `{arguments}` directly. **Nothing in the prompt template or handler re-reads the skill from disk or re-fetches full content.**

The Security agent instance is created as `Security_{request_id}` via `engine._create_system_agent(agent_class='Security', task=prompt, ...)` (lines 267-275), so the prompt **is** the task.

### 3. Truncation evidence — the smoking gun

**`agent_cascade/tools/custom/propose_skill.py` line 153:**

```python
approved, reason = self.agent_pool.operation_manager.request_user_approval(
    agent_name=agent_name,
    tool_name='propose_skill',
    tool_args={
        'skill_content': skill_content[:500] + '...' if len(skill_content) > 500 else skill_content,
        'justification': justification,
        'update_existing': is_update,
    },
    description=description,
)
```

- When `skill_content` exceeds 500 chars, only `skill_content[:500] + '...'` is sent.
- This truncated string flows unmodified: `PendingApproval.tool_args` (approval.py:106-113) → `json.dumps(tool_args)` (security_handler.py:258) → `SECURITY_ADVISOR_PROMPT {arguments}` (dna.py:114).
- **Confirmed: the Security agent sees at most 500 chars of the skill markdown.** There is no truncation downstream — the truncation is at the source and propagates.

### 4. What the Security agent actually sees vs. what gets registered

| Item | Full content to Security agent? | Evidence |
|---|---|---|
| `skill_content` (SKILL.md) | ❌ **Truncated to 500 chars** | propose_skill.py:153 |
| `description` (approval description) | ✅ Full | built from frontmatter at lines 140-146; passed as `description` → prompt line 112 |
| `justification` | ✅ Full | tool_args → prompt |
| Registered skill file | ✅ Full content | `register_skill_from_content` / `update_skill_in_place` (propose_skill.py:164-175) run on the **untruncated** `skill_content` variable |

**Implication**: content AFTER byte 500 of the SKILL.md body (prompt-injection payloads, malicious instructions) is never seen by the Security agent yet is registered verbatim after approval. The only full-content check is `validate_skill` (`skills/validator.py:40`, called from `skills/manager.py:602`) — deterministic rules only (size, frontmatter fields, body length, ≥2 known injection patterns): useful, but not a Security-agent semantic review, and it runs after approval in the registration step.

### 5. Known issue confirmation

`todo.md` line 96:
> `[ ] propose_skill needs to give the full skill text to the security agent when its checked, no truncation.`

Matches exactly what the code does.

---

## Supporting evidence (file:line table)

| # | Evidence | Location |
|---|---|---|
| 1 | Truncation: `skill_content[:500] + '...'` | `agent_cascade/tools/custom/propose_skill.py:153` |
| 2 | Approval gate always taken for propose_skill | `agent_cascade/tools/custom/propose_skill.py:148-158` |
| 3 | `tool_args` stored verbatim in `PendingApproval` | `agent_cascade/operation_manager/approval.py:106-113` |
| 4 | Security prompt: `arguments=json.dumps(ap.get('tool_args', {}))` | `agent_cascade/security_handler.py:255-261` |
| 5 | Prompt template `{arguments}` — no re-fetch | `agent_cascade/prompts/dna.py:110-123` |
| 6 | Security instance creation with `task=prompt` | `agent_cascade/security_handler.py:267-275` |
| 7 | Full content used only AFTER approval | `agent_cascade/tools/custom/propose_skill.py:164-175` |
| 8 | Deterministic (non-LLM) full-content validation | `agent_cascade/skills/validator.py:40-119`; call at `skills/manager.py:602` |
| 9 | `ask_security` WS entry → handler delegate | `agent_cascade/ws_handlers.py:977-992` |
| 10 | Todo entry for the known issue | `todo.md:96` |

---

## Confidence Level

**Confirmed** — the truncation is directly visible in source (single, unambiguous line), the data-flow from `tool_args` → `json.dumps` → prompt is fully traceable, and no downstream re-fetch exists.

---

## Open Questions

1. **Intended fix shape**: pass full `skill_content` in `tool_args`, or have `SecurityAdvisorHandler` fetch the pending skill content by another keyed reference (e.g., name/temp file)? Note: approving requests may be long-lived; a `request_id`→content map or always-inline approach are both viable. This is an implementation decision for Maine/coder.
2. **Persisted approval payloads**: WebUI forwards `tool_args` as-is — if full content is passed, the frontend renders it in the approval modal (potential UI size concern). Existing UI truncation (if any) in `web_ui/app.js` was **not** verified during this pass; the security agent's prompt is the confirmed gap regardless.
3. **Update path**: the same truncation applies to `update_existing` in-place updates (same line 153) — fix must cover both new and update flows.

---

## Suggested Next Actions

1. Change `propose_skill.py:153` to pass the **full** `skill_content` (remove the `[:500]` slice), keeping the same `tool_args` shape.
2. Verify the frontend approval modal (`web_ui/app.js`) can display large `tool_args` without its own truncation for the Security stream (quick check — Security agent's prompt source is `json.dumps(tool_args)`, not the UI, so the backend fix is the critical one).
3. Add a regression test asserting `PendingApproval.tool_args['skill_content'] == full content` for a >500-char skill (currently no tests reference ProposeSkill — `grep` found zero matches in `tests/`).
4. Re-run a manual end-to-end check: propose a long skill with a marker at the end of the body; confirm the Security agent's received prompt (via `Security_*` conversation log or a `[NO]` verdict triggered by the tail marker) contains it.

---

## Handoff Notes

- Memory saved: `.agent_lessons/propose-skill-security-truncation.md` (tags: propose-skill, security-review, truncation, approval-flow).
- No reviewer delegation performed yet.