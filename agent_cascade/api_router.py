"""
API Router — Multi-endpoint API selection with priority-based fallback.

Manages a pool of named LLM API endpoints. Each agent *type* (orchestrator,
coder, researcher…) can have its own priority-ordered list of endpoints.
The General Settings API (default_llm_cfg) is always the last-resort fallback.

Persistence: workspace/config/api_endpoints.json
"""

import collections
import copy
import json
import logging
import random
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from agent_cascade.settings import ENDPOINT_SLOT_ACQUIRE_TIMEOUT

# Canonical agent type mapping for case-insensitive normalization and idempotency
# Maps lowercase agent types to their canonical PascalCase form
CANONICAL_AGENT_TYPES: Dict[str, str] = {
    "coder": "Coder",
    "researcher": "Researcher",
    "orchestrator": "Orchestrator",
    "security": "Security",
    "writer": "Writer",
    "reviewer": "Reviewer",
    "compressor": "Compressor",
    "generalist": "Generalist",
}


# ── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class APIEndpoint:
    """A single LLM API endpoint configuration."""
    id: str = ""                       # UUID, auto-generated if empty
    name: str = "Unnamed Endpoint"     # Human-friendly label
    api_base: str = ""                 # e.g. "http://localhost:1234/v1"
    api_key: str = "EMPTY"             # API key (may be "EMPTY" for local)
    model: str = ""                    # Model name/ID
    model_type: str = "qwenvl_oai"     # "qwenvl_oai", "openai", etc.
    enabled: bool = True               # Toggle on/off without deleting
    max_retries: int = 2               # Per-endpoint retry count before moving to next
    concurrency_limit: int = -1        # -1 = unlimited, 0 = sequential delegation, 1+ = max parallel requests
    max_input_tokens: int = 0          # 0 = unlimited/auto, 1+ = specific limit for this model
    base_retry_delay: float = 1.0      # Base delay for retry backoff in seconds (exponential from here)
    max_retry_delay: float = 30.0      # Maximum cap on retry delay in seconds
    rate_limit_rpm: int = 0            # Rate limit in requests per minute (0 = unlimited)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())

    def to_llm_cfg(self) -> dict:
        """Convert to the llm_cfg dict format used by agent_cascade."""
        return {
            'model': self.model,
            'model_server': self.api_base,
            'api_base': self.api_base,
            'api_key': self.api_key,
            'model_type': self.model_type,
            'max_input_tokens': self.max_input_tokens,
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'APIEndpoint':
        if not isinstance(data, dict):
            return cls()
        # Filter to only known fields to prevent TypeError on unexpected keys
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ── Endpoint Scheduler (Lifecycle-aware serialization) ───────────────────────

class EndpointScheduler:
    """
    Manages per-API-base scheduling with lifecycle-aware serialization.
    
    Uses threading.Semaphore per endpoint for race-free capacity control.
    For concurrency=0 endpoints: agents are strictly serialized — one at a time,
    from task submission to full completion (including all LLM calls and tool waits).
    All concurrency=0 endpoints share the SAME slot to avoid cache trashing from
    interleaving across different API addresses.
    For concurrency=N endpoints: at most N agents can run simultaneously.
    For concurrency=-1 endpoints: no scheduling needed (unlimited).
    
    This operates at the AGENT TASK level (entire agent lifecycle), NOT the
    individual API call level. This prevents interleaving of LLM calls between
    different agents on the same endpoint.
    """
    
    def __init__(self):
        self._lock = threading.Lock()
        # Per-endpoint semaphore for blocking acquire + active counter
        # api_base -> {'sem': Semaphore(max_active), 'active_count': int}
        self._schedules: Dict[str, Dict] = {}
        # SLOT_TIMEOUT FIX v2: Track which instances hold slots for better debugging
        # slot_key -> list of (instance_name, agent_class, acquired_at_timestamp, acquisition_id) tuples
        # ISSUE #2 FIX: Use unique acquisition_id to match acquire/release pairs correctly
        self._slot_holders: Dict[str, List[tuple]] = {}
        self._next_acquisition_id = 0  # Counter for unique acquisition IDs
    
    def acquire(self, api_base: str, concurrency_limit: int, instance_name: str = "unknown", agent_class: str = "unknown") -> Optional[Callable[[], None]]:
        """
        Acquire a slot on the endpoint. Blocks if at capacity.
        Returns a cleanup callback to release the slot, or None if unlimited.
        
        If the concurrency limit has changed since this endpoint was last scheduled,
        the semaphore is safely resized — active agents retain their slots, and
        new agents see the updated capacity.
        
        Args:
            api_base: The API base URL of the endpoint
            concurrency_limit: -1=unlimited, 0=sequential, N>0=max parallel
            instance_name: Name of the agent instance acquiring the slot (for tracking)
            agent_class: Class of the agent instance (for tracking)
            
        Returns:
            A callable that releases the slot when called, or None if no scheduling needed.
        """
        if concurrency_limit == -1:  # unlimited — no scheduling needed
            logger.debug(f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — api_base={api_base}, concurrency=-1 (unlimited), returning None")
            return None
        
        # BUG FIX: All concurrency=0 endpoints share the same slot to avoid cache trashing.
        # Use a shared slot key for sequential endpoints instead of the api_base.
        is_sequential = (concurrency_limit == 0)
        slot_key = '_shared_sequential_slot_' if is_sequential else api_base
        
        new_max = concurrency_limit if concurrency_limit > 0 else 1  # 0→1 (sequential), -1 handled above
        logger.debug(
            f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — api_base={api_base}, "
            f"concurrency_limit={concurrency_limit}, slot_key={slot_key}, new_max={new_max}, "
            f"instance_name={instance_name}, agent_class={agent_class}"
        )
        
        with self._lock:
            if slot_key not in self._schedules:
                self._schedules[slot_key] = {
                    'sem': threading.Semaphore(new_max),
                    'active_count': 0,
                    'max_active': new_max,
                }
            else:
                sched = self._schedules[slot_key]
                # Check if concurrency limit changed and resize if needed
                if sched['max_active'] != new_max:
                    old_max = sched['max_active']
                    sched['max_active'] = new_max
                    
                    # Resize the semaphore safely: create a new one and transfer
                    # active_count worth of permits to reflect currently running agents.
                    new_sem = threading.Semaphore(new_max)
                    
                    # Pre-acquire slots in new sem equal to current active count
                    # (these agents already hold their slots, so the new sem should
                    # have fewer available permits accordingly).
                    for _ in range(sched['active_count']):
                        new_sem.acquire()
                    
                    # Swap atomically under lock — all subsequent acquire/release
                    # calls will use the resized semaphore.
                    sched['sem'] = new_sem
                    
                    # Log with original api_base for clarity, but note if using shared slot
                    log_target = api_base if not is_sequential else f"{api_base} (shared sequential)"
                    logger.info(f"[EndpointScheduler] Resized '{log_target}' from {old_max} → {new_max}")
            
            sched = self._schedules[slot_key]
        
        # Semaphore.acquire() blocks atomically if at capacity.
        # Note: sched['sem'] is captured under lock above, but theoretically a concurrent
        # resize could swap it between here and the acquire call. In practice this is benign
        # because all concurrency=0 endpoints use new_max=1 (no resize), and the race window
        # is extremely narrow.
        logger.debug(f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — blocking on semaphore for api_base={api_base}")
        if not sched['sem'].acquire(timeout=ENDPOINT_SLOT_ACQUIRE_TIMEOUT):
            # SLOT_TIMEOUT FIX v2: Include slot holder information in timeout error
            holder_info = ""
            with self._lock:
                holders = self._slot_holders.get(slot_key, [])
                if holders:
                    holder_names = [f"{h[0]} ({h[1]})" for h in holders]
                    holder_info = f". Currently held by: {', '.join(holder_names)}"
            
            raise TimeoutError(
                f"Timed out after {ENDPOINT_SLOT_ACQUIRE_TIMEOUT}s waiting for endpoint slot on {api_base}. "
                f"Current active count: {sched['active_count']}, max allowed: {sched['max_active']}{holder_info}"
            )
        
        with self._lock:
            # ISSUE #2 FIX: Generate unique acquisition ID BEFORE incrementing active_count
            # This ensures atomic assignment of both the ID and the counter update
            acquisition_id = self._next_acquisition_id
            self._next_acquisition_id += 1
            
            sched['active_count'] += 1
            current = sched['active_count']
            
            # SLOT_TIMEOUT FIX v2: Track which instance holds the slot with unique ID
            if slot_key not in self._slot_holders:
                self._slot_holders[slot_key] = []
            self._slot_holders[slot_key].append((instance_name, agent_class, time.monotonic(), acquisition_id))
            
            logger.info(f"[EndpointScheduler] Agent '{instance_name}' ({agent_class}) acquired slot on '{api_base}' "
                       f"(active: {current}, limit: {sched['max_active']})")
            logger.debug(
                f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — successfully acquired, "
                f"api_base={api_base}, active_count={current}, max_active={sched['max_active']}, "
                f"instance_name={instance_name}, agent_class={agent_class}"
            )
        
        # Return cleanup callback that releases the semaphore when called.
        # IMPORTANT: reads the CURRENT semaphore from the live schedule entry,
        # not a captured reference — this is critical because the semaphore may
        # have been resized (swapped) since acquire() was called.
        # Capture slot_key in the closure to ensure correct schedule lookup on release.
        
        # FIX C1: Double-release protection using nonlocal flag in closure
        _released = False  # Flag to track if release has been called
        
        # ISSUE #2 FIX: Capture acquisition_id for precise holder removal matching
        captured_acquisition_id = acquisition_id
        
        def release():
            nonlocal _released  # Use nonlocal for cleaner Python 3 syntax
            
            # FIX C1: Guard against double-release - if already released, return silently
            if _released:
                logger.debug(
                    f"[CALL_AGENT_DEBUG] EndpointScheduler.release — already released for slot_key={slot_key}, "
                    f"skipping double-release"
                )
                return
            
            with self._lock:
                # Log with original api_base for clarity, but note if using shared slot
                log_target = api_base if not is_sequential else f"{api_base} (shared sequential)"
                
                current_sched = self._schedules.get(slot_key)
                if not current_sched:
                    # FIX: Handle case where schedule entry was deleted (e.g., stale cleanup)
                    logger.error(
                        f"[SLOT_RELEASE_ERROR] No schedule found for slot_key={slot_key} on release. "
                        f"Slot may be permanently held. {log_target}"
                    )
                    _released = True  # Mark as released even on error
                    return
                
                # FIX: Release semaphore FIRST, then decrement counter
                # This prevents deadlock where active_count=0 but semaphore blocks forever
                try:
                    current_sched['sem'].release()
                except Exception as e:
                    logger.error(
                        f"[SLOT_RELEASE_ERROR] Failed to release semaphore for {log_target}: {e}",
                        exc_info=True
                    )
                    # On sem.release() failure, slot remains held (don't decrement counter)
                    _released = True  # Mark as released even on error
                    return
                
                # Decrement counter AFTER successful sem.release()
                old_count = current_sched['active_count']
                if old_count < 0:
                    logger.error(
                        f"[SLOT_RELEASE_ERROR] active_count was {old_count} (negative) on release "
                        f"for {log_target}. This indicates a double-release or tracking bug."
                    )
                elif old_count == 0:
                    logger.warning(
                        f"[SLOT_RELEASE_WARNING] active_count was {old_count} on release for {log_target}. "
                        f"This may indicate the schedule was recreated or there's a tracking issue."
                    )
                
                # SLOT_TIMEOUT FIX v2: Remove this instance from slot holders list
                # ISSUE #2 FIX: Match by unique acquisition_id for precise removal
                if slot_key in self._slot_holders:
                    original_len = len(self._slot_holders[slot_key])
                    self._slot_holders[slot_key] = [
                        h for h in self._slot_holders[slot_key] 
                        if h[3] != captured_acquisition_id  # Match by unique acquisition ID
                    ]
                    removed_count = original_len - len(self._slot_holders[slot_key])
                    if removed_count == 1:
                        logger.debug(
                            f"[CALL_AGENT_DEBUG] EndpointScheduler.release — removed holder entry "
                            f"for instance_name={instance_name}, agent_class={agent_class}, acquisition_id={captured_acquisition_id}"
                        )
                    elif removed_count > 1:
                        logger.warning(
                            f"[SLOT_RELEASE_WARNING] Removed {removed_count} holder entries for acquisition_id={captured_acquisition_id}. "
                            f"This should typically be 1. instance_name={instance_name}, agent_class={agent_class}"
                        )
                    elif removed_count == 0:
                        logger.debug(
                            f"[CALL_AGENT_DEBUG] EndpointScheduler.release — holder entry already removed for acquisition_id={captured_acquisition_id}. "
                            f"instance_name={instance_name}, agent_class={agent_class}"
                        )
                
                current_sched['active_count'] = max(0, old_count - 1)
                new_count = current_sched['active_count']
                
                logger.info(f"[EndpointScheduler] Agent '{instance_name}' ({agent_class}) released slot on '{log_target}' "
                           f"(active: {new_count}, limit: {current_sched['max_active']})")
                logger.debug(
                    f"[CALL_AGENT_DEBUG] EndpointScheduler.release — api_base={api_base}, "
                    f"slot_key={slot_key}, active_count={new_count}, max_active={current_sched['max_active']}, "
                    f"instance_name={instance_name}, agent_class={agent_class}"
                )
                
                _released = True  # Mark as successfully released
        
        return release
    
    def count_active(self, api_base: str, concurrency_limit: int) -> int:
        """Count active tasks on an endpoint.
        
        Args:
            api_base: The API base URL of the endpoint
            concurrency_limit: -1=unlimited, 0=sequential, N>0=max parallel
            
        Returns:
            Number of currently active agents on this endpoint
        """
        # For concurrency=0, use shared slot key; otherwise use api_base directly
        slot_key = '_shared_sequential_slot_' if concurrency_limit == 0 else api_base
        with self._lock:
            sched = self._schedules.get(slot_key)
            return sched['active_count'] if sched else 0
    
    def get_status(self) -> Dict[str, Dict]:
        """Get status of all scheduled endpoints (for diagnostics).
        
        Returns:
            Dictionary mapping endpoint identifiers to their current status.
            Shared sequential slots are labeled as '[SHARED] _shared_sequential_slot_'
            to distinguish them from per-endpoint schedules.
        """
        with self._lock:
            result = {}
            for key, sched in self._schedules.items():
                # Use meaningful label for shared slot to avoid confusion in diagnostics
                display_key = f"[SHARED] {key}" if 'shared' in key.lower() else key
                # Semaphore._value is implementation detail but useful for diagnostics
                sem_value = sched['sem']._value if hasattr(sched['sem'], '_value') else 'unknown'
                
                # SLOT_TIMEOUT FIX v2: Include slot holder information in status
                holders_info = []
                if key in self._slot_holders:
                    # FIX CRITICAL BUG #2: Tuple has 4 elements (instance_name, agent_class, acquired_at, acquisition_id)
                    for instance_name, agent_class, acquired_at, _acquisition_id in self._slot_holders[key]:
                        held_duration = time.monotonic() - acquired_at
                        holders_info.append({
                            'instance_name': instance_name,
                            'agent_class': agent_class,
                            'held_duration_seconds': held_duration
                        })
                
                result[display_key] = {
                    'active_count': sched['active_count'],
                    'max_active': sched['max_active'],
                    'semaphore_slots': sem_value,
                    'slot_holders': holders_info,  # List of instances holding slots with metadata
                }
            return result
    
    def cleanup_stale(self):
        """Remove schedule entries for endpoints with no activity.
        
        A schedule is stale when active_count is 0 AND all semaphore permits
        are available (no waiting agents). This prevents memory leaks from
        endpoints that were used temporarily and have since gone idle.
        
        Note: The shared sequential slot (_shared_sequential_slot_) is NOT cleaned up
        to avoid unnecessary recreation of the shared semaphore across different
        concurrency=0 endpoints.
        """
        with self._lock:
            stale = [ab for ab, s in self._schedules.items()
                     if ab != '_shared_sequential_slot_'  # Protect shared slot from cleanup
                     and s['active_count'] == 0 and s['sem']._value >= s['max_active']]
            for ab in stale:
                del self._schedules[ab]
                # Also clean up slot holders for this endpoint
                if ab in self._slot_holders:
                    del self._slot_holders[ab]
            if stale:
                logger.info(f"[EndpointScheduler] Cleaned up {len(stale)} stale schedule(s)")

    def get_slot_holders(self, slot_key: str = None) -> Dict[str, List[tuple]]:
        """Get information about which instances are holding slots.
        
        SLOT_TIMEOUT FIX v2: Diagnostic method to identify slot holders for debugging.
        
        Args:
            slot_key: Optional specific slot key to query. If None, returns all.
            
        Returns:
            Dictionary mapping slot keys to lists of (instance_name, agent_class, acquired_at, acquisition_id) tuples.
            Returns deep copies to prevent external modification of internal state (Issue #3).
        """
        import copy
        with self._lock:
            if slot_key:
                holders = self._slot_holders.get(slot_key, [])
                return {slot_key: copy.deepcopy(holders)}  # ISSUE #3 FIX: Return deep copy
            return copy.deepcopy(self._slot_holders)  # ISSUE #3 FIX: Return deep copy

    def detect_stuck_slots(self, threshold_seconds: float = 60.0) -> List[dict]:
        """Detect slots that have been held for longer than the threshold.
        
        SLOT_TIMEOUT FIX v2: Diagnostic method to identify potentially stuck slots.
        
        Args:
            threshold_seconds: Time in seconds after which a slot is considered "stuck"
            
        Returns:
            List of dictionaries with information about stuck slots, including:
            - slot_key: The slot identifier
            - instance_name: Name of the holding instance
            - agent_class: Class of the holding instance  
            - held_duration: How long the slot has been held (seconds)
            - acquired_at: Timestamp when slot was acquired
        """
        stuck_slots = []
        current_time = time.monotonic()
        
        with self._lock:
            for slot_key, holders in self._slot_holders.items():
                # ISSUE #6 FIX: Cross-reference with _schedules to verify slot is still active
                sched = self._schedules.get(slot_key)
                if not sched or sched['active_count'] == 0:
                    continue  # Skip if no schedule or no active agents
                
                # FIX CRITICAL BUG #1: Tuple has 4 elements (instance_name, agent_class, acquired_at, acquisition_id)
                for instance_name, agent_class, acquired_at, _acquisition_id in holders:
                    held_duration = current_time - acquired_at
                    if held_duration > threshold_seconds:
                        stuck_slots.append({
                            'slot_key': slot_key,
                            'instance_name': instance_name,
                            'agent_class': agent_class,
                            'held_duration': held_duration,
                            'acquired_at': acquired_at
                        })
                        logger.warning(
                            f"[SLOT_STUCK_DETECTION] Slot on '{slot_key}' held by '{instance_name}' "
                            f"({agent_class}) for {held_duration:.1f}s (threshold: {threshold_seconds}s)"
                        )
        
        return stuck_slots


# ── API Router ───────────────────────────────────────────────────────────────

class APIRouter:
    """
    Manages multi-endpoint API selection with priority-based fallback.

    The General Settings API (``default_llm_cfg``) is always available as the
    last-resort endpoint for every agent type.
    """

    def __init__(self, default_llm_cfg: dict, config_dir: Optional[str] = None):
        """
        Args:
            default_llm_cfg: The main LLM config from General Settings. This
                            is always the last-resort fallback and is never deleted.
            config_dir:     Directory to persist api_endpoints.json.
                            Defaults to workspace/config.
        """
        self.default_llm_cfg = default_llm_cfg
        self.endpoints: Dict[str, APIEndpoint] = {}           # id → endpoint
        self.agent_priorities: Dict[str, List[str]] = {}      # agent_type → [endpoint_ids]
        self._lock = threading.Lock()
        
        # Per-server semaphores for concurrency control: api_base -> (Semaphore, limit)
        self._semaphores: Dict[str, Tuple[threading.Semaphore, int]] = {}
        self._sem_lock = threading.Lock()

        # Lifecycle-aware endpoint scheduler for parallel agent management.
        # Acquires a slot at task submission time and holds it for the entire
        # agent lifecycle — prevents interleaving of LLM calls between agents.
        self.scheduler = EndpointScheduler()

        # Track the last successfully used endpoint config for automatic recovery.
        # When an agent's configured endpoints become unavailable, this provides
        # a validated fallback that previously succeeded (Tier 2 in fallback chain).
        self._last_successful_endpoint_cfg: Optional[Dict[str, Any]] = None

        # Rate limiting: track call timestamps per endpoint for rate limit enforcement.
        # api_base -> deque of timestamps (seconds since epoch) for efficient sliding window.
        self._endpoint_call_history: Dict[str, Deque[float]] = {}

        # Persistence path
        if config_dir:
            self._config_dir = Path(config_dir)
        else:
            # API config lives in the project root config/ dir, not workspace
            project_root = Path(__file__).resolve().parent.parent
            self._config_dir = project_root / 'config'
        self._config_path = self._config_dir / 'api_endpoints.json'

        # Load persisted config if available
        self._load()

    # ── Endpoint CRUD ────────────────────────────────────────────────────

    def add_endpoint(self, endpoint: APIEndpoint) -> str:
        """Add or update an endpoint. Returns the endpoint ID."""
        with self._lock:
            if not endpoint.id:
                endpoint.id = str(uuid.uuid4())
            self.endpoints[endpoint.id] = endpoint
            self._save()
            return endpoint.id

    def remove_endpoint(self, endpoint_id: str) -> bool:
        """Remove an endpoint by ID. Returns True if removed."""
        with self._lock:
            if endpoint_id not in self.endpoints:
                return False
            # Get the api_base before deleting to clean up related state
            endpoint_api_base = self.endpoints[endpoint_id].api_base
            del self.endpoints[endpoint_id]
            
            # Clean up rate limit history for this endpoint's api_base
            if endpoint_api_base in self._endpoint_call_history:
                del self._endpoint_call_history[endpoint_api_base]
            
            # Also clean up any agent_priorities referencing this endpoint
            for agent_type in list(self.agent_priorities.keys()):
                self.agent_priorities[agent_type] = [
                    eid for eid in self.agent_priorities[agent_type]
                    if eid != endpoint_id
                ]
                # Remove empty lists
                if not self.agent_priorities[agent_type]:
                    del self.agent_priorities[agent_type]
            self._save()
            return True

    def update_endpoint(self, endpoint_id: str, updates: dict) -> bool:
        """Partially update an existing endpoint. Returns True if found."""
        with self._lock:
            ep = self.endpoints.get(endpoint_id)
            if not ep:
                return False
            for k, v in updates.items():
                if hasattr(ep, k) and k != 'id':
                    setattr(ep, k, v)
            self._save()
            return True

    def get_endpoint(self, endpoint_id: str) -> Optional[APIEndpoint]:
        """Get a single endpoint by ID."""
        return self.endpoints.get(endpoint_id)

    def list_endpoints(self) -> List[APIEndpoint]:
        """Return all endpoints in insertion order."""
        with self._lock:
            return list(self.endpoints.values())

    # ── Agent Priority Management ────────────────────────────────────────

    def set_agent_priorities(self, agent_type: str, endpoint_ids: List[str]):
        """
        Set the priority-ordered endpoint list for an agent type.
        
        Performs case-insensitive key normalization to prevent duplicate keys
        when frontend (PascalCase) and backend (lowercase) both update priorities.
        """
        with self._lock:
            # Normalize to canonical case (existing key or input as-is)
            canonical = self._normalize_agent_type(agent_type)
            
            # If normalized key differs from input and input exists, remove it to prevent duplicates
            if canonical != agent_type and agent_type in self.agent_priorities:
                del self.agent_priorities[agent_type]
            
            # Validate that all IDs exist
            valid_ids = [eid for eid in endpoint_ids if eid in self.endpoints]
            filtered_count = len(endpoint_ids) - len(valid_ids)
            
            if valid_ids:
                self.agent_priorities[canonical] = valid_ids
                logger.info(f"[APIRouter.set_agent_priorities] {canonical} → {valid_ids} "
                           f"({'filtered ' + str(filtered_count) + ' invalid IDs, ' if filtered_count else ''}"
                           f"canonical key: {canonical})")
            elif canonical in self.agent_priorities:
                del self.agent_priorities[canonical]
                logger.info(f"[APIRouter.set_agent_priorities] Removed priorities for {canonical} "
                           f"(all {len(endpoint_ids)} IDs were invalid)")
            else:
                logger.debug(f"[APIRouter.set_agent_priorities] No action for {agent_type} "
                            f"(no valid IDs, no existing priorities)")
            self._save()

    def get_agent_priorities(self, agent_type: str) -> List[str]:
        """Get the endpoint ID list for a specific agent type."""
        with self._lock:
            normalized = self._normalize_agent_type(agent_type)
            return list(self.agent_priorities.get(normalized, []))

    def get_effective_concurrency(self, agent_type: str) -> int:
        """
        Returns the concurrency limit of the actual endpoint that will be used 
        for the given agent type, including the default fallback.
        Returns -1 only if truly unlimited (no endpoint config found at all).
        Returns 0 as a conservative default when the default config specifies an
        api_base but no matching endpoint exists in self.endpoints — this prevents
        unexpected parallel launches on unknown endpoints.
        
        This is the correct method to use for parallel launch checks because it
        resolves the real endpoint — agents with no custom endpoints (like
        Security Advisor) inherit the caller's default and must respect its
        concurrency, not blindly return -1.
        """
        defaults = self.default_llm_cfg or {}
        with self._lock:
            # Normalize agent_type for case-insensitive lookup (Fix Finding 1)
            normalized_agent_type = self._normalize_agent_type(agent_type)
            # First check agent-specific priorities
            for eid in self.agent_priorities.get(normalized_agent_type, []):
                ep = self.endpoints.get(eid)
                if ep and ep.enabled:
                    return ep.concurrency_limit
            # Fall back to default endpoint — find it by api_base
            default_base = defaults.get('api_base') or defaults.get('model_server', '')
            for ep in self.endpoints.values():
                if ep.api_base == default_base:
                    return ep.concurrency_limit
        # Default config exists with an api_base but no matching endpoint found.
        # Return 0 (sequential) as a conservative safety measure rather than -1,
        # because the user has configured an endpoint — we just can't find it.
        if defaults.get('api_base') or defaults.get('model_server'):
            return 0
        # Truly no config at all — unlimited
        return -1

    # ── LLM Config Resolution ────────────────────────────────────────────

    def get_llm_config(self, agent_type: str) -> dict:
        """
        Returns the highest-priority *enabled* endpoint config for the given
        agent type. Falls back to ``default_llm_cfg`` if no custom endpoints
        are configured or all are disabled.
        """
        chain = self.get_endpoint_chain(agent_type)
        if chain:
            return chain[0]
        return copy.deepcopy(self.default_llm_cfg)

    def get_effective_max_tokens(self, agent_type: str) -> int:
        """
        Returns the effective max_input_tokens for an agent type.
        
        Uses the per-endpoint value if configured, otherwise falls back to
        the general settings value. The general settings is a fallback only,
        not a hard cap — each agent type keeps its own configured limit.
        """
        # Read general_limit AND resolve endpoint chain inside a single lock scope
        # to ensure atomicity — no risk of general_limit changing mid-computation.
        ep_limit = 0
        general_limit = 0
        with self._lock:
            defaults = self.default_llm_cfg or {}
            general_limit = defaults.get('max_input_tokens', 0)
            
            # Normalize agent_type for case-insensitive lookup (Fix Finding 1)
            normalized_agent_type = self._normalize_agent_type(agent_type)
            
            for eid in self.agent_priorities.get(normalized_agent_type, []):
                ep = self.endpoints.get(eid)
                if ep and ep.enabled:
                    ep_limit = ep.max_input_tokens
                    break
        
        # Use endpoint-specific limit; fall back to general settings only when endpoint has none configured
        if ep_limit > 0:
            return ep_limit
        if general_limit > 0:
            return general_limit
        return 0

    def _normalize_agent_type(self, agent_type: str) -> str:
        """
        Normalize agent_type for case-insensitive lookup.
        
        Frontend stores priorities with PascalCase keys (e.g., "Coder", "Security")
        while backend uses lowercase during streaming (e.g., "coder", "security").
        This method performs case-insensitive lookup to ensure live updates work.
        
        Returns the canonical key from agent_priorities if found, otherwise returns
        the canonical form from CANONICAL_AGENT_TYPES or the original agent_type.
        
        CONTRACT: Must be called under self._lock to prevent concurrent modification.
        """
        # Fix Finding 4: Strip whitespace before processing
        if not agent_type:
            return agent_type
        
        agent_type = agent_type.strip()
        if not agent_type:
            return agent_type
        
        agent_type_lower = agent_type.lower()
        
        # Fix Finding 2: Take a snapshot of keys to prevent concurrent modification issues
        existing_keys_snapshot = list(self.agent_priorities.keys())
        
        # Try exact match first (fastest path)
        if agent_type in self.agent_priorities:
            return agent_type
        
        # Case-insensitive fallback - check existing keys first
        for key in existing_keys_snapshot:
            if key.lower() == agent_type_lower:
                return key
        
        # If no match found, return the canonical form (Fix Finding 3)
        # This ensures consistent behavior across restarts regardless of source ordering
        return CANONICAL_AGENT_TYPES.get(agent_type_lower, agent_type)

    def get_endpoint_chain(self, agent_type: str, allocated_tokens: Optional[int] = None) -> List[dict]:
        """
        Returns an ordered list of LLM configs to try for the given agent type:
          1. Agent-specific endpoints (priority order, enabled only) — Tier 1
          2. Last successful endpoint (if available and validated) — Tier 2
          3. General Settings default (always last) — Tier 3
        
        Args:
            agent_type: The type of agent requesting endpoints
            allocated_tokens: Optional - the agent's allocated context size in tokens.
                            When provided, used to filter/weight endpoint selection for sufficient capacity.
        """
        # Read general_limit inside lock scope for thread safety (same pattern as get_effective_max_tokens)
        general_limit = 0
        configs = []

        # 1. Agent-specific endpoints — under lock to prevent RuntimeError
        # from concurrent dict modification during iteration
        with self._lock:
            defaults = self.default_llm_cfg or {}
            general_limit = defaults.get('max_input_tokens', 0)
            
            # Normalize agent_type for case-insensitive lookup (Fix Finding 1)
            normalized_agent_type = self._normalize_agent_type(agent_type)
            
            for eid in self.agent_priorities.get(normalized_agent_type, []):
                ep = self.endpoints.get(eid)
                if ep and ep.enabled:
                    cfg = ep.to_llm_cfg()
                    
                    # Use endpoint-specific max_input_tokens; fall back to general settings only when endpoint has none configured
                    ep_limit = ep.max_input_tokens
                    if ep_limit <= 0 and general_limit > 0:
                        cfg['max_input_tokens'] = general_limit
                    
                    # Dynamic endpoint selection based on token requirements: when allocated_tokens is provided, 
                    # ensure endpoint config reflects the agent's actual context requirements.
                    # This allows dynamically-sized agents (e.g., compression agent) to be routed 
                    # to endpoints that can handle their full token budget rather than being capped by
                    # static endpoint limits. NOTE: If the LLM API has a hard cap below allocated_tokens, 
                    # the call may fail with an error — this is acceptable as it's better than silent truncation.
                    if allocated_tokens is not None:
                        effective_limit = cfg.get('max_input_tokens', 0)
                        # Only adjust if effective_limit > 0 (explicitly configured). A limit of 0 means "unlimited".
                        if effective_limit > 0 and effective_limit < allocated_tokens:
                            cfg['max_input_tokens'] = allocated_tokens
                    
                    configs.append(cfg)

            # 2. If no agent-specific endpoints found, try the last successful endpoint (Tier 2)
            #    This provides automatic recovery when an agent's configured endpoints become unavailable.
            #    Kept inside lock to prevent TOCTOU race between condition check and data access.
            if not configs and self._last_successful_endpoint_cfg is not None:
                last_success_cfg = self._last_successful_endpoint_cfg
                # Validate that the last successful endpoint still exists and is enabled
                api_base = last_success_cfg.get('api_base') or last_success_cfg.get('model_server', '')
                for ep in self.endpoints.values():
                    if ep.api_base == api_base and ep.enabled:
                        # Use the stored config but ensure it has proper token limits
                        cfg = copy.deepcopy(last_success_cfg)
                        ep_limit = ep.max_input_tokens
                        if ep_limit <= 0 and general_limit > 0:
                            cfg['max_input_tokens'] = general_limit
                        
                        # Adjust for allocated tokens requirement (dynamic endpoint selection)
                        if allocated_tokens is not None:
                            effective_limit = cfg.get('max_input_tokens', 0)
                            # Only adjust if effective_limit > 0 (explicitly configured). A limit of 0 means "unlimited".
                            if effective_limit > 0 and effective_limit < allocated_tokens:
                                cfg['max_input_tokens'] = allocated_tokens
                        
                        configs.append(cfg)
                        break

        # 3. Always append the default as last resort (Tier 3)
        configs.append(copy.deepcopy(self.default_llm_cfg))
        
        # Adjust default endpoint for allocated tokens requirement (dynamic endpoint selection)
        # Note: We only adjust if effective_limit > 0 (explicitly configured). A limit of 0 means "unlimited".
        if allocated_tokens is not None and configs:
            default_cfg = configs[-1]
            effective_limit = default_cfg.get('max_input_tokens', 0)
            if effective_limit > 0 and effective_limit < allocated_tokens:
                default_cfg['max_input_tokens'] = allocated_tokens
        
        return configs

    # ── Retry + Fallback Execution ───────────────────────────────────────

    def call_with_fallback(
        self,
        agent_type: str,
        call_fn: Callable,
        *args,
        allocated_tokens: Optional[int] = None,
        **kwargs
    ) -> Any:
        """
        Execute ``call_fn(*args, **kwargs)`` with automatic endpoint fallback.
        Supports both regular functions and generators.
        
        Uses per-server semaphores if concurrency_limit is set for the selected endpoint.
        
        Args:
            agent_type: The type of agent making the call (e.g., 'coder', 'researcher')
            call_fn: The function to execute with the selected endpoint config
            allocated_tokens: Optional - the agent's allocated context size in tokens.
                           When provided, used for endpoint selection to ensure sufficient capacity.
            *args, **kwargs: Additional arguments passed to call_fn
        """
        chain = self.get_endpoint_chain(agent_type, allocated_tokens=allocated_tokens)
        all_errors = []

        for cfg_idx, llm_cfg in enumerate(chain):
            max_retries = 2
            concurrency_limit = 0
            base_retry_delay = 1.0       # Default for exponential backoff
            max_retry_delay = 30.0       # Default maximum delay cap
            rate_limit_rpm = 0           # Default: unlimited
            is_default = (cfg_idx == len(chain) - 1)
            
            # Resolve endpoint-specific settings — always try to read from
            # the endpoint config, even for the default fallback endpoint.
            # The default endpoint may also be in self.endpoints with its own
            # concurrency setting (Phase 1 fix).
            endpoint_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
            with self._lock:
                for ep in self.endpoints.values():
                    if ep.api_base == endpoint_base:
                        max_retries = ep.max_retries
                        concurrency_limit = ep.concurrency_limit
                        base_retry_delay = ep.base_retry_delay
                        max_retry_delay = ep.max_retry_delay
                        rate_limit_rpm = ep.rate_limit_rpm
                        break
            
            # ── CONCURRENCY CONTROL (Per-API-Call Semaphore, Layer 2) ──
            # Layer 1 (EndpointScheduler in register_async_call): serializes agent lifecycles.
            # Layer 2 (this semaphore): limits parallel API calls WITHIN an agent's window.
            # For concurrency=0 endpoints both layers are active but redundant — 
            # EndpointScheduler ensures only one agent at a time, so this just adds
            # an extra gate on each individual LLM call (harmless).
            # For concurrency=N>0 endpoints, this layer prevents an agent from making
            # more than N concurrent API calls to the same server.
            sem = None
            if concurrency_limit >= 0:
                # 0 means sequential delegation AND sequential requests (Semaphore of 1)
                sem_size = max(1, concurrency_limit)
                with self._sem_lock:
                    if endpoint_base not in self._semaphores or self._semaphores[endpoint_base][1] != sem_size:
                        self._semaphores[endpoint_base] = (threading.Semaphore(sem_size), sem_size)
                    sem = self._semaphores[endpoint_base][0]

            def execute_with_sem(current_agent_name=None):
                if not sem:
                    return call_fn(*args, **kwargs)
                
                # Track waiting agents by name
                if current_agent_name:
                    if not hasattr(self, '_waiting_agents'):
                        self._waiting_agents = set()
                    with self._sem_lock:
                        self._waiting_agents.add(current_agent_name)

                sem.acquire()
                try:
                    # Track that we are no longer waiting
                    if current_agent_name and hasattr(self, '_waiting_agents'):
                        with self._sem_lock:
                            self._waiting_agents.discard(current_agent_name)

                    result = call_fn(*args, **kwargs)
                    if hasattr(result, '__iter__') and not isinstance(result, (list, dict, str)):
                        # It's a generator. Pull first chunk to detect API errors early
                        # (connection/auth/model failures only surface on first next()).
                        it = iter(result)
                        try:
                            first_chunk = next(it)
                        except StopIteration:
                            sem.release()
                            return iter([])  # Empty but valid

                        # Wrap the ORIGINAL generator chain directly — not a reconstructed one.
                        # This preserves the streaming pipeline without double-wrapping.
                        def sem_generator_wrapper(first, rest, _sem=sem):
                            yield first
                            try:
                                yield from rest
                            finally:
                                _sem.release()
                        return sem_generator_wrapper(first_chunk, it)
                    else:
                        # Regular result, release now
                        sem.release()
                        return result
                except Exception:
                    sem.release()
                    if current_agent_name and hasattr(self, '_waiting_agents'):
                        with self._sem_lock:
                            self._waiting_agents.discard(current_agent_name)
                    raise

            endpoint_name = llm_cfg.get('model', 'unknown')

            for attempt in range(max_retries + 1):
                try:
                    kwargs['llm_cfg'] = llm_cfg
                    
                    # Try to get the agent instance name from kwargs if available
                    agent_obj = kwargs.get('agent_obj')
                    current_agent_name = (
                        kwargs.get('agent_instance_name') or 
                        getattr(agent_obj, 'instance_name', None) or
                        (agent_type if agent_type else 'orchestrator')
                    )
                    
                    # Rate limiting: check and enforce per-endpoint rate limit before each call attempt.
                    # Each retry attempt counts against the rate limit.
                    if rate_limit_rpm > 0:
                        now = time.time()
                        with self._lock:
                            if endpoint_base not in self._endpoint_call_history:
                                self._endpoint_call_history[endpoint_base] = collections.deque()
                            # Remove entries older than 60 seconds (sliding window) using deque for efficiency
                            history = self._endpoint_call_history[endpoint_base]
                            while history and now - history[0] >= 60:
                                history.popleft()
                            # Check if we're over the limit
                            if len(history) >= rate_limit_rpm:
                                raise RuntimeError(
                                    f"Rate limit exceeded for endpoint '{endpoint_name}' @ {endpoint_base} ({rate_limit_rpm} rpm)"
                                )
                            # Track this call atomically within the same lock to prevent race conditions
                            history.append(now)
                    
                    result = execute_with_sem(current_agent_name)
                    
                    # Track the last successful endpoint config for automatic recovery.
                    # Stored only after complete success (including all retries), not during retries.
                    # This enables Tier 2 fallback when agent-specific endpoints become unavailable.
                    with self._lock:
                        self._last_successful_endpoint_cfg = copy.deepcopy(llm_cfg)
                    
                    # Generator errors are already detected inside execute_with_sem (first-chunk pull).
                    # Pass the generator through directly — no double-wrapping needed.
                    return result
                    
                except RuntimeError as e:
                    # Check if this is a rate limit error - if so, skip retries and move to next endpoint
                    if "Rate limit exceeded" in str(e):
                        logger.warning(f"[APIRouter] Rate limit hit for '{endpoint_name}' @ {endpoint_base}. Skipping to next endpoint.")
                        all_errors.append(f"Rate limit exceeded for '{endpoint_name}' @ {endpoint_base}")
                        break  # Immediately go to next endpoint, don't waste retries
                    raise  # Re-raise other RuntimeErrors to be caught below
                except Exception as e:
                    tb_str = traceback.format_exc()
                    error_msg = f"Endpoint '{endpoint_name}' @ {endpoint_base} attempt {attempt+1}/{max_retries+1}: {e}\nTraceback: {tb_str}"
                    logger.warning(f"[APIRouter] {error_msg}")
                    all_errors.append(error_msg)

                    if attempt < max_retries:
                        # Exponential backoff with jitter: base * 2^attempt + random_jitter
                        # This prevents thundering herd when multiple agents retry simultaneously
                        delay = min(base_retry_delay * (2 ** attempt), max_retry_delay)
                        jitter = random.uniform(0, 0.1 * delay)  # Up to 10% jitter
                        logger.info(f"[APIRouter] Backing off {delay+jitter:.1f}s before retry for endpoint '{endpoint_name}' @ {endpoint_base}")
                        time.sleep(delay + jitter)

            logger.info(f"[APIRouter] Exhausted retries for endpoint '{endpoint_name}'. Moving to next...")

        raise RuntimeError(
            f"All API endpoints exhausted for agent type '{agent_type}'.\n"
            + "\n".join(all_errors)
        )

    # ── Semaphore Reset (for stop+resume) ────────────────────────────────

    def reset_semaphores(self):
        """Reset all per-API-call semaphores to their initial state.
        
        Releases all held permits to restore each semaphore to its initial state.
        This avoids replacing the semaphore object (which could cause lost releases
        from generator finally blocks).
        """
        with self._sem_lock:
            for base, (sem, size) in list(self._semaphores.items()):
                # Release all held permits by adding missing ones back
                current_value = sem._value if hasattr(sem, '_value') else 0
                missing_permits = size - current_value
                for _ in range(missing_permits):
                    sem.release()
                
        logger.debug(f"[APIRouter] Reset {len(self._semaphores)} semaphores for stop+resume safety")

    # ── Persistence ──────────────────────────────────────────────────────

    def _save(self):
        """Persist config to disk."""
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            data = {
                'endpoints': [ep.to_dict() for ep in self.endpoints.values()],
                'agent_priorities': self.agent_priorities,
            }
            with open(self._config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[APIRouter] Failed to save config: {e}")

    def _normalize_agent_priorities(self, priorities: dict) -> dict:
        """
        Normalize agent_priorities dict to remove case-insensitive duplicate keys.
        
        When both 'Coder' and 'coder' exist, keeps the first one encountered
        (typically PascalCase from frontend). This prevents double entries in UI.
        
        Args:
            priorities: Raw agent_priorities dict that may have duplicates
            
        Returns:
            Normalized dict with only one key per agent type (case-insensitive)
        """
        normalized = {}
        seen_lower = {}  # Maps lowercase key -> canonical key to track which we kept
        
        for key, value in priorities.items():
            if not key:
                continue
                
            key_lower = key.lower()
            if key_lower not in seen_lower:
                # First occurrence - keep it
                normalized[key] = value
                seen_lower[key_lower] = key
            # Else: duplicate found, skip this one (keep the first)
        
        if len(normalized) != len(priorities):
            logger.info(
                f"[APIRouter] Normalized agent_priorities: {len(priorities)} keys → "
                f"{len(normalized)} keys (removed {len(priorities) - len(normalized)} case duplicates)"
            )
        
        return normalized

    def _load(self):
        """Load config from disk if available."""
        if not self._config_path.exists():
            return
        try:
            with open(self._config_path, 'r', encoding='utf-8-sig') as f:
                content = f.read().strip()
                if not content:
                    return
                data = json.loads(content)

            if not isinstance(data, dict):
                logger.warning(f"[APIRouter] Config file {self._config_path} is not a dictionary. Skipping.")
                return

            for ep_data in data.get('endpoints', []):
                try:
                    ep = APIEndpoint.from_dict(ep_data)
                    self.endpoints[ep.id] = ep
                except Exception as e:
                    logger.error(f"[APIRouter] Failed to parse endpoint data: {e}")

            # Normalize agent_priorities to remove case-insensitive duplicates
            raw_priorities = data.get('agent_priorities', {})
            self.agent_priorities = self._normalize_agent_priorities(raw_priorities)
            logger.info(f"[APIRouter] Loaded {len(self.endpoints)} endpoints from {self._config_path}")
        except Exception as e:
            logger.error(f"[APIRouter] Failed to load config from {self._config_path}: {e}")

    # ── Serialization (for UI transport) ─────────────────────────────────

    def to_dict(self) -> dict:
        """Full serialization for WebSocket state broadcast."""
        return {
            'endpoints': [ep.to_dict() for ep in self.endpoints.values()],
            'agent_priorities': copy.deepcopy(self.agent_priorities),
        }

    def from_dict(self, data: dict):
        """
        Load full state from a dict (e.g. from UI update).
        
        Normalizes agent_priorities to prevent duplicate keys from case mismatches
        between frontend and backend updates.
        """
        with self._lock:
            # Parse endpoints into a temporary dict first — don't clear existing endpoints yet.
            # This prevents leaving the router in a corrupted (empty) state if parsing fails mid-way.
            new_endpoints = {}
            for ep_data in data.get('endpoints', []):
                try:
                    ep = APIEndpoint.from_dict(ep_data)
                    new_endpoints[ep.id] = ep
                except Exception as e:
                    logger.error(f"[APIRouter.from_dict] Failed to parse endpoint data: {e}")
            
            # Swap atomically only after all parsing succeeds
            self.endpoints.clear()
            self.endpoints.update(new_endpoints)
            
            # Normalize agent_priorities to remove case-insensitive duplicates
            raw_priorities = data.get('agent_priorities', {})
            self.agent_priorities = self._normalize_agent_priorities(raw_priorities)
            
            ep_ids = list(self.endpoints.keys())
            logger.info(f"[APIRouter.from_dict] Updated: {len(self.endpoints)} endpoints "
                       f"({ep_ids}) with "
                       f"{len(self.agent_priorities)} priority mappings: "
                       f"{dict(self.agent_priorities)}")
            self._save()

    def update_default_llm_cfg(self, new_cfg: dict):
        """
        Update the default fallback config (from General Settings changes).
        
        Note: This is a partial update — only keys present in new_cfg are updated.
        Keys removed from the UI will persist in default_llm_cfg until explicitly overwritten.
        """
        with self._lock:
            # Defensive: ensure default_llm_cfg is not None
            if self.default_llm_cfg is None:
                self.default_llm_cfg = {}
            
            # Log which keys are being updated (for debugging config propagation issues)
            # Only keys present in new_cfg are checked; keys removed from the UI persist in default_llm_cfg
            changed_keys = [k for k in new_cfg if k not in self.default_llm_cfg or self.default_llm_cfg[k] != new_cfg[k]]
            if changed_keys:
                logger.info(f"[APIRouter.update_default_llm_cfg] Updating {len(changed_keys)} keys: {changed_keys}")
            self.default_llm_cfg.update(new_cfg)

    def is_waiting(self, agent_name: str) -> bool:
        """Check if a specific agent instance is currently waiting for a semaphore."""
        if not hasattr(self, '_waiting_agents'):
            return False
        with self._sem_lock:
            return agent_name in self._waiting_agents
