"""
MessageQueueMixin — per-agent message queues and the active execution stack. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
class MessageQueueMixin:
    @property
    def _state_lock(self):
        """Delegate to ParallelAgentManager's state lock.

        Required by code that references self.agent_pool._state_lock (e.g.,
        agent_invoker.py for thread-safe access).
        """
        return self._execution._state_lock

    @property
    def active_stack(self) -> List[tuple]:
        """Active execution stack — delegates to ParallelAgentManager (thread-safe read).

        Returns a list of (instance_name, nest_depth) tuples.
        Lock is held during copy to ensure a consistent snapshot even under concurrent mutation.
        Writes go through mutation methods which acquire _execution._state_lock.
        """
        with self._execution._state_lock:
            return list(self._execution.active_stack)  # defensive copy for thread safety

    # ── Active stack mutation methods (Fix #2) ────────────────────────────────
    # The active_stack property returns a defensive copy, so mutations must go
    # through these methods to actually modify the underlying stack.

    def active_stack_append(self, name: str, depth: int = 0):
        """Append an instance name with nesting depth to the active execution stack (thread-safe)."""
        with self._execution._state_lock:
            self._execution.active_stack.append((name, depth))

    def active_stack_remove(self, name: str):
        """Remove an instance name from the active execution stack (thread-safe)."""
        with self._execution._state_lock:
            for i, (n, _depth) in enumerate(self._execution.active_stack):
                if n == name:
                    self._execution.active_stack.pop(i)
                    break

    def active_stack_clear(self):
        """Clear the entire active execution stack (thread-safe)."""
        with self._execution._state_lock:
            self._execution.active_stack.clear()

    def active_stack_pop_at(self, index: int):
        """Pop an entry at a specific index from the active execution stack (thread-safe)."""
        with self._execution._state_lock:
            if 0 <= index < len(self._execution.active_stack):
                self._execution.active_stack.pop(index)

    def send_message(self, from_name: str, to_name: str, text: str):
        """Route a message to an agent."""
        with self._queue_lock:
            self.message_queues.setdefault(to_name, []).append(text)

    def enqueue_message(self, instance_name: str, text: str):
        """Push a message into a specific agent's queue (no sender tracking)."""
        with self._queue_lock:
            self.message_queues.setdefault(instance_name, []).append(text)
            self._message_condition.notify_all()  # Wake any __wait callers
        self._mark_activity(instance_name)

    def drain_queue(self, instance_name: str) -> List[str]:
        """Drain all pending messages for an instance atomically.

        Batched drain operation for efficiency.

        This operation pops the entire queue at once, minimizing lock contention
        and ensuring no messages are missed between drain calls. Returns empty
        list if no messages queued.

        Args:
            instance_name: The agent instance to drain messages for.

        Returns:
            List of message texts (may be empty). Original queue is cleared.
        """
        # Atomic pop ensures thread-safe batched drain
        with self._queue_lock:
            return self.message_queues.pop(instance_name, [])

    def has_messages(self, instance_name: str) -> bool:
        """Check if there are pending messages for an instance."""
        with self._queue_lock:
            return bool(self.message_queues.get(instance_name))

    def get_queue_messages(self, instance_name: str) -> List[str]:
        """Get queued messages for an instance.

        Args:
            instance_name: The agent instance name to query.

        Returns:
            List of full message strings (empty if no queue).
        """
        with self._queue_lock:
            queue = list(self.message_queues.get(instance_name, []))
        return [str(msg) for msg in queue]

    def dismiss_queue_message(self, instance_name: str, message_index: int) -> bool:
        """Remove a specific message from the queue by index.

        Args:
            instance_name: The agent instance name.
            message_index: Index of the message to remove (0-based).
                          Use -1 to clear all queued messages.

        Returns:
            True if a message was removed, False otherwise.
        """
        with self._queue_lock:
            queue = self.message_queues.get(instance_name)
            if queue is None or len(queue) == 0:
                return False

            if message_index == -1:
                # Clear all queued messages for this instance
                self.message_queues.pop(instance_name, None)
                return True

            if 0 <= message_index < len(queue):
                queue.pop(message_index)
                if not queue:
                    self.message_queues.pop(instance_name, None)
                return True

        return False

    def wait_for_message(self, instance_name: str, timeout: float = 30.0,
                         consume_predicate=None) -> Optional[str]:
        """Block until ANY message is available for this instance (or timeout/terminated), then
        decide whether to consume it based on the front-of-queue message.

        Wake-up contract (v2): wake on ANY queued message — user, system, other-tool shell,
        etc. On wake, inspect only the FRONT of the queue (`msgs[0]`):
          - `consume_predicate is None` → pop(0) and return it (unchanged default behavior).
          - else if `consume_predicate(msgs[0])` is True → pop(0) and return it (consumed).
          - else → **peek**: return `msgs[0]` WITHOUT popping, leaving the queue intact for the
            normal drain (`engine/core.py:2320`). The caller re-checks the predicate to tell
            "consumed" from "peeked".

        Used by the shell_cmd `__wait` tool: it wakes on any queued message, intercepts and
        returns its own tool_id's heartbeat/completion verbatim when that is at the front, and
        otherwise leaves the queue untouched so the normal drain delivers everything in sequence.

        Returns None if dismissed/terminated or if the timeout elapses with an EMPTY queue.

        Args:
            instance_name: The agent instance to wait for messages.
            timeout: Maximum seconds to wait. If None, wait indefinitely.
            consume_predicate: Optional callable applied to the FRONT message on wake. When set
                and it returns True, the front message is consumed (popped); when it returns
                False, the front message is returned by reference but left queued (peek).

        Returns:
            A single message string — either a consumed one or a peeked (still-queued) front
            message — or None if the timeout elapses with an empty queue / instance terminated.

            The returned value is the actual queued message object (no copy). On the peek path
            it remains in the queue: returning it does NOT alter the queue (only the consume
            path removes it). Strings are immutable, so holding this reference is safe.
        """
        with self._message_condition:
            deadline = None if timeout is None else time.time() + timeout

            while True:
                # Check termination before each wait iteration
                if self.is_instance_terminated(instance_name):
                    return None  # Dismissed — wake up and let caller handle it

                msgs = self.message_queues.get(instance_name)
                if msgs and len(msgs) > 0:
                    if consume_predicate is None:
                        # Take the first available message for this instance
                        return msgs.pop(0)
                    # Consume-vs-peek on the FRONT message only. All under _message_condition
                    # (which holds _queue_lock). The peek path must NOT mutate the list.
                    front = msgs[0]
                    if consume_predicate(front):
                        return msgs.pop(0)
                    return front  # not ours → leave queued; caller re-checks to decide

                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        # Clean up empty list if we created one
                        if instance_name in self.message_queues and not self.message_queues[instance_name]:
                            del self.message_queues[instance_name]
                        return None
                    self._message_condition.wait(timeout=min(remaining, 1.0))
                else:
                    # For indefinite waits, use a shorter timeout to allow periodic termination checks
                    self._message_condition.wait(timeout=2.0)

    def has_pending(self, instance_name: str) -> bool:
        """Check if there are pending async tool calls for an instance.

        Uses AsyncToolRegistry to track pending background tool entries,
        and AsyncShellTracker for background shell commands.

        Args:
            instance_name: The agent instance to check.

        Returns:
            True if the instance has pending async tools, False otherwise.
        """
        # Check async tool registry (call_agent background tools)
        if self._async_registry.has_pending(instance_name):
            return True
        # Also check async shell tasks (shell commands launched in async execution mode)
        if hasattr(self, '_async_shell_tracker') and self._async_shell_tracker:
            if self._async_shell_tracker.has_active_tasks(instance_name):
                return True
        return False
