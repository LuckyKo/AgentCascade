"""
Agent Instance Logger — Persistent JSONL logging for agent sessions.

Each agent instance gets its own timestamped log file that records
metadata and all messages (including tool calls and results).
"""

import copy
import json
import os
import datetime
from typing import Any, Dict, List, Optional, Union

from agent_cascade.log import logger


class AgentInstanceLogger:
    """Handles persistent logging for an agent instance."""
    
    def __init__(self, agent_class: str, instance_name: str, log_dir: str, base_metadata: Optional[Dict] = None):
        self.agent_class = agent_class
        self.instance_name = instance_name
        self.start_time = datetime.datetime.now()
        
        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        filename = f"{agent_class}_{instance_name}_{timestamp}.jsonl"
        self.log_path = os.path.join(log_dir, filename)
        
        self.data = {
            "metadata": {
                "agent_class": agent_class,
                "instance_name": instance_name,
                "start_timestamp": self.start_time.isoformat(),
                "current_log_path": self.log_path,
            },
            "history": []
        }
        
        # Merge base metadata if provided (e.g. from a loaded session)
        if base_metadata:
            for k, v in base_metadata.items():
                if k not in self.data["metadata"]:
                    self.data["metadata"][k] = v
                elif k == "original_log_path":
                     # Carry over origin if it exists, or set it if we're the first continuation
                     self.data["metadata"][k] = v
            # If we don't have an original_log_path yet and we are continuing, set it
            if "original_log_path" not in self.data["metadata"] and "current_log_path" in base_metadata:
                self.data["metadata"]["original_log_path"] = base_metadata["current_log_path"]
        self._initial_save()

    def _format_message(self, message: Union[Dict, Any]) -> Dict:
        """Ensure message is a dict and has a timestamp."""
        if hasattr(message, 'model_dump'):  # For Pydantic-based Message
            msg_dict = message.model_dump()
        elif hasattr(message, 'to_dict'):
            msg_dict = message.to_dict()
        elif isinstance(message, dict):
            msg_dict = copy.deepcopy(message)
        else:
            # Fallback for generic objects or Message dataclass
            msg_dict = {}
            for k in ['role', 'content', 'name', 'function_call', 'extra']:
                if hasattr(message, k):
                    val = getattr(message, k)
                    if val is not None:
                        msg_dict[k] = val
            if not msg_dict and isinstance(message, str):
                msg_dict = {'role': 'unknown', 'content': message}
        
        # Add timestamp if missing
        if 'timestamp' not in msg_dict:
            msg_dict['timestamp'] = datetime.datetime.now().isoformat()
        
        return msg_dict

    def _append_line(self, data: Dict):
        """Append a single JSON line to the log file."""
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"Failed to append to agent log {self.log_path}: {e}")

    def _initial_save(self):
        """Write metadata as the first line."""
        self._append_line({"metadata": self.data["metadata"]})

    def log_message(self, message: Any):
        """Append a single message to history and file."""
        formatted_msg = self._format_message(message)
        self.data["history"].append(formatted_msg)
        self._append_line(formatted_msg)

    def update_history(self, history: List[Any]):
        """
        Additive sync for persistent logs (JSONL). 
        Only appends new messages found in `history` that aren't in the log yet.
        """
        old_history = self.data["history"]
        last_match_idx = -1  # Index in old_history
        
        for msg in history:
            formatted = self._format_message(msg)
            
            # Look for this message in the log AFTER the last matched message
            found = False
            # Check a reasonable range to find a match (e.g. up to 10 messages ahead)
            start_search = last_match_idx + 1
            for j in range(start_search, len(old_history)):
                potential_match = old_history[j]
                if potential_match.get('role') == formatted.get('role') and \
                   potential_match.get('content') == formatted.get('content'):
                    # Found a match!
                    last_match_idx = j
                    found = True
                    break
            
            if not found:
                # This is a truly new message — append it!
                old_history.append(formatted)
                last_match_idx = len(old_history) - 1
                self._append_line(formatted)

    def reset_history(self, new_history: List[Any]):
        """
        Update internal tracking after a compression event.
        
        We insert the compressed summary back into the persistent log file
        at the same point it appears in the cached message queue (new_history).
        """
        import datetime as _dt
        
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
            # We append the summary AND the remaining messages to the log file.
            # This ensures that on load, load_session_from_log finds the summary 
            # and takes all subsequent messages as the 'rest of log' working set.
            # The original messages are still preserved earlier in the log.
            self._append_line({
                "event": "COMPRESSION",
                "timestamp": _dt.datetime.now().isoformat(),
                "message": "Context was compressed. Re-asserting working set baseline."
            })
            
            # 1. Append the summary message
            self._append_line(summary_msg)
            
            # 2. Append all messages that follow the summary in the new working set
            # (These were already in the log, but we re-append them so they are 
            # found after the latest summary marker on load)
            for i in range(idx_in_new + 1, len(new_history)):
                self._append_line(self._format_message(new_history[i]))
                
            logger.info(f"Appended summary baseline and {len(new_history)-1-idx_in_new} messages to agent log {self.log_path}.")
        else:
            logger.warning(f"Could not find summary marker in new_history for {self.instance_name}. No baseline appended.")
        
        # Reset internal tracking to the compressed baseline.
        # This is critical: update_history() does sequential matching against
        # self.data["history"]. After compression the in-memory history changed,
        # so we must update our tracking to match, otherwise it will re-append
        # all the remaining messages as "new".
        self.data["history"] = [self._format_message(msg) for msg in new_history]

    def rollback(self, count: int, soft: bool = False, reason: Optional[str] = None):
        """
        Rollback the history by popping N messages.
        If soft=False, re-writes the log file (truncates).
        If soft=True, appends a ROLLBACK marker to the log and keeps the file intact.
        """
        if count <= 0:
            return
        
        # 1. Update physical log file
        if soft:
            # Append a marker to the log file instead of truncating
            marker = {
                "event": "ROLLBACK",
                "timestamp": datetime.datetime.now().isoformat(),
                "message": f"Surgical rollback of {count} messages.{f' Reason: {reason}' if reason else ''}",
                "rolled_back_count": count
            }
            self._append_line(marker)
        else:
            # Remove lines from the end of the physical log file
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
        
        if soft:
            logger.info(f"Soft rollback of {count} messages for {self.instance_name} recorded in log.")

    def truncate_to(self, target_len: int, soft: bool = False, reason: Optional[str] = None):
        """Truncate the history to a specific target length."""
        current_len = len(self.data["history"])
        if target_len >= current_len:
            return
        self.rollback(current_len - target_len, soft=soft, reason=reason)
