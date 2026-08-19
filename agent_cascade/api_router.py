"""
API Router — Facade (Phase 3a pure-move refactor).

This module was split into the ``agent_cascade.api_router_pkg`` package. It now
re-exports the full production import surface so that existing import sites
(``from agent_cascade.api_router import ...``) keep working unchanged.

Sub-modules (see api_router_pkg/__init__.py for the dependency DAG):
  - endpoints.py : APIEndpoint, ensure_api_endpoints_config, _normalize_repeat_penalty
  - scheduler.py : EndpointScheduler
  - router.py    : APIRouter
  - helpers.py   : _check_termination, _interruptible_sleep

NOTE: internal constants (e.g. ``QUEUE_WAIT_TIMEOUT``) live in their true home
sub-module and must be patched there by tests — this facade does not re-export them.
"""

from agent_cascade.api_router_pkg.router import APIRouter
from agent_cascade.api_router_pkg.scheduler import EndpointScheduler
from agent_cascade.api_router_pkg.endpoints import (
    APIEndpoint,
    ensure_api_endpoints_config,
    _normalize_repeat_penalty,
)
from agent_cascade.api_router_pkg.helpers import _check_termination, _interruptible_sleep

__all__ = [
    "APIRouter",
    "EndpointScheduler",
    "APIEndpoint",
    "ensure_api_endpoints_config",
    "_normalize_repeat_penalty",
    "_check_termination",
    "_interruptible_sleep",
]
