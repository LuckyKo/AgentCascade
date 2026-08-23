"""APIRouter — multi-endpoint selection with priority-based fallback.

Moved verbatim from api_router.py (Phase 3a pure-move refactor).

HTTP EXIT-POINT RULE: any code that opens an HTTP connection to a configured
endpoint MUST either go through :meth:`call_with_fallback` or explicitly consult
the per-normalized-base circuit breaker (:meth:`_breaker_should_skip`) before
firing. Bypass paths (context detection, image captioning) are gated in-place —
do not add new ungated ones.
"""

import collections
import copy
import json
import logging
import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from agent_cascade.settings import (
    ENDPOINT_COOLDOWN_SECONDS,
    ENDPOINT_FAILURE_CLEANUP_HOURS,
    BREAKER_BASE_WINDOW_SECONDS,
    BREAKER_MAX_WINDOW_SECONDS,
    BREAKER_WINDOW_GROWTH,
    SERVER_BUSY_WAIT_CAP_SECONDS,
)
from agent_cascade.exceptions import ContextWindowExceeded, AgentTerminatedError, ServerBusyError
from agent_cascade.retry_policy import calculate_backoff, RetryPolicy, POLICY_DEFAULT
from agent_cascade.api_router_pkg.endpoints import (
    APIEndpoint,
    MAX_CAPTION_LENGTH,
    RATE_LIMIT_WINDOW_SECONDS,
    CANONICAL_AGENT_TYPES,
)
from agent_cascade.api_router_pkg.scheduler import EndpointScheduler
from agent_cascade.api_router_pkg.helpers import _check_termination, _interruptible_sleep
from agent_cascade.api_router_pkg.normalization import (
    normalize_api_base,
)

logger = logging.getLogger(__name__)

