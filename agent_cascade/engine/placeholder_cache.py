"""Placeholder-cache helpers for the execution engine (Phase 1c split).

``PlaceholderCacheMixin`` owns the rolling argument/output cache and
``{USE_CACHED_ENTRY_N}`` resolution. Method bodies are moved VERBATIM from
:mod:`agent_cascade.engine.core`; the mixin is composed into
``ExecutionEngine`` there so instance patch points remain unchanged.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

from agent_cascade.agent_instance import ArgumentCachePool
from agent_cascade.log import logger
from agent_cascade.tool_utils import apply_cached_entry_resolutions, resolve_cached_entry_refs


class PlaceholderCacheMixin:
    """Lazily create and use the per-instance placeholder cache."""

    def _ensure_cache_pool(self, instance_name: str) -> None:
        """Lazily initialize the cache pool for an instance if not yet created.

        Instances are single-threaded in practice, so a simple check suffices.

        Args:
            instance_name: The agent instance name.
        """
        inst = self.pool.get_instance(instance_name)
        if inst is None or inst.cache_pool is not None:
            return
        try:
            inst.cache_pool = ArgumentCachePool(max_size=self.pool.settings.cache_pool_size,)
            inst.cache_pool.enabled = self.pool.settings.cache_pool_enabled
        except Exception as e:
            logger.warning(f"Failed to initialize cache pool for '{instance_name}': {e}")

    def _cache_tool_args(self, instance_name: str, tool_name: str, tool_args: Any) -> None:
        """Store resolved tool arguments in the rolling cache pool for {USE_CACHED_ENTRY_N} reuse.

        Args are deep-copied to prevent later mutation of cached values.

        Args:
            instance_name: The agent instance name (scope key).
            tool_name: Name of the tool whose args are being cached.
            tool_args: Resolved arguments (after placeholder substitution).
        """
        if not isinstance(tool_args, dict):
            return  # Nothing to cache for non-dict args

        # ── Add to rolling cache pool ───────────────────────────────────────
        self._ensure_cache_pool(instance_name)
        inst = self.pool.get_instance(instance_name)
        if inst is None or inst.cache_pool is None:
            return

        cp = inst.cache_pool
        if not cp.enabled:
            return

        threshold = self.pool.settings.cache_threshold_chars

        # Cache individual arg values that pass the threshold
        cache_refs = {}
        for key, val in tool_args.items():
            if isinstance(val, str) and len(val) > threshold:
                try:
                    idx = cp.add('arg', f"{tool_name}.{key}", val, threshold=threshold)
                    cache_refs[key] = idx
                except (TypeError, AttributeError):
                    pass

        # Build notification only if something was actually cached
        if cache_refs:
            refs_str = ', '.join(f'"{k}" → N={n}' for k, n in cache_refs.items())
            with inst._compression_lock:
                inst._cache_notifications.append(f'[{tool_name}] Cached: {refs_str}')

    def _cache_tool_output(self, instance_name: str, tool_name: str, output: str, threshold: int = 1000) -> None:
        """Cache tool output in the rolling pool if it exceeds the threshold.

        Called BEFORE truncation so the full content is preserved.

        Args:
            instance_name: Agent instance name (scope key).
            tool_name: Name of the tool that produced this output.
            output: The tool result string (full, pre-truncation).
            threshold: Minimum character count to trigger caching.
        """
        if not isinstance(output, str) or len(output) <= threshold:
            return

        self._ensure_cache_pool(instance_name)
        inst = self.pool.get_instance(instance_name)
        if inst is None or inst.cache_pool is None:
            return

        cp = inst.cache_pool
        if not cp.enabled:
            return

        char_count = len(output)
        try:
            idx = cp.add('output', tool_name, output, threshold=threshold)
            with inst._compression_lock:
                inst._cache_notifications.append(f'[{tool_name}] Output cached: N={idx} ({char_count} chars)')
        except (TypeError, AttributeError):
            pass

    def _resolve_placeholders(self, tool_args: Any, instance_name: str, tool_name: str) -> Optional[dict]:
        """Resolve {USE_CACHED_ENTRY_N} placeholders in tool arguments.

        If *tool_args* is a JSON string it is parsed first, then resolved.
        Resolution looks up cached entries from the rolling cache pool.
        Unresolvable placeholders are left as-is — no error is raised so
        that regular tool use is unaffected.

        Args:
            tool_args: Raw tool arguments (dict or JSON string).
            instance_name: Agent instance name (scope key).
            tool_name: Name of the tool being called.

        Returns:
            Resolved dict on success (with or without placeholder resolution),
            or None on JSON parse failure / unexpected type.
        """
        # ── Step 1: ensure we have a dict to work with ──────────────────────
        if isinstance(tool_args, dict):
            parsed = tool_args
        elif isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except json.JSONDecodeError:
                logger.debug('JSON parse failure for %s/%s', instance_name, tool_name)
                return None  # JSON parse failure — signal error to caller
            if not isinstance(parsed, dict):
                logger.debug('parsed to non-dict for %s/%s: %s', instance_name, tool_name, type(parsed).__name__)
                return None  # Parsed to non-dict — signal error
        else:
            logger.debug('unexpected type for %s/%s: %s', instance_name, tool_name, type(tool_args).__name__)
            return None  # Unexpected type — signal error

        # Scan for {USE_CACHED_ENTRY_N} patterns using shared function (avoids
        # regex recompilation + code duplication)
        inst = self.pool.get_instance(instance_name)
        cache_pool = getattr(inst, 'cache_pool', None) if inst else None
        cached_refs = resolve_cached_entry_refs(parsed, cache_pool)

        # Always deep-copy for consistency (same path whether placeholders
        # exist or not)
        resolved_args = copy.deepcopy(parsed)

        if not cached_refs:
            return resolved_args  # Nothing to resolve, but return a safe copy

        # ── Resolve {USE_CACHED_ENTRY_N} using shared function ──────────────
        apply_cached_entry_resolutions(resolved_args, cached_refs)

        return resolved_args
