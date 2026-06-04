# Agent Type Identifier Renaming - Summary

## Overview
Renamed agent type identifiers to eliminate underscores and use consistent naming conventions.

## Changes Made

### 1. compression_agent → Compressor

**Files Modified:**
- `agent_cascade/compression/agent_invoker.py` - Updated all agent type references from 'compression_agent' to 'Compressor'
- `agent_cascade/compression/core.py` - Updated get_agent() call
- `agent_orchestrator.py` - Updated startswith checks and exempt lists
- `web_ui/app.js` - Updated UI agent type keys and display name mappings
- `tests/test_compression.py` - Updated all test cases

**Files Renamed:**
- `agents/compression_agent_soul.md` → `agents/Compressor_soul.md`

### 2. security_advisor → Security

**Files Modified:**
- `api_server.py` - Updated all agent type references from 'security_advisor' to 'Security'
- `web_ui/app.js` - Added Security agent type to UI for endpoint assignment

**Files Renamed:**
- `agents/security_advisor_soul.md` → `agents/Security_soul.md`

## What Was Changed

### Agent Type Identifiers (Keys)
These are the internal identifiers used throughout the codebase:
- In `agent_pool.get_agent()` calls
- In `agent_pool.load_agent()` calls
- In `agent_pool.halt_instance()` calls
- In `agent_pool.enqueue_message()` calls
- In `agent_pool.sub_agent_state` keys
- In tool_args for call_agent pattern
- In UI typeToName mappings

### What Was NOT Changed

**Display Text:** User-facing text remains unchanged:
- "Compression Agent" (display name)
- "Security Advisor" (display name)

**Python Constants:** Timeout and configuration constants remain unchanged:
- `SECURITY_ADVISOR_TIMEOUT_SECONDS`
- `SECURITY_ADVISOR_WARNING_SECONDS`

**Import Statements:** Module imports remain unchanged:
- `from agent_cascade.prompts.dna import SECURITY_ADVISOR_PROMPT, COMPRESSION_MARKER`

## Testing
All modified Python files pass syntax validation. Test cases in `tests/test_compression.py` have been updated to reflect the new agent type identifiers.

## UI Fix - renderAgentApiAssignments

**Issue:** The `renderAgentApiAssignments` function only built `typeToName` from `state.agents`, missing keys that exist in `agent_priorities`. Also needed cleanup of stale old names.

**Files Modified:**
- `web_ui/app.js` (main branch) - lines 3622-3645
- `web_ui/app.js` (unified branch) - lines 3807-3830

**Fixes Applied:**

1. **Stale Key Cleanup:** Before rendering, removes any keys from `agent_priorities` that are no longer valid:
   - `compression_agent` (old key, now `compressor`)
   - `security_advisor` (old key, now `security`)  
   - `coder_agent` (defensive cleanup)

2. **Include Priorities in typeToName:** After building `typeToName` from `state.agents`, iterates over `Object.keys(priorities)` to add any missing agent types. This ensures saved endpoint assignments for unloaded agent types still display in the UI.

**Code Pattern:**
```javascript
// Cleanup stale keys first
const staleKeys = Object.keys(priorities).filter(key => {
  const staleNames = ['compression_agent', 'security_advisor', 'coder_agent'];
  return staleNames.includes(key);
});
staleKeys.forEach(key => delete priorities[key]);

// Then include any remaining priority keys in typeToName
for (const type of Object.keys(priorities)) {
  if (!typeToName[type]) {
    typeToName[type] = type.charAt(0).toUpperCase() + type.slice(1);
  }
}
```

## Migration Notes
If you have custom code or configurations that reference these agent types:
- Update any `'compression_agent'` strings to `'Compressor'`
- Update any `'security_advisor'` strings to `'Security'`
- Ensure soul files are renamed accordingly if using custom instances