class APIRouter:
    """
    Manages multi-endpoint API selection with priority-based fallback.

    The General Settings API (``default_llm_cfg``) is always available as the
    last-resort endpoint for every agent type.
    """

    def __init__(
        self,
        default_llm_cfg: dict,
        config_dir: Optional[str] = None,
        policy: Optional[RetryPolicy] = None,
    ):
        """
        Args:
            default_llm_cfg: The main LLM config from General Settings. This
                            is always the last-resort fallback and is never deleted.
            config_dir:     Directory to persist api_endpoints.json.
                            Defaults to workspace/config.
            policy:         RetryPolicy governing retry behavior. Controls both
                            per-endpoint max retries (endpoint_max_retries) and
                            backoff parameters (base_delay, max_delay, jitter_factor).
                            Falls back to POLICY_DEFAULT if not provided.
        """
        self.default_llm_cfg = default_llm_cfg
        self.policy = policy or POLICY_DEFAULT
        self.endpoints: Dict[str, APIEndpoint] = {}           # id → endpoint
        self.agent_priorities: Dict[str, List[str]] = {}      # agent_type → [endpoint_ids]
        self._agent_types_with_priorities: set = set()        # Agent types with active endpoint priorities (gates Tier 3 last-successful fallback)
        self._lock = threading.Lock()
        self._pool = None  # Set externally by AgentPool when router is attached

        # Register with the bypass-path gate so HTTP paths that skip
        # call_with_fallback (context detection, image_gen) can consult our
        # breakers (Change E). Weakref-based; no dispose hook needed.
        from agent_cascade.api_router_pkg import breaker_gate
        breaker_gate.register_router(self)

        # Lifecycle-aware endpoint scheduler for parallel agent management.
        # Acquires a slot at task submission time and holds it for the entire
        # agent lifecycle — prevents interleaving of LLM calls between agents.
        self.scheduler = EndpointScheduler()
        # Phase 3: Give scheduler a reference back to this router for endpoint resolution.
        self.scheduler._router_ref = self

        # Track the last successfully used endpoint config for automatic recovery.
        # When an agent's configured endpoints become unavailable, this provides
        # a validated fallback that previously succeeded (Tier 2 in fallback chain).
        self._last_successful_endpoint_cfg: Optional[Dict[str, Any]] = None

        # Rate limiting: track call timestamps per endpoint for rate limit enforcement.
        # api_base -> deque of timestamps (seconds since epoch) for efficient sliding window.
        self._endpoint_call_history: Dict[str, Deque[float]] = {}

        # Cooldown tracking: (normalized_base, model) → last failure timestamp (epoch seconds).
        # Failed endpoints are skipped during cooldown to prevent hammering dead servers.
        # Keyed per-(base, model) so shared-base endpoints on one physical server are independent.
        self._endpoint_failure_times: Dict[Tuple[str, str], float] = {}

        # Per-physical-server circuit breaker state (normalized_base → breaker dict).
        # Tripped by SERVER_BUSY_LOADING signatures (503 / "Failed to load model ... failed
        # to start") — NOT by real per-model errors (404/400), which only set cooldown above.
        self._server_breakers: Dict[str, dict] = {}

        # Per-instance endpoint cursor for "kick to next endpoint" behavior (thread-safe via self._lock).
        self._instance_endpoint_position: Dict[str, int] = {}

        # Persistence path — env var takes precedence for test isolation
        if os.environ.get("AGENT_CASCADE_TEST_CONFIG_DIR"):
            self._config_dir = Path(os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"])
        elif config_dir:
            self._config_dir = Path(config_dir)
        else:
            # API config lives in the project root config/ dir, not workspace.
            # This file lives in api_router_pkg/ — one level deeper than the original
            # monolith (agent_cascade/api_router.py), so it needs an extra .parent to
            # reach project root (verified: original .parent.parent from the monolith
            # resolved to N:\work\WD\AgentCascade, where config/ with secrets.json lives).
            project_root = Path(__file__).resolve().parent.parent.parent
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
                # Remove empty lists and clean up tracking set
                if not self.agent_priorities[agent_type]:
                    del self.agent_priorities[agent_type]
                    self._agent_types_with_priorities.discard(agent_type)
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
                self._agent_types_with_priorities.discard(agent_type)
            
            # Validate that all IDs exist
            valid_ids = [eid for eid in endpoint_ids if eid in self.endpoints]
            filtered_count = len(endpoint_ids) - len(valid_ids)
            
            if valid_ids:
                self.agent_priorities[canonical] = valid_ids
                self._agent_types_with_priorities.add(canonical)  # Track that this agent type was configured
                logger.info(f"[APIRouter.set_agent_priorities] {canonical} → {valid_ids} "
                           f"({'filtered ' + str(filtered_count) + ' invalid IDs, ' if filtered_count else ''}"
                           f"canonical key: {canonical})")
            elif canonical in self.agent_priorities:
                del self.agent_priorities[canonical]
                self._agent_types_with_priorities.discard(canonical)
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

    def _resolve_own_endpoints(
        self,
        agent_type: str,
    ) -> Tuple[List[Tuple[str, APIEndpoint]], bool]:
        """
        Resolve the agent's OWN enabled endpoints (Tier 1).

        Returns:
            (list of (endpoint_id, endpoint_obj) tuples, had_own_endpoints flag)
            The list contains only enabled endpoints from the agent's own priority
            config. Empty if the agent has none configured/enabled.
            had_own_endpoints indicates if agent_type had its own enabled endpoints.

        NOTE: Tier-2 caller inheritance was removed — every agent meters against its
        own resolved endpoint pool (single FIFO slot system). Agents without their own
        endpoints fall through to Tier 3 (last-successful) / Tier 4 (global default) in
        get_endpoint_chain, exactly like any other unconfigured agent.
        """
        # Normalize agent_type for case-insensitive lookup (Fix Finding 1)
        normalized_agent_type = self._normalize_agent_type(agent_type)

        # Agent-specific priorities — check for enabled endpoints
        result = []
        has_enabled_endpoints = False
        for eid in self.agent_priorities.get(normalized_agent_type, []):
            ep = self.endpoints.get(eid)
            if ep and ep.enabled:
                has_enabled_endpoints = True
                result.append((eid, ep))

        return result, has_enabled_endpoints

    def get_effective_concurrency(self, agent_type: str) -> int:
        """
        Returns the concurrency limit of the actual endpoint that will be used
        for the given agent type. Falls back to default config if no endpoints match.

        Returns -1 only if truly unlimited (no endpoint config found at all).
        Returns 0 as a conservative default when the default config specifies an
        api_base but no matching endpoint exists in self.endpoints — this prevents
        unexpected parallel launches on unknown endpoints.
        
        Args:
            agent_type: The agent type to resolve concurrency for
        """
        defaults = self.default_llm_cfg or {}
        with self._lock:
            own_endpoints, _had_own = self._resolve_own_endpoints(agent_type)

            if own_endpoints:
                eid, ep = own_endpoints[0]
                logger.debug(
                    f"[ENDPOINT_CONCURRENCY] get_effective_concurrency — "
                    f"agent_type={agent_type}, endpoint_id={eid}, "
                    f"concurrency_limit={ep.concurrency_limit}"
                )
                return ep.concurrency_limit

            # Tier 3+: Fall back to default endpoint by api_base
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
    
    def get_agent_slot_info(self, agent_class: str) -> dict:
        """Get the slot type that an agent_class would use.

        Args:
            agent_class: The class name of the agent

        Returns:
            Dict with keys: slot_key, is_sequential, concurrency_limit, api_base, needs_slot.
            When concurrency_limit is -1 (unlimited), slot_key and api_base will be None.
        """
        concurrency = self.get_effective_concurrency(agent_class)
        if concurrency == -1:
            return {
                'slot_key': None,
                'is_sequential': False,
                'concurrency_limit': -1,
                'api_base': None,
                'needs_slot': False,
            }

        llm_cfg = self.get_llm_config(agent_class)
        api_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')

        slot_info = self.scheduler.get_slot_info(api_base, concurrency)
        slot_info['api_base'] = api_base
        slot_info['needs_slot'] = True

        return slot_info

    # ── LLM Config Resolution ────────────────────────────────────────────

    def get_llm_config(self, agent_type: str) -> dict:
        """
        Returns the highest-priority *enabled* endpoint config for the given
        agent type. Falls back to ``default_llm_cfg`` if no custom endpoints
        are configured or all are disabled.

        Args:
            agent_type: The agent type to resolve config for
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

    def get_endpoint_chain(
        self,
        agent_type: str,
        allocated_tokens: Optional[int] = None,
        instance_name: Optional[str] = None,
    ) -> List[dict]:
        """
        Returns an ordered list of LLM configs to try for the given agent type:
          1. Agent-specific endpoints (priority order, enabled only) — Tier 1
          2. Last successful endpoint (if available and validated) — Tier 3
          3. General Settings default (always last) — Tier 4

        Priority order is preserved as configured by the user. Images are captioned
        upstream so any endpoint in the chain can handle them without reordering,
        ensuring the admin's preferred endpoint always gets tried first.

        When ``instance_name`` is provided, the returned chain is rotated to start
        from that instance's tracked cursor position (see :meth:`advance_instance_endpoint`).
        This enables "kick to next endpoint" — after inner-loop detection, retries
        skip past endpoints that already failed instead of starting from index 0.

        NOTE: There is no caller-inheritance tier. Every agent resolves through its own
        Tier 1 → Tier 3 → Tier 4 chain (single FIFO slot system). An unconfigured agent
        falls to the last-successful / global default, exactly like any other agent.

        Args:
            agent_type: The type of agent requesting endpoints
            allocated_tokens: Optional - the agent's allocated context size in tokens.
                            Retained for API compatibility; it does NOT alter endpoint
                            selection or inflate any cfg's max_input_tokens (see Tier-1 note below).
            instance_name:  Optional - when provided, rotates chain starting from
                          this instance's tracked cursor position (per-instance memory).
        """
        # Read general_limit inside lock scope for thread safety (same pattern as get_effective_max_tokens)
        general_limit = 0
        endpoint_configs = []

        # Agent-specific endpoints — under lock to prevent RuntimeError from concurrent dict modification
        with self._lock:
            defaults = self.default_llm_cfg or {}
            general_limit = defaults.get('max_input_tokens', 0)
            
            own_endpoints, _had_own = self._resolve_own_endpoints(agent_type)

            # Normalize for downstream checks
            normalized_agent_type = self._normalize_agent_type(agent_type)

            # Build configs from the agent's own (Tier 1) endpoints
            for eid, ep in own_endpoints:
                cfg = ep.to_llm_cfg()

                # Apply token limits and dynamic adjustment
                ep_limit = ep.max_input_tokens
                if ep_limit <= 0 and general_limit > 0:
                    cfg['max_input_tokens'] = general_limit

                # NOTE: max_input_tokens is intentionally left as the endpoint's TRUE
                # configured limit. It is consumed as ground truth by the A1/A2 gate
                # (context-exceeded classification) and the client-side pre-check in
                # llm/base.py. The chain-head allocation must NOT inflate it — doing so
                # blinded both layers when an agent failed over to a smaller endpoint
                # (see reports/fallback-compression-misclass-investigation.md).
                endpoint_configs.append(cfg)

            # Tier 3: Last successful endpoint fallback — only for agents that ever had priorities configured
            if not endpoint_configs and self._last_successful_endpoint_cfg is not None:
                if normalized_agent_type not in self._agent_types_with_priorities:
                    logger.debug(f"[APIRouter] Skipping Tier 3 fallback for '{normalized_agent_type}' (no priorities configured)")
                else:
                    last_success_cfg = self._last_successful_endpoint_cfg
                    # Validate last successful endpoint still exists and is enabled
                    # (compare NORMALIZED bases — raw strings may differ cosmetically).
                    api_base = last_success_cfg.get('api_base') or last_success_cfg.get('model_server', '')
                    for ep in self.endpoints.values():
                        if normalize_api_base(ep.api_base) == normalize_api_base(api_base) and ep.enabled:
                            cfg = copy.deepcopy(last_success_cfg)
                            ep_limit = ep.max_input_tokens
                            if ep_limit <= 0 and general_limit > 0:
                                cfg['max_input_tokens'] = general_limit

                            # max_input_tokens kept as the endpoint's TRUE limit (see Tier-1 note).
                            endpoint_configs.append(cfg)
                            logger.debug(f"[APIRouter] Using Tier 3 fallback for '{normalized_agent_type}': {api_base}")
                            break

            # Filter out endpoints currently in cooldown period.
            # Default endpoint is appended after filtering, so it's always available as last resort.
            # NOTE: Already under self._lock from outer scope — read _endpoint_failure_times directly.
            if ENDPOINT_COOLDOWN_SECONDS > 0 and endpoint_configs:
                now = time.time()
                filtered_configs = []
                skipped_count = 0
                for cfg in endpoint_configs:
                    cfg_base = cfg.get('api_base') or cfg.get('model_server', '')
                    cooldown_key = (normalize_api_base(cfg_base), cfg.get('model', ''))
                    last_fail = self._endpoint_failure_times.get(cooldown_key, 0)
                    if cfg_base and (now - last_fail) < ENDPOINT_COOLDOWN_SECONDS:
                        skipped_count += 1
                        logger.debug(
                            f"[APIRouter] Skipping endpoint '{cfg.get('model', 'unknown')}' @ {cfg_base} "
                            f"in cooldown ({ENDPOINT_COOLDOWN_SECONDS - int(now - last_fail)}s remaining)"
                        )
                    else:
                        filtered_configs.append(cfg)
                if skipped_count > 0:
                    endpoint_configs = filtered_configs if filtered_configs else []
                    logger.info(
                        f"[APIRouter] Endpoint cooldown: skipped {skipped_count} endpoint(s), "
                        f"{len(endpoint_configs)} available for '{normalized_agent_type}'"
                    )

            # Tier 4: Always append the default as last resort — inside lock so cursor rotation reads atomically
            if self.default_llm_cfg is not None:
                default_cfg = copy.deepcopy(self.default_llm_cfg)
                # Guarantee the cfg carries max_input_tokens (mirrors Tier 1 at ~line 457):
                # a keyless/0 limit would otherwise reach llm/base.py's pre-check and silently
                # cap at DEFAULT_MAX_INPUT_TOKENS, and make the router's context-exceeded gate
                # treat every server-side 400 as "unknown limit" (never compress). Injecting the
                # general limit or 0 keeps behavior explicit.
                if not isinstance(default_cfg.get('max_input_tokens'), int) or default_cfg['max_input_tokens'] <= 0:
                    default_cfg['max_input_tokens'] = general_limit if general_limit > 0 else 0
                endpoint_configs.append(default_cfg)
            
            # NOTE: every cfg in the returned chain carries an int max_input_tokens
            # (injected above for Tier-4; set by to_llm_cfg() for tiers 1/3). The default's
            # limit is intentionally NOT inflated with allocated_tokens (see Tier-1 note above).

            # Per-instance cursor rotation: rotate chain from tracked position to skip previously-failed endpoints.
            # Done inside lock to atomically read cursor and return a consistent rotated view.
            if instance_name and len(endpoint_configs) > 1:
                tier_count = len(endpoint_configs) - 1  # Everything except the default (Tier 3)
                cursor = self._instance_endpoint_position.get(instance_name, 0)
                default_cfg = endpoint_configs[-1]
                tier_configs = endpoint_configs[:tier_count]

                if tier_count == 1:
                    if cursor > 0:
                        endpoint_configs = [default_cfg] + tier_configs
                else:
                    effective_cursor = cursor % tier_count
                    if effective_cursor > 0:
                        # Simple positional rotation. Robustness against mid-flight endpoint reorders
                        # relies on from_dict() clearing ALL instance cursors on every config change
                        # (the FIX-2a reset is the real protection). A full identity-keyed cursor —
                        # recording WHICH endpoint was current at advance_instance_endpoint() time so
                        # rotation survives a mid-turn reorder that does NOT go through from_dict — is a
                        # documented follow-up. Do not reintroduce a "defensive" head-identity check here:
                        # rotated_tiers is a permutation of tier_configs, so any such check is a no-op.
                        rotated_tiers = tier_configs[effective_cursor:] + tier_configs[:effective_cursor]
                        endpoint_configs = rotated_tiers + [default_cfg]
                        logger.debug(f"[APIRouter] Endpoint cursor for '{instance_name}' rotated by {effective_cursor}")
                         
        # Validate: no endpoint configured at all
        if not endpoint_configs:
            raise ValueError(
                f"No LLM endpoint configured for agent type '{agent_type}'. "
                f"Set endpoints in General Settings or assign endpoints to this agent type."
            )

        # Validate: only config available is incomplete (missing both api_base and model)
        if len(endpoint_configs) == 1:
            only_cfg = endpoint_configs[0]
            has_api_base = bool(only_cfg.get('api_base') or only_cfg.get('model_server'))
            has_model = bool(only_cfg.get('model'))
            if not has_api_base and not has_model:
                raise ValueError(
                    f"No usable LLM endpoint configured for agent type '{agent_type}'. "
                    f"Configure api_base and model in General Settings or assign endpoints to this agent type."
                )

        # Validate: at least one config in the chain has required fields (multi-config case)
        if endpoint_configs:
            has_valid = any(
                bool(cfg.get('api_base') or cfg.get('model_server'))
                for cfg in endpoint_configs
            )
            if not has_valid:
                raise ValueError(
                    f"No usable LLM endpoint configured for agent type '{agent_type}'. "
                    f"All endpoints in the fallback chain are missing api_base. "
                    f"Configure endpoints in General Settings or assign endpoints to this agent type."
                )

        return endpoint_configs

    def get_assigned_max_tokens(self, agent_type: str, instance_name: Optional[str] = None) -> Optional[int]:
        """Return the TRUE max_input_tokens of the endpoint actually about to be called
        (post-cursor-rotation chain head), or None if it can't be resolved. Read-only —
        does not advance any cursor."""
        try:
            chain = self.get_endpoint_chain(agent_type, instance_name=instance_name)
            if chain:
                limit = chain[0].get('max_input_tokens')
                if isinstance(limit, int) and limit > 0:
                    return limit
        except Exception:
            pass
        return None

    # ── Per-Instance Endpoint Cursor Management ───────────────────────────
    # Manages the "kick to next endpoint" mechanism. When inner-loop detection
    # happens, execution_engine calls advance_instance_endpoint() to move past
    # the failing endpoint before retrying. On success or dismissal, the cursor
    # is reset so the instance starts fresh from index 0 again.

    def advance_instance_endpoint(self, instance_name: str) -> int:
        """Advance endpoint cursor by one for the given instance (called on inner-loop detection)."""
        if not instance_name:
            return 0
        with self._lock:
            pos = self._instance_endpoint_position.get(instance_name, 0) + 1
            self._instance_endpoint_position[instance_name] = pos
            logger.debug(
                f"[APIRouter] Endpoint cursor advanced for '{instance_name}': "
                f"position {pos - 1} → {pos}. Next call will skip past this endpoint."
            )
        return pos

    def reset_instance_endpoint(self, instance_name: str) -> None:
        """Reset endpoint cursor to zero for the given instance (called on success/dismissal)."""
        if not instance_name:
            return
        with self._lock:
            old_pos = self._instance_endpoint_position.pop(instance_name, None)
            if old_pos is not None and old_pos > 0:
                logger.debug(
                    f"[APIRouter] Endpoint cursor reset for '{instance_name}': "
                    f"position {old_pos} → 0 (cleaned up)."
                )

    @staticmethod
    def _is_context_exceeded_error(error: Exception) -> bool:
        """Check if an error indicates the input exceeded the model's context window.

        llama.cpp returns HTTP 400 with "exceed_context_size_error" in body.
        Other servers may use different patterns — catch them too.
        """
        # Already a typed ContextWindowExceeded exception
        if isinstance(error, ContextWindowExceeded):
            return True

        err_str = str(error).lower()
        from agent_cascade.llm.base import ModelServiceError
        code = getattr(error, 'code', None)

        # llama.cpp and similar servers: HTTP 400 with context-size patterns
        if code == '400' and any(
            pattern in err_str
            for pattern in ('exceed_context_size', 'context length', 'maximum input context', 'context window')
        ):
            return True

        # Generic patterns from various servers.
        # Only trusted on HTTP 400: free-text phrases in a 5xx body (e.g. "max_tokens exceeded"
        # inside an upstream error payload) are NOT evidence of caller overflow — treating them as
        # context-exceeded would trigger fallback compression off a service failure.
        if code == '400' and any(
            pattern in err_str
            for pattern in ('prompt is too long', 'input tokens exceed', 'max_tokens exceeded', 'exceeds the context limit')
        ):
            return True

        return False

    @staticmethod
    def _estimate_payload_tokens(messages) -> Optional[int]:
        """Estimate total input tokens of a message list using the same estimator as the
        client-side pre-check in llm/base.py (get_message_stats per message).

        Returns None when estimation fails — callers must treat that as "unknown" and must
        NOT make a context-exceeded decision off an unknown estimate.
        """
        if not messages:
            return 0
        try:
            from agent_cascade.utils.utils import get_message_stats
            total = 0
            for m in messages:
                # Skip values that can leak via JSON parsing/logger recovery (mirrors base.py chat())
                if m is None or isinstance(m, (list, bool)):
                    continue
                total += get_message_stats(m)['tokens']
            return total
        except Exception as est_err:
            logger.warning(f"[APIRouter] Payload token estimation failed: {est_err}")
            return None

    def _cleanup_stale_failure_records(self, now: float) -> None:
        """Remove failure records older than ENDPOINT_FAILURE_CLEANUP_HOURS to prevent unbounded growth."""
        if not self._endpoint_failure_times:
            return
        cutoff = now - (ENDPOINT_FAILURE_CLEANUP_HOURS * 3600)
        stale_keys = [key for key, ts in self._endpoint_failure_times.items() if ts < cutoff]
        if stale_keys:
            for key in stale_keys:
                del self._endpoint_failure_times[key]
            logger.debug(
                f"[APIRouter] Cleaned up {len(stale_keys)} stale endpoint failure record(s) "
                f"(older than {ENDPOINT_FAILURE_CLEANUP_HOURS}h)"
            )

    # ── Per-Server Circuit Breaker (Change B/D) ──────────────────────────

    def _is_server_busy_loading(self, error: Exception) -> bool:
        """True when an error carries the SERVER_BUSY_LOADING signature.

        Matches ModelServiceError with HTTP code 503, or error text like
        'Failed to load model ... failed to start' (llama.cpp loader busy).
        Real per-model errors (404 model-not-found, 400 invalid-request) do NOT
        match and therefore never trip the server breaker — they only earn the
        per-(base, model) cooldown.
        """
        from agent_cascade.llm.base import ModelServiceError
        if isinstance(error, ModelServiceError) and str(getattr(error, 'code', None)) == '503':
            return True
        err_str = str(error).lower()
        return (
            'failed to load model' in err_str
            or 'failed to start' in err_str
            or 'model load error' in err_str
            or 'loading of model' in err_str
        )

    def _breaker_trip(self, base_key: str, reason: str) -> None:
        """Trip or re-trip (with grown window) the breaker for a normalized base.

        MUST be called under ``self._lock``.
        """
        now = time.monotonic()
        br = self._server_breakers.get(base_key)
        if br and br['state'] == 'open':
            return  # already open — keep original opened_at/window
        prev_window = br['window'] if br else BREAKER_BASE_WINDOW_SECONDS
        window = min(prev_window * BREAKER_WINDOW_GROWTH, BREAKER_MAX_WINDOW_SECONDS) \
            if br else BREAKER_BASE_WINDOW_SECONDS
        self._server_breakers[base_key] = {
            'state': 'open',
            'opened_at': now,
            'window': window,
            'probing': False,
        }
        logger.warning(
            f"[APIRouter] Server breaker OPEN for {base_key} "
            f"(window={window:.0f}s): {reason}"
        )

    def _record_server_busy(self, base_key: str, error: Exception) -> None:
        """Record a SERVER_BUSY_LOADING failure for a base (trip / grow breaker).

        No-op for errors that do not carry the busy-loading signature — real
        per-model errors (404/400) must never trip the server breaker.
        """
        if not self._is_server_busy_loading(error):
            return
        with self._lock:
            self._breaker_trip(base_key, f"{type(error).__name__}: {error}")

    def _breaker_should_skip(self, base: str) -> bool:
        """Consult-before-fire check for a raw api_base.

        Returns True when the caller must NOT fire an HTTP request at this base
        right now:
          - breaker 'open' and window not yet elapsed → skip;
          - breaker half-open and another caller holds the single probe slot → skip.

        As a side effect, when the open→half_open transition fires it claims THE single probe
        INLINE in this same critical section (sets ``probing``/``probe_owner``) so no other caller
        can also fire while we are the designated prober. Production then reads
        :meth:`_caller_holds_probe` and releases in a ``finally``; ``self._lock`` is non-reentrant,
        so we must NOT call :meth:`_breaker_claim_probe` here (it would self-deadlock).
        """
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.get(key)
            if not br:
                return False  # closed — proceed
            state = br['state']
            if state == 'open':
                if time.monotonic() - br['opened_at'] >= br['window']:
                    br['state'] = 'half_open'
                    # Claim THE single probe inline in this SAME critical section so no other caller can
                    # also fire while we are the designated prober. (self._lock is non-reentrant, so we
                    # cannot call _breaker_claim_probe here.) BOTH fields must be set: _caller_holds_probe
                    # (used by call_with_fallback's finally to release) checks probe_owner == get_ident().
                    br['probing'] = True
                    br['probe_owner'] = threading.get_ident()
                    logger.info(
                        f"[APIRouter] Server breaker {key}: open → half_open "
                        f"(claiming exactly one probe)"
                    )
                    return False  # this caller won the single-probe claim — proceed
                return True  # still inside the open window
            # 'half_open': skip unless this caller wins the atomic probe claim.
            # NOTE: we are ALREADY holding self._lock (a non-reentrant Lock), so we must
            # NOT call _breaker_claim_probe here — it would re-acquire the same lock and
            # self-deadlock. Inline the compare-and-set instead (same semantics).
            if br.get('probing'):
                return True  # another caller holds THE single probe slot
            br['probing'] = True
            br['probe_owner'] = threading.get_ident()
            logger.info(
                f"[APIRouter] Server breaker {key}: half_open probe claimed "
                f"(exactly one caller may fire HTTP)"
            )
            return False  # this caller won the single-probe claim — proceed

    def _breaker_claim_probe(self, base: str) -> bool:
        """Atomically claim THE single half-open probe slot for a normalized base.

        Compare-and-set under ``self._lock``: only one caller can win; losers get
        False and must skip/fail fast. The winner fires its HTTP call WITHOUT the
        lock and MUST call :meth:`_breaker_release_probe` in a ``finally`` so a
        hung/terminated probe cannot wedge the breaker.
        """
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.get(key)
            if not br or br.get('probing'):
                return False
            br['probing'] = True
            br['probe_owner'] = threading.get_ident()
            return True

    def _caller_holds_probe(self, base: str) -> bool:
        """True when THIS thread holds the half-open probe claim for a base."""
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.get(key)
            return bool(br) and br.get('probing') and br.get('probe_owner') == threading.get_ident()

    def _breaker_release_probe(self, base: str) -> None:
        """Clear the probing flag (call in a finally after the half-open probe)."""
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.get(key)
            if br:
                br['probing'] = False

    def _breaker_on_success(self, base: str) -> None:
        """Probe/real-call success on this base → close the breaker."""
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.pop(key, None)
            if br:
                logger.info(f"[APIRouter] Server breaker {key}: → closed (call succeeded)")

    def _breaker_is_open(self, base: str) -> bool:
        """Non-mutating breaker-open check for BYPASS paths (Change E).

        Unlike :meth:`_breaker_should_skip` this NEVER transitions state or
        claims the probe slot — context-detection/captioning/image-gen callers
        must not consume the single half-open probe or delay the open→half_open
        transition. They only need "is this server currently off-limits?".
        """
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.get(key)
            if not br:
                return False
            if br['state'] == 'open':
                return time.monotonic() - br['opened_at'] < br['window']
            return True  # half_open/probing: probe slot reserved for real traffic

    def _breaker_wait_seconds_remaining(self, base: str) -> float:
        """Seconds left until the open breaker would allow a probe (0.0 if closed)."""
        key = normalize_api_base(base)
        with self._lock:
            br = self._server_breakers.get(key)
            if not br or br['state'] != 'open':
                return 0.0
            remaining = br['window'] - (time.monotonic() - br['opened_at'])
            return max(0.0, remaining)


    # ── Retry + Fallback Execution ───────────────────────────────────────

    def call_with_fallback(
        self,
        agent_type: str,
        call_fn: Callable,
        *args,
        allocated_tokens: Optional[int] = None,
        messages: Optional[list] = None,
        **kwargs
    ) -> Any:
        """
        Execute ``call_fn(*args, **kwargs)`` with automatic endpoint fallback.
        Supports both regular functions and generators.

        Concurrency is controlled at the agent lifecycle level via SlotPool
        (acquired before the turn starts). No per-call gating occurs here.

        Args:
            agent_type: The type of agent making the call (e.g., 'coder', 'researcher')
            call_fn: The function to execute with the selected endpoint config
            allocated_tokens: Optional - the agent's allocated context size in tokens.
                           Retained for API compatibility; it does NOT inflate any cfg's
                           max_input_tokens (see get_endpoint_chain Tier-1 note).
            messages: Optional - the caller's message list. Enables the context-exceeded gate
                      (A1/A2) to estimate payload size against the endpoint's configured
                      max_input_tokens. When absent, a server-side context-exceeded error is
                      treated as a service error (never triggers fallback compression).
            *args, **kwargs: Additional arguments passed to call_fn. If ``agent_instance_name``
                          is present it is forwarded as ``instance_name`` to rotate the chain.
        """
        # Extract instance name from kwargs (set by execution_engine) so we can
        # apply per-instance cursor rotation and skip already-failed endpoints.
        _inst_name = kwargs.pop('agent_instance_name', None)

        # ── Change D (D1): bounded fail-fast wait when every remaining endpoint sits on a
        # breaker-open physical server. Zero HTTP to busy bases; wait is capped by
        # SERVER_BUSY_WAIT_CAP_SECONDS so this can never hot-loop. After the cap the call
        # degrades with ServerBusyError (a plain RuntimeError subclass) — NOT
        # FallbackCompressionRequired — so the context-compression path stays untouched.
        #
        # NOTE: the wait is a PRE-LOOP step (run once before the endpoint loop), NOT a
        # wrapper around the loop. Wrapping the loop in `for _busy_cycle in range(2)` made
        # the exhaustion `raise RuntimeError` below unreachable on the normal path (cycle 0
        # implicit-continued to cycle 1, swallowing the raise). Running the wait once up
        # front keeps the endpoint loop a single pass that hits its natural exhaustion raise.
        chain = self.get_endpoint_chain(
            agent_type, allocated_tokens=allocated_tokens, instance_name=_inst_name,
        )
        # D1 fail-fast scan: use the NON-mutating _breaker_is_open (not _breaker_should_skip).
        # The pre-loop scan must NOT claim the single half-open probe — doing so would wedge
        # recovery (the probe is claimed here, then the endpoint loop re-consults, sees
        # half_open/probing, skips the busy base, and the claimed probe is never fired). Only
        # the endpoint loop's consult may claim the probe. _breaker_is_open returns the same
        # open/skip boolean (closed→False, open-within-window→True, open-elapsed→False,
        # half_open/probing→True) WITHOUT that side effect.
        if chain and all(
            self._breaker_is_open(
                cfg.get('api_base') or cfg.get('model_server', 'unknown')
            )
            for cfg in chain
        ):
            remaining = max(
                self._breaker_wait_seconds_remaining(
                    cfg.get('api_base') or cfg.get('model_server', 'unknown')
                )
                for cfg in chain
            )
            wait = min(remaining, SERVER_BUSY_WAIT_CAP_SECONDS)
            if wait > 0:
                logger.warning(
                    f"[APIRouter] All endpoints for '{agent_type}' are on breaker-open servers. "
                    f"Failing fast: waiting {wait:.1f}s (cap {SERVER_BUSY_WAIT_CAP_SECONDS:.0f}s) "
                    f"before retrying — zero HTTP requests while busy."
                )
                try:
                    # Termination-aware sleep (D1 requirement).
                    _interruptible_sleep(wait, self._pool, _inst_name)
                except AgentTerminatedError:
                    raise

        all_errors = []

        for cfg_idx, llm_cfg in enumerate(chain):
            # Default per-endpoint retry count from policy. Endpoint config (max_retries field)
            # overrides this — explicit endpoint max_retries always takes precedence.
            max_retries = self.policy.endpoint_max_retries

            concurrency_limit = 0
            rate_limit_rpm = 0           # Default: unlimited
            is_default = (cfg_idx == len(chain) - 1)

            # Resolve endpoint-specific settings — always try to read from
            # the endpoint config, even for the default fallback endpoint.
            # The default endpoint may also be in self.endpoints with its own
            # concurrency setting (Phase 1 fix).
            # Match on (normalized_base, model): with many endpoints sharing one
            # physical server, a raw-base first-match could return the wrong
            # endpoint's settings (plan §7.6 / L739-746 first-match ambiguity).
            endpoint_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
            _norm_endpoint_base = normalize_api_base(endpoint_base)
            with self._lock:
                for ep in self.endpoints.values():
                    if normalize_api_base(ep.api_base) == _norm_endpoint_base and ep.model == llm_cfg.get('model'):
                        max_retries = ep.max_retries
                        concurrency_limit = ep.concurrency_limit
                        rate_limit_rpm = ep.rate_limit_rpm
                        break

            def execute_api_call():
                """Execute the API call. The agent already holds its endpoint's lifecycle slot for this turn."""
                result = call_fn(llm_cfg, *args, **kwargs)
                if not isinstance(result, (list, dict, str)) and hasattr(result, '__iter__'):
                    # Generator — pull first chunk to detect API errors early
                    it = iter(result)
                    try:
                        first_chunk = next(it)
                    except StopIteration:
                        return iter([])

                    def _gen_wrapper(first, rest):
                        yield first
                        try:
                            yield from rest
                        finally:
                            # Close the underlying generator to release HTTP connection
                            try:
                                rest.close()
                            except Exception:
                                pass
                    return _gen_wrapper(first_chunk, it)

                return result

            endpoint_name = llm_cfg.get('model', 'unknown')

            # ── Consult-before-fire (Change B): never fire HTTP at a busy physical server.
            # Applies to EVERY tier including the Tier-4 default (REVIEW M4). Endpoints sharing
            # this normalized base all skip together; different-server failover is unaffected.
            if self._breaker_should_skip(endpoint_base):
                logger.info(
                    f"[APIRouter] Server busy — skipping endpoint '{endpoint_name}' @ {endpoint_base} "
                    f"(breaker open / another agent probing)"
                )
                continue

            # Half-open probe custody: when THIS thread won the single-probe claim it must be
            # released on EVERY exit path from the attempt loop (success/exception/exhaustion).
            _holds_probe = self._caller_holds_probe(endpoint_base)
            try:
                for attempt in range(max_retries + 1):
                    # Check if instance was terminated during a previous failed attempt or between retries.
                    # Prevents starting new API calls after termination (does not interrupt mid-stream calls).
                    if self._pool and _inst_name:
                        if _inst_name in self._pool.terminated_instances:
                            logger.debug(f"[TERMINATION] Instance '{_inst_name}' terminated, aborting LLM call")
                            raise RuntimeError(f"Instance '{_inst_name}' has been terminated")

                    try:
                        # Rate limiting: check and enforce per-endpoint rate limit before each call attempt.
                        # Each retry attempt counts against the rate limit.
                        if rate_limit_rpm > 0:
                            with self._lock:
                                if endpoint_base not in self._endpoint_call_history:
                                    self._endpoint_call_history[endpoint_base] = collections.deque()
                            # Loop until we successfully record this call (handles race conditions when multiple threads sleep)
                            while True:
                                now = time.time()
                                wait_time = 0.0
                                with self._lock:
                                    history = self._endpoint_call_history[endpoint_base]
                                    # Sliding window: remove calls older than RATE_LIMIT_WINDOW_SECONDS
                                    while history and now - history[0] >= RATE_LIMIT_WINDOW_SECONDS:
                                        history.popleft()
                                    # Check if we're over the limit — calculate wait time instead of raising
                                    if len(history) >= rate_limit_rpm:
                                        wait_time = RATE_LIMIT_WINDOW_SECONDS - (now - history[0])
                                # Sleep outside the lock to avoid blocking other threads
                                if wait_time > 0:
                                    logger.debug(
                                        f"[APIRouter] Rate limit reached for '{endpoint_name}' @ {endpoint_base}. "
                                        f"Waiting {wait_time:.1f}s before next call ({rate_limit_rpm} rpm)"
                                    )
                                    # Interruptible sleep: check termination every 0.5s during rate-limit wait
                                    _interruptible_sleep(wait_time, self._pool, _inst_name)
                                # After sleeping, re-check and try to record (loop handles contention)
                                with self._lock:
                                    now = time.time()
                                    history = self._endpoint_call_history[endpoint_base]
                                    while history and now - history[0] >= RATE_LIMIT_WINDOW_SECONDS:
                                        history.popleft()
                                    if len(history) < rate_limit_rpm:
                                        # Track this call atomically within the same lock to prevent race conditions
                                        history.append(now)
                                        break  # Successfully recorded, exit loop

                        # Check termination again just before making the actual API call,
                        # in case instance was terminated during rate limit wait or backoff.
                        if self._pool and _inst_name:
                            if _inst_name in self._pool.terminated_instances:
                                logger.debug(f"[TERMINATION] Instance '{_inst_name}' terminated before API call, aborting")
                                raise RuntimeError(f"Instance '{_inst_name}' has been terminated")

                        result = execute_api_call()

                        # Success on this base → close any open/half-open breaker (Change B).
                        self._breaker_on_success(endpoint_base)

                        # Track the last successful endpoint config for automatic recovery.
                        # Stored only after complete success (including all retries), not during retries.
                        # This enables Tier 3 (last-successful) fallback when agent-specific endpoints become unavailable.
                        with self._lock:
                            self._last_successful_endpoint_cfg = copy.deepcopy(llm_cfg)

                        # Generator errors are already detected inside execute_api_call (first-chunk pull).
                        # Pass the generator through directly — no double-wrapping needed.
                        return result

                    except Exception as e:
                        # AgentTerminatedError is a clean abort signal — re-raise immediately
                        # without logging, retrying, or moving to next endpoint.
                        if isinstance(e, AgentTerminatedError):
                            raise

                        err_msg = str(e)

                        # SERVER_BUSY_LOADING signature → trip/grow the per-server breaker
                        # (Change B). Real per-model errors (404/400) do NOT match and never
                        # trip it — they only earn the per-(base,model) cooldown on exhaustion.
                        if self._is_server_busy_loading(e):
                            self._record_server_busy(endpoint_base, e)
                            # Stop hammering a base that just confirmed busy: if there is another
                            # endpoint later in the chain, break out of the per-endpoint retry loop
                            # and failover (a same-base next endpoint is caught by the breaker skip
                            # at the top of its iteration; a different-base one is real failover).
                            # If this is the LAST endpoint (no failover target), do NOT break — allow
                            # the within-endpoint retry so single-endpoint recovery still works.
                            # EXCEPTION: the designated single-probe holder (_holds_probe) must be
                            # allowed to COMPLETE its probe attempt — success closes the breaker,
                            # failure re-trips it — so the state machine sees a clean outcome. If the
                            # prober broke out and failed over instead, no endpoint on that base would
                            # ever deliver a successful probe during this pass and recovery would stall.
                            # Only non-probers break out to failover. This preserves both no-hammering
                            # (non-probers bail after one 503) and single-probe recovery (the prober
                            # finishes its job).
                            if cfg_idx < len(chain) - 1 and not _holds_probe:
                                logger.info(
                                    f"[APIRouter] Server busy on '{endpoint_name}' @ {endpoint_base} — "
                                    f"stopping retries for this endpoint (failover to next)"
                                )
                                break

                        # Detect context window exceeded errors and advance cursor.
                        # Unlike CharacterRunDetected/MaxTokenExceeded (which occur during streaming
                        # in execution_engine), context-exceeded happens here at API call time.
                        # Advance the per-instance cursor so engine-level retries skip past this endpoint.
                        _inst_name_for_cursor = _inst_name  # Use the variable extracted at call time (kwargs.pop removed it)
                        if _inst_name_for_cursor and self._is_context_exceeded_error(e):
                            # ── A1/A2 gate: sanity-check a server-side context-exceeded error against the
                            # endpoint's CONFIGURED limit before triggering fallback compression.
                            # A 400 under model-swap/eviction conditions (server now running a smaller
                            # window than configured) is NOT evidence of caller overflow — compressing
                            # the caller's history off it was the 2026-08-21 incident (see
                            # reports/fallback-compression-misclass-and-stop-cascade.md).
                            #   - payload <= configured limit → service error: do NOT advance the cursor,
                            #     do NOT compress — fall through to generic retry/cascade handling below.
                            #   - payload >  configured limit → genuine overflow: keep existing behavior.
                            #   - unknown limit (<=0) or failed estimation → never interpret as
                            #     context-exceeded; treat as service error and fall through.
                            #     DEFAULT_MAX_INPUT_TOKENS is NEVER used as a stand-in here (mirrors the
                            #     `> 0` gating at llm/base.py:332).
                            _cfg_limit = llm_cfg.get('max_input_tokens') or 0
                            _has_known_limit = isinstance(_cfg_limit, int) and _cfg_limit > 0
                            # TYPED ContextWindowExceeded (no HTTP status code) is raised by the client-side
                            # pre-check in llm/base.py, which already compared against the real limit — trust it
                            # unconditionally (preserves pre-gate behavior for genuine client-detected overflow).
                            # SERVER errors (status-carrying ModelServiceError etc.) are only trusted when the
                            # payload provably exceeds the configured limit.
                            _estimated = None
                            if _has_known_limit:
                                _estimated = self._estimate_payload_tokens(messages)
                            _genuine_overflow = isinstance(e, ContextWindowExceeded) or (
                                _has_known_limit and _estimated is not None and _estimated > _cfg_limit
                            )
                            if not _genuine_overflow:
                                # Service error (state drift / unknown limit / estimation failure):
                                # fall through to generic retry-within-endpoint then cascade below.
                                # The cursor is deliberately NOT advanced — that mechanism is reserved
                                # for genuine context errors (reset happens at turn end anyway).
                                if not _has_known_limit:
                                    logger.warning(
                                        f"[APIRouter] Context-exceeded reported by server for '{_inst_name_for_cursor}' "
                                        f"on endpoint '{endpoint_name}' but the endpoint has no configured "
                                        f"max_input_tokens — treating as service error, falling through to next endpoint."
                                    )
                                elif _estimated is None:
                                    logger.warning(
                                        f"[APIRouter] Context-exceeded reported by server for '{_inst_name_for_cursor}' "
                                        f"on endpoint '{endpoint_name}' but payload token estimation failed — "
                                        f"treating as service error, falling through to next endpoint."
                                    )
                                else:
                                    logger.warning(
                                        f"[APIRouter] Context-exceeded reported by server for '{_inst_name_for_cursor}' "
                                        f"on endpoint '{endpoint_name}' but payload fits configured limit "
                                        f"{_cfg_limit} (~{_estimated} tokens) — treating as service error, "
                                        f"falling through to next endpoint."
                                    )
                            if _genuine_overflow:
                                # For Compressor agents: just advance cursor (they handle their own compression)
                                if agent_type.lower().startswith('compressor'):
                                    new_pos = self.advance_instance_endpoint(_inst_name_for_cursor)
                                    logger.warning(
                                        f"[APIRouter] Context window exceeded for Compressor '{_inst_name_for_cursor}' "
                                        f"on endpoint '{endpoint_name}'. Cursor advanced to {new_pos}."
                                    )
                                else:
                                    # Advance cursor NOW so retry uses a different (hopefully larger) endpoint after compression.
                                    new_pos = self.advance_instance_endpoint(_inst_name_for_cursor)
                                    logger.warning(
                                        f"[APIRouter] Context window exceeded for '{_inst_name_for_cursor}' "
                                        f"on endpoint '{endpoint_name}'. Triggering iterative fallback compression. "
                                        f"Cursor advanced to {new_pos}."
                                    )
                                    # Lazy import to avoid potential circular imports
                                    from agent_cascade.exceptions import FallbackCompressionRequired
                                    raise FallbackCompressionRequired(
                                        _inst_name_for_cursor, agent_type, endpoint_name, original_error=e
                                    ) from e

                        # NOTE: CharacterRunDetected/MaxTokenExceeded exceptions are raised during
                        # generator iteration inside execution_engine.py, after this method has returned.
                        # All endpoint advancement for those errors happens via
                        # _handle_inner_loop_detection → advance_instance_endpoint.
                        # This block only handles connection/timeout/etc. errors from execute_api_call.

                        # All errors (connection, timeout, etc.) retry within the current
                        # endpoint first, then cascade through the fallback chain on exhaustion.
                        tb_str = traceback.format_exc()
                        error_msg = (
                            f"Endpoint '{endpoint_name}' @ {endpoint_base} "
                            f"attempt {attempt+1}/{max_retries+1}: {e}\nTraceback: {tb_str}"
                        )
                        logger.warning(f"[APIRouter] {error_msg}")
                        all_errors.append(error_msg)

                        if attempt < max_retries:
                            # Use centralized backoff policy (consistent with execution engine)
                            delay = calculate_backoff(attempt + 1, self.policy)
                            logger.info(
                                f"[APIRouter] Backing off {delay:.1f}s before retry "
                                f"for endpoint '{endpoint_name}' @ {endpoint_base}"
                            )
                            # Interruptible sleep: check termination every 0.5s during retry backoff
                            _interruptible_sleep(delay, self._pool, _inst_name)
            finally:
                if _holds_probe:
                    self._breaker_release_probe(endpoint_base)

            logger.info(f"[APIRouter] Exhausted retries for endpoint '{endpoint_name}'. Moving to next...")

            # Record failure time for cooldown tracking when abandoning this endpoint after all retries.
            # Rate limit waits are self-imposed throttles, not real failures — only record on actual exhaustion.
            # Keyed per-(normalized base, model) so shared-base endpoints stay independent (Change C).
            if ENDPOINT_COOLDOWN_SECONDS > 0:
                now = time.time()
                with self._lock:
                    # Clean up stale records to prevent unbounded growth
                    self._cleanup_stale_failure_records(now)
                    self._endpoint_failure_times[(normalize_api_base(endpoint_base), endpoint_name)] = now
                logger.debug(
                        f"[APIRouter] Endpoint '{endpoint_name}' @ {endpoint_base} marked as failed. "
                        f"Cooldown for {ENDPOINT_COOLDOWN_SECONDS}s."
                    )

        # ── D1 final degradation (Change D): if the chain is exhausted and every endpoint
        # was on a breaker-open physical server, this is "server busy — will retry", NOT an
        # unrecoverable failure. Raise a distinguishable ServerBusyError (a plain
        # RuntimeError subclass) so callers can degrade cleanly; it is deliberately NOT a
        # FallbackCompressionRequired, so the context-compression path stays untouched.
        if chain and all(
            self._breaker_is_open(cfg.get('api_base') or cfg.get('model_server', 'unknown'))
            for cfg in chain
        ):
            logger.error(
                f"[APIRouter] Server busy — will retry: all endpoints for '{agent_type}' are on "
                f"breaker-open servers (wait cap {SERVER_BUSY_WAIT_CAP_SECONDS:.0f}s already used). "
                f"No HTTP requests were sent to the busy server."
            )
            raise ServerBusyError(
                f"Server busy — will retry: all endpoints for agent type '{agent_type}' are on a "
                f"busy physical server (circuit breaker open, wait cap exhausted)."
            )

        raise RuntimeError(
            f"All API endpoints exhausted for agent type '{agent_type}'.\n"
            + "\n".join(all_errors)
        )

    # ── Image Captioning ─────────────────────────────────────────────────
    # When images lack captions and the target endpoint is text-only, generate
    # a caption using any available vision-capable endpoint.

    CAPTION_PROMPT = (
        "Describe this image in one concise sentence suitable for use as an alt-text description. "
        "Focus on key visual elements: objects, people, colors, layout, and any text visible."
    )

    @staticmethod
    def _has_uncaptioned_images(messages):
        """Check if any message contains images without captions."""
        from agent_cascade.llm.schema import ContentItem
        for msg in messages:
            if isinstance(msg.content, list):
                for item in msg.content:
                    if isinstance(item, dict) and item.get('image'):
                        if not item.get('caption'):
                            return True
                    elif hasattr(item, 'image') and item.image:
                        if not getattr(item, 'caption', None):
                            return True
        return False

    def _get_any_vision_endpoint(self) -> Optional[dict]:
        """Return the config of any enabled vision-capable endpoint."""
        with self._lock:
            for ep in self.endpoints.values():
                if ep.enabled and getattr(ep, 'vision_enabled', True):
                    return ep.to_llm_cfg()
            # Fallback: default config (assume vision by default)
            if self.default_llm_cfg:
                cfg = copy.deepcopy(self.default_llm_cfg)
                cfg.setdefault('vision_enabled', True)
                return cfg
        return None

    def _get_vision_endpoint_for_agent(self, agent_type: str) -> Optional[dict]:
        """Return the first vision-capable endpoint from the agent's own endpoint chain.

        Falls back to any vision endpoint if none in the chain has vision.
        """
        chain = self.get_endpoint_chain(agent_type)
        for cfg in chain:
            if cfg.get('vision_enabled', True):
                return cfg
        # Fallback: any vision endpoint
        return self._get_any_vision_endpoint()

    def caption_images(
        self, messages, agent_type: str = 'generalist'
    ) -> List:
        """
        Generate captions for uncaptioned images in the message list.

        Uses the agent's own endpoint chain to find a vision-capable endpoint
        for captioning, preserving the agent's configured endpoint order.

        Args:
            messages: List of Message objects (may contain images)
            agent_type: Agent type used for endpoint resolution

        Returns:
            Modified message list with captions attached to images.
        """
        if not self._has_uncaptioned_images(messages):
            return messages

        from agent_cascade.llm.schema import ContentItem, Message
        vision_cfg = self._get_vision_endpoint_for_agent(agent_type)
        if not vision_cfg:
            # Replace uncaptioned images with placeholder text to ensure safe fallback
            logger.warning("[APIRouter] No vision-capable endpoint found for image captioning — replacing with placeholders")
            for msg in messages:
                items = msg.content if isinstance(msg.content, list) else []
                for item in items:
                    img_val = item.get('image') if isinstance(item, dict) else getattr(item, 'image', None)
                    existing_caption = item.get('caption') if isinstance(item, dict) else getattr(item, 'caption', None)
                    if img_val and not existing_caption:
                        cap = '[Image]'
                        if isinstance(item, dict):
                            item['caption'] = cap
                        else:
                            item.caption = cap
            return messages

        from agent_cascade.llm import get_chat_model

        # Collect all uncaptioned images for batch processing
        uncaptioned_items = []  # (msg_index, item_index) tuples to patch back

        for msg_idx, msg in enumerate(messages):
            items = msg.content if isinstance(msg.content, list) else []
            for item_idx, item in enumerate(items):
                img_val = item.get('image') if isinstance(item, dict) else getattr(item, 'image', None)
                existing_caption = item.get('caption') if isinstance(item, dict) else getattr(item, 'caption', None)
                if img_val and not existing_caption:
                    uncaptioned_items.append((msg_idx, item_idx, img_val))

        if not uncaptioned_items:
            return messages

        # Process each uncaptioned image individually for accurate captions
        all_captions = {}  # key = (msg_idx, item_idx) -> caption text
        
        # Batch: send prompt + one image at a time to get per-image captions
        for msg_idx, item_idx, img_val in uncaptioned_items:
            try:
                # ── Change E gate: never fire a captioning call at a breaker-open base.
                # Captioning is non-critical — skip the busy endpoint and fall through to
                # the '[Image]' placeholder below (same as any other caption failure).
                _vbase = vision_cfg.get('api_base') or vision_cfg.get('model_server', 'unknown')
                if self._breaker_is_open(_vbase):
                    logger.info(
                        f"[APIRouter] Server busy — skipping image captioning at {_vbase} "
                        f"(breaker open); using '[Image]' placeholder."
                    )
                    raise RuntimeError(f"Server busy (breaker open) at {_vbase}")
                chat_model = get_chat_model(vision_cfg)
                cap_msg = Message(
                    role='user',
                    content=[
                        ContentItem(text=self.CAPTION_PROMPT),
                        ContentItem(image=img_val),
                    ]
                )
                result_iter = chat_model.chat(
                    messages=[cap_msg],
                    stream=True,
                    delta_stream=False,
                    extra_generate_cfg=vision_cfg,
                )
                # Consume the generator to get the final response
                last_chunk = None
                for chunk in result_iter:
                    last_chunk = chunk
                if last_chunk and len(last_chunk) > 0:
                    caption_text = ''
                    for m in last_chunk:
                        # Handle both Message objects and dicts returned by chat models
                        content = getattr(m, 'content', None) or (m.get('content') if isinstance(m, dict) else None)
                        if isinstance(content, str):
                            caption_text += content
                        elif isinstance(content, list):
                            for ci in content:
                                txt = getattr(ci, 'text', '') or (ci.get('text') if isinstance(ci, dict) else '')
                                if txt:
                                    caption_text += txt
                    caption_text = caption_text.strip()[:MAX_CAPTION_LENGTH]  # Cap length to avoid bloating messages
                else:
                    caption_text = '[Image]'
            except Exception as e:
                logger.warning(f"[APIRouter] Failed to generate image caption: {e}")
                caption_text = '[Image]'

            all_captions[(msg_idx, item_idx)] = caption_text

        # Patch captions back into messages in-place
        for msg_idx, item_idx, _ in uncaptioned_items:
            key = (msg_idx, item_idx)
            caption = all_captions.get(key, '[Image]')
            items = messages[msg_idx].content if isinstance(messages[msg_idx].content, list) else []
            if item_idx < len(items):
                item = items[item_idx]
                if isinstance(item, dict):
                    item['caption'] = caption
                else:
                    item.caption = caption

        logger.info(f"[APIRouter] Captioned {len(uncaptioned_items)} image(s) for vision fallback")
        return messages

    # ── Persistence ──────────────────────────────────────────────────────

    def _save(self):
        """Persist config to disk."""
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            data = {
                'endpoints': [ep.to_dict() for ep in self.endpoints.values()],
                'agent_priorities': self.agent_priorities,
                'agent_types_with_priorities': list(self._agent_types_with_priorities),
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
        # Validate: all values must be lists of endpoint IDs. Malformed entries (e.g., ints)
        # from corrupted or hand-edited config files would crash _resolve_own_endpoints later
        # (line ~839 iterates self.agent_priorities.get(...)). Drop them with a warning.
        validated = {}
        for key, value in priorities.items():
            if not isinstance(value, list):
                logger.warning(
                    f"[APIRouter._normalize_agent_priorities] Invalid priority value for "
                    f"{key!r}: {value!r} (expected list). Skipping."
                )
                continue
            validated[key] = value
        priorities = validated

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
        """Load config from disk if available.

        If the file does not exist, it is created with an empty default
        configuration and a warning is logged so the user knows to configure
        at least one LLM endpoint.
        """
        if not self._config_path.exists():
            # Auto-create with safe defaults on first startup.
            try:
                self._config_dir.mkdir(parents=True, exist_ok=True)
                default_config = {
                    "endpoints": [],
                    "agent_priorities": {},
                    "agent_types_with_priorities": [],
                }
                with open(self._config_path, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, indent=2)
                logger.warning(
                    "config/api_endpoints.json not found; created with empty default configuration. "
                    "Please configure at least one LLM endpoint."
                )
            except OSError as e:
                logger.error(f"[APIRouter] Failed to create config/api_endpoints.json: {e}")
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
            
            # Load tracking set for agent types that ever had endpoints configured.
            # This gates the Tier 3 (last-successful) fallback: agents with no configuration should go straight to global default.
            # For backward compatibility, if the field is missing (old config), we infer it from current priorities.
            raw_tracked = data.get('agent_types_with_priorities', [])
            if raw_tracked:
                self._agent_types_with_priorities = set(raw_tracked)
            else:
                # Backward compat: any agent type with current priorities was clearly configured at some point
                self._agent_types_with_priorities = set(self.agent_priorities.keys())
            
            # Sync tracking set with actual priorities — remove any stale entries from old configs.
            # Prevents incorrect Tier 3 (last-successful) fallback if config has tracked types without actual priorities.
            self._agent_types_with_priorities &= set(self.agent_priorities.keys())
            
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
            self._agent_types_with_priorities = set(self.agent_priorities.keys())
            
            ep_ids = list(self.endpoints.keys())

            # FIX (trigger a): the per-instance endpoint cursor is a POSITIONAL index into the
            # tier chain, and from_dict() rebuilds self.endpoints / agent_priorities — so any
            # stale positional cursor now points at the WRONG endpoint. Reset ALL instance
            # cursors under the lock so a config change never leaves a dangling rotation behind.
            if self._instance_endpoint_position:
                cleared = len(self._instance_endpoint_position)
                self._instance_endpoint_position.clear()
                logger.debug(
                    f"[APIRouter.from_dict] Endpoint config changed — reset {cleared} instance "
                    f"endpoint cursor(s) (stale positional cursors invalidated)."
                )

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
        """Check if an agent instance is currently waiting for a slot in the FIFO queue."""
        for pool in self.scheduler._pools.values():
            status = pool.get_status()
            for waiter in status.get('waiters', []):
                if waiter.get('instance_name') == agent_name:
                    return True
        return False
