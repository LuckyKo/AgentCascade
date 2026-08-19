"""EndpointScheduler — per-API-base FIFO slot scheduling.

Moved verbatim from api_router.py (Phase 3a pure-move refactor).
Re-exports the timeout constants it reads as module globals so tests can patch them here.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from agent_cascade.settings import (
    ENDPOINT_SLOT_ACQUIRE_TIMEOUT,
    ENDPOINT_COOLDOWN_SECONDS,
    ENDPOINT_FAILURE_CLEANUP_HOURS,
)
from agent_cascade.slot_queue import SlotPool, QUEUE_WAIT_TIMEOUT

logger = logging.getLogger(__name__)

class EndpointScheduler:
    """
    Manages per-API-base scheduling with lifecycle-aware serialization.
    
    Phase 2: Uses SlotPool per endpoint for FIFO queueing + strict capacity control.
    For concurrency=0 endpoints: agents are strictly serialized — one at a time,
    from task submission to full completion (including all LLM calls and tool waits).
    All concurrency=0 endpoints share the SAME slot to avoid cache trashing from
    interleaving across different API addresses.
    For concurrency=N endpoints: at most N agents can run simultaneously.
    For concurrency=-1 endpoints: no scheduling needed (unlimited).
    
    This operates at the AGENT TASK level (entire agent lifecycle), NOT the
    individual API call level. This prevents interleaving of LLM calls between
    different agents on the same endpoint.

    Uses SlotPool per endpoint for FIFO queueing + strict capacity control.
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        # Per-endpoint SlotPool for FIFO queueing + capacity control.
        # api_base -> SlotPool(key=api_base, capacity=N)
        self._pools: Dict[str, SlotPool] = {}
        # Lazy counter for acquisition IDs (for backward compat with diagnostics).
        self._next_acquisition_id = 0

    def _get_or_create_pool(self, api_base: str, concurrency_limit: int) -> Optional[SlotPool]:
        """Get or lazily create a SlotPool for the given endpoint.
        
        Args:
            api_base: The API base URL of the endpoint
            concurrency_limit: -1=unlimited, 0=sequential, N>0=max parallel
            
        Returns:
            SlotPool instance, or None if unlimited (-1).
        """
        if concurrency_limit == -1:
            return None
        
        # BUG FIX: All concurrency=0 endpoints share the same slot to avoid cache trashing.
        is_sequential = (concurrency_limit == 0)
        slot_key = '_shared_sequential_slot_' if is_sequential else api_base
        
        capacity = concurrency_limit if concurrency_limit > 0 else 1  # 0→1 (sequential)
        
        with self._lock:
            if slot_key not in self._pools:
                pool = SlotPool(key=slot_key, capacity=capacity)
                self._pools[slot_key] = pool
                logger.info(f"[EndpointScheduler] Created pool '{slot_key}' with capacity={capacity}")
            return self._pools[slot_key]
    
    def acquire(
        self,
        api_base: str,
        concurrency_limit: int,
        instance_name: str = "unknown",
        agent_class: str = "unknown",
        pool=None,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Optional[Callable[[], None]]:
        """
        Acquire a slot on the endpoint. Blocks if at capacity.
        Returns a cleanup callback to release the slot, or None if unlimited.
        
        Uses SlotPool.acquire() for FIFO queueing + strict capacity control.
        
        Args:
            api_base: The API base URL of the endpoint
            concurrency_limit: -1=unlimited, 0=sequential, N>0=max parallel
            instance_name: Name of the agent instance acquiring the slot (for tracking)
            agent_class: Class of the agent instance (for tracking)
            pool: Optional AgentPool reference for termination checks during blocking acquire
            timeout: Optional override for wait timeout in seconds
            
        Returns:
            A callable that releases the slot when called, or None if no scheduling needed.
            
        Raises:
            TimeoutError: If slot cannot be acquired within ENDPOINT_SLOT_ACQUIRE_TIMEOUT.
            AgentTerminatedError: If instance is terminated while waiting.
        """
        if concurrency_limit == -1:  # unlimited — no scheduling needed
            logger.debug(f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — api_base={api_base}, concurrency=-1 (unlimited), returning None")
            return None
        
        # BUG FIX: All concurrency=0 endpoints share the same slot to avoid cache trashing.
        is_sequential = (concurrency_limit == 0)
        slot_key = '_shared_sequential_slot_' if is_sequential else api_base
        
        logger.debug(
            f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — api_base={api_base}, "
            f"concurrency_limit={concurrency_limit}, slot_key={slot_key}, "
            f"instance_name={instance_name}, agent_class={agent_class}"
        )
        
        # Get or create the pool (handles lazy initialization + capacity mapping).
        sched_pool = self._get_or_create_pool(api_base, concurrency_limit)
        if sched_pool is None:
            return None
        
        # Use QUEUE_WAIT_TIMEOUT (300s) as primary; ENDPOINT_SLOT_ACQUIRE_TIMEOUT as fallback for backward compat.
        effective_timeout = timeout if timeout is not None else (QUEUE_WAIT_TIMEOUT or ENDPOINT_SLOT_ACQUIRE_TIMEOUT)
        
        try:
            # Phase 2: Call SlotPool.acquire() — FIFO queueing, strict capacity, interruptible ticks.
            release_from_pool = sched_pool.acquire(
                instance_name=instance_name,
                agent_class=agent_class,
                timeout=effective_timeout,
            )
            
            logger.info(f"[EndpointScheduler] Agent '{instance_name}' ({agent_class}) acquired slot on '{api_base}' "
                       f"(pool={slot_key}, active={len(sched_pool._running)}, capacity={sched_pool.capacity})")
            
            # Wrap the pool's release callback to preserve existing logging behavior.
            def release():
                try:
                    release_from_pool()
                except Exception as e:
                    log_target = api_base if not is_sequential else f"{api_base} (shared sequential)"
                    logger.error(f"[SLOT_RELEASE_ERROR] Failed to release slot for {log_target}: {e}", exc_info=True)
                    return
                
                # Log release with current pool stats.
                log_target = api_base if not is_sequential else f"{api_base} (shared sequential)"
                logger.info(f"[EndpointScheduler] Agent '{instance_name}' ({agent_class}) released slot on '{log_target}' "
                           f"(pool={slot_key}, active={len(sched_pool._running)}, capacity={sched_pool.capacity})")
            
            return release
            
        except TimeoutError as e:
            # Wrap with holder info for diagnostics.
            holder_info = ""
            holders = list(sched_pool._running.values())
            if holders:
                holder_names = [f"{h.instance_name} ({h.agent_name})" for h in holders]
                holder_info = f". Currently held by: {', '.join(holder_names)}"
            
            raise TimeoutError(
                f"Timed out after {effective_timeout}s waiting for endpoint slot on {api_base}. "
                f"Current active count: {len(sched_pool._running)}, max allowed: {sched_pool.capacity}{holder_info}"
            ) from e
        
        except Exception as e:
            # Catch-all for unexpected errors during acquire.
            logger.error(f"[EndpointScheduler] Acquire failed for '{instance_name}' on '{api_base}': {e}", exc_info=True)
            raise
    
    def count_active(self, api_base: str, concurrency_limit: int) -> int:
        """Count active tasks on an endpoint.
        
        Args:
            api_base: The API base URL of the endpoint
            concurrency_limit: -1=unlimited, 0=sequential, N>0=max parallel
            
        Returns:
            Number of currently active agents on this endpoint
        """
        if concurrency_limit == -1:
            return 0
        
        slot_key = '_shared_sequential_slot_' if concurrency_limit == 0 else api_base
        pool = self._pools.get(slot_key)
        return len(pool._running) if pool else 0
    
    def get_status(self) -> Dict[str, Dict]:
        """Get status of all scheduled endpoints (for diagnostics).
        
        Returns:
            Dictionary mapping endpoint identifiers to their current status.
            Shared sequential slots are labeled as '[SHARED] _shared_sequential_slot_'
            to distinguish them from per-endpoint schedules.
        """
        result = {}
        for key, pool in self._pools.items():
            display_key = f"[SHARED] {key}" if 'shared' in key.lower() else key
            
            # Build slot holders info from pool._running (SlotHolder has instance_name, agent_name).
            holders_info = []
            for holder in pool._running.values():
                held_duration = time.monotonic() - holder.granted_at
                holders_info.append({
                    'instance_name': holder.instance_name,
                    'agent_class': holder.agent_name,  # agent_name used as display name
                    'held_duration_seconds': held_duration,
                })
            
            result[display_key] = {
                'active_count': len(pool._running),
                'max_active': pool.capacity,
                'available_slots': pool.capacity - len(pool._running),
                'waiters_count': len(pool._waiters),
                'slot_holders': holders_info,
            }
        return result
    
    def cleanup_stale(self):
        """Remove schedule entries for endpoints with no activity.
        
        A pool is stale when _running is empty AND _waiters is empty (no active or waiting agents).
        This prevents memory leaks from endpoints that were used temporarily and have since gone idle.
        
        Note: The shared sequential slot (_shared_sequential_slot_) is NOT cleaned up
        to avoid unnecessary recreation of the shared pool across different
        concurrency=0 endpoints.
        """
        with self._lock:
            stale = [key for key, pool in self._pools.items()
                     if key != '_shared_sequential_slot_'  # Protect shared slot from cleanup
                     and len(pool._running) == 0 and len(pool._waiters) == 0]
            for key in stale:
                del self._pools[key]
            if stale:
                logger.info(f"[EndpointScheduler] Cleaned up {len(stale)} stale schedule(s)")

    def get_slot_holders(self, slot_key: str = None) -> Dict[str, List[tuple]]:
        """Get information about which instances are holding slots.
        
        Phase 2: Returns data from SlotPool._running as tuples for backward compat.
        
        Args:
            slot_key: Optional specific slot key to query. If None, returns all.
            
        Returns:
            Dictionary mapping slot keys to lists of
            (instance_name, agent_name, granted_at, acquisition_id) tuples.
            NOTE: SlotHolder.agent_name is populated with the instance name by
            _grant() in slot_queue.py (the agent_class is not stored on the holder),
            so tuple[1] is the instance/agent name, not a class string.
        """
        import copy
        result = {}
        
        if slot_key:
            pool = self._pools.get(slot_key)
            if pool:
                holders = [(h.instance_name, h.agent_name, h.granted_at, h.acquisition_id) 
                           for h in pool._running.values()]
                result[slot_key] = copy.deepcopy(holders)
        else:
            for key, pool in self._pools.items():
                holders = [(h.instance_name, h.agent_name, h.granted_at, h.acquisition_id) 
                           for h in pool._running.values()]
                if holders:
                    result[key] = copy.deepcopy(holders)
        
        return result

    def detect_stuck_slots(self, threshold_seconds: float = 60.0) -> List[dict]:
        """Detect slots that have been held for longer than the threshold.
        
        Phase 2: Uses SlotPool._running to check hold durations.
        
        Args:
            threshold_seconds: Time in seconds after which a slot is considered "stuck"
            
        Returns:
            List of dictionaries with information about stuck slots, including:
            - slot_key: The slot identifier
            - instance_name: Name of the holding instance
            - agent_class: Class of the holding instance  
            - held_duration: How long the slot has been held (seconds)
            - acquired_at: Timestamp when slot was acquired
        """
        stuck_slots = []
        current_time = time.monotonic()
        
        # Check held slots — _running access is safe without lock for diagnostics.
        for key, pool in self._pools.items():
            with pool._cond:
                holders_snapshot = list(pool._running.values())
            for holder in holders_snapshot:
                held_duration = current_time - holder.granted_at
                if held_duration > threshold_seconds:
                    stuck_slots.append({
                        'slot_key': key,
                        'instance_name': holder.instance_name,
                        'agent_class': holder.agent_name,
                        'held_duration': held_duration,
                        'acquired_at': holder.granted_at,
                    })
                    logger.warning(
                        f"[SLOT_STUCK_DETECTION] Slot on '{key}' held by '{holder.instance_name}' "
                        f"({holder.agent_name}) for {held_duration:.1f}s (threshold: {threshold_seconds}s)"
                    )
        
        # Flag waiters older than QUEUE_WAIT_TIMEOUT.
        # Copy data under lock with short critical section, then iterate outside.
        for key, pool in self._pools.items():
            with pool._cond:
                waiters_snapshot = list(pool._waiters.values())
            
            # Flag aged waiters (outside lock)
            for ticket in waiters_snapshot:
                age = current_time - ticket.created_at
                if age > QUEUE_WAIT_TIMEOUT:
                    stuck_slots.append({
                        'slot_key': key,
                        'instance_name': ticket.instance_name,
                        'agent_class': ticket.agent_class,
                        'held_duration': age,
                        'acquired_at': ticket.created_at,
                        'issue': 'waiter_expired',
                    })
                    logger.warning(
                        f"[SLOT_STUCK_DETECTION] Waiter '{ticket.instance_name}' on '{key}' "
                        f"has waited {age:.1f}s > QUEUE_WAIT_TIMEOUT={QUEUE_WAIT_TIMEOUT}s"
                    )
        
        return stuck_slots
    
    def get_slot_info(self, api_base: str, concurrency_limit: int) -> dict:
        """Get slot information for an endpoint without acquiring.
        
        Returns dict with:
          - slot_key: The internal key used ('_shared_sequential_slot_' or api_base)
          - is_sequential: True if conc=0 (shared sequential pool)
          - concurrency_limit: The effective limit (-1, 0, or N>0)
        """
        is_sequential = (concurrency_limit == 0)
        slot_key = '_shared_sequential_slot_' if is_sequential else api_base
        
        return {
            'slot_key': slot_key,
            'is_sequential': is_sequential,
            'concurrency_limit': concurrency_limit,
        }
    



    def cancel(self, instance_name: str = None, ticket_id: str = None) -> bool:
        """Cancel a waiting ticket in the queue.
        
        Called during termination to remove pending waiters from the queue.
        
        Args:
            instance_name: Cancel all tickets for this agent.
            ticket_id: Cancel a specific ticket by ID.
            
        Returns:
            True if any ticket was cancelled, False otherwise.
        """
        for pool in self._pools.values():
            if ticket_id:
                if pool.cancel(ticket_id=ticket_id):
                    logger.debug(f"[CANCELLATION] Cancelled ticket {ticket_id}")
                    return True
            elif instance_name:
                count = pool.cancel(agent_name=instance_name)
                if count > 0:
                    logger.info(f"[CANCELLATION] Cancelled {count} ticket(s) for {instance_name}")
                    return True
        
        return False

    def cancel_all(self) -> int:
        """Cancel ALL waiting tickets across all pools.
        
        Called during stop_session() to clean up all pending waiters.
        
        Returns:
            Total number of tickets cancelled.
        """
        total = 0
        for pool in self._pools.values():
            count = len(pool._waiters)
            if count > 0:
                with pool._cond:
                    for ticket in pool._waiters.values():
                        ticket.cancelled.set()
                    pool._waiters.clear()
                    pool._cond.notify_all()
                total += count
        
        if total > 0:
            logger.info(f"[CANCELLATION] Cancelled {total} total tickets across all pools")
        
        return total
    
    def terminate_for_agent(self, agent_name: str) -> tuple:
        """Cancel all tickets for an agent.
        
        Called during AgentInstance.terminate() to fully clean up that agent's
        presence in all scheduling queues.
        
        Args:
            agent_name: Name of the agent to terminate from queues.
            
        Returns:
            Tuple of (tickets_cancelled, 0) for backward-compat with callers.
        """
        tickets = 0
        
        for pool in self._pools.values():
            # Cancel all waiting tickets for this agent.
            with pool._cond:
                to_remove = [tid for tid, t in pool._waiters.items() if t.instance_name == agent_name]
                for tid in to_remove:
                    pool._waiters[tid].cancelled.set()
                    pool._waiters.pop(tid)
                    tickets += 1
                pool._cond.notify_all()
        
        if tickets > 0:
            logger.info(f"[TERMINATION] Cleaned up {agent_name}: {tickets} ticket(s)")
        
        return (tickets, 0)
