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