"""
Tail Sync Check — Lightweight length verification between pool and JSONL logs.

Design doc §5.2 requires that "the tail end past the last marker MUST be in sync
at all times and have the EXACT same number of messages." This module provides a
fast, length-only check that runs after every write operation to catch drift early.

This is NOT a full content comparison — just verify message counts match between:
  - Pool conversation tail (messages after last compression marker in memory)
  - JSONL file tail (messages after last compression marker on disk)

Usage:
    from agent_cascade.logger.tail_sync_check import check_tail_sync, check_and_log
    
    success = check_and_log(instance_name, conv, log_path, context="log_message")
"""

import json
import os
from typing import List, Optional, Tuple

from agent_cascade.log import logger as _log

# Compression marker prefix (matches dna.py COMPRESSION_MARKER)
_COMPRESSED_PREFIX = "--- CONTEXT COMPRESSED"


# ── Tail length counting helpers ──────────────────────────────────────────────

def _count_pool_tail(conv: List, last_marker_idx: int) -> int:
    """Count messages after the last compression marker in an in-memory conversation.
    
    Args:
        conv: The agent's conversation list (pool working set).
        last_marker_idx: Index of the latest compression marker (-1 if none).
        
    Returns:
        Number of tail messages (messages AFTER the marker, not including it).
    """
    if last_marker_idx >= 0:
        return len(conv) - last_marker_idx - 1
    # No marker → entire conversation is the "tail"
    return len(conv)


def _count_jsonl_tail(log_path: str) -> Tuple[int, int, Optional[int]]:
    """Count messages after the last compression marker in a JSONL file.
    
    Optimized backwards scan: reads all lines once but parses from the end,
    stopping as soon as the last compression marker is found. This avoids
    parsing the entire file for conversations with compression markers.
    
    Args:
        log_path: Absolute path to the JSONL log file.
        
    Returns:
        Tuple of (tail_count, total_messages, marker_line_number).
        marker_line_number is the 1-based line index where the last marker was found,
        or None if no marker was found. Returns (0, 0, None) on errors.
    """
    if not log_path or not os.path.exists(log_path):
        return 0, 0, None
    
    try:
        from agent_cascade.llm.schema import USER as USER_ROLE
        
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]
        
        if not lines:
            return 0, 0, None
        
        # First pass: count total messages (skip metadata/events)
        total_msgs = 0
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                if "metadata" in item or "event" in item:
                    continue
                total_msgs += 1
        
        if total_msgs == 0:
            return 0, 0, None
        
        # Second pass: scan backwards to find last compression marker
        # This is fast — markers are rare, so we usually parse only a few lines
        msg_count = 0
        found_marker = False
        marker_line = None  # 1-based line number of the marker
        for i in range(len(lines) - 1, -1, -1):
            try:
                item = json.loads(lines[i])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                if "metadata" in item or "event" in item:
                    continue
                msg_count += 1
                # Check for compression marker (must match find_last_marker behavior)
                content = item.get('content', '')
                if (item.get('role') == USER_ROLE and
                        isinstance(content, str) and content.startswith(_COMPRESSED_PREFIX)):
                    found_marker = True
                    marker_line = i + 1  # Convert to 1-based line number
                    break
        
        # If marker found, subtract it from count. Otherwise all messages are tail.
        tail_count = msg_count - 1 if found_marker else msg_count
        return max(tail_count, 0), total_msgs, marker_line
        
    except OSError as e:
        _log.debug(f"Failed to read JSONL tail for {log_path}: {e}")
        return 0, 0, None


# ── Main check function ───────────────────────────────────────────────────────

def check_tail_sync(
    instance_name: str,
    conv: List,
    log_path: Optional[str] = None,
) -> Tuple[bool, int, int]:
    """Verify that pool tail length matches JSONL tail length.
    
    This is a LENGTH-ONLY check — no content comparison, no deep copies.
    Designed to be fast enough to run after every write operation.
    
    Args:
        instance_name: Agent instance name (for logging).
        conv: The agent's in-memory conversation list from the pool.
        log_path: Path to the JSONL file. If None or missing, returns True.
        
    Returns:
        Tuple of (in_sync: bool, pool_tail_len: int, jsonl_tail_len: int).
        In sync means the counts match OR the JSONL doesn't exist yet.
    """
    # Find last marker index in pool conversation
    from agent_cascade.agent_pool import AgentPool
    last_marker_idx = AgentPool.find_last_marker(conv)
    
    # Count tail in pool (in-memory, O(1))
    pool_tail_len = _count_pool_tail(conv, last_marker_idx)
    
    # Count tail in JSONL (file read, but lightweight — only reads line structure)
    jsonl_tail_len, _, _ = _count_jsonl_tail(log_path) if log_path else (0, 0, None)
    
    in_sync = (pool_tail_len == jsonl_tail_len)
    return in_sync, pool_tail_len, jsonl_tail_len


def check_and_log(
    instance_name: str,
    conv: List,
    log_path: Optional[str] = None,
    context: str = "write",
) -> bool:
    """Run the tail sync check and log a warning if drift is detected.
    
    Convenience wrapper around check_tail_sync() that handles logging.
    
    Args:
        instance_name: Agent instance name (for logging).
        conv: The agent's in-memory conversation list from the pool.
        log_path: Path to the JSONL file.
        context: Description of when the check ran (e.g., "log_message", "compression").
        
    Returns:
        True if in sync, False if drift detected or error occurred.
    """
    try:
        in_sync, pool_tail, jsonl_tail = check_tail_sync(
            instance_name, conv, log_path
        )
        
        if not in_sync:
            # Gather diagnostic info for actionable error message
            from agent_cascade.agent_pool import AgentPool
            last_marker_idx = AgentPool.find_last_marker(conv)
            conv_len = len(conv)
            jsonl_total = 0
            marker_line = None
            
            if log_path:
                _, jsonl_total, marker_line = _count_jsonl_tail(log_path)
            
            # Build detailed diagnostic message
            if last_marker_idx >= 0:
                pool_marker_info = f"marker@idx={last_marker_idx}"
            else:
                pool_marker_info = "no_marker"
            
            if marker_line is not None:
                jsonl_marker_info = f"marker@line={marker_line}"
            else:
                jsonl_marker_info = "no_marker"
            
            _log.warning(
                f"[TAIL SYNC DRIFT] '{instance_name}' after {context}: "
                f"pool_tail={pool_tail} (conv_len={conv_len}, marker={pool_marker_info}) "
                f"vs jsonl_tail={jsonl_tail} (total_msgs={jsonl_total}, marker={jsonl_marker_info})"
            )
            return False
        
        return True
        
    except Exception as e:
        # Signal failure so drift cannot be confirmed
        _log.warning(f"[TAIL SYNC CHECK] '{instance_name}' check failed ({context}): {e}")
        return False