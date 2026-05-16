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
    concurrency_limit: int = 0         # 0 = unlimited, 1+ = max parallel requests for this server

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
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'APIEndpoint':
        # Filter to only known fields to prevent TypeError on unexpected keys
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


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
        return self.agent_priorities.get(agent_type, [])

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

    def get_endpoint_chain(self, agent_type: str) -> List[dict]:
        """
        Returns an ordered list of LLM configs to try for the given agent type:
          1. Agent-specific endpoints (priority order, enabled only)
          2. General Settings default (always last)
        """
        configs = []

        # 1. Agent-specific endpoints
        for eid in self.agent_priorities.get(agent_type, []):
            ep = self.endpoints.get(eid)
            if ep and ep.enabled:
                configs.append(ep.to_llm_cfg())

        # 2. Always append the default as last resort
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
            
            # Resolve endpoint-specific settings
            endpoint_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
            if not is_default:
                for ep in self.endpoints.values():
                    if ep.api_base == endpoint_base:
                        max_retries = ep.max_retries
                        concurrency_limit = ep.concurrency_limit
                        break
            
            # ── CONCURRENCY CONTROL (Semaphore) ──
            # We manage semaphores per api_base because different models 
            # on the same server share the same hardware (e.g. LM Studio).
            sem = None
            if concurrency_limit > 0:
                with self._sem_lock:
                    if endpoint_base not in self._semaphores or self._semaphores[endpoint_base][1] != concurrency_limit:
                        self._semaphores[endpoint_base] = (threading.Semaphore(concurrency_limit), concurrency_limit)
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
                        # It's a generator. Wrap it to release the semaphore only when exhausted.
                        def sem_generator_wrapper(gen):
                            try:
                                yield from gen
                            finally:
                                sem.release()
                        return sem_generator_wrapper(result)
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
                    
                    # Handle generator fallback: we must attempt to get the first item
                    # to catch connection/auth/model errors.
                    if hasattr(result, '__iter__') and not isinstance(result, (list, dict, str)):
                        try:
                            # Wrap the generator to preserve the first item
                            def generator_wrapper(gen):
                                try:
                                    first = next(gen)
                                    yield first
                                    yield from gen
                                except Exception:
                                    raise
                            
                            # We can only "test" the generator once. 
                            # If it fails on the VERY FIRST next(), we catch and fallback.
                            # Note: This is a bit of a hack but common for API stream wrappers.
                            it = iter(result)
                            first_chunk = next(it)
                            
                            # Re-construct the generator for the caller
                            def reconstruct(first, rest):
                                yield first
                                yield from rest
                            return reconstruct(first_chunk, it)
                            
                        except StopIteration:
                            return iter([]) # Empty but valid
                    
                    return result
                    
                except Exception as e:
                    error_msg = f"Endpoint '{endpoint_name}' @ {endpoint_base} attempt {attempt+1}/{max_retries+1}: {e}"
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
            with open(self._config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for ep_data in data.get('endpoints', []):
                ep = APIEndpoint.from_dict(ep_data)
                self.endpoints[ep.id] = ep

            self.agent_priorities = data.get('agent_priorities', {})
            logger.info(f"[APIRouter] Loaded {len(self.endpoints)} endpoints from {self._config_path}")
        except Exception as e:
            logger.error(f"[APIRouter] Failed to load config: {e}")

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
        self.default_llm_cfg.update(new_cfg)

    def is_waiting(self, agent_name: str) -> bool:
        """Check if a specific agent instance is currently waiting for a semaphore."""
        if not hasattr(self, '_waiting_agents'):
            return False
        with self._sem_lock:
            return agent_name in self._waiting_agents
