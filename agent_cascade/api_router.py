"""
API Router — Facade (Phase 3a pure-move refactor).

This module was split into the ``agent_cascade.api_router_pkg`` package. It now
re-exports the full production import surface so that existing import sites
(``from agent_cascade.api_router import ...``) keep working unchanged.

Sub-modules (see api_router_pkg/__init__.py for the dependency DAG):
  - endpoints.py : APIEndpoint, ensure_api_endpoints_config, constants, _normalize_repeat_penalty
  - scheduler.py : EndpointScheduler + re-exported timeout constants
  - router.py    : APIRouter
  - helpers.py   : _check_termination, _interruptible_sleep

NOTE on ``QUEUE_WAIT_TIMEOUT``: it is imported from ``slot_queue`` and read as a
module global inside ``EndpointScheduler.acquire``. After the split it lives in
``api_router_pkg.scheduler`` — that is the module whose attribute must be patched
by tests (the re-export here is only for backward-compatible import access).
"""

from agent_cascade.api_router_pkg.router import APIRouter
from agent_cascade.api_router_pkg.scheduler import (
    EndpointScheduler,
    QUEUE_WAIT_TIMEOUT,
    ENDPOINT_SLOT_ACQUIRE_TIMEOUT,
    ENDPOINT_COOLDOWN_SECONDS,
    ENDPOINT_FAILURE_CLEANUP_HOURS,
)
from agent_cascade.api_router_pkg.endpoints import (
    APIEndpoint,
    ensure_api_endpoints_config,
    MAX_CAPTION_LENGTH,
    RATE_LIMIT_WINDOW_SECONDS,
    CANONICAL_AGENT_TYPES,
    _normalize_repeat_penalty,
)
from agent_cascade.api_router_pkg.helpers import _check_termination, _interruptible_sleep

__all__ = [
    "APIRouter",
    "EndpointScheduler",
    "APIEndpoint",
    "ensure_api_endpoints_config",
    "MAX_CAPTION_LENGTH",
    "RATE_LIMIT_WINDOW_SECONDS",
    "CANONICAL_AGENT_TYPES",
    "_normalize_repeat_penalty",
    "QUEUE_WAIT_TIMEOUT",
    "ENDPOINT_SLOT_ACQUIRE_TIMEOUT",
    "ENDPOINT_COOLDOWN_SECONDS",
    "ENDPOINT_FAILURE_CLEANUP_HOURS",
    "_check_termination",
    "_interruptible_sleep",
]
