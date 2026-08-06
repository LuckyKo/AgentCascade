---
name: startup-error-audit
description: Audit entry points for missing error handling and improve startup robustness
triggers:
  - startup
  - error handling
  - entry point
  - crash on launch
  - initialization failure
source: auto-skill
---

# Startup Error Audit

Procedure for auditing application entry points to ensure proper error handling.

## Checklist

1. Identify all entry points (CLI, API startup, background workers)
2. Verify try/except blocks around critical initialization code
3. Ensure graceful degradation when optional dependencies fail
4. Check that configuration errors produce clear messages before crashing
5. Confirm logging is initialized before other components

## Common Patterns

- Wrap database connections in retry logic with backoff
- Validate required environment variables early with descriptive errors
- Use health check endpoints to detect partial startup failures