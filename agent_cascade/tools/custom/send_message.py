import asyncio
import logging
import time
from typing import TYPE_CHECKING

from agent_cascade.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.agent_instance import ACTIVE_STATES
from agent_cascade.operation_manager.path_security import _get_current_instance_name

if TYPE_CHECKING:
    from agent_cascade.agent_pool import AgentPool


@register_tool('send_message', allow_overwrite=True)
class SendMessage(BaseTool):
    """Sends an async message to another running agent or to the user."""

    name = 'send_message'
    description = TOOL_METADATA['send_message']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'destination': {
                'type': 'string',
                'description': "Target of the message. Use 'user' to send to the human user, or an exact agent instance name (e.g., 'worker1') to send to another agent."
            },
            'message': {
                'type': 'string',
                'description': 'The message content to send.'
            }
        },
        'required': ['destination', 'message']
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool: 'AgentPool' = agent_pool

    def call(self, params: str, **kwargs) -> str:
        if not self.agent_pool:
            return "Error: No agent pool available."

        parsed = self._verify_json_format_args(params)
        destination = str(parsed.get('destination', '')).strip()
        message = str(parsed.get('message', '')).strip()

        if not destination:
            return "Failed: Destination cannot be empty."
        if not message:
            return "Failed: Message content cannot be empty."

        # Handle user destination
        if destination == 'user':
            return self._send_to_user(message)

        # Handle agent destination
        return self._send_to_agent(destination, message)

    def _get_sender_name(self) -> str:
        """Get the current agent instance name from thread-local storage."""
        return _get_current_instance_name() or 'unknown'

    def _send_to_user(self, message: str) -> str:
        """Push message to frontend via WebSocket as notification."""
        pool = self.agent_pool
        sender = self._get_sender_name()

        try:
            ws_queue = getattr(pool, '_ws_send_queue', None)
            ws_loop = getattr(pool, '_ws_loop', None)

            if not (ws_queue and ws_loop and not ws_loop.is_closed()):
                logger.warning(f"WebSocket unavailable, message to user not delivered via notification: [{sender}] {message}")
                return "Warning: User notification sent but WebSocket unavailable. Message logged."

            event = {
                'type': 'agent_message_to_user',
                'sender': sender,
                'message': message,
                'timestamp': time.time()
            }

            asyncio.run_coroutine_threadsafe(
                _put_stream_update(ws_queue, event),
                ws_loop
            )
            return "Message sent successfully to the user. They will see it in their notifications."
        except Exception as e:
            # Log full traceback, don't expose details to caller
            logger.exception("Failed to send message to user via WebSocket")
            return "Warning: Message queued but notification may not be delivered immediately."

    def _send_to_agent(self, destination: str, message: str) -> str:
        """Queue message for target agent."""
        pool = self.agent_pool
        sender = self._get_sender_name()

        # Self-message guard
        if destination == sender:
            return "Failed: Cannot send a message to yourself."

        # Thread-safe access: acquire pool lock while checking instance + state
        with pool._pool_lock:
            target_instance = pool.instances.get(destination)
            if target_instance is None:
                return f"Failed: No agent instance named '{destination}' exists."

            # Use direct .state field (AgentState enum), not get_state() which doesn't exist
            current_state = target_instance.state
            if current_state not in ACTIVE_STATES:
                state_name = current_state.name
                return f"Failed: Agent '{destination}' is currently {state_name}. Messages are only delivered to actively running agents."

        # Tag message with sender for agent-to-agent context
        tagged_message = f"[MESSAGE from {sender}]: {message}"
        
        # Enqueue outside the pool lock (enqueue_message has its own queue lock)
        pool.enqueue_message(destination, tagged_message)

        return f"Message sent successfully to '{destination}'. It will be delivered on their next turn."


async def _put_stream_update(queue, event):
    """Helper: put event on queue, drop if full."""
    try:
        await asyncio.wait_for(queue.put(event), timeout=1.0)
    except asyncio.TimeoutError:
        pass  # Drop message if queue is full (non-critical notification)
