"""
Agent Instance Logger — Persistent JSONL logging for agent sessions.

Each agent instance gets its own timestamped log file that records
metadata and all messages (including tool calls and results).

Ported from agent_logger.py for the new unified architecture.
Writes to Layer 1 (JSONL file). The pool owns Layer 2 (in-memory working set).
"""

import json
import os
import shutil
import datetime
from typing import Any, Dict, List, Optional, Union

from agent_cascade.log import logger
from agent_cascade.prompts.dna import COMPRESSION_MARKER


class AgentInstanceLogger:
    """Handles persistent logging for an agent instance.

    Writes to a JSONL file per instance. The pool owns the in-memory working set
    (AgentInstance.conversation), so this logger only writes to disk and syncs via
    update_history() after compression/rollback events.
    """

    def __init__(self, agent_class: str, instance_name: str, log_dir: str,
                 base_metadata: Optional[Dict] = None, log_path: Optional[str] = None):
        self.agent_class = (agent_class or '').strip().lower()  # Normalize for case-insensitive tracking
        self.instance_name = instance_name
        self.start_time = datetime.datetime.now()

        # Use provided log_path if given, otherwise generate a new timestamped filename
        self._log_path_provided = bool(log_path)  # Track if log_path was externally provided (Fix #2)
        if log_path:
            self.log_path = log_path
        else:
            timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
            filename = f"{self.agent_class}_{instance_name}_{timestamp}.jsonl"
            self.log_path = os.path.join(log_dir, filename)

        self.data = {
            "metadata": {
                "agent_class": self.agent_class,  # Already normalized to lowercase
                "instance_name": instance_name,
                "start_timestamp": self.start_time.isoformat(),
                "last_update": self.start_time.isoformat(),
                "current_log_path": self.log_path,
                "working_dir": os.getcwd(),  # Default to current CWD
                "supervisor": "System",      # Default supervisor
            },
            "history": []
        }

        # Merge base metadata if provided (e.g. from a loaded session)
        if base_metadata:
            for k, v in base_metadata.items():
                self.data["metadata"][k] = v
            if "original_log_path" not in self.data["metadata"] and "current_log_path" in base_metadata:
                self.data["metadata"]["original_log_path"] = base_metadata["current_log_path"]

        self._file_handle = None  # Cached file handle to avoid open/write/close per message (Fix #1)
        self._initialized = False  # Belt-and-suspenders guard against duplicate _initial_save() (get_logger lock is primary protection)
        self._file_history_synced = False  # One-shot file sync guard for update_history() — prevents duplicate file loads
        self._initial_save()

    @classmethod
    def copy_session_file(cls, source_path: str, log_dir: str, agent_class: str, instance_name: str) -> str:
        """Copy a session file to a new timestamped location.
        
        This creates a working copy of an existing session file with a new timestamp,
        preserving the original file intact. Returns the path to the copied file.
        
        Args:
            source_path: Path to the original session file
            log_dir: Directory where the copy should be created
            agent_class: Normalized agent class name (lowercase)
            instance_name: Agent instance name
            
        Returns:
            Path to the newly created copy
            
        Raises:
            FileNotFoundError: If source_path does not exist
        """
        # Guard against missing source file (Fix #3)
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Source session file not found: {source_path}")
        
        # Generate a new timestamped filename for this working session
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        new_filename = f"{agent_class}_{instance_name}_{timestamp}.jsonl"
        new_log_path = os.path.join(log_dir, new_filename)
        
        # Copy the original file to the new path (preserves metadata with copy2)
        shutil.copy2(source_path, new_log_path)
        logger.debug(f"Copied session from {source_path} to {new_log_path}")
        
        return new_log_path

    # ── File handle management ────────────────────────────────────────────

    def _ensure_file(self):
        """Open the log file if it's not already open. Reopens if closed."""
        if self._file_handle is None or self._file_handle.closed:
            self._file_handle = open(self.log_path, 'a', encoding='utf-8')

    # ── Formatting ────────────────────────────────────────────────────────

    def _format_message(self, message: Union[Dict, Any]) -> Dict:
        """Ensure message is a dict and has a timestamp. Returns a copy to avoid mutation.

        CRITICAL DESIGN NOTE — Timestamps as Identity Markers:
        The timestamp field is NOT just metadata; it serves as the PRIMARY KEY for message
        identity in the deduplication logic of update_history(). Two messages with the same
        timestamp are considered the "same slot" and will be treated as an update rather than
        a duplicate. DO NOT remove or randomize timestamps — doing so would break dedup and
        cause message duplication on every sync cycle.
        """
        # Fix #6: Deep copy before mutating to avoid side effects on original Message objects
        if hasattr(message, 'model_dump'):  # For Pydantic-based Message
            msg_dict = message.model_dump()

            # If it already has a timestamp (from a prior call or from constructor), reuse it.
            # Check both the dict and getattr as fallback — Pydantic extra='allow' may store
            # dynamic fields in model_dump but not always as direct attributes.
            ts = msg_dict.get('timestamp') or getattr(message, 'timestamp', None)
            if ts:
                msg_dict['timestamp'] = ts
            else:
                ts = datetime.datetime.now().isoformat()
                try:
                    message.timestamp = ts
                except Exception:
                    pass
                msg_dict['timestamp'] = ts

            return msg_dict

        if isinstance(message, dict):
            # Return a copy instead of mutating in-place (Fix #6)
            msg_copy = dict(message)
            if 'timestamp' not in msg_copy:
                msg_copy['timestamp'] = datetime.datetime.now().isoformat()
            return msg_copy

        # Fallback for generic objects or Message dataclass
        msg_dict = {}
        for k in ['role', 'content', 'name', 'function_call', 'extra', 'timestamp']:
            if hasattr(message, k):
                val = getattr(message, k)
                if val is not None:
                    msg_dict[k] = val

        if 'timestamp' not in msg_dict:
            ts = datetime.datetime.now().isoformat()
            msg_dict['timestamp'] = ts
            try:
                setattr(message, 'timestamp', ts)
            except Exception:
                pass

        if not msg_dict and isinstance(message, str):
            msg_dict = {'role': 'unknown', 'content': message,
                        'timestamp': datetime.datetime.now().isoformat()}

        return msg_dict

    # ── File I/O ──────────────────────────────────────────────────────────

    def _append_line(self, data: Dict):
        """Append a single JSON line to the log file (uses cached file handle)."""
        try:
            self._ensure_file()
            self._file_handle.write(json.dumps(data, ensure_ascii=False) + '\n')
            self._file_handle.flush()  # Flush for durability — prevents data loss on crash
        except Exception as e:
            logger.error(f"Failed to append to agent log {self.log_path}: {e}")
            self._file_handle = None  # Invalidate so _ensure_file reopens clean next time

    def _initial_save(self):
        """Write metadata as the first line. Guard against duplicate calls,
        and also check if file already has metadata from another logger instance.
        
        Thread-safety note: The primary protection against duplicate writes is the
        LoggerManager._lock which protects get_logger() calls. This method provides
        a secondary defense for edge cases where multiple instances might access the same file.
        The file-read-then-write sequence is not atomic, but race conditions are unlikely
        in practice since the composite key cache ensures only one logger instance per (instance_name, agent_class).
        
        When log_path was externally provided (session load scenario), skip writing metadata here
        as reset_history(rewrite=True) will handle it. This avoids redundant I/O. (Fix #2)
        """
        if self._initialized:
            return
        
        # Skip initial save when log_path was externally provided (session load path)
        # The caller will handle file initialization via reset_history/rewrite
        if getattr(self, '_log_path_provided', False):
            self._initialized = True
            return
        
        # Defensive check: if file already exists with metadata on first line, skip writing
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, 'r') as f:
                    first_line = f.readline().strip()
                    if first_line:
                        first_data = json.loads(first_line)
                        if "metadata" in first_data:
                            self._initialized = True
                            return
            except Exception:
                # Corrupted file — remove it so we start fresh instead of appending to garbage
                try:
                    os.remove(self.log_path)
                except OSError:
                    pass
        
        self._append_line({"metadata": self.data["metadata"]})
        self._initialized = True

    def update_timestamp(self):
        """Update the last_update metadata timestamp."""
        self.data["metadata"]["last_update"] = datetime.datetime.now().isoformat()

    # ── Core logging operations ───────────────────────────────────────────

    def log_message(self, message: Any):
        """Append a single message to history and file."""
        self.update_timestamp()
        formatted_msg = self._format_message(message)
        self.data["history"].append(formatted_msg)
        self._append_line(formatted_msg)

    # ── History loading from disk ─────────────────────────────────────────

    def load_history_from_file(self):
        """Load existing message history from the JSONL file into in-memory data["history"].
        
        Called during session restore to ensure the logger's in-memory state matches
        what's already persisted on disk. This prevents double-logging when initial
        messages are re-added to the conversation.
        
        The JSONL format is:
          - Line 1: metadata dict (has "metadata" key) — skip this
          - Lines 2+: message dicts — load these into data["history"]
        """
        # FIX #1: Clear history before loading to prevent duplication if called twice
        self.data["history"] = []
        
        if not os.path.exists(self.log_path):
            return
        
        try:
            with open(self.log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            for line in lines[1:]:  # Skip metadata line
                line = line.strip()
                if not line:
                    continue
                try:
                    msg_dict = json.loads(line)
                    # FIX #2: Ensure msg_dict is actually a dict before checking keys
                    if isinstance(msg_dict, dict) and "metadata" not in msg_dict:  # Only load message dicts, skip metadata lines and non-dict JSON values
                        self.data["history"].append(msg_dict)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping malformed JSON line in {self.log_path} — data may be incomplete")
                    continue  # Skip malformed lines
        except OSError as e:
            logger.warning(f"Could not read log file {self.log_path} during sync: {e}. "
                           "History may be out of sync, potentially causing duplicate appends.")
            pass  # File disappeared or unreadable — stay with empty history

    # ── Compression marker insertion ──────────────────────────────────────

    def insert_compression_marker(self, summary_msg: Any, tail_count: int):
        """Insert a compression summary marker into the cumulative log at the
        correct position — calculated as an offset from the end of the log.

        Args:
            summary_msg: The compression summary message (USER role with
                         ``<context_summary>`` tags).
            tail_count: Number of tail messages that should appear after the
                        summary marker in both pool and log.
        """
        formatted = self._format_message(summary_msg)
        log_history = self.data["history"]

        # --- Derive insertion point from offset-from-end ---
        insert_pos = len(log_history) - tail_count

        # Safety: Never insert before the SYSTEM message (index 0)
        if insert_pos == 0 and log_history and log_history[0].get('role') == 'system':
            insert_pos = 1

        # Clamp to valid range
        insert_pos = min(insert_pos, len(log_history))

        log_history.insert(insert_pos, formatted)

        logger.info(
            f"Logger [{self.instance_name}]: Inserted compression marker at "
            f"index {insert_pos} (log_len={len(log_history)}, tail_count={tail_count})"
        )

        # Rewrite the entire file since we inserted in the middle
        self.reset_history(log_history, rewrite=True)

    # ── History sync ──────────────────────────────────────────────────────

    def update_history(self, history: List[Any]):
        """Additive sync for persistent logs (JSONL).

        Only appends new messages found in `history` that aren't in the log yet.
        Handles context compression by identifying the most advanced sync point.

        CRITICAL DESIGN NOTE — Timestamp Identity Matching:
        Deduplication uses timestamps as the primary matching key. When a message from
        `history` has the same timestamp as the next expected entry in self.data["history"],
        it's treated as an UPDATE to that slot (content may differ) rather than a new message.
        This is intentional: timestamps are assigned at message creation time and persist
        across pool mutations. DO NOT change this matching logic without ensuring the logger
        sync after compression events also uses timestamp-based identity.
        """
        # FIX: Always load from file when _file_history_synced=False (not just when memory is empty).
        # This prevents stale comparison baseline after compression.
        if not self._file_history_synced and os.path.exists(self.log_path):
            self.load_history_from_file()
            self._file_history_synced = True
        
        # Update timestamp AFTER sync check to avoid misleading metadata when no writes occur
        self.update_timestamp()
        
        old_history = self.data["history"]
        last_match_in_log = -1
        needs_rewrite = False

        # Robust comparison helper — defined outside loop to avoid redefinition on each iteration (Fix #6)
        def normalize(v):
            if v is None:
                return ""
            if isinstance(v, dict):
                try:
                    return json.dumps(v, sort_keys=True, ensure_ascii=False).strip()
                except Exception:
                    return str(v).strip()
            return str(v).replace('\r\n', '\n').strip()

        # Surgical Merge: Identify gaps, insertions, and UPDATES
        buffer = []
        for i, msg in enumerate(history):
            formatted = self._format_message(msg)

            # Search for this message in the log
            found_idx = -1
            start_search = last_match_in_log + 1

            # Check if it's an UPDATE to the very next message in log
            if start_search < len(old_history):
                potential_match = old_history[start_search]

                # Use timestamp as a reliable identifier for the same slot
                same_slot = False
                if potential_match.get('timestamp') == formatted.get('timestamp'):
                    same_slot = True
                elif (potential_match.get('role') == formatted.get('role') and
                      potential_match.get('name') == formatted.get('name')):
                    # Fallback for messages without timestamps
                    old_c = str(potential_match.get('content', ''))
                    new_c = str(formatted.get('content', ''))
                    if COMPRESSION_MARKER in old_c and COMPRESSION_MARKER in new_c:
                        same_slot = True
                    elif normalize(old_c) == normalize(new_c):
                        same_slot = True

                if same_slot:
                    if (normalize(potential_match.get('content')) != normalize(formatted.get('content')) or
                            normalize(potential_match.get('reasoning_content')) != normalize(formatted.get('reasoning_content')) or
                            normalize(potential_match.get('function_call')) != normalize(formatted.get('function_call'))):
                        # CONTENT CHANGED (Manual Edit)
                        old_history[start_search] = formatted
                        needs_rewrite = True

                    found_idx = start_search

            # If not an immediate update, search forward for a match
            if found_idx == -1:
                for j in range(start_search, len(old_history)):
                    potential_match = old_history[j]
                    if (potential_match.get('role') == formatted.get('role') and
                            normalize(potential_match.get('content')) == normalize(formatted.get('content')) and
                            normalize(potential_match.get('name')) == normalize(formatted.get('name')) and
                            normalize(potential_match.get('reasoning_content')) == normalize(formatted.get('reasoning_content')) and
                            normalize(potential_match.get('function_call')) == normalize(formatted.get('function_call'))):
                        found_idx = j
                        break

            if found_idx != -1:
                # We found a match (or an update slot)!
                if buffer:
                    if found_idx > last_match_in_log + 1:
                        insert_pos = found_idx
                        logger.info(
                            f"Logger [{self.instance_name}]: Compression detected — "
                            f"inserting {len(buffer)} message(s) into log at gap boundary index {insert_pos}."
                        )
                    else:
                        insert_pos = last_match_in_log + 1
                        logger.info(
                            f"Logger [{self.instance_name}]: Surgically inserting "
                            f"{len(buffer)} messages into log at index {insert_pos}."
                        )
                    self.data["history"] = old_history[:insert_pos] + buffer + old_history[insert_pos:]
                    old_history = self.data["history"]
                    found_idx += len(buffer)
                    buffer = []
                    needs_rewrite = True

                last_match_in_log = found_idx
            else:
                # No match found yet, add to buffer
                buffer.append(formatted)

        # Any remaining messages in buffer are truly new tail messages
        if buffer:
            for msg in buffer:
                old_history.append(msg)
                if not needs_rewrite:
                    self._append_line(msg)

        if needs_rewrite:
            self.reset_history(old_history, rewrite=True)

    # ── History reset / rewrite ───────────────────────────────────────────

    def reset_history(self, new_history: List[Any], rewrite: bool = False):
        """Update internal tracking after a compression event or manual edit.

        If rewrite=True, the log file is truncated and rewritten from scratch.
        Otherwise, we append a compression baseline to the end of the log.
        """
        if rewrite:
            # Close cached handle before overwriting (Fix #1)
            if self._file_handle and not self._file_handle.closed:
                self._file_handle.flush()
                self._file_handle.close()
                self._file_handle = None

            try:
                # 1. Prepare all lines (metadata + history)
                lines = [json.dumps({"metadata": self.data["metadata"]}, ensure_ascii=False) + '\n']
                for msg in new_history:
                    lines.append(json.dumps(self._format_message(msg), ensure_ascii=False) + '\n')

                # 2. Write to file (overwrite)
                with open(self.log_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)

                logger.info(f"Rewrote agent log {self.log_path} with {len(new_history)} messages.")
            except Exception as e:
                logger.error(f"Failed to rewrite agent log {self.log_path}: {e}")
                return False

            # Update internal tracking
            self.data["history"] = [self._format_message(msg) for msg in new_history]
            
            # ARCHITECTURAL FIX: After rewrite=True, both file AND data["history"] ARE in sync.
            # Set _file_history_synced = True to reflect this accurate state. This prevents the
            # next update_history() call from loading from file unnecessarily (which would clear
            # and reload, potentially causing issues). The flag should only be False when we know
            # the internal state is out of sync with the file.
            self._file_history_synced = True
            
            return True

        # Find the summary message in new_history
        summary_msg = None
        idx_in_new = -1
        for i, msg in enumerate(new_history):
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if isinstance(content, str) and "<context_summary>" in content:
                summary_msg = self._format_message(msg)
                idx_in_new = i
                break

        if summary_msg and idx_in_new != -1:
            # Append the compression baseline event marker
            self._append_line({
                "event": "COMPRESSION",
                "timestamp": datetime.datetime.now().isoformat(),
                "message": "Context was compressed. Re-asserting working set baseline."
            })

            # 1. Append the summary message
            self._append_line(summary_msg)

            # 2. Append all messages that follow the summary in the new working set
            for i in range(idx_in_new + 1, len(new_history)):
                self._append_line(self._format_message(new_history[i]))

            logger.info(
                f"Appended summary baseline and {len(new_history) - 1 - idx_in_new} "
                f"messages to agent log {self.log_path}."
            )
        else:
            logger.warning(
                f"Could not find summary marker in new_history for {self.instance_name}. "
                f"No baseline appended."
            )

        # Reset internal tracking to the compressed baseline.
        self.data["history"] = [self._format_message(msg) for msg in new_history]
        return True

    # ── Rollback / truncation ─────────────────────────────────────────────

    def close(self):
        """Flush and close the cached file handle (Fix #1)."""
        if self._file_handle and not self._file_handle.closed:
            try:
                self._file_handle.flush()
                self._file_handle.close()
            except Exception:
                pass  # Best effort flush
            finally:
                self._file_handle = None

    def rollback(self, count: int, soft: bool = False, reason: Optional[str] = None):
        """Rollback the history by popping N messages.

        If soft=False, re-writes the log file (truncates).
        If soft=True, appends a ROLLBACK marker to the log and keeps the file intact.
        """
        if count <= 0:
            return

        # 1. Update physical log file
        if soft:
            marker = {
                "event": "ROLLBACK",
                "timestamp": datetime.datetime.now().isoformat(),
                "message": f"Surgical rollback of {count} messages."
                           f"{' Reason: ' + reason if reason else ''}",
                "rolled_back_count": count
            }
            self._append_line(marker)
        else:
            # Close cached handle before truncating (Fix #1)
            if self._file_handle and not self._file_handle.closed:
                self._file_handle.flush()
                self._file_handle.close()
                self._file_handle = None

            try:
                with open(self.log_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                # Make sure we don't pop the metadata line
                if len(lines) > count + 1:
                    lines = lines[:-count]
                else:
                    lines = [lines[0]] if lines else []

                with open(self.log_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
            except Exception as e:
                logger.error(f"Failed to rollback agent log {self.log_path}: {e}")

        # 2. Pop from internal history tracking
        for _ in range(count):
            if self.data["history"]:
                self.data["history"].pop()
            else:
                break

        # Fix #2: Reset the sync guard flag after file modification (soft=False) so future update_history() calls can properly sync again
        if not soft:
            self._file_history_synced = False
        
        if soft:
            logger.info(f"Soft rollback of {count} messages for {self.instance_name} recorded in log.")

    def truncate_to(self, target_len: int, soft: bool = False, reason: Optional[str] = None):
        """Truncate the history to a specific target length."""
        current_len = len(self.data["history"])
        if target_len >= current_len:
            return
        self.rollback(current_len - target_len, soft=soft, reason=reason)