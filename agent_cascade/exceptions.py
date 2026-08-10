"""
Custom exceptions for AgentCascade internal operations.
"""


class CharacterRunDetected(Exception):
    """Raised when the model is stuck in a character repetition loop.

    Indicates degraded model state — the model should switch to a different
    endpoint rather than retry the same one.

    Attributes:
        detection_reason: The raw reason string from the inner loop detector
            (e.g. "character run ' ' (142)", "repeated sentence", etc.).
    """
    def __init__(self, message: str, detection_reason: str = "unknown"):
        super().__init__(message)
        self.detection_reason = detection_reason


class MaxTokenExceeded(Exception):
    """Raised when the model exceeds its output token budget.

    Indicates the model is generating beyond reasonable limits and likely
    looping — switch endpoints rather than retry the same one.
    """
    pass


class ContextWindowExceeded(Exception):
    """Raised when input exceeds the model's context window.

    Indicates the current endpoint cannot handle this payload size — switch to
    an endpoint with larger context window rather than retrying the same one.
    """
    pass


class FallbackCompressionRequired(Exception):
    """Raised by APIRouter when a context-exceeded error occurs during fallback.
    
    Signals to the ExecutionEngine that it should iteratively compress the agent's
    conversation until it fits an available endpoint, before retrying.
    
    Attributes:
        instance_name: The agent instance name that needs compression
        agent_type: The agent type (e.g., 'coder', 'researcher')
        failed_endpoint: Name/model of the endpoint that rejected due to context size
        original_error: The underlying ContextWindowExceeded or API error
    """
    def __init__(self, instance_name: str, agent_type: str, failed_endpoint: str, original_error: Exception = None):
        self.instance_name = instance_name
        self.agent_type = agent_type
        self.failed_endpoint = failed_endpoint
        self.original_error = original_error
        super().__init__(
            f"Context window exceeded on fallback endpoint '{failed_endpoint}' for "
            f"'{instance_name}' ({agent_type}). Iterative compression required before retry."
        )


class AgentTerminatedError(Exception):
    """Raised when an agent instance is terminated/dismissed mid-execution.

    Used to propagate the termination signal through call stacks (especially sync
    children running inline in parent threads) so they can abort promptly rather
    than waiting for long operations to complete.

    This is NOT an error — it's a clean abort signal. Callers should catch this
    and return early without retrying or logging as a failure.
    """
    def __init__(self, instance_name: str):
        self.instance_name = instance_name
        super().__init__(f"Instance '{instance_name}' has been terminated")