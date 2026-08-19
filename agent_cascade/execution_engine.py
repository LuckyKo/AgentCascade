"""Facade: preserves the historical import surface for ``execution_engine``.

The real implementations live in the :mod:`agent_cascade.engine` sub-package.
This thin shim re-exports only the names production code still imports from
``agent_cascade.execution_engine`` (the class plus two lazily-imported helpers).
mock.patch targets for internal helpers point at the true home sub-module, not here.
"""

from agent_cascade.engine.core import ExecutionEngine  # noqa: F401
from agent_cascade.engine.helpers import (             # noqa: F401
    _build_session_metadata,
    _inject_self_augmentation_skill,
)

__all__ = [
    "ExecutionEngine",
    "_build_session_metadata",
    "_inject_self_augmentation_skill",
]
