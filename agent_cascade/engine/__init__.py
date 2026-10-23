"""agent_cascade.engine — sub-package for the execution engine.

This package splits the former monolithic ``execution_engine.py`` into cohesive
sub-modules:

- :mod:`agent_cascade.engine.helpers`          — module-level pure/near-pure helpers
- :mod:`agent_cascade.engine.llm_call`         — LLM-call cluster (LLMCallMixin)
- :mod:`agent_cascade.engine.compression_exec` — engine-side compression (CompressionExecMixin)
- :mod:`agent_cascade.engine.tool_execution`   — tool dispatch/execution (ToolExecMixin)
- :mod:`agent_cascade.engine.memory_hint`      — memory-hint query/submit (MemoryHintMixin)
- :mod:`agent_cascade.engine.placeholder_cache` — rolling arg/output cache (PlaceholderCacheMixin)
- :mod:`agent_cascade.engine.slot_lifecycle`    — slot acquire/release/reacquire (SlotLifecycleMixin)
- :mod:`agent_cascade.engine.system_agent`      — system-agent lifecycle/launch (SystemAgentMixin)
- :mod:`agent_cascade.engine.turn_setup`        — turn-state restore/prompt/cache setup (TurnSetupMixin)
- :mod:`agent_cascade.engine.telegram_stream`  — stream-time Telegram pushes
- :mod:`agent_cascade.engine.token_budget`     — context limits/token counting (TokenBudgetMixin)
- :mod:`agent_cascade.engine.core`             — ExecutionEngine class composing the mixins

The historical import surface is preserved by the thin facade at
``agent_cascade/execution_engine.py``, which re-exports every symbol production
code imports from that path. This ``__init__`` deliberately stays lightweight
(no heavy imports) so importing the package does not pull in the full engine.
"""
