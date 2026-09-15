"""api_router package — pure-move split of api_router.py (Phase 3a).

Sub-modules follow the dependency DAG: endpoints/scheduler/helpers are independent;
router depends on all three. Import order below is bottom-up.
"""

from agent_cascade.api_router_pkg import endpoints  # noqa: F401  (import-order side effect)
from agent_cascade.api_router_pkg import helpers  # noqa: F401  (import-order side effect)
from agent_cascade.api_router_pkg import router  # noqa: F401  (import-order side effect)
from agent_cascade.api_router_pkg import scheduler  # noqa: F401  (import-order side effect)

__all__ = [
    'APIRouter',
    'EndpointScheduler',
    'APIEndpoint',
    'ensure_api_endpoints_config',
    'MAX_CAPTION_LENGTH',
    'RATE_LIMIT_WINDOW_SECONDS',
    'CANONICAL_AGENT_TYPES',
    '_normalize_repeat_penalty',
    'QUEUE_WAIT_TIMEOUT',
    'ENDPOINT_SLOT_ACQUIRE_TIMEOUT',
    'ENDPOINT_COOLDOWN_SECONDS',
    'ENDPOINT_FAILURE_CLEANUP_HOURS',
    '_check_termination',
    '_interruptible_sleep',
]
