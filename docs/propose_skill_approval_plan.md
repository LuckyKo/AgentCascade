# propose_skill Approval + Versioning Implementation Plan

## Summary
Add user approval gate and update-in-place versioning to `propose_skill`, reusing the same `operation_manager.request_user_approval()` pattern used by shell_cmd and file tools.

---

## 1. Parameter Changes

### New required parameter: `justification`
- Same as shell_cmd/write_file/edit_file
- Agent must explain why this skill is needed
- Used in approval description shown to user

### Optional parameter: `update_existing` (boolean, default False)
- When True and skill name already exists → update version in-place instead of rejecting
- When False and skill name exists → reject with current error message (backwards compatible)
- If omitted, tool can auto-detect intent: if proposed version > existing version, treat as update

**Updated schema:**
```python
parameters = {
    'type': 'object',
    'properties': {
        'skill_content': {...},       # unchanged
        'test_task': {...},           # unchanged
        'justification': {
            'type': 'string',
            'description': 'Why this skill is needed. Required for both new skills and updates.',
        },
        'update_existing': {
            'type': 'boolean',
            'default': False,
            'description': 'If True and skill name exists, create a new version instead of rejecting.',
        },
    },
    'required': ['skill_content', 'justification'],
}
```

---

## 2. Approval Flow

