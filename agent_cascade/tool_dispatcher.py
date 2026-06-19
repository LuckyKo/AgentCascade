"""
ToolDispatcher — Phase 4.3 of the AgentCascade Architecture Rewrite.

Handles all tool execution and call_agent routing. Extracted from ExecutionEngine
to reduce God Object complexity.

See DESIGN_REWRITE.md §4.3 for design rationale.

Design Pattern: Lazy Initialization (same as AgentLifecycleManager, CompressionHandler)
- __init__ receives pool only
- set_engine() called after ExecutionEngine construction completes
- self.engine property raises RuntimeError if accessed before initialization
"""

import time
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.agent_instance import AgentInstance

from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_TOOL_RESULT_MAX_CHARS
from agent_cascade.tool_utils import (
    MAX_SPILL_SIZE,
    mark_tool_call_truncated,
    generate_spillover_filename,
)


# ── Module-level helper functions ────────────────────────────────────────────

def _msg_role(msg: dict | Any) -> str:
    """Safely get role from dict or Message object."""
    return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')


def _msg_content(msg: dict | Any) -> str:
    """Safely get content from dict or Message object."""
    return msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')


# ── ToolDispatcher Class ─────────────────────────────────────────────────────

class ToolDispatcher:
    """Dispatches tool calls to appropriate handlers.
    
    This class handles:
    - Tool execution routing (execute_tool -> _handle_* methods)
    - call_agent sync/async paths (_run_child_sync, _run_child_async)
    - dismiss_agent logic
    - compress_context delegation to CompressionHandler
    - Tool result truncation
    
    Usage:
        dispatcher = ToolDispatcher(pool)
        engine.tool_dispatcher.set_engine(engine)  # Two-phase init
        result = dispatcher.execute_tool(instance, tool_name, args, ...)
    """
    
    def __init__(self, pool):
        """Initialize with pool reference only (lazy engine initialization).
        
        Args:
            pool: AgentPool for template lookup and agent management
        """
        self.pool = pool
        self._engine = None  # Lazy initialization
    
    @property
    def engine(self) -> 'ExecutionEngine':
        """Get engine reference, raising RuntimeError if not set."""
        if self._engine is None:
            raise RuntimeError("ToolDispatcher._engine not set — call set_engine() first")
        return self._engine
    
    def set_engine(self, engine: 'ExecutionEngine') -> None:
        """Set engine reference after all handlers constructed (two-phase init)."""
        self._engine = engine

    # ── Main Tool Execution Entry Point ──────────────────────────────────────
    
    def execute_tool(
        self,
        instance: 'AgentInstance',
        tool_name: str,
        tool_args: Any,
        llm_messages: List[Any],
        function_id: Optional[str] = None
    ) -> str:
        """Execute a tool by name.
        
        Extracted from ExecutionEngine._execute_tool() - Phase 4.3
        
        This is the main routing method that dispatches to appropriate handlers:
        - call_agent → handle_call_agent()
        - dismiss_agent → handle_dismiss_agent()
        - compress_context → CompressionHandler.handle_compress_tool()
        - Generic tools → template._call_tool()
        
        Args:
            instance: The agent calling the tool
            tool_name: Name of the tool to execute
            tool_args: Arguments for the tool (str or dict)
            llm_messages: Current conversation messages
            function_id: The LLM's tool_call_id for this tool call (optional)
            
        Returns:
            Tool execution result as a string
        """
        if tool_name == 'call_agent':
            resolved = self.engine._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            if resolved is None:
                logger.warning("_resolve_placeholders returned None for %s", instance.instance_name)
            result = self.handle_call_agent(resolved, llm_messages, instance, function_id=function_id)
            logger.debug("handle_call_agent returned type=%s", type(result).__name__)
            self.engine._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result
        elif tool_name == 'dismiss_agent':
            resolved = self.engine._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            result = self.handle_dismiss_agent(resolved, instance)
            self.engine._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result
        elif tool_name == 'compress_context':
            resolved = self.engine._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            result = self.engine.compression_handler.handle_compress_tool(resolved, instance, instance.instance_name)
            self.engine._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result
        else:
            # Standard tool execution via template's function_map
            template = self.pool.get_template(instance.agent_class)
            if not template:
                raise ValueError(f"No template for agent class {instance.agent_class}")

            resolved = self.engine._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            if resolved is None:
                return f"Error: Invalid JSON arguments for tool '{tool_name}'."

            result = template._call_tool(
                tool_name, resolved,
                agent_instance_name=instance.instance_name,
                agent_obj=self.engine,
                messages=llm_messages,
            )
            self.engine._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result

    # ── call_agent Handlers ──────────────────────────────────────────────────
    
    def handle_call_agent(
        self,
        args: Any,
        messages: List[Any],
        instance: 'AgentInstance',
        function_id: Optional[str] = None
    ) -> str:
        """Handle call_agent tool call.
        
        Extracted from ExecutionEngine._handle_call_agent() - Phase 4.3
        
        This method orchestrates the entire call_agent flow:
        1. Validates arguments via _validate_call_agent_args()
        2. Handles recursive self-call cloning
        3. Checks for class mismatch on existing instances
        4. Checks nesting depth via _check_nesting_depth()
        5. Routes to sync or async path based on slot collision detection
        
        Args:
            args: Tool arguments (instance_name, agent_class, task)
            messages: Caller's conversation messages
            instance: The calling agent instance
            function_id: The LLM's tool_call_id for this async call (optional)
            
        Returns:
            Result string from the called agent or error message
        """
        caller_name = instance.instance_name
        
        # Extracted to _validate_call_agent_args() - Phase 4.3
        instance_name, agent_class, error = self._validate_call_agent_args(args, caller_name)
        if error:
            return error

        # P2: Recursive self-call cloning — prevent state corruption on self-delegation
        with self.pool._execution._state_lock:
            if any(n == instance_name for n, _depth in self.pool._execution.active_stack):
                count = sum(1 for n, _depth in self.pool._execution.active_stack if n == instance_name)
                original_instance = instance_name
                instance_name = f"{instance_name}_child{count}"
                logger.debug("Recursive self-call - cloning %s to %s", original_instance, instance_name)

        # P5: Class mismatch detection — clear history if class differs on existing instance
        existing_class = self.pool.instance_classes.get(instance_name)
        if existing_class and agent_class and existing_class != agent_class:
            logger.warning("call_agent class mismatch - %s/%s exists as %s, requested %s", 
                          caller_name, instance_name, existing_class, agent_class)
            return (f"Error: Agent '{instance_name}' already exists as '{existing_class}'. "
                    f"Cannot create as '{agent_class}'. Use a different instance name.")

        # Extracted to _check_nesting_depth() - Phase 4.3
        caller_depth = 0
        if caller_inst := self.pool.get_instance(instance.instance_name):
            caller_depth = getattr(caller_inst, '_nest_depth', 0)
        child_depth = caller_depth + 1
        
        depth_error = self._check_nesting_depth(instance, child_depth)
        if depth_error:
            return depth_error

        # ── Slot Collision Detection: Fake Sync Mode ────────────────────────
        # When the caller holds a concurrency slot, using ASYNC path causes deadlock:
        # 1. Caller holds the slot and continues making LLM calls
        # 2. Child is submitted to ThreadPoolExecutor but can't acquire the slot
        # 3. Child's engine.run() tries to acquire the slot at line 348, but same thread 
        #    already holds it via run_child_agent() (agent_pool.py:1284) → DEADLOCK with Semaphore(1)
        #
        # Fix: Check if caller holds a slot. If so, use SYNC mode — run child directly
        # and return actual result. This avoids deadlock and ensures child can make progress.
        
        # Check if caller currently holds the concurrency slot
        caller_slot_holder = self.pool.get_instance(caller_name)
        caller_holds_slot = False
        if caller_slot_holder and hasattr(caller_slot_holder, '_slot_release') and caller_slot_holder._slot_release is not None:
            caller_holds_slot = True
        
        if caller_holds_slot:
            # Extracted to _run_child_sync() - Phase 4.3
            return self._run_child_sync(agent_class, instance_name, args, caller_slot_holder, caller_name, child_depth)
        else:
            # Extracted to _run_child_async() - Phase 4.3
            return self._run_child_async(caller_name, function_id, agent_class, instance_name, args, child_depth)

    def handle_dismiss_agent(
        self,
        args: Any,
        instance: 'AgentInstance'
    ) -> str:
        """Handle dismiss_agent tool call.
        
        Extracted from ExecutionEngine._handle_dismiss_agent() - Phase 4.3
        
        Removes another agent from the pool. Prevents dismissing self or supervisor.
        
        Args:
            args: Tool arguments (instance_name)
            instance: The calling agent instance
            
        Returns:
            Result string confirming dismissal or error message
        """
        if args is None:
            # JSON parsing failed in _resolve_placeholders — return error
            return 'Error: Invalid JSON arguments.'

        target_name = args.get('instance_name', '')
        if not target_name:
            return "Error: dismiss_agent requires instance_name."

        # Don't allow dismissing self or the root agent
        if target_name == instance.instance_name:
            return f"Error: Cannot dismiss yourself ({target_name})."
        if instance.parent_instance and target_name == instance.parent_instance:
            return f"Error: Cannot dismiss your supervisor ({target_name})."

        self.pool.dismiss_instance(target_name)
        return f"[Agent '{target_name}' dismissed successfully.]"

    # ── call_agent Sub-Methods (extracted from ExecutionEngine._handle_call_agent) ───────────
    
    def _run_child_sync(
        self,
        agent_class: str,
        instance_name: str,
        args: Any,
        caller_slot_holder: 'AgentInstance',
        caller_name: str,
        child_depth: int
    ) -> str:
        """Run child agent synchronously (caller holds slot).
        
        Extracted from ExecutionEngine._handle_call_agent() - Phase 4.3
        
        This method:
        1. Releases caller's slot
        2. Runs _create_and_run_agent()
        3. Re-acquires caller's slot via _reacquire_caller_slot() (in finally block for FIX 3)
        4. Extracts and returns result
        
        Args:
            agent_class, instance_name, args, caller_slot_holder, caller_name, child_depth
            
        Returns:
            Result string from child agent
        """
        # Local import to match pattern in agent_pool.py:1287
        from agent_cascade.compression.helpers import extract_instance_output
        
        sync_path_start = time.monotonic()
        
        # Release caller's slot so the child can acquire it inside engine.run()
        if caller_slot_holder and hasattr(caller_slot_holder, '_slot_release') and caller_slot_holder._slot_release is not None:
            logger.debug(
                f"[SLOT_SYNC_RELEASE] Releasing slot for '{caller_name}' before running sync child '{instance_name}'"
            )
            self.engine._release_slot(caller_slot_holder, caller_name, "sync child")
            logger.debug(
                f"[SLOT_SYNC_RELEASE] Slot released for '{caller_name}', active agents can now acquire"
            )
        
        inst = None
        conv = None
        
        try:
            # Run child synchronously via _create_and_run_agent
            force_fresh = agent_class in ('security', 'compressor')  # Lowercase comparison
            logger.debug(
                f"[SLOT_SYNC_CHILD_START] Starting sync child '{instance_name}' ({agent_class}) for caller '{caller_name}'"
            )
            inst, conv = self.engine._create_and_run_agent(agent_class, instance_name, args, caller_name, child_depth, force_fresh=force_fresh)
            logger.debug(
                f"[SLOT_SYNC_CHILD_COMPLETE] Sync child '{instance_name}' completed in {time.monotonic() - sync_path_start:.2f}s"
            )
            
        finally:
            # FIX 3: Always re-acquire caller's slot, even on early exit due to stop
            # This ensures the parent agent can continue its turn after child execution ends
            logger.debug(
                f"[SLOT_SYNC_REACQUIRE] Attempting to re-acquire slot for '{caller_name}' after sync child"
            )
            if not self._reacquire_caller_slot(caller_slot_holder, caller_name, "sync child"):
                logger.warning(
                    f"[SLOT_SYNC_REACQUIRE_FAILED] Failed to re-acquire slot for '{caller_name}' after sync child. "
                    f"Total SYNC path elapsed: {time.monotonic() - sync_path_start:.2f}s"
                )
            else:
                logger.debug(
                    f"[SLOT_SYNC_REACQUIRED] Successfully re-acquired slot for '{caller_name}'. "
                    f"Total SYNC path elapsed: {time.monotonic() - sync_path_start:.2f}s"
                )
        
        # Check if execution was stopped or child was terminated (FIX 4)
        # REVIEWER FIX: Active stack check removed - by the time we reach here, 
        # _create_and_run_agent()'s finally block already removed the instance from active_stack
        was_stopped = self.pool.stopped
        was_terminated = instance_name in self.pool.terminated_instances
        
        # Consolidated null/empty check
        if inst is None or not conv:
            logger.warning("SYNC path FAILED - %s creation returned inst=%s", instance_name, inst)
            return f"Error: Agent '{instance_name}' execution failed with no output."

        # Extract and format result — provide clear feedback for stopped/terminated agents (FIX 4)
        if was_stopped:
            result = extract_instance_output(conv, instance_name, was_terminated=False)
            return f"[Agent '{instance_name}' Stopped]: Execution was stopped by user.\n{result}"
        elif was_terminated:
            result = extract_instance_output(conv, instance_name, was_terminated=True)
            return f"[Agent '{instance_name}' Terminated]:\n{result}"
        else:
            result = extract_instance_output(conv, instance_name, was_terminated=False)
            return f"[Agent '{instance_name}' Completed]:\n{result}"

    def _run_child_async(
        self,
        caller_name: str,
        function_id: Optional[str],
        agent_class: str,
        instance_name: str,
        args: dict,
        child_depth: int
    ) -> str:
        """Run child agent asynchronously via register_async_call.
        
        Extracted from ExecutionEngine._handle_call_agent() - Phase 4.3
        
        Args:
            caller_name, function_id, agent_class, instance_name, args, child_depth
            
        Returns:
            Async confirmation message
        """
        logger.debug("Taking ASYNC path - %s calls %s/%s at depth %d", 
                    caller_name, instance_name, agent_class, child_depth)
        
        # Register and launch agent asynchronously via AsyncToolRegistry.
        self.pool.register_async_call(
            instance_name=caller_name,
            function_id=function_id,
            agent_class=agent_class,
            child_instance_name=instance_name,
            args=args,
            caller=caller_name,
            nest_depth=child_depth,
        )

        logger.debug("ASYNC - %s launched by %s", instance_name, caller_name)
        return f"Agent '{instance_name}' launched asynchronously. Waiting for result."

    def _reacquire_caller_slot(
        self,
        slot_holder: 'AgentInstance',
        slot_holder_name: str,
        context_label: str
    ) -> bool:
        """Re-acquire caller's slot with retry logic.
        
        Lifted from nested function in ExecutionEngine._handle_call_agent() - Phase 4.3
        
        Args:
            slot_holder: Instance holding the slot (has .agent_class attr)
            slot_holder_name: Name of the instance for logging
            context_label: Description of context for warning messages
            
        Returns:
            True if successfully re-acquired, False otherwise.
            
        Note:
            On failure, _slot_release is NOT set to None — it retains whatever 
            value it had before (or remains unchanged). The caller's outer context 
            handles cleanup via its own finally block.
        """
        if not slot_holder:
            return False
            
        # MAJOR-1 FIX: Increase retry timeout from 0.2s total to 2s+ total for better robustness
        # during stop cleanup when semaphore may be contended. Use 20 attempts with 0.1s sleep = 2s total.
        max_attempts = 20
        retry_delay = 0.1
        
        for attempt in range(max_attempts):
            try:
                slot_holder._slot_release = self.pool._acquire_slot(
                    slot_holder.agent_class, slot_holder_name
                )
                return True
            except Exception as e:
                if attempt < max_attempts - 1:
                    logger.debug(f"Attempt {attempt + 1}/{max_attempts} failed to re-acquire caller slot after {context_label}: {e}. Retrying...")
                    time.sleep(retry_delay)
                else:
                    # If pool is stopped, no need to re-acquire - just release and return False
                    if self.pool.stopped:
                        logger.debug(f"Pool stopped during slot re-acquisition for '{slot_holder_name}' after {context_label}")
                        return False
                    logger.warning(f"Failed to re-acquire caller slot after {context_label} ({max_attempts} attempts, ~{max_attempts * retry_delay}s total): {e}. Subsequent calls will use ASYNC path.")
        
        return False

    def _validate_call_agent_args(
        self,
        args: Any,
        caller_name: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Validate call_agent tool arguments.
        
        Extracted from ExecutionEngine._handle_call_agent() - Phase 4.3
        
        Args:
            args: Tool arguments dictionary
            caller_name: Caller instance name for error messages
            
        Returns:
            Tuple of (instance_name, agent_class, error_message)
            error_message is None if validation passed
        """
        if args is None:
            logger.warning("call_agent early exit - %s (args is None)", caller_name)
            return None, None, 'Error: Invalid JSON arguments.'

        instance_name = args.get('instance_name', '')
        agent_class = (args.get('agent_class') or '').strip().lower()

        if not instance_name or not agent_class:
            logger.warning("call_agent early exit - %s missing instance_name='%s' or agent_class='%s'", 
                          caller_name, instance_name, agent_class)
            return instance_name, agent_class, "Error: call_agent requires instance_name and agent_class."

        return instance_name, agent_class, None

    def _check_nesting_depth(
        self,
        instance: 'AgentInstance',
        child_depth: int
    ) -> Optional[str]:
        """Check if nesting depth limit exceeded.
        
        Extracted from ExecutionEngine._handle_call_agent() - Phase 4.3
        
        Args:
            instance: Caller agent instance
            child_depth: Proposed depth for child
            
        Returns:
            Error message if depth exceeded, None otherwise
        """
        max_depth = self.pool.settings.max_nesting_depth if hasattr(self.pool, 'settings') else 10
        caller_name = instance.instance_name
        logger.debug("call_agent nesting - %s depth=%d/%d", caller_name, child_depth, max_depth)
        if child_depth > max_depth:
            logger.warning("call_agent depth exceeded - %s at depth %d (max=%d)", 
                          caller_name, child_depth, max_depth)
            return (f"Error: Nesting depth limit ({max_depth}) exceeded. "
                    f"The caller '{instance.instance_name}' is at depth {child_depth - 1}. "
                    f"Cannot create agent at depth {child_depth}.")
        return None

    # ── Tool Result Truncation ───────────────────────────────────────────────
    
    def truncate_tool_result(
        self,
        tool_result: str,
        tool_name: str,
        messages: List[Any],
        instance_name: str
    ) -> str:
        """Truncate a tool result if it would push context past 95% capacity.
        
        Extracted from ExecutionEngine._truncate_tool_result() - Phase 4.3
        
        Writes the full original content to a spillover file on disk when truncation occurs,
        and appends the spillover path to the truncation notice so the agent can read it back.
        
        call_agent is exempt from wild-read detection (the 10K char limit) — sub-agent outputs
        are structured responses, not raw data dumps.
        
        Args:
            tool_result: The tool result string to potentially truncate
            tool_name: Name of the tool that produced this result
            messages: Current conversation messages for token counting
            instance_name: Name of the agent instance
            
        Returns:
            Truncated result string or original if no truncation needed
        """
        if not isinstance(tool_result, str):
            return tool_result

        # Exempt tools with short, structured output where truncation could confuse the agent
        if tool_name in ['compress_context', 'read_file', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'move_file']:
            return tool_result

        inst = self.pool.get_instance(instance_name)
        max_tokens = self.engine._get_max_tokens(inst) if inst else 128000
        if max_tokens <= 0:
            return tool_result

        # Inline image content — skip truncation (compact markdown data)
        if '![image/' in tool_result:
            return tool_result

        try:
            from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
            from agent_cascade.llm.schema import Message, SYSTEM
            from agent_cascade.utils.utils import extract_text_from_message

            system_tokens = 0
            non_system_tokens = 0
            for msg in messages:
                # Explicit type guard for known message types (dict or Message object)
                if isinstance(msg, list):
                    # Skip unexpected list objects to prevent incorrect processing
                    continue
                
                role = _msg_role(msg)
                msg_obj = Message(**msg) if isinstance(msg, dict) else msg
                text = extract_text_from_message(msg_obj, add_upload_info=True)
                tokens = qwen_count(text)
                if role == SYSTEM:
                    system_tokens += tokens
                else:
                    non_system_tokens += tokens

            available_tokens = max_tokens - system_tokens
            if available_tokens <= 0:
                available_tokens = max_tokens

            total_threshold = int(available_tokens * 0.95)
            per_tool_threshold = int(available_tokens * 0.25)
            result_tokens = max(1, len(tool_result) // 3)

            # Wild read detection — exempt call_agent (sub-agent outputs are not raw data dumps)
            # Tier 1: Start with settings constant (env-var configurable via QWEN_AGENT_TOOL_RESULT_MAX_CHARS)
            wild_read_limit = DEFAULT_TOOL_RESULT_MAX_CHARS  # Imported at module level
            # Tier 2: Override from pool runtime config if set by UI slider (takes immediate effect)
            if hasattr(self.pool, 'llm_cfg') and self.pool.llm_cfg:
                wild_read_limit = self.pool.llm_cfg.get('tool_result_max_chars', wild_read_limit)
            is_wild_read = (len(tool_result) > wild_read_limit and tool_name != 'call_agent')

            if (result_tokens <= per_tool_threshold and
                    non_system_tokens + result_tokens <= total_threshold and
                    not is_wild_read):
                return tool_result

            # ── Truncation required — write spillover file first ──────────────
            spill_rel = self._write_spillover_file(tool_result, tool_name, instance_name)

            target_tokens = min(result_tokens, per_tool_threshold) if not is_wild_read else 500
            if non_system_tokens + target_tokens > total_threshold:
                target_tokens = max(200, total_threshold - non_system_tokens)

            truncated = tool_result[:target_tokens * 3]

            # Mark this tool call as truncated for thread-local state tracking
            mark_tool_call_truncated(instance_name, tool_name)

            # Build truncation notice with spillover path — matches format used by
            # operation_manager.py and code_interpreter.py across the unified branch
            if spill_rel:
                return (
                    f"{truncated}\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. "
                    f"Full output ({len(tool_result)} chars) saved to: {spill_rel}"
                    f"\nYou can read it with read_file if needed. "
                    f"Consider compressing context before continuing.]"
                )
            else:
                return (
                    f"{truncated}\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. "
                    f"Spillover file could not be saved (disk error). "
                    f"Consider compressing context before continuing.]"
                )

        except Exception as e:
            logger.debug(f"Tool result truncation calculation failed (using fallback): {e}")
            # Fallback: just truncate to a reasonable size
            if len(tool_result) > 8000:
                spill_rel = self._write_spillover_file(tool_result, tool_name, instance_name)
                # Mark as truncated in fallback path too
                mark_tool_call_truncated(instance_name, tool_name)
                if spill_rel:
                    return (
                        f"{tool_result[:8000]}\n\n[TOOL RESPONSE TRUNCATED — fallback path. "
                        f"Full output ({len(tool_result)} chars) saved to: {spill_rel}"
                        f"\nYou can read it with read_file if needed. "
                        f"Consider compressing context before continuing.]"
                    )
                return f"{tool_result[:8000]}\n\n[TOOL RESPONSE TRUNCATED — fallback path. Spillover file could not be saved (disk error).]"
            return tool_result

    def _write_spillover_file(
        self, tool_result: str, tool_name: str, instance_name: str
    ) -> Optional[str]:
        """Write the full tool result to a spillover file on disk.
        
        Extracted from ExecutionEngine._write_spillover_file() - Phase 4.3
        
        Returns a workspace-relative path string that the agent can use with read_file,
        or None if writing failed.
        
        Follows the pattern from the old branch (agent_orchestrator.py):
            - Files go to <workspace>/logs/spillover/
            - Filenames are {instance}_{tool}_{timestamp}.txt
            - Paths are normalized to forward slashes for cross-platform compatibility
            - Output is capped at MAX_SPILL_SIZE (50MB) to prevent disk exhaustion
            
        Args:
            tool_result: The full tool result content
            tool_name: Name of the tool that produced this result
            instance_name: Name of the agent instance
            
        Returns:
            Workspace-relative path string or None if write failed
        """
        try:
            # Resolve workspace directory via the logger manager (defensive guard)
            from pathlib import Path
            
            workspace_dir = self.pool._logger.workspace_dir
            log_dir = workspace_dir / 'logs' / 'spillover'
            log_dir.mkdir(parents=True, exist_ok=True)

            # Cap output to prevent disk exhaustion from massive tool results
            if len(tool_result) > MAX_SPILL_SIZE:
                tool_result = tool_result[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"

            # Use generate_spillover_filename helper for collision detection with counter cap < 1000
            spill_filename = generate_spillover_filename(instance_name, tool_name, log_dir)
            spill_path = log_dir / spill_filename

            spill_path.write_text(tool_result, encoding='utf-8')

            # Convert to workspace-relative path so agent can use it with read_file
            try:
                spill_rel = str(spill_path.relative_to(workspace_dir)).replace('\\', '/')
            except ValueError:
                # Fallback if spill_path is outside workspace_dir
                spill_rel = str(spill_path).replace('\\', '/')

            logger.info(
                f"Wrote spillover file for '{tool_name}' result of {instance_name}: "
                f"{len(tool_result)} chars -> {spill_rel}"
            )
            return spill_rel

        except Exception as e:
            logger.error(f"Failed to write spillover file for {instance_name}/{tool_name}: {e}")
            return None

    # Note: LLM call helper methods (_classify_llm_error, _make_retrying_message, 
# _make_error_message) remain in ExecutionEngine as they are used by 
# _execute_llm_call_with_retry() which is still owned by ExecutionEngine.