"""
LifecycleMixin — instance create/dismiss/terminate/halt/resume/remove and session reset/clear. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.llm.schema import FUNCTION, Message, ROLE, SYSTEM, USER
from ..agent_instance import AgentInstance, PoolSettings, AgentState, ACTIVE_STATES
from ..async_tools import AsyncToolRegistry

try:
    from agent_cascade.tools.code_interpreter import cleanup_kernels_for_session, _AGENT_KERNELS, _KERNEL_LOCK
except ImportError:
    cleanup_kernels_for_session = None
    _AGENT_KERNELS = {}
    _KERNEL_LOCK = threading.Lock()
class LifecycleMixin:
    def _resolve_instance_name(
        self,
        instance_name: str,
        exclude: Optional[str] = None,
    ) -> str:
        """Resolve instance name to avoid case-insensitive duplicates.

        Returns the canonical name (the one already in the pool if it matches,
        otherwise the original name). Raises ValueError if name is empty after strip.

        Args:
            instance_name: The proposed instance name.
            exclude: If provided, skip this name when searching for duplicates.
                Used when loading a session — the target instance should be excluded
                so it can be replaced by the loaded data.
        """
        if not instance_name:
            raise ValueError("Instance name cannot be empty")

        instance_name = instance_name.strip()
        if not instance_name:
            raise ValueError("Instance name cannot be whitespace only")

        # Check for case-insensitive match
        for name in self.instances:
            if name != exclude and name.lower() == instance_name.lower():
                return name  # Reuse the existing canonical name
        return instance_name

    def create_instance(
        self,
        instance_name: str,
        agent_class: str,
        parent_instance: Optional[str] = None,
        max_turns: Optional[int] = None,
        conversation: Optional[List[Message]] = None,
    ) -> AgentInstance:
        """Create a new agent instance and register it in the pool.

        Args:
            instance_name: Unique identifier for this instance.
            agent_class: Template class name (e.g., "coder", "researcher").
            parent_instance: Name of the calling agent (None for root/main).
            max_turns: Per-instance turn limit (None = use default 50).
            conversation: Initial conversation history (default: empty list).

        Returns:
            The newly created AgentInstance.
        """
        instance_name = self._resolve_instance_name(instance_name)
        now = time.monotonic()
        instance = AgentInstance(
            instance_name=instance_name,
            agent_class=agent_class,
            conversation=conversation or [],
            max_turns=max_turns,
            parent_instance=parent_instance,
            created_at=now,
            last_activity=now,
            compression_summary=None,
            latest_marker_index=-1,
        )
        # Phase 3: Give instance a reference back to the pool for queue cleanup on terminate().
        instance._pool_ref = self
        
        # Register instance atomically under _pool_lock to prevent race during creation
        with self._pool_lock:
            self.instances[instance_name] = instance
            self._instances_version += 1  # Fix #3: signal that instances changed
        # Track parent-child relationship for cascade termination (Fix Bug41, thread-safe via helper)
        if parent_instance:
            self._update_child_relationship(parent_instance, instance_name, add=True)
        # RECOMMENDED FIX: Removed redundant _mark_activity() call - constructor already sets last_activity=now above
        return instance

    def get_instance(self, instance_name: str) -> Optional[AgentInstance]:
        """Get an agent instance by name.

        Returns None if the instance doesn't exist (instead of raising KeyError).
        This is intentional — callers often check existence before acting.
        """
        return self.instances.get(instance_name.strip())

    def remove_instance(self, instance_name: str):
        """Remove an agent instance from the pool.

        Used by IdleManager for auto-dismissal and by dismiss_agent tool execution.
        Fires dismissal callbacks and cleans up message queues.
        """
        instance_name = instance_name.strip()
        self._instances_version += 1  # Fix #3: signal that instances changed
        with self._pool_lock:
            inst = self.instances.pop(instance_name, None)
        # Capture agent_class from the popped instance BEFORE removal — re-deriving it
        # from instance_classes (derived from self.instances) would yield '' and make
        # the logger key miss, leaking the cached file handle. Normalize as get_logger does.
        agent_class = (inst.agent_class or '').strip().lower() if inst else ''
        # Don't discard from terminated_instances here — keep the signal alive until
        # the thread confirms it stopped via join in dismiss_instance(). 
        # Memory leak prevention now handled in dismiss_instance() after join.
        with self._queue_lock:
            self.message_queues.pop(instance_name, None)
        # Clean up mapping's dict storage to prevent stale keys
        if hasattr(self, '_instance_conversations'):
            try:
                del self._instance_conversations[instance_name]
            except KeyError as e:
                logger.debug(f"Instance conversation cleanup key missing (expected): {e}")
        # Clean up logger entry for the instance
        with self._logger._lock:
            key = (instance_name, agent_class)  # composite key matching get_logger
            log_inst = self._logger._loggers.pop(key, None)

        # Fix #3: Clean up stale instance_state entries
        if hasattr(self, 'instance_state'):
            self.instance_state.pop(instance_name, None)

        # Fix #1: Close the cached file handle for the logger if it exists
        if log_inst and hasattr(log_inst, 'close'):
            try:
                log_inst.close()
            except Exception as e:
                logger.debug(f"Logger close failed for {instance_name} (non-critical): {e}")

        # Capture log path before it's lost — needed by dismissal callbacks to tell frontend where logs are
        log_path = log_inst.log_path if log_inst else None
        self._fire_on_dismissed(instance_name, log_path)

        # Clean up children tracking (Fix Bug41) — snapshot under locks to minimize hold time.
        # Use _pool_lock for instances access, _state_lock for _child_instances reads.
        stray_parents = []
        with self._pool_lock:
            for pi in self.instances.values():
                try:
                    with pi._state_lock:
                        if instance_name in pi._child_instances:
                            stray_parents.append(pi)
                except Exception:
                    pass  # Ignore if lock unavailable (non-critical cleanup path)
        with self._children_lock:
            self.children.pop(instance_name, None)
            parent_keys = list(self.children.keys())

        # Remove from all parents' tracking via helper (handles both pool.children and _child_instances)
        for parent in parent_keys:
            if instance_name in self.children.get(parent, []):
                self._update_child_relationship(parent, instance_name, add=False)

        # Clean up any remaining per-instance references. Best-effort — state may change concurrently.
        for parent_inst in stray_parents:
            try:
                with parent_inst._state_lock:
                    if instance_name in parent_inst._child_instances:
                        parent_inst._child_instances.remove(instance_name)
            except Exception as e:
                logger.debug(f"Cleanup of child reference {instance_name} from {parent_inst.instance_name} failed (non-critical): {e}")

        # BUG31 Fix: Clean up api_integration module-level caches to prevent memory leaks
        # and stale data when instances are dismissed and re-created with same name.
        from agent_cascade.api_integration import _cache_mgr
        _cache_mgr.evict_instance(instance_name)

        # Clean up per-instance endpoint cursor (kick-to-next-endpoint mechanism)
        if hasattr(self, 'api_router') and self.api_router is not None:
            self.api_router.reset_instance_endpoint(instance_name)
        # Note: _token_stats_cache is NOT cleaned here — it's keyed by conversation identity
        # (msg_count, id(last_msg)), not instance name. Entries auto-evict via FIFO at 5000 cap.

    def halt_all_instances(self, except_instance: str = None,
                           except_instances: Optional[List[str]] = None):
        """Halt all active instances except the given one(s). Used before forced compression.

        Tracks which instances were halted by compression (not manual) so that
        resume_all_instances only clears those — preserving manual halts.
        """
        skip = set()
        if except_instance:
            skip.add(except_instance)
        if except_instances:
            skip.update(except_instances)

        for inst_name in self.instances:
            if inst_name not in skip:
                was_already_halted = inst_name in self._halted_instances  # check per-instance only, not global pause
                self.halt_instance(inst_name)
                # Only track instances that weren't already halted — preserves manual halts
                if not was_already_halted:
                    self._compression_halted.add(inst_name)

    def resume_all_instances(self):
        """Resume only the instances that were halted by forced compression (not manual halts)."""
        for inst_name in list(self._compression_halted):
            self.resume_instance(inst_name)
        self._compression_halted.clear()

    def terminate_instance(self, instance_name: str, set_global_stopped: bool = False):
        """Mark an instance for immediate termination.

        Adds to terminated_instances and calls inst.terminate(). Cascade-terminates children (Bug41).

        Args:
            instance_name: Name of the instance to terminate.
            set_global_stopped: If True, signals ALL agents via _stopped_event. False (default) only terminates this instance (Bug5 fix).
        """
        instance_name = instance_name.strip()
        # First cascade-terminate all children (recursive, Fix Bug41).
        # Snapshot child list under _children_lock, then check existence under _pool_lock before recursing.
        with self._children_lock:
            child_list = list(self.children.get(instance_name, []))
        for child_name in child_list:
            with self._pool_lock:
                child_exists = child_name in self.instances
            if child_exists:
                self.terminate_instance(child_name, set_global_stopped=False)  # Recursive — handles nested trees

        # Get instance and add termination signal atomically under _pool_lock.
        # Always add to terminated set (even if instance gone) so status checks detect it.
        with self._pool_lock:
            inst = self.instances.get(instance_name)
            # Add to terminated_instances FIRST so stop-checks can detect dismissal immediately
            self.terminated_instances.add(instance_name)

        if inst:
            # Check if instance was in an active state before calling terminate()
            with inst._state_lock:
                is_active = inst.state in ACTIVE_STATES

            # Call canonical terminate() method — sets is_terminated=True, transitions state,
            # clears streaming responses and volatile state. Idempotent so safe even if called twice.
            inst.terminate()

            if is_active:
                # Bug5 Fix #1: Only set global _stopped_event when explicitly requested
                if set_global_stopped:
                    self._stopped_event.set()  # Global signal for ALL agents

                # RECOMMENDED FIX: Mark activity before transitioning to TERMINATED for consistency
                self._mark_activity(instance_name)

        # ── Fix TODO #41 Root Cause 1: Cancel pending async tool tasks ────────
        # Remove and cancel running background tools BEFORE draining results,
        # so no new results are produced for the terminated instance.
        if hasattr(self, '_async_registry'):
            try:
                cancelled = self._async_registry.clear_pending(instance_name)
                if cancelled:
                    logger.debug(f"Cancelled {cancelled} pending async tool(s) for {instance_name}")
            except Exception as e:
                logger.debug(f"Cancelling async tools for {instance_name} failed (non-critical): {e}")

        # Kill all background shell processes for this agent
        if hasattr(self, '_async_shell_tracker'):
            try:
                killed = self._async_shell_tracker.kill_all(instance_name)
                if killed:
                    logger.debug(f"Killed {killed} async shell process(es) for {instance_name}")
                    # Fix #5: Brief wait for tracking threads to exit after kill
                    import time as _time
                    _time.sleep(0.3)
            except Exception as e:
                logger.debug(f"Killing async shells for {instance_name} failed (non-critical): {e}")

        # Clear message queue to prevent stale messages from being processed
        with self._queue_lock:
            if instance_name in self.message_queues:
                try:
                    self.message_queues[instance_name].clear()
                except Exception as e:
                    logger.debug(f"Clearing message queue for {instance_name} failed (non-critical): {e}")

    def _clear_state_label(self, inst) -> None:
        """Clear the state label and cached endpoint config on an instance to avoid stale references.

        Best-effort — silent failure if lock acquisition fails.
        Used during termination and dismissal cleanup.
        """
        try:
            with inst._state_lock:
                inst._state_label = None
                inst._last_endpoint_config = None
        except Exception as e:
            logger.debug(f"Clearing state label for {inst.instance_name} failed (non-critical): {e}")

    def dismiss_instance(self, instance_name: str):
        """Remove an instance from the pool. If active, terminate it; otherwise clean up.

        Cascade-dismisses children first (Bug41). Does not set global _stopped_event — only this instance is terminated (Bug5 fix).
        """
        instance_name = instance_name.strip()
        # First dismiss all children (recursive cascade, Fix Bug41).
        # Snapshot child list under lock, then release before recursing to avoid deadlock.
        with self._children_lock:
            child_list = list(self.children.get(instance_name, []))
        for child_name in child_list:
            with self._pool_lock:
                child_exists = child_name in self.instances
            if child_exists:
                self.dismiss_instance(child_name)  # Recursive — handles nested trees

        # Get instance and check active state atomically under _pool_lock
        with self._pool_lock:
            inst = self.instances.get(instance_name)

        is_active = False
        if inst:
            with inst._state_lock:
                is_active = inst.state in ACTIVE_STATES

        if is_active:
            # Bug5 fix: only terminate this instance, not all agents
            self.terminate_instance(instance_name, set_global_stopped=False)
        else:
            # Set termination flag even for non-active instances (IDLE/COMPLETING).
            with self._pool_lock:
                inst = self.instances.get(instance_name)
            if inst and not inst.is_terminated:
                inst.terminate()

        # Wake SLEEPING parent when async child is dismissed (Bug41 fix).
        # Fix B (idle-wakeup): also relaunch an IDLE parent — its run() thread has
        # already exited, so enqueue+notify alone would leave the dismissal result
        # unprocessed (mirrors the user-message path's enqueue + relaunch).
        if inst and inst.parent_instance:
            parent_name = inst.parent_instance
            parent = self.get_instance(parent_name)
            if parent:
                from agent_cascade.agent_instance import AgentState
                with parent._state_lock:
                    parent_state = parent.state
                parent_is_sleeping = (parent_state == AgentState.SLEEPING)
                parent_is_idle = (parent_state == AgentState.IDLE)

                if (parent_is_sleeping or parent_is_idle) and hasattr(self, '_async_registry'):
                    # Look up the async registration for this child
                    parent_info = self._async_registry.get_parent_for_child(instance_name)
                    if parent_info:
                        _, func_id = parent_info
                        result_msg = f"[Agent '{instance_name}' Dismissed]:\nAgent was dismissed before completing."
                        try:
                            # Enqueue the dismissal result to wake up the parent
                            self.enqueue_message(parent_name, result_msg)
                            logger.debug(
                                f"[ASYNC_WAKEUP] Enqueued dismissal result for child '{instance_name}' "
                                f"to wake {parent_state.name} parent '{parent_name}'"
                            )
                        except Exception as e:
                            logger.debug(f"Failed to enqueue dismissal result for {instance_name}: {e}")
                        # Fix B: a SLEEPING parent's live poll thread drains the queue
                        # itself — only an IDLE parent needs a relaunch. No-op in all
                        # other cases (stopped pool / terminated instance / non-IDLE).
                        if parent_is_idle:
                            try:
                                from agent_cascade.utils.wakeup_helpers import relaunch_idle_agent
                                relaunch_idle_agent(self, parent_name)
                            except Exception as e:
                                logger.debug(
                                    f"[ASYNC_WAKEUP] Idle relaunch failed for parent '{parent_name}' (non-critical): {e}"
                                )
                        # Clean up the child mapping since this instance is being removed
                        self._async_registry.remove_child_mapping(instance_name)

        # ── Sticky slot cleanup on dismiss (plan change #14 / §3.11, G8). ──
        # The old thread may still hold the shared sequential slot (mid-LLM-call, mid-tool,
        # or queued at FIFO tail after a yield) and its run()-finally release could be
        # arbitrarily delayed past the 2s join below. Release the held permit NOW, at the
        # dismiss site: idempotent capture-and-nullify under _state_lock (exact pattern of
        # engine._release_slot / stop_session). The old thread's later release is a no-op
        # (nullified callback; SlotPool.release() is also idempotent via acquisition_id).
        if inst and hasattr(inst, '_state_lock'):
            try:
                # Shared capture-nullify-release-log helper (slot_queue.release_slot_permit):
                # same idempotent semantics as before — release under the state lock,
                # [SLOTPOOL] drop-dismiss line only when a live permit was held.
                from agent_cascade.slot_queue import release_slot_permit
                _dismiss_pool = None
                try:
                    _sched = getattr(self, 'api_router', None) and self.api_router.scheduler
                    if _sched is not None:
                        _held_key = getattr(inst, '_slot_key', None)
                        _dismiss_pool = _sched._pools.get(_held_key) if _held_key else None
                except Exception:
                    pass
                release_slot_permit(inst, instance_name, action="drop-dismiss",
                                    context="on dismiss", pool=_dismiss_pool)
            except Exception as e:
                # Non-critical: the old thread's own run()-finally release still covers it.
                logger.warning(f"Slot release on dismiss failed for '{instance_name}' (non-critical): {e}")

        # Clear state label before removing from pool (terminate already clears it if active).
        if inst:
            self._clear_state_label(inst)

        # Wait for agent's execution thread to actually stop.
        with self._instance_threads_lock:
            thread = self._instance_threads.pop(instance_name, None)
        if thread and thread.is_alive():
            logger.info(f"Waiting for '{instance_name}' thread to stop...")
            try:
                join_timeout = getattr(self.settings, 'dismiss_thread_join_timeout', 2.0)
                thread.join(timeout=join_timeout)  # Short wait; join only waits, doesn't force-stop
                if thread.is_alive():
                    logger.warning(
                        f"Thread for '{instance_name}' did not stop within {join_timeout}s timeout. "
                        f"Termination signal kept active — agent will stop at next cooperative check."
                    )
            except Exception as e:
                logger.warning(f"Error joining thread for '{instance_name}': {e}")
        else:
            logger.debug(f"No active thread to join for '{instance_name}'")

        # Only discard termination signal if we confirmed the thread stopped.
        with self._pool_lock:
            if thread and not thread.is_alive():
                self.terminated_instances.discard(instance_name)

        # Clean up code interpreter kernels for this session to prevent ZMQ socket leaks.
        # Check _AGENT_KERNELS directly — if any kernels are tracked under this instance's name,
        # clean them up regardless of parent/child relationship. This handles:
        # - Root agents (always own their kernels)
        # - Child agents that somehow got kernels under their own session name
        # Avoids double-cleanup since cleanup_kernels_for_session pops the entry atomically.
        try:
            with _KERNEL_LOCK:
                has_kernels = instance_name in _AGENT_KERNELS and len(_AGENT_KERNELS[instance_name]) > 0
            
            if has_kernels:
                cleaned = cleanup_kernels_for_session(instance_name)
                if cleaned > 0:
                    logger.info(f"Cleaned up {cleaned} kernel(s) for dismissed session '{instance_name}'")
        except Exception as e:
            # Non-critical: __del__ fallback will eventually clean up, but we log the issue
            logger.warning(f"Kernel cleanup failed for session '{instance_name}' (non-critical): {e}")

        # Always remove the instance from the pool so its tab disappears from the UI
        self.remove_instance(instance_name)
    def reset(self):
        """Full reset of agent state for "New Session".

        Order of operations:
          1. Clear pending approvals (unblocks any threads waiting in operation_manager)
          2. Dismiss all non-orchestrator sub-agents (with cascade and double-dismiss guard)
          3. Create new logger session for the main orchestrator (so new messages
             go to a fresh JSONL file instead of appending to the old one)
          4. Clear instance conversations mapping
          5. Clear per-instance state (halted, active_stack, tool args, etc.)
          6. Clear performance caches and WebSocket references
          7. Shutdown and recreate async infrastructure (Phase 4)

        Does NOT delete AgentInstances — only clears their conversations.
        The main orchestrator instance (parent_instance is None) survives reset.

        Note: This method sets pool.stopped as a safety net to signal active threads
        to halt even if the caller forgot. The stopped event is cleared at the end of
        reset so executors can run in the new session. Callers may re-set
        pool.stopped = True after reset if they need threads halted during post-reset
        operations (e.g., api_server.py line 1697).
        """
        # Safety net: signal all active threads to halt even if caller forgot.
        # Direct _stopped_event manipulation avoids triggering the property setter's
        # side effects (idle.stop() + async_registry.shutdown()) which are handled
        # explicitly later in this method at steps 6-7.
        self._stopped_event.set()

        # ── Step 1: Clear pending approvals ──────────────────────────────────
        # Prevent dangling threads waiting for user approval.
        if self.operation_manager:
            try:
                with self.operation_manager._lock:
                    for approval in self.operation_manager.pending.values():
                        if not approval.event.is_set():
                            approval.approved = False
                            approval.outcome_reason = "Session reset"
                            approval.event.set()
                    self.operation_manager.pending.clear()
            except Exception as e:
                logger.warning(f"clear_pending failed during reset (threads may hang): {e}")

        # ── Step 2: Dismiss all sub-agents (non-orchestrator) ───────────────
        # Take a snapshot of instance keys under _pool_lock to avoid RuntimeError during iteration.
        # dismiss_instance() recursively cascade-dismisses children first, then
        # calls remove_instance() which cleans up loggers, queues, caches.
        with self._pool_lock:
            sub_agent_names = [name for name, inst in self.instances.items()
                               if inst.parent_instance is not None]
        for name in sub_agent_names:
            # Double-dismiss guard: instance may have been cascade-dismissed by parent
            with self._pool_lock:
                still_exists = name in self.instances
            if not still_exists:
                continue
            self.dismiss_instance(name)

        # ── Step 3: New logger session for main orchestrator ────────────────
        # Create a new JSONL log file so the new session doesn't append to old.
        for name, inst in list(self.instances.items()):
            if inst.parent_instance is None:
                try:
                    self._logger.create_new_session(name, inst.agent_class)
                except Exception as e:
                    logger.warning(f"Logger reset failed for {name} (new session may append to old logs): {e}")
                break

        # ── Step 4: Clear instance conversations mapping ──────────────────────
        if hasattr(self, '_instance_conversations'):
            self._instance_conversations.clear()
        else:
            for inst in self.instances.values():
                inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync
        self._instances_version += 1

        # ── Step 5: Clear per-instance state ────────────────────────────────
        self._paused.set()  # reset to resumed state
        # Setting _paused directly (not via resume()) would skip stream-cache invalidation,
        # so force it here — a full reset also clears _halted_instances, and the frontend
        # must see fresh is_halted/paused state on the next broadcast.
        self._invalidate_stream_cache_on_pause_change()
        with self._pool_lock:
            self.terminated_instances.clear()
        with self._children_lock:
            self.children.clear()
        self._halted_instances.clear()
        self._compression_halted.clear()
        # Keep unified's active_stack_clear() — temp removed it but unified still needs it
        if hasattr(self, 'active_stack_clear'):
            self.active_stack_clear()
        self.instance_state.clear()
        self.instance_summaries.clear()

        # ── Step 6: Clear performance caches and WebSocket references ───────
        try:
            from agent_cascade.api_integration import _clear_performance_caches
            _clear_performance_caches()
        except Exception as e:
            logger.warning(f"Cache clear failed during reset (stale data may persist): {e}")
        self._ws_send_queue = None
        self._ws_loop = None

        # ── Step 7: Shutdown and recreate async infrastructure (Phase 4) ─────
        # Shutdown executor to clean up background tool threads
        try:
            self._async_registry.shutdown()
        except Exception as e:
            logger.warning(f"Async registry shutdown failed during reset (threads may leak): {e}")
        # Recreate for new session
        self._async_registry = AsyncToolRegistry(pool=self)

        # Restart idle checker — it may have been stopped by the caller's
        # pool.stopped = True. IdleManager.start() is idempotent.
        self._idle.start()

        # Clear the safety-net stopped signal so executors can run in the new session.
        # Callers that explicitly set stopped=True before reset will need to re-set it
        # if they want threads halted during post-reset operations (e.g., api_server line 1697).
        self._stopped_event.clear()

    def clear_sub_agents(self):
        """Clear all sub-agent instances from the pool, preserving root orchestrator(s).
        
        This method dismisses all non-root instances (where parent_instance is not None),
        which are typically delegated workers created during session execution. Root 
        orchestrator instances (parent_instance is None) are preserved.
        
        Use case: Called before loading a saved session to remove stale sub-agents from
        previous sessions that would otherwise appear in the UI as if they belong to
        the newly loaded session.
        
        Order of operations:
          1. Temporarily suppress dismissal callbacks (prevents premature broadcasts)
          2. Take snapshot of instance keys (avoids RuntimeError during iteration)
          3. Dismiss all instances where parent_instance is not None
             - dismiss_instance() recursively cascade-dismisses children first
          4. Use double-dismiss guard (instance may have been cascade-dismissed by parent)
          5. Clean up instance_summaries for dismissed instances
          6. Increment _instances_version to signal the change
          7. Restore dismissal callbacks
        
        Does NOT:
          - Touch root orchestrator instances (parent_instance is None)
          - Clear conversations of remaining instances
          - Reset logger sessions or async infrastructure
          - Fire dismissal callbacks during clear (suppressed to prevent UX flicker)
        
        See reset() for full session reset that clears everything including root instances.
        See load_session_from_log() for where this is typically called before loading.
        
        Example:
            >>> # Before loading a new session, clear stale sub-agents
            >>> agent_pool.clear_sub_agents()
            >>> agent_pool.load_session_from_log(path, target_instance='Maine')
        """
        # Issue 1 fix: Temporarily suppress dismissal callbacks to prevent premature 
        # frontend broadcasts. The final state broadcast happens after load completes.
        _callbacks = self._on_dismissed_callbacks.copy()
        self._on_dismissed_callbacks = []
        
        try:
            # Take a snapshot of instance keys under _pool_lock to avoid RuntimeError during iteration.
            # dismiss_instance() modifies self.instances by removing dismissed instances
            # and recursively cascade-dismisses children first.
            with self._pool_lock:
                sub_agent_names = [name for name, inst in self.instances.items()
                                   if inst.parent_instance is not None]
            for name in sub_agent_names:
                # Double-dismiss guard: instance may have been cascade-dismissed by parent
                with self._pool_lock:
                    still_exists = name in self.instances
                if not still_exists:
                    continue
                self.dismiss_instance(name)
            
            # Clean up instance_summaries for dismissed instances (Issue 2 fix)
            for name in list(self.instance_summaries.keys()):
                if name not in self.instances:
                    self.instance_summaries.pop(name, None)
            
            # Signal that instances changed (for lazy sync compatibility)
            self._instances_version += 1
        finally:
            # Restore dismissal callbacks
            self._on_dismissed_callbacks = _callbacks