### When to request approval
- **Always** for propose_skill (no "safe" equivalent like shell_cmd's read-only commands)
- For both NEW skills and UPDATES to existing skills

### Auto-approve behavior
Two options (pick one):

**Option A: Always require approval** (recommended — skills are system-wide, high impact)
- No auto-approve path; every call goes through `request_user_approval()`

**Option B: Auto-approve updates to agent-owned skills**
- If skill was created by this agent in current session → auto-approve updates
- New skills or cross-agent updates → require approval

### Approval description shown to user

**For NEW skill:**
```
📝 **Propose New Skill**: <skill_name>

Description: <description from frontmatter>
Version: <version from frontmatter, default 1.0.0>
Justification: <agent's justification>

This will be registered and available to all agents via scan_skills/load_skill.
```

**For UPDATE:**
```
📝 **Update Existing Skill**: <skill_name>

Current version: v<old_version> → New version: v<new_version>
Justification: <agent's justification>

This will overwrite the existing SKILL.md with the updated content.
```

### Code pattern (same as shell_cmd)
```python
approved, reason = self.agent_pool.operation_manager.request_user_approval(
    agent_name=agent_name,
    tool_name='propose_skill',
    tool_args={
        'skill_content': skill_content[:500] + '...' if len(skill_content) > 500 else skill_content,
        'justification': justification,
        'update_existing': update_existing,
    },
    description=approval_description,
)

if not approved:
    return f"REJECTED: {reason}"
```

---

## 3. Update vs Create Logic

### Flowchart (in propose_skill.py call method)

```
1. Parse params → skill_content, justification, update_existing, test_task
2. Validate justification is present
3. Parse frontmatter → get proposed_name, proposed_version
4. Check if name exists in registry:

   EXISTS + update_existing=True OR version > existing:
       → Request approval for UPDATE
       → Call manager.update_skill_in_place(name, skill_content)
       → Return success with new version

   EXISTS + update_existing=False:
       → Return rejection message (current behavior)

   DOES NOT EXIST:
       → Request approval for NEW skill
       → Call manager.register_skill_from_content(...)
       → Return success
```

### Version handling for updates
- If frontmatter includes `version` field → use it
- If not → auto-increment patch from existing version (reuse logic already in propose_skill.py lines 80-89)
- Use parser.normalize_version() to validate

---

## 4. Code Changes Per File

### File: agent_cascade/tools/custom/propose_skill.py

**Change 1:** Add `justification` and `update_existing` to parameters schema (lines 27-40)

```python
parameters = {
    'type': 'object',
    'properties': {
        'skill_content': {...},
        'test_task': {...},
        'justification': {
            'type': 'string',
            'description': 'Why this skill is needed. Required for both new skills and updates.',
        },
        'update_existing': {
            'type': 'boolean',
            'default': False,
            'description': 'If True and skill name exists, create a new version instead of rejecting.',
        },
    },
    'required': ['skill_content', 'justification'],
}
```

**Change 2:** In `call()` method, after parsing params (around line 60), add:

try:
    justification = parsed.get('justification')
    update_existing = bool(parsed.get('update_existing', False))
except (AttributeError, TypeError):
    return "Invalid parameters for propose_skill"

if not justification:
    return "'justification' is required for propose_skill"


**Change 3:** Replace current duplicate-check block (lines 71-99) with approval + update logic:

```python
# Parse frontmatter for name and version
fm, _ = parse_frontmatter(skill_content)
proposed_name = fm.get('name', '') if fm else ''
proposed_version = fm.get('version', '1.0.0') if fm else '1.0.0'

if not proposed_name:
    return "Skill name is required in YAML frontmatter."

agent_name = kwargs.get('agent_instance_name', 'unknown')
existing_meta = skill_manager.get_skill_metadata(proposed_name)
is_update = existing_meta is not None

if is_update:
    existing_version = existing_meta.get('version', '1.0.0')
    
    # Auto-increment version if not provided or same as existing
    from agent_cascade.skills.parser import normalize_version
    effective_version = normalize_version(proposed_version)
    
    if effective_version == existing_version:
        # Compute next patch version (reuse existing logic)
        try:
            parts = existing_version.split('.')
            padded = parts + ['0'] * (3 - len(parts))
            effective_version = f"{padded[0]}.{padded[1]}.{int(padded[2]) + 1}"
        except (ValueError, IndexError):
            effective_version = "1.0.1"
        
        # Patch frontmatter with computed version
        skill_content = skill_content.replace(
            f'version: {proposed_version}',
            f'version: {effective_version}',
            1
        )
    
    # Reject only if no explicit update flag AND user didn't provide a higher version
    # If proposed_version > existing_version, allow implicit update even without update_existing flag
    if not update_existing and effective_version == existing_version:
        return (
            f"Skill '{proposed_name}' already exists (v{existing_version}).\n\n"
            f"To update it, set update_existing=true or provide a higher version in frontmatter."
        )
    
    # Approval for UPDATE
    description = (
        f"📝 **Update Existing Skill**: {proposed_name}\n\n"
        f"Current version: v{existing_version} → New version: v{effective_version}\n"
        f"Justification: {justification}"
    )
else:
    effective_version = normalize_version(proposed_version) or '1.0.0'
    # Approval for NEW skill
    description = (
        f"📝 **Propose New Skill**: {proposed_name}\n\n"
        f"Description: {fm.get('description', '') if fm else ''}\n"
        f"Version: v{effective_version}\n"
        f"Justification: {justification}\n\n"
        f"This will be registered and available to all agents via scan_skills/load_skill."
    )

# Request user approval (same pattern as shell_cmd)
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

if not approved:
    return f"REJECTED: {reason}"

# Proceed with registration or update
if is_update:
    success, errors = skill_manager.update_skill_in_place(
        name=proposed_name,
        skill_content=skill_content,
        source="auto-generated",
    )
else:
    success, errors = skill_manager.register_skill_from_content(
        skill_content=skill_content,
        source="auto-generated",
        task_text=test_task,
    )

if success:
    return f"Skill '{proposed_name}' registered successfully (v{effective_version})."
else:
    error_detail = '; '.join(errors) if errors else 'Unknown error'
    return f"Skill registration failed: {error_detail}"
```

---

### File: agent_cascade/skills/manager.py

**Change:** Add `update_skill_in_place()` method after `register_skill_from_content()`:

```python
def update_skill_in_place(
    self,
    name: str,
    skill_content: str,
    source: str = "auto-generated",
) -> Tuple[bool, List[str]]:
    """Update an existing skill's content and version in-place.

    Writes new SKILL.md to the same file path, updates registry entry,
    preserves old version metrics, and rebuilds matcher index.

    Args:
        name: Skill name (must exist in registry).
        skill_content: New full SKILL.md content.
        source: Provenance label.

    Returns:
        Tuple of (success, error_messages).
    """
    from agent_cascade.skills.parser import parse_skill_file, normalize_version

    with self._write_lock:
        existing = self._skills_registry.get(name)
        if not existing:
            return False, [f"Skill '{name}' not found in registry"]

        old_version = existing.get('version', '1.0.0')
        file_path = Path(existing.get('file_path', ''))

        # Validate file exists before attempting update
        if not file_path.exists():
            return False, [f"Skill file not found for '{name}': {file_path}"]

        # Parse new content to extract version and validate
        tmp_dir = None
        try:
            # Temporarily write to temp for parsing (reuse register pattern)
            import uuid as _uuid
            tmp_dir = Path(f".qwen/pending-skills/{_uuid.uuid4().hex}")
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = tmp_dir / "SKILL.md"
            tmp_file.write_text(skill_content, encoding='utf-8')
            
            parsed = parse_skill_file(tmp_file)
            new_version = normalize_version(parsed.get('frontmatter', {}).get('version', '1.0.0'))
        except Exception as e:
            # Clean up temp directory on parse failure
            if tmp_dir and tmp_dir.exists():
                try:
                    tmp_file.unlink(missing_ok=True)
                    tmp_dir.rmdir()
                except OSError:
                    pass  # Best-effort cleanup
            return False, [f"Failed to parse updated skill content: {e}"]

        # Clean up temp directory after successful parse
        if tmp_dir and tmp_dir.exists():
            try:
                (tmp_dir / "SKILL.md").unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                pass  # Best-effort cleanup

        # Preserve metrics for old version under a sub-key
        with self._metrics_lock:
            if name in self._metrics:
                metrics = self._metrics[name]
                metrics.setdefault('by_version', {})
                # Archive old version's load count (not total_loads, which is the running sum)
                old_loads = metrics['by_version'].get(old_version, 0)
                metrics['by_version'][old_version] = old_loads
                metrics['by_version'][new_version] = 0

        # Write new content to same file path (atomic via temp + replace)
        try:
            tmp_out = file_path.with_suffix('.tmp')
            tmp_out.write_text(skill_content, encoding='utf-8')
            import os as _os
            _os.replace(str(tmp_out), str(file_path))
        except Exception as e:
            return False, [f"Failed to write updated skill file: {e}"]

        # Update registry entry
        frontmatter = parsed.get('frontmatter', {})
        self._skills_registry[name].update({
            'description': frontmatter.get('description', existing.get('description', '')),
            'version': new_version,
            'triggers': frontmatter.get('triggers', existing.get('triggers', [])),
            '_parsed_data': parsed,
        })

        # Rebuild matcher index
        self._rebuild_index()

        logger.info("[SKILLS] Updated skill '%s' from v%s to v%s", name, old_version, new_version)
        return True, []
```

---

## 5. Testing Checklist

- [ ] New skill with justification → prompts approval → registers on approve
- [ ] New skill without justification → rejected immediately
- [ ] Update existing skill with update_existing=true → prompts approval → updates version
- [ ] Update existing skill without update_existing and no version bump → rejected
- [ ] Update preserves old version metrics in by_version dict
- [ ] Rejected skills don't modify registry or files
- [ ] scan_skills/load_skill work correctly after update