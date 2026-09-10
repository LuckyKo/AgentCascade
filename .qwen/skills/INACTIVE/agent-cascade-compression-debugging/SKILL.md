---
name: agent-cascade-compression-debugging
description: Debug AgentCascade context-compression / fallback-compression bugs (infinite compression loops, logger/pool desync, endpoint cursor issues). Use when investigating or fixing FALLBACK_COMPRESSION, forced compression, compressor empty-summary, or context-window-exceeded loops in the N:\work\WD\AgentCascade codebase.
triggers: [compression loop, fallback compression, context window exceeded, compressor empty summary, logger desync, _sync_logger_after_compression, endpoint cursor, AgentCascade compression]
---

# Debugging AgentCascade Compression Bugs

## Code layout (refactored into packages — old flat filenames are shims)
- `execution_engine.py` is a shim → real logic in `engine/` (`core.py`, `llm_call.py`, `compression_exec.py`, `helpers.py`)
- `api_router.py` is a shim → real router in `api_router_pkg/router.py` (+ `endpoints.py`)
- Compression: `compression/{handler,core,agent_invoker,helpers}.py`
- Pool: `pool/*.py`; Logger: `logger/agent_instance_logger.py`

## Key invariant (root of most compression bugs)
`compress_context()` (`compression/core.py`) rewrites the POOL (`instance.conversation`) via
`rebuild_conversation(new_history)` but does NOT touch the JSONL logger. It delegates logger-sync
to its CALLER (core.py ~645). Every compression path MUST call
`compression_handler._sync_logger_after_compression(inst_name, agent_class, op, instance)` which
does `log_inst.reset_history(conv, rewrite=True)`.

- Forced path (`handler.py`) DOES call it → pool & log stay consistent.
- Fallback path (`engine/llm_call.py` FALLBACK_COMPRESSION block ~587-810) historically did NOT →
  pool compressed but JSONL kept old large history + only an appended notification
  (`_append_and_log`, engine/core.py:201, is append-only). Recovery (`handler.py:140-149`) and
  session reload read the LOG → restore old large history into pool → context-exceeded re-fires →
  loop. This was todo.md line 123.

## Diagnostic shortcuts
- "Compressor returned empty summary" in a burst right after a user STOP = retry loop spawning
  compressors during shutdown (secondary symptom, not root cause). Check agent_invoker.py for a
  stop/halt guard before each retry.
- Post-compression "fits next endpoint" check reads `chain[0].max_input_tokens` from the ROUTER cfg
  (llm_call.py ~721-738), NOT the LLM instance's dynamically-detected limit (oai.py). If no endpoint
  assigned, this is 0 → "assume fits → break" without verifying against the real server limit.
- Endpoint cursor `_instance_endpoint_position[instance]` is a POSITIONAL index into a chain rebuilt
  from live settings each call (router.py:519-533). Reordering endpoints mid-flight makes it point at
  the wrong endpoint. Prefer identity-based cursors (keyed by api_base/model) + reset on config change.

## Always verify before trusting an investigation report
Read the cited file:line yourself; check whether the report's primary claim is marked "hypothesis"
or actually traced. Cross-check log line numbers/dates against the incident in todo.md — a report may
be analyzing a DIFFERENT incident (wrong Compressor_N / date).

See project memory `compression_loop_bug_investigation.md` for the verified root-cause chain.