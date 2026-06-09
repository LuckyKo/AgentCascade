"""
API Router — Multi-endpoint API selection with priority-based fallback.

Manages a pool of named LLM API endpoints. Each agent *type* (orchestrator,
coder, researcher…) can have its own priority-ordered list of endpoints.
The General Settings API (default_llm_cfg) is always the last-resort fallback.

Persistence: workspace/config/api_endpoints.json
"""

import copy
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
    
    def acquire(self, api_base: str, concurrency_limit: int) -> Optional[Callable[[], None]]:
        """
        Acquire a slot on the endpoint. Blocks if at capacity.
        Returns a cleanup callback to release the slot, or None if unlimited.
        
        If the concurrency limit has changed since this endpoint was last scheduled,
        the semaphore is safely resized — active agents retain their slots, and
        new agents see the updated capacity.
        
        Args:
            api_base: The API base URL of the endpoint
            concurrency_limit: -1=unlimited, 0=sequential, N>0=max parallel
            
        Returns:
            A callable that releases the slot when called, or None if no scheduling needed.
        """
        if concurrency_limit == -1:  # unlimited — no scheduling needed
            logger.debug(f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — api_base={api_base}, concurrency=-1 (unlimited), returning None")
            return None
        
        new_max = concurrency_limit if concurrency_limit > 0 else 1  # 0→1 (sequential), -1 handled above
        logger.debug(
            f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — api_base={api_base}, "
            f"concurrency_limit={concurrency_limit}, new_max={new_max}"
        )
        
        with self._lock:
            if api_base not in self._schedules:
                self._schedules[api_base] = {
                    'sem': threading.Semaphore(new_max),
                    'active_count': 0,
                    'max_active': new_max,
                }
            else:
                sched = self._schedules[api_base]
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
                    
                    logger.info(f"[EndpointScheduler] Resized '{api_base}' from {old_max} → {new_max}")
            
            sched = self._schedules[api_base]
        
        # Semaphore.acquire() blocks atomically if at capacity — no TOCTOU race
        logger.debug(f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — blocking on semaphore for api_base={api_base}")
        sched['sem'].acquire()
        
        with self._lock:
            sched['active_count'] += 1
            current = sched['active_count']
            logger.info(f"[EndpointScheduler] Agent acquired slot on '{api_base}' "
                       f"(active: {current}, limit: {sched['max_active']})")
            logger.debug(
                f"[CALL_AGENT_DEBUG] EndpointScheduler.acquire — successfully acquired, "
                f"api_base={api_base}, active_count={current}, max_active={sched['max_active']}"
            )
        
        # Return cleanup callback that releases the semaphore when called.
        # IMPORTANT: reads the CURRENT semaphore from the live schedule entry,
        # not a captured reference — this is critical because the semaphore may
        # have been resized (swapped) since acquire() was called.
        def release():
            with self._lock:
                current_sched = self._schedules.get(api_base)
                if current_sched:
                    # Guard against negative active_count (shouldn't happen, but safe)
                    current_sched['active_count'] = max(0, current_sched['active_count'] - 1)
                    new_count = current_sched['active_count']
                    logger.info(f"[EndpointScheduler] Agent released slot on '{api_base}' "
                               f"(active: {new_count}, limit: {current_sched['max_active']})")
                    logger.debug(
                        f"[CALL_AGENT_DEBUG] EndpointScheduler.release — api_base={api_base}, "
                        f"active_count={new_count}, max_active={current_sched['max_active']}"
                    )
                    # Release the CURRENT semaphore (may differ from acquire-time sem after resize)
                    current_sched['sem'].release()
        
        return release
    
    def count_active(self, api_base: str) -> int:
        """Count active tasks on an endpoint."""
        with self._lock:
            sched = self._schedules.get(api_base)
            return sched['active_count'] if sched else 0
    
    def get_status(self) -> Dict[str, Dict]:
        """Get status of all scheduled endpoints (for diagnostics)."""
        with self._lock:
            result = {}
            for api_base, sched in self._schedules.items():
                # Semaphore._value is implementation detail but useful for diagnostics
                sem_value = sched['sem']._value if hasattr(sched['sem'], '_value') else 'unknown'
                result[api_base] = {
                    'active_count': sched['active_count'],
                    'max_active': sched['max_active'],
                    'semaphore_slots': sem_value,
                }
            return result
    
    def cleanup_stale(self):
        """Remove schedule entries for endpoints with no activity.
        
        A schedule is stale when active_count is 0 AND all semaphore permits
        are available (no waiting agents). This prevents memory leaks from
        endpoints that were used temporarily and have since gone idle.
        """
        with self._lock:
            stale = [ab for ab, s in self._schedules.items()
                     if s['active_count'] == 0 and s['sem']._value >= s['max_active']]
            for ab in stale:
                del self._schedules[ab]
            if stale:
                logger.info(f"[EndpointScheduler] Cleaned up {len(stale)} stale schedule(s)")


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

        # Persistence path
        if config_dir:
            self._config_dir = Path(config_dir)
        else:
            from agent_cascade.settings import DEFAULT_WORKSPACE
            self._config_dir = Path(DEFAULT_WORKSPACE) / 'config'
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
            del self.endpoints[endpoint_id]
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
        """Set the priority-ordered endpoint list for an agent type."""
        with self._lock:
            # Validate that all IDs exist
            valid_ids = [eid for eid in endpoint_ids if eid in self.endpoints]
            if valid_ids:
                self.agent_priorities[agent_type] = valid_ids
            elif agent_type in self.agent_priorities:
                del self.agent_priorities[agent_type]
            self._save()

    def get_agent_priorities(self, agent_type: str) -> List[str]:
        """Get the endpoint ID list for a specific agent type."""
        with self._lock:
            return list(self.agent_priorities.get(agent_type, []))

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
            # First check agent-specific priorities
            for eid in self.agent_priorities.get(agent_type, []):
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
        
        If general settings has a non-zero max_input_tokens, returns MIN of
        (general settings limit) and (highest priority endpoint limit).
        Otherwise (general_limit = 0), returns the endpoint limit directly
        with no capping — allowing each agent type its own configured limit.
        """
        # Read general_limit AND resolve endpoint chain inside a single lock scope
        # to ensure atomicity — no risk of general_limit changing mid-computation.
        ep_limit = 0
        general_limit = 0
        with self._lock:
            defaults = self.default_llm_cfg or {}
            general_limit = defaults.get('max_input_tokens', 0)
            
            for eid in self.agent_priorities.get(agent_type, []):
                ep = self.endpoints.get(eid)
                if ep and ep.enabled:
                    ep_limit = ep.max_input_tokens
                    break
        
        # Resolve MIN logic
        if general_limit <= 0:
            return ep_limit
        if ep_limit <= 0:
            return general_limit
        return min(general_limit, ep_limit)

    def get_endpoint_chain(self, agent_type: str) -> List[dict]:
        """
        Returns an ordered list of LLM configs to try for the given agent type:
          1. Agent-specific endpoints (priority order, enabled only)
          2. General Settings default (always last)
        """
        # Read general_limit inside lock scope for thread safety (same pattern as get_effective_max_tokens)
        general_limit = 0
        configs = []

        # 1. Agent-specific endpoints — under lock to prevent RuntimeError
        # from concurrent dict modification during iteration
        with self._lock:
            defaults = self.default_llm_cfg or {}
            general_limit = defaults.get('max_input_tokens', 0)
            
            for eid in self.agent_priorities.get(agent_type, []):
                ep = self.endpoints.get(eid)
                if ep and ep.enabled:
                    cfg = ep.to_llm_cfg()
                    
                    # Apply MIN logic: min(endpoint_limit, general_limit)
                    ep_limit = ep.max_input_tokens
                    if general_limit > 0:
                        if ep_limit <= 0 or ep_limit > general_limit:
                            cfg['max_input_tokens'] = general_limit
                    
                    configs.append(cfg)

        # 2. If no agent-specific endpoints found, try orchestrator's endpoints as fallback (Bug 40)
        #    This ensures agents like Security inherit the caller's API configuration
        if not configs and 'orchestrator' in self.agent_priorities:
            with self._lock:
                for eid in self.agent_priorities.get('orchestrator', []):
                    ep = self.endpoints.get(eid)
                    if ep and ep.enabled:
                        cfg = ep.to_llm_cfg()
                        # Apply MIN logic: min(endpoint_limit, general_limit)
                        ep_limit = ep.max_input_tokens
                        if general_limit > 0:
                            if ep_limit <= 0 or ep_limit > general_limit:
                                cfg['max_input_tokens'] = general_limit
                        configs.append(cfg)

        # 3. Always append the default as last resort
        configs.append(copy.deepcopy(self.default_llm_cfg))
        return configs

    # ── Retry + Fallback Execution ───────────────────────────────────────

    def call_with_fallback(
        self,
        agent_type: str,
        call_fn: Callable,
        *args,
        **kwargs
    ) -> Any:
        """
        Execute ``call_fn(*args, **kwargs)`` with automatic endpoint fallback.
        Supports both regular functions and generators.
        
        Uses per-server semaphores if concurrency_limit is set for the selected endpoint.
        """
        chain = self.get_endpoint_chain(agent_type)
        all_errors = []

        for cfg_idx, llm_cfg in enumerate(chain):
            max_retries = 2
            concurrency_limit = 0
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
                        break
            
            # ── CONCURRENCY CONTROL (Per-API-Call Semaphore, Layer 2) ──
            # Layer 1 (EndpointScheduler in submit_task): serializes agent lifecycles.
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
                    
                    result = execute_with_sem(current_agent_name)
                    
                    # Generator errors are already detected inside execute_with_sem (first-chunk pull).
                    # Pass the generator through directly — no double-wrapping needed.
                    return result
                    
                except Exception as e:
                    import traceback
                    tb_str = traceback.format_exc()
                    error_msg = f"Endpoint '{endpoint_name}' @ {endpoint_base} attempt {attempt+1}/{max_retries+1}: {e}\nTraceback: {tb_str}"
                    logger.warning(f"[APIRouter] {error_msg}")
                    all_errors.append(error_msg)

                    if attempt < max_retries:
                        time.sleep(min(2 ** attempt, 5))

            logger.info(f"[APIRouter] Exhausted retries for endpoint '{endpoint_name}'. Moving to next...")

        raise RuntimeError(
            f"All API endpoints exhausted for agent type '{agent_type}'.\n"
            + "\n".join(all_errors)
        )

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

            self.agent_priorities = data.get('agent_priorities', {})
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
        """Load full state from a dict (e.g. from UI update)."""
        with self._lock:
            self.endpoints.clear()
            for ep_data in data.get('endpoints', []):
                ep = APIEndpoint.from_dict(ep_data)
                self.endpoints[ep.id] = ep
            self.agent_priorities = data.get('agent_priorities', {})
            self._save()

    def update_default_llm_cfg(self, new_cfg: dict):
        """Update the default fallback config (from General Settings changes)."""
        with self._lock:
            self.default_llm_cfg.update(new_cfg)

    def is_waiting(self, agent_name: str) -> bool:
        """Check if a specific agent instance is currently waiting for a semaphore."""
        if not hasattr(self, '_waiting_agents'):
            return False
        with self._sem_lock:
            return agent_name in self._waiting_agents
