"""Cache management + the module-level CacheManager singleton (moved verbatim from api_integration.py).

Phase 3b pure-move refactor. This module is SELF-CONTAINED: it must NOT import
state_builder (that would create a cycle). ``_cache_mgr`` is instantiated exactly once
here; every consumer imports the SAME object from this module.
"""

import copy as _copy
import threading
from typing import Dict

class CacheManager:
    """Centralized performance cache management for API integration.
    
    Consolidates 5 separate module-level caches (and their locks) into a single
    thread-safe structure. This eliminates cache sprawl and makes clearing/eviction
    atomic across all caches.
    
    PAIRED CACHE EVICTION NOTE:
        The stream_versions and cached_instances caches are paired — they share
        the same lock in the original code. When evicting from one, the corresponding
        entry in the other is also removed to prevent orphaned data.
    """
    
    def __init__(self):
        self._lock = threading.RLock()  # Single reentrant lock for all caches
        
        # Token stats cache: (msg_count, last_msg_id, stream_len) -> stats dict
        self.token_stats: Dict[tuple, dict] = {}
        
        # Stream version tracking: instance_name -> (msg_count, id, stream_len)
        self.stream_versions: Dict[str, tuple] = {}
        
        # Cached serialized instance data: instance_name -> dict
        self.cached_instances: Dict[str, dict] = {}
        
        # UI serialization cache: msg_id -> serialized dict
        self.ui_serialization: Dict[int, dict] = {}
        
        # Stream token stats: instance_name -> (h_stats, r_stats) tuple of dicts
        self.stream_token_stats: Dict[str, tuple] = {}
        
        # BUG_0005 follow-up: Separate version tracking for token-stats caching.
        # build_stream_update_from_pool uses a 3-tuple key (no stream_content_len) while
        # _serialize_instances_incremental uses a 4-tuple (with stream_content_len).
        # They must NOT share stream_versions or the mismatch causes permanent cache misses.
        self.stream_token_stats_versions: Dict[str, tuple] = {}
    
    def clear_all(self) -> None:
        """Clear all caches. Called during session reset."""
        with self._lock:
            self.token_stats.clear()
            self.stream_versions.clear()
            self.cached_instances.clear()
            self.ui_serialization.clear()
            self.stream_token_stats.clear()
            self.stream_token_stats_versions.clear()
    
    def evict_if_full(self, cache_name: str, maxsize: int) -> None:
        """Evict oldest entry if cache exceeds max size (FIFO).
        
        Handles paired cache eviction for stream_versions/cached_instances.
        """
        with self._lock:
            target = getattr(self, cache_name, {})
            
            # Determine paired cache (stream_versions <-> cached_instances)
            paired = None
            if cache_name == 'stream_versions':
                paired = ('cached_instances', self.cached_instances)
            elif cache_name == 'cached_instances':
                paired = ('stream_versions', self.stream_versions)
            
            while len(target) >= maxsize:
                oldest_key = next(iter(target))
                target.pop(oldest_key)
                if paired and oldest_key in paired[1]:
                    paired[1].pop(oldest_key, None)
    
    def evict_instance(self, instance_name: str) -> None:
        """Evict all cached data for a specific instance (paired eviction)."""
        with self._lock:
            self.stream_versions.pop(instance_name, None)
            self.cached_instances.pop(instance_name, None)
            self.stream_token_stats.pop(instance_name, None)


# Module-level CacheManager instance
_cache_mgr = CacheManager()

_TOKEN_STATS_CACHE_MAXSIZE = 5000
_UI_CACHE_MAXSIZE = 2000
_STREAM_TOKEN_STATS_CACHE_MAXSIZE = 100

def _clear_performance_caches():
    """Clear all module-level performance caches. Called during session reset."""
    _cache_mgr.clear_all()


def _clear_ui_serialization_cache() -> None:
    """Clear the id()-keyed UI serialization cache.

    Must be called whenever message objects are replaced (e.g., compression)
    because old objects are GC'd and their memory addresses can be reused by
    new objects, causing stale cache hits that return wrong content.
    """
    with _cache_mgr._lock:
        _cache_mgr.ui_serialization.clear()


def _store_ui_cache(msg_id: int, cached_data: dict) -> None:
    """Store serialized message data in the CacheManager UI cache with bounded size.
    
    Uses a deep copy to prevent nested mutable leakage between cached entries.
    Thread-safe via CacheManager._lock."""
    import copy as _copy  # Lazy import — only used during serialization, not hot path
    with _cache_mgr._lock:
        # Evict oldest entry when cache exceeds max size (FIFO via insertion order)
        if len(_cache_mgr.ui_serialization) >= _UI_CACHE_MAXSIZE:
            _cache_mgr.ui_serialization.pop(next(iter(_cache_mgr.ui_serialization)))
        _cache_mgr.ui_serialization[msg_id] = _copy.deepcopy(cached_data)
