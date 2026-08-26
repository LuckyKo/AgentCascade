"""
SlotsMixin — endpoint slot acquisition, async call registration, and pause/resume state. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
class SlotsMixin:
    def _acquire_slot(
        self,
        agent_class: str,
        instance_name: str,
        **kwargs,
    ) -> Optional[Callable[[], None]]:
        """Acquire an endpoint scheduling slot. Returns a release callback or None for unlimited endpoints."""

        if not hasattr(self, 'api_router') or not self.api_router:
            return None

        router = self.api_router

        try:
            # Cursor-aware resolution (sticky slot plan change #4): resolve the slot for
            # the endpoint this instance will ACTUALLY call next (chain rotated by its
            # cursor), not the raw chain head. Falls back to the legacy chain-head
            # resolvers when the router lacks the helper (e.g. minimal test doubles).
            if hasattr(router, 'get_effective_slot_info'):
                slot_info = router.get_effective_slot_info(agent_class, instance_name=instance_name)
                api_base = slot_info.get('api_base') or 'unknown'
                concurrency_limit = slot_info.get('concurrency_limit', 0)
            else:
                # Get the effective concurrency for this agent class (includes default fallback)
                concurrency_limit = router.get_effective_concurrency(agent_class)

                # Resolve the actual api_base that will be used
                llm_cfg = router.get_llm_config(agent_class)
                api_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')

            logger.debug(
                f"[CALL_AGENT_DEBUG] _acquire_slot — agent_class={agent_class}, "
                f"instance_name={instance_name}, api_base={api_base}, concurrency_limit={concurrency_limit}"
            )

            # Acquire a slot on the endpoint scheduler (blocks if at capacity)
            # SLOT_TIMEOUT FIX v2: Pass instance_name and agent_class for tracking
            return router.scheduler.acquire(
                api_base, concurrency_limit, instance_name, agent_class, pool=self
            )
        except Exception as e:
            logger.error(f"Failed to acquire endpoint slot for {instance_name}: {e}")
            raise

    def register_async_call(self, instance_name: str, function_id: Optional[str] = None,
                            agent_class: Optional[str] = None, child_instance_name: Optional[str] = None,
                            args: Optional[dict] = None, caller: Optional[str] = None, nest_depth: int = 0):
        """Register and execute an async tool call via AsyncToolRegistry.

        Creates a callable that wraps the child agent execution logic (endpoint slot
        acquisition, ExecutionEngine creation, result extraction) and submits it to
        the thread pool via AsyncToolRegistry.

        Args:
            instance_name: The caller's instance name (results go here)
            function_id: The LLM's tool_call_id for this call
            agent_class: Class of child agent to run
            child_instance_name: Name of the child agent instance
            args: Tool arguments for the child agent
            caller: Name of the calling agent
            nest_depth: Nesting depth for max_nesting_depth enforcement
        """
        if not agent_class or not child_instance_name:
            logger.error(f"register_async_call requires agent_class and child_instance_name")
            return

        def run_child_agent() -> str:
            """Callable that runs the child agent and returns the result string.

            NOTE: We do NOT acquire the endpoint slot here — engine.run() inside
            _create_and_run_agent acquires its own slot at line 348 of execution_engine.py.
            Acquiring before AND inside would deadlock on Semaphore(1) (same thread,
            same semaphore). The child's engine.run() handles all concurrency control.
            """
            from agent_cascade.execution_engine import ExecutionEngine
            from agent_cascade.child_runner import run_child_core, ChildAgentFailedError

            engine = ExecutionEngine(self)
            # initialize() now called automatically in __init__ (Phase 4.5 cleanup)

            try:
                result = run_child_core(
                    engine=engine,
                    pool=self,
                    agent_class=agent_class,
                    instance_name=child_instance_name,
                    args=args,
                    caller_name=caller,
                    child_depth=nest_depth,
                    prefix="Agent",
                )

                # Save child's state after async completion (state save/restore flow step 4).
                try:
                    from agent_cascade.state_ops import save_instance_state
                    child_inst = self.get_instance(child_instance_name)
                    if child_inst:
                        save_instance_state(child_inst)
                except Exception as e:
                    logger.debug(f"Failed to save async child state for {child_instance_name}: {e}")

                return result
            except ChildAgentFailedError:
                # The child's only output was a system error or termination notice.
                # Re-raise so async_tools._execute sets entry.error → the parent receives
                # [Background Tool Error] instead of [Background Tool Result].
                # Clean up the zombie instance first.
                try:
                    self.dismiss_instance(child_instance_name)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to dismiss child {child_instance_name} during failure cleanup: {cleanup_err}")
                raise
            except Exception as e:
                # Cleanup zombie instance on failure (e.g., slot timeout after instance creation)
                try:
                    self.dismiss_instance(child_instance_name)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to dismiss zombie instance {child_instance_name} during error cleanup: {cleanup_err}")
                return f"[Agent '{child_instance_name}' Failed]:\n{str(e)}"

        self._async_registry.register(instance_name, run_child_agent, function_id=function_id, child_instance_name=child_instance_name)
        
        # NOTE: Slot acquisition happens later when the child agent actually runs,
        # not at spawn time. Spawn just registers the async task.
        #
        # DESIGN GOAL (2026-08-16): There is NO slot borrowing/inheritance. Every agent
        # meters against its OWN resolved endpoint pool. Sync children run inline on the
        # parent's thread, but they do NOT inherit the parent's permit — the parent
        # RELEASES its slot (tool_dispatcher._run_child_sync) and the child acquires its
        # own via engine.run(). The old "Rule 4: sync children inherit parent's permit"
        # behavior was removed in the Stage 3/4 slot consolidation.

    # ── Pause/Resume state management ───────────────────────────────────────

    # ── Pause/Resume state management ───────────────────────────────────────

    def pause(self):
        """Pause ALL instances by clearing the global pause flag.
        
        Clears _paused Event so all agents block in wait_if_paused() until resumed.
        Unlike stop(), this does NOT trigger idle.stop() or async_registry.shutdown() —
        those are side effects of pool.stopped=True (the stop path), not pause.
        """
        self._paused.clear()

    def resume(self):
        """Resume all paused instances by setting the global pause flag."""
        self._paused.set()

    def is_paused(self) -> bool:
        """Check if the pool is currently paused."""
        return not self._paused.is_set()

    def wait_if_paused(self, timeout: float = 1.0) -> None:
        """Block until resumed or timeout expires. Used by execution loop to wait efficiently on pause."""
        self._paused.wait(timeout=timeout)

    # ── Instance halt check (checks both global pause + per-instance halt) ───

    def is_instance_halted(self, instance_name: str) -> bool:
        """Check if an instance is halted. Returns True if globally paused or per-instance halted.
        
        Note: resume_instance() only clears per-instance halt; call resume() first to clear _paused."""
        return self.is_paused() or instance_name in self._halted_instances
    
    # (internal helpers used by compression handler and REST endpoints)

    def halt_instance(self, instance_name: str):
        """Halt a specific instance (per-instance tracking)."""
        self._halted_instances.add(instance_name)

    def resume_instance(self, instance_name: str):
        """Resume a halted instance."""
        self._halted_instances.discard(instance_name)

    # ── Activity tracking ──────────────────────────────────────────────────

    def _mark_activity(self, instance_name: str):
        """Update last_activity timestamp for an instance."""
        instance_name = instance_name.strip()
        inst = self.instances.get(instance_name)
        if inst:
            inst.last_activity = time.monotonic()

    # ── Convenience methods (thin wrappers around instance state) ───────────

    def is_active(self, instance_name: str) -> bool:
        """Check if an instance is currently executing (derived from state machine)."""
        instance_name = instance_name.strip()
        inst = self.instances.get(instance_name)
        return inst.is_running if inst else False

    def is_instance_terminated(self, instance_name: str) -> bool:
        """Check if an instance has been marked for termination.

        Per-instance check (unlike _stopped_event). Checks terminated_instances set, then inst.is_terminated flag.
        Thread-safe via _pool_lock.
        """
        instance_name = instance_name.strip()
        with self._pool_lock:
            in_set = instance_name in self.terminated_instances
            inst = self.instances.get(instance_name)
            inst_flag = inst.is_terminated if inst else False
            result = in_set or inst_flag
            return result
