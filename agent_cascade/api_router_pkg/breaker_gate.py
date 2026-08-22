"""Bypass-path breaker gate (Change E of reports/router-cascade-fix-plan.md).

Live APIRouters register themselves here; HTTP paths that bypass
call_with_fallback (llm/oai.py context detection, tools/image_gen.py) consult
:func:`should_skip` before firing so they cannot hammer a busy physical server.

Uses weakrefs so test-created routers disappear automatically when garbage
collected — no explicit dispose hook needed. Consultations are NON-mutating
(``_breaker_is_open``): they never transition the breaker or claim the
half-open probe slot — only real call_with_fallback traffic does that.
"""

import logging
import threading
import weakref
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_routers: list = []  # weakrefs to live APIRouter instances


def register_router(router) -> None:
    """Called by APIRouter.__init__ so bypass paths can find its breakers."""
    with _lock:
        _routers.append(weakref.ref(router))
        # Prune dead refs opportunistically.
        _routers[:] = [ref for ref in _routers if ref() is not None]


def should_skip(api_base: Optional[str]) -> bool:
    """True when ANY live router's breaker forbids contacting this base right now.

    Fail-safe: returns False on any internal error — the gate must never break
    normal operation in paths that have their own try/except handling.
    """
    if not api_base:
        return False
    try:
        from agent_cascade.api_router_pkg.normalization import normalize_api_base
        key = normalize_api_base(api_base)
        with _lock:
            refs = list(_routers)
        for ref in refs:
            router = ref()
            if router is None:
                continue
            try:
                if router._breaker_is_open(api_base):
                    return True
            except Exception as e:
                logger.debug(f"[breaker_gate] consult failed: {e}")
    except Exception as e:
        logger.debug(f"[breaker_gate] should_skip error (failing open): {e}")
        return False
    return False
