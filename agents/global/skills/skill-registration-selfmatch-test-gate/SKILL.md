---
name: skill-registration-selfmatch-test-gate
description: Make tests that drive AgentCascade register_skill_from_content / candidate flows pass the Tier-2 self-match gate (threshold 0.3) by overlapping generated_from_task/task_text with the description, or they are rejected before reaching the code under test.
source: auto-generated
version: "1.0.0"
triggers:
  - "self-match score below threshold"
  - "register_skill_from_content test"
  - "candidate registration test fails"
  - "skill may not match its generating task"
  - "generated_from_task"
---

## Goal
Write tests that exercise AgentCascade skill **registration / candidate-upgrade flows** without being silently rejected by the Tier-2 self-match gate, so the test actually reaches the code path under test.

## The trap
`register_skill_from_content(content, task_text=...)` runs a self-match validation: it compares the skill's `generated_from_task` field (and/or `task_text`) against the skill's description/body via TF-IDF/cosine similarity. If the score is **below 0.3**, registration returns `(False, ["Self-match score 0.000 below threshold 0.3 — skill may not match its generating task"])` and **never reaches** the candidate/registration logic you're trying to test.

Symptom: a whole batch of new tests fails identically at the `assert success` line right after `register_skill_from_content`, with the self-match error — even though your production code change is correct. The failure is in the *test fixture*, not the code.

## Procedure
### Step 1 — Confirm it's the gate, not your code
Read the assertion error. If it says "Self-match score X below threshold 0.3", stop debugging the production logic — the registration was rejected at the front door. (Distinguish from other pre-gate rejections: `MIN_SKILL_BODY_LENGTH` ~100 chars, duplicate name.)

### Step 2 — Make generated_from_task overlap the description
The self-match compares task text against the skill's own words. Use the SAME string for both so similarity is ~1.0:
```python
def _register_candidate(self, m, name, version, body_marker):
    task = f'{body_marker} candidate version for registration-flow test'  # overlaps description
    content = _make_skill_content(
        name=name,
        description=task,                 # <-- same words as task
        triggers=['regflow', 'candidate'],
        generated_from_task=task,         # <-- same words as task
    ).replace('---\n', f'---\nversion: {version}\n', 1)
    success, errors = m.register_skill_from_content(content, task_text=task)
    assert success, f'registration failed for {version}: {errors}'
```
Do NOT use a generic task string like `f'registration flow {version}'` that shares no words with the description — that scores 0.0 and is rejected.

### Step 3 — Seed the metrics entry before asserting "seated fresh" state
If your test asserts on `ratings_by_version[<B version>]` after a candidate is seated, seed the incumbent's baseline first (the real flow always has one). Helpers that early-return when no metrics entry exists (e.g. `_reset_candidate_rating_state`) will leave the key absent otherwise:
```python
with m._metrics_lock:
    entry = m._metrics.setdefault(name, {'total_loads': 0, 'by_version': {}})
    entry.setdefault('ratings_by_version', {})['1.0.0'] = {'count': 2, 'sum': 10.0, 'latest': 6.0}
```

### Step 4 — Run serially for a deterministic signal
Run the affected skill tests with `-n 0`. Under xdist (`-n auto`), the shared `agents/global/pending-skills/` tree causes intermittent Permission-denied / No-such-file failures unrelated to your change. See [[xdist-shared-tree-test-isolation]].

## Tips
- The self-match threshold (0.3) is a real gate in production, not a test artifact — your fixture content must be plausible skill content whose task text genuinely matches it.
- Repeated-char or empty-string descriptions can also score 0.0; use realistic multi-word text.
- If you need to bypass the gate for a pure state-machine test (not testing registration itself), prefer driving `_register_candidate_upgrade` / the candidate helpers directly with a pre-staged file rather than weakening the public path — but only if that's actually what's under test.
- Related: [[skill-manager-hermetic-test-fixture]] (isolation/caching fixtures), [[preexisting-test-failures-idle-wakeup-and-skill-cleanup]] (xdist flakes).
