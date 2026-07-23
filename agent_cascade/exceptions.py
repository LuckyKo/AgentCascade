"""
Custom exceptions for AgentCascade internal operations.
"""


class CharacterRunDetected(Exception):
    """Raised when the model is stuck in a character repetition loop.

    Indicates degraded model state — the model should switch to a different
    endpoint rather than retry the same one.
    """
    pass


class MaxTokenExceeded(Exception):
    """Raised when the model exceeds its output token budget.

    Indicates the model is generating beyond reasonable limits and likely
    looping — switch endpoints rather than retry the same one.
    """
    pass