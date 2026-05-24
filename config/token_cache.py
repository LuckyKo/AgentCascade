"""
Unified token cache for all agent instances.
Replaces the split caching: _cached_hist_stats (root) + _sa_stats_{name} (sub-agents).
"""
import threading
import time


class AgentTokenCache:
    """Thread-safe, TTL-based cache mapping agent instance names to token count stats."""
    
    def __init__(self, ttl=300):
        """
        Args:
            ttl: Time-to-live in seconds for each cached entry. Default 5 minutes.
        """
        self._cache = {}       # instance_name -> {'count': int, 'tokens': int, 'timestamp': float}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._cleanup_timer = None
        # Start periodic cleanup (every 5 minutes)
        self._start_cleanup_timer()
    
    def _start_cleanup_timer(self):
        """Start a background timer that calls cleanup_expired every 5 minutes."""
        def _cleanup_and_reschedule():
            try:
                self.cleanup_expired()
            finally:
                # Re-arm the timer for the next cycle
                if not self._cleanup_timer or not self._cleanup_timer.is_alive():
                    self._start_cleanup_timer()
        
        self._cleanup_timer = threading.Timer(300, _cleanup_and_reschedule)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()
    
    def get(self, instance_name):
        """Get cached stats for an instance. Returns None if not found or expired."""
        with self._lock:
            entry = self._cache.get(instance_name)
            if entry is None:
                return None
            if time.time() - entry['timestamp'] > self._ttl:
                del self._cache[instance_name]
                return None
            return {'count': entry['count'], 'tokens': entry['tokens']}
    
    def set(self, instance_name, count, tokens):
        """Set cached stats for an instance."""
        with self._lock:
            self._cache[instance_name] = {
                'count': count,
                'tokens': tokens,
                'timestamp': time.time(),
            }
    
    def invalidate(self, instance_name):
        """Remove cache entry for a specific instance (e.g., after compression)."""
        with self._lock:
            self._cache.pop(instance_name, None)
    
    def clear_all(self):
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
    
    def cleanup_expired(self):
        """Remove all expired entries. Call periodically."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._cache.items() if now - v['timestamp'] > self._ttl]
            for k in expired:
                del self._cache[k]
    
    def size(self):
        """Number of cached instances."""
        with self._lock:
            return len(self._cache)
    
    def __repr__(self):
        """String representation showing cache size and TTL."""
        return f'AgentTokenCache(size={self.size()}, ttl={self._ttl})'