"""
Operation Manager - Blocking user-facing approval system for agent operations.

All mutating operations (file write, edit, delete, move, copy, code execution)
require explicit user approval via the WebUI. The tool call blocks (via
threading.Event) until the user clicks Approve or Reject.

Read operations (read_file, list_dir, grep, view_image) are free access.
"""

import json
import os
import re
import uuid
import threading
import time
import difflib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from agent_cascade.settings import DEFAULT_WORKSPACE, DEFAULT_HEURISTIC_MATCH_THRESHOLD
from agent_cascade.log import logger


class OperationType(Enum):
    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    FILE_DELETE = "file_delete"
    FILE_COPY = "file_copy"
    FILE_MOVE = "file_move"
    FILE_REPLACE = "file_replace"
    CODE_EXECUTE = "code_execute"
    EXTERNAL_TOOL = "external_tool"
    CONTEXT_COMPRESSION = "context_compression"
    CUSTOM = "custom"


@dataclass
class PendingApproval:
    """Represents a tool call waiting for user approval."""
    request_id: str
    agent_name: str
    tool_name: str
    tool_args: Dict[str, Any]
    description: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    # Threading primitives for blocking
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    outcome_reason: str = ""


# Timeout for user approval (seconds). Auto-rejects after this.
APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes


class OperationManager:
    """
    Manages blocking user-approval for tool operations.

    When a tool needs approval, it calls request_user_approval() which blocks
    the calling thread until the user responds via the WebUI. The WebUI calls
    user_approve() or user_reject() to unblock the thread.
    """

    def __init__(self, base_dir: str = DEFAULT_WORKSPACE, agent_pool=None):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.agent_pool = agent_pool
        self.extra_work_folders_ro: List[Path] = []
        self.extra_work_folders_rw: List[Path] = []

        # Currently pending approvals (request_id -> PendingApproval)
        self.pending: Dict[str, PendingApproval] = {}

        # Lock for thread-safe access to pending dict
        self._lock = threading.Lock()

        # File ownership tracking (still useful for context in approval UI)
        self.file_ownership: Dict[str, str] = {}
        
        # User toggleable timeout
        self.enable_timeout: bool = True

        import atexit
        atexit.register(self.cleanup_backups)

    def set_base_dir(self, path: str):
        """Update the base workspace directory."""
        new_path = Path(path).resolve()
        if new_path != self.base_dir:
            self.base_dir = new_path
            self.base_dir.mkdir(parents=True, exist_ok=True)
            return True
        return False

    def cleanup_backups(self, agent_name: Optional[str] = None):
        """Clean up backup files for a specific agent, or all agents if None."""
        try:
            import shutil
            backup_base = self.base_dir / 'logs' / 'backups'
            if not backup_base.exists():
                return
            if agent_name:
                agent_backup_dir = backup_base / agent_name
                if agent_backup_dir.exists():
                    shutil.rmtree(agent_backup_dir)
            else:
                shutil.rmtree(backup_base)
        except Exception as e:
            logger.warning("Failed to clean up backups: %s", e)

    def set_extra_work_folders(self, folders_ro: List[str], folders_rw: List[str]):
        """Set extra directories that the agents can access."""
        self.extra_work_folders_ro = []
        for folder in folders_ro:
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                self.extra_work_folders_ro.append(p)
            except Exception as e:
                logger.warning("Failed to resolve extra RO work folder %s: %s", folder, e)

        self.extra_work_folders_rw = []
        for folder in folders_rw:
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                self.extra_work_folders_rw.append(p)
            except Exception as e:
                logger.warning("Failed to resolve extra RW work folder %s: %s", folder, e)
        
        logger.info("[Workspace] Tiered folders updated: RO=%d, RW=%d", len(self.extra_work_folders_ro), len(self.extra_work_folders_rw))

    # ─── Auto-Approval for Agent-Owned Files ──────────────────────────────

    def _is_auto_approved(self, path: str, agent_name: str, creating_new: bool = False) -> bool:
        """
        Check if this operation can skip user approval.
        Auto-approved when:
          - The file was created by this agent during the current session.
          - The agent is creating a brand new file (doesn't exist yet).
        """
        if creating_new:
            resolved = self._resolve_path(path, mode="rw")
            if not resolved.exists():
                return True  # New file — no existing work affected

        resolved = self._resolve_path(path, mode="rw")
        owner = self.file_ownership.get(str(resolved))
        return owner == agent_name

    # ─── Blocking Approval API ────────────────────────────────────────────

    def request_user_approval(
        self,
        agent_name: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        description: str = "",
    ) -> Tuple[bool, str]:
        """
        Block the calling thread until the user approves or rejects.

        Returns:
            (True, "") if approved
            (False, reason) if rejected or timed out
        """
        request_id = f"op_{uuid.uuid4().hex[:8]}"

        approval = PendingApproval(
            request_id=request_id,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description,
        )

        with self._lock:
            self.pending[request_id] = approval

        # Block until user responds, timeout, or agent is stopped
        timeout_val = APPROVAL_TIMEOUT_SECONDS if self.enable_timeout else 3600
        start_time = time.time()
        got_response = False
        
        while time.time() - start_time < timeout_val:
            if self.agent_pool and getattr(self.agent_pool, 'stopped', False):
                break
            
            # Wait in small increments to remain responsive to stopped flag
            if approval.event.wait(timeout=1.0):
                got_response = True
                break

        # Clean up
        with self._lock:
            self.pending.pop(request_id, None)

        if not got_response:
            # Timed out
            return False, "User is AFK, try another method if possible"

        if approval.approved:
            return True, approval.outcome_reason
        else:
            return False, approval.outcome_reason or "Rejected by user."

    def user_approve(self, request_id: str, reason: str = "") -> str:
        """Called by WebUI when user clicks Approve."""
        with self._lock:
            approval = self.pending.get(request_id)
            
        if not approval:
            return f"ERROR: Request '{request_id}' not found or already resolved."
            
        approval.approved = True
        approval.outcome_reason = reason
        approval.event.set()
        return f"Approved: {request_id}"

    def user_reject(self, request_id: str, reason: str = "") -> str:
        """Called by WebUI when user clicks Reject."""
        with self._lock:
            approval = self.pending.get(request_id)

        if not approval:
            return f"ERROR: Request '{request_id}' not found or already resolved."

        approval.approved = False
        approval.outcome_reason = reason or "Rejected by user."
        approval.event.set()
        return f"Rejected: {request_id}"

    def list_pending_approvals(self) -> List[dict]:
        """List all currently pending approvals (for the WebUI to poll)."""
        with self._lock:
            return [
                {
                    'request_id': a.request_id,
                    'agent_name': a.agent_name,
                    'tool_name': a.tool_name,
                    'tool_args': a.tool_args,
                    'description': a.description,
                    'timestamp': a.timestamp,
                }
                for a in self.pending.values()
            ]

    # ─── Path Resolution ──────────────────────────────────────────────────

    def _resolve_path(self, path: str, mode: str = "ro") -> Path:
        """Resolve a path to be within the allowed directories (security)."""
        # Handle virtual /workspace/ prefix
        clean_path = path
        if clean_path.startswith('/workspace/'):
            clean_path = clean_path[len('/workspace/'):]
        elif clean_path.startswith('workspace/'):
            clean_path = clean_path[len('workspace/'):]
        elif clean_path == '/workspace' or clean_path == 'workspace':
            clean_path = '.'
        
        # If the path is already absolute (e.g., an agent passing 
        # "N:\work\WD\AgentCascade" to access an extra work folder), use it directly
        # instead of joining with base_dir — on Windows, Path(base) / abs_path
        # replaces base entirely, which can cause security check mismatches.
        if Path(clean_path).is_absolute():
            resolved = Path(clean_path).resolve()
        else:
            resolved = (self.base_dir / clean_path).resolve()
        
        # 1. Base directory is always RW (and thus RO)
        if str(resolved).startswith(str(self.base_dir)):
            return resolved
        if os.name == 'nt' and str(resolved).lower().startswith(str(self.base_dir).lower()):
            return resolved

        # 2. Check extra RW folders (allowed for both RO and RW)
        for extra in self.extra_work_folders_rw:
            if str(resolved).startswith(str(extra)):
                return resolved
            if os.name == 'nt' and str(resolved).lower().startswith(str(extra).lower()):
                return resolved

        # 3. Check extra RO folders (allowed only if mode is "ro")
        if mode == "ro":
            for extra in self.extra_work_folders_ro:
                if str(resolved).startswith(str(extra)):
                    return resolved
                if os.name == 'nt' and str(resolved).lower().startswith(str(extra).lower()):
                    return resolved

        raise ValueError(f"Path '{path}' is outside the allowed {mode.upper()} directories")


    # ─── Read Operations (Free Access) ────────────────────────────────────

    def list_directory(self, path: str = ".") -> str:
        """List contents of a directory."""
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"
            if not resolved.is_dir():
                return f"Not a directory: {path}"

            result = f"Contents of {path}/ (Absolute path: {resolved}):\n\n"
            dirs = []
            files = []
            for item in resolved.iterdir():
                if item.is_dir():
                    dirs.append(item.name)
                else:
                    files.append(item.name)

            if dirs:
                result += "📁 Directories:\n"
                for d in sorted(dirs):
                    result += f"  📂 {d}/\n"

            if files:
                result += "\n📄 Files:\n"
                for f in sorted(files):
                    try:
                        size = (resolved / f).stat().st_size
                        size_str = f"{size:,} bytes" if size > 1000 else f"{size} bytes"
                    except:
                        size_str = "?"
                    result += f"  📝 {f} ({size_str})\n"

            if not dirs and not files:
                result += "  (empty directory)"

            return result
        except Exception as e:
            return f"Error listing directory: {str(e)}"

    def read_file(self, path: str, start_line: int = 1, limit: int = 1000) -> str:
        """Read a file."""
        try:
            resolved = self._resolve_path(path, mode="ro")
            if not resolved.exists():
                return f"File not found: {path}"
            if not resolved.is_file():
                return f"Not a file: {path}"

            with open(resolved, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, start_idx + limit)

            content = "".join([f"{i+1}: {lines[i]}" for i in range(start_idx, end_idx)])
            header = f"File content ({path}), lines {start_idx+1} to {end_idx} of {total_lines}:"
            if end_idx < total_lines:
                header += " [TRUNCATED]"

            return f"{header}\n```\n{content}\n```"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    def grep(self, pattern: str, path: str = ".", include: str = "*", char_limit: int = 2000, agent_name: str = "unknown") -> str:
        """Search for text pattern in files."""
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"

            results = []
            try:
                pattern_re = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

            start_time = time.time()
            timeout = 30.0  # seconds
            was_timed_out = False
            hit_result_limit = False
            file_count = 0
            for file_path in resolved.rglob(include):
                if time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if file_path.is_file():
                    try:
                        content = file_path.read_text(encoding='utf-8', errors='ignore')
                        lines = content.split('\n')
                        for line_num, line in enumerate(lines, 1):
                            if pattern_re.search(line):
                                try:
                                    rel_path = file_path.relative_to(self.base_dir)
                                except ValueError:
                                    rel_path = file_path.name # Fallback
                                results.append(f"{rel_path}:{line_num}: {line.strip()}")
                            # Periodic timeout check inside line loop to prevent single huge files from bypassing it
                            if len(results) % 500 == 0 and time.time() - start_time > timeout:
                                was_timed_out = True
                                break
                        if was_timed_out:
                            break
                        file_count += 1  # Count only successfully processed files
                        if len(results) > 5000: # Safety limit to prevent OOM
                            hit_result_limit = True
                            break
                    except Exception:
                        continue

            if not results:
                if was_timed_out:
                    return f"Search timed out after {int(timeout)}s before finding any matches for '{pattern}'. Narrow your pattern or scope."
                return f"No matches found for pattern '{pattern}' in {path}/**/{include}"

            summary = f"Found {len(results)} matches for '{pattern}'"
            output_text = '\n'.join(results)
            
            if was_timed_out:
                summary += f" [TIMED OUT after {int(timeout)}s]"
                output_text += f"\n\n[TOOL RESPONSE TIMED OUT — Searched {file_count} files before exceeding {int(timeout)} second limit. Narrow your pattern or scope to a specific directory.]"
            elif hit_result_limit:
                summary += " [TRUNCATED at 5000 results]"

            if char_limit != -1 and len(output_text) > char_limit and not hit_result_limit:
                # Save full result to spill file
                log_dir = self.base_dir / 'logs'
                log_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_agent = agent_name.replace('/', '_').replace('\\', '_')
                spill_filename = f"{safe_agent}_grep_{timestamp}.txt"
                spill_path = log_dir / spill_filename
                
                try:
                    spill_path.write_text(output_text, encoding='utf-8')
                    try:
                        rel_spill = str(spill_path.relative_to(self.base_dir))
                    except ValueError:
                        rel_spill = str(spill_path)
                except Exception as e:
                    rel_spill = f"ERROR SAVING SPILL: {e}"

                output_text = output_text[:char_limit] + f"\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. Full output saved to: {rel_spill}]"
                summary += " [TRUNCATED]"

            return f"{summary}:\n\n" + output_text
        except Exception as e:
            return f"Error searching: {str(e)}"

    # ─── Write Operations (Require User Approval) ─────────────────────────

    def write_file(self, path: str, content: str, agent_name: str) -> str:
        """Write a file — auto-approved for new files and owned files."""
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        is_new = not resolved.exists()

        if not self._is_auto_approved(path, agent_name, creating_new=True):
            description = f"Overwrite existing file: {path} ({len(content)} chars)"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='write_file',
                tool_args={'path': path, 'content': content},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""


        try:
            resolved = self._resolve_path(path, mode="rw")
            
            # Backup if overwriting
            backup_path_str = ""
            if resolved.exists():
                import time, shutil
                backup_dir = self.base_dir / "logs" / "backups" / agent_name
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
                shutil.copy2(resolved, backup_path)
                try:
                    backup_path_str = str(backup_path.relative_to(self.base_dir))
                except ValueError:
                    backup_path_str = str(backup_path)
            
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding='utf-8')
            self.file_ownership[str(resolved)] = agent_name
            msg = f"APPROVED: Created {path} ({len(content)} characters)"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                msg += f". Backup created: {backup_path_str}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    def edit_file(self, path: str, agent_name: str,
                  old_content: str,
                  new_content: str,
                  match_mode: str = 'exact') -> str:
        """Edit a file surgically — auto-approved for agent-owned files."""
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"

        # Validate the surgical edit before asking for approval
        if not resolved.exists():
            return f"File not found for surgical edit: {path}"
        
        file_content = resolved.read_text(encoding='utf-8')
        actual_old_content = old_content
        match_ratio = 1.0

        if match_mode == 'exact':
            count = file_content.count(old_content)
            if count == 0:
                return f"ERROR: Pattern not found in {path}. The 'old_content' string must exactly match the existing file content character-for-character, including whitespace and indentation."
            if count > 1:
                return f"ERROR: Pattern found {count} times in {path}. The 'old_content' block must be unique. Please include more surrounding lines in 'old_content' to make it unique."
        elif match_mode == 'heuristic':
            ext = Path(path).suffix.lower()
            
            def remove_comments_keep_layout(content: str, file_ext: str) -> str:
                is_python_style = file_ext in ['.py', '.sh', '.yaml', '.yml', '.toml', '.ini', '.properties', '.dockerfile', '.conf']
                is_c_style = file_ext in ['.c', '.cpp', '.h', '.hpp', '.java', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.cs', '.php', '.css', '.swift', '.kt', '.scala']
                is_html_style = file_ext in ['.html', '.htm', '.xml', '.svg', '.xhtml']
                is_sql_style = file_ext in ['.sql']

                # No known comment syntax — skip the char-level scan entirely
                if not (is_python_style or is_c_style or is_html_style or is_sql_style):
                    return content

                chars = list(content)
                n = len(chars)
                i = 0
                in_string = None

                while i < n:
                    if in_string:
                        if chars[i] == '\\' and i + 1 < n:
                            i += 2
                            continue
                        if chars[i] == in_string:
                            in_string = None
                        elif len(in_string) == 3 and chars[i:i+3] == list(in_string):
                            in_string = None
                            i += 2
                        i += 1
                        continue

                    if chars[i] in ['"', "'"]:
                        in_string = chars[i]
                        if is_python_style and i + 2 < n and chars[i+1] == in_string and chars[i+2] == in_string:
                            in_string = in_string * 3
                            i += 3
                            continue
                        i += 1
                        continue
                    elif is_c_style and chars[i] == '`':
                        in_string = '`'
                        i += 1
                        continue

                    if is_python_style and chars[i] == '#':
                        while i < n and chars[i] not in ['\n', '\r']:
                            chars[i] = ' '
                            i += 1
                        continue

                    if is_c_style and chars[i] == '/' and i + 1 < n and chars[i+1] == '/':
                        while i < n and chars[i] not in ['\n', '\r']:
                            chars[i] = ' '
                            i += 1
                        continue

                    if (is_c_style or is_sql_style) and chars[i] == '/' and i + 1 < n and chars[i+1] == '*':
                        chars[i] = ' '
                        chars[i+1] = ' '
                        i += 2
                        while i < n:
                            if chars[i] == '*' and i + 1 < n and chars[i+1] == '/':
                                chars[i] = ' '
                                chars[i+1] = ' '
                                i += 2
                                break
                            if chars[i] not in ['\n', '\r']:
                                chars[i] = ' '
                            i += 1
                        continue

                    if is_sql_style and chars[i] == '-' and i + 1 < n and chars[i+1] == '-':
                        while i < n and chars[i] not in ['\n', '\r']:
                            chars[i] = ' '
                            i += 1
                        continue

                    if is_html_style and chars[i] == '<' and i + 3 < n and chars[i+1] == '!' and chars[i+2] == '-' and chars[i+3] == '-':
                        chars[i] = ' '
                        chars[i+1] = ' '
                        chars[i+2] = ' '
                        chars[i+3] = ' '
                        i += 4
                        while i < n:
                            if chars[i] == '-' and i + 2 < n and chars[i+1] == '-' and chars[i+2] == '>':
                                chars[i] = ' '
                                chars[i+1] = ' '
                                chars[i+2] = ' '
                                i += 3
                                break
                            if chars[i] not in ['\n', '\r']:
                                chars[i] = ' '
                            i += 1
                        continue

                    i += 1

                return "".join(chars)

            clean_file_content = remove_comments_keep_layout(file_content, ext)
            clean_old_content = remove_comments_keep_layout(old_content, ext)

            file_lines = file_content.splitlines(keepends=True)
            clean_file_lines = clean_file_content.splitlines(keepends=True)
            clean_old_lines = clean_old_content.splitlines(keepends=True)
            
            # Map normalized clean content
            file_line_info = []
            for idx, line in enumerate(clean_file_lines):
                norm = "".join(line.split())
                if norm:
                    file_line_info.append((idx, norm))
                    
            old_line_info = []
            for line in clean_old_lines:
                norm = "".join(line.split())
                if norm:
                    old_line_info.append(norm)
            
            if not old_line_info:
                return "ERROR: The 'old_content' contains only whitespace and/or comments. Heuristic match mode requires at least some non-whitespace actual content to match."
            
            # Map normalized line to its indices in the filtered file list
            file_line_map = {}
            for list_idx, (orig_idx, norm) in enumerate(file_line_info):
                if norm not in file_line_map:
                    file_line_map[norm] = []
                file_line_map[norm].append(list_idx)
            
            candidates = set()
            n_old_non_empty = len(old_line_info)
            n_file_non_empty = len(file_line_info)
            
            for old_idx, norm in enumerate(old_line_info):
                if norm and norm in file_line_map:
                    if len(file_line_map[norm]) <= 20:
                        for list_idx in file_line_map[norm]:
                            start_list_idx = list_idx - old_idx
                            if 0 <= start_list_idx <= n_file_non_empty - n_old_non_empty:
                                candidates.add(start_list_idx)
            
            if len(candidates) > 100:
                return f"ERROR: Heuristic pattern is too ambiguous (found {len(candidates)} candidate locations). Please include more unique surrounding lines of context."
            
            norm_old_joined = "".join(old_line_info)
            threshold = DEFAULT_HEURISTIC_MATCH_THRESHOLD
            matches = []
            
            for start_list_idx in candidates:
                best_ratio = 0.0
                best_match_info = None
                
                # Check window sizes in file_line_info close to n_old_non_empty
                for size in range(max(1, n_old_non_empty - 2), min(n_file_non_empty - start_list_idx + 1, n_old_non_empty + 3)):
                    candidate_slice = file_line_info[start_list_idx : start_list_idx + size]
                    candidate_norms = [item[1] for item in candidate_slice]
                    norm_candidate_joined = "".join(candidate_norms)
                    
                    ratio = difflib.SequenceMatcher(None, norm_old_joined, norm_candidate_joined).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match_info = {
                            'start_list_idx': start_list_idx,
                            'end_list_idx': start_list_idx + size,
                            'ratio': ratio
                        }
                
                if best_match_info and best_ratio >= threshold:
                    matches.append(best_match_info)
            
            if len(matches) == 0:
                return f"ERROR: Heuristic pattern not found in {path} (threshold={threshold:.0%})."
            if len(matches) > 1:
                return f"ERROR: Heuristic pattern found {len(matches)} times in {path} above the similarity threshold. The pattern must be unique."
            
            # Map back to the original file lines range
            unique_match = matches[0]
            orig_start_idx = file_line_info[unique_match['start_list_idx']][0]
            orig_end_idx = file_line_info[unique_match['end_list_idx'] - 1][0]
            
            actual_old_content = "".join(file_lines[orig_start_idx : orig_end_idx + 1])
            match_ratio = unique_match['ratio']
            
            last_matched_line = file_lines[orig_end_idx]
            has_trailing_newline = last_matched_line.endswith('\n') or last_matched_line.endswith('\r')
            if has_trailing_newline:
                if new_content and not (new_content.endswith('\n') or new_content.endswith('\r')):
                    if last_matched_line.endswith('\r\n'):
                        ending = '\r\n'
                    elif last_matched_line.endswith('\n'):
                        ending = '\n'
                    else:
                        ending = '\r'
                    new_content = new_content + ending
        else:
            return f"ERROR: Invalid match_mode '{match_mode}'."

        description = f"Surgical edit to: {path} (mode: {match_mode})"
        tool_args = {'path': path, 'old_content': old_content, 'new_content': new_content, 'match_mode': match_mode}

        if not self._is_auto_approved(path, agent_name):
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='edit_file',
                tool_args=tool_args,
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""


        try:
            import time, shutil
            resolved = self._resolve_path(path, mode="rw")
            backup_dir = self.base_dir / "logs" / "backups" / agent_name
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
            shutil.copy2(resolved, backup_path)
            try:
                backup_path_str = str(backup_path.relative_to(self.base_dir))
            except ValueError:
                backup_path_str = str(backup_path)

            file_content = resolved.read_text(encoding='utf-8')
            new_file_content = file_content.replace(actual_old_content, new_content, 1)
            resolved.write_text(new_file_content, encoding='utf-8')
            
            self.file_ownership[str(resolved)] = agent_name
            
            res_msg = f"APPROVED: Edited {path}"
            if match_mode == 'heuristic':
                res_msg += f" (Heuristic match similarity: {match_ratio:.1%}). Please check the file to ensure the insertion was applied correctly."
            if justification:
                res_msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                res_msg += f" (Backup saved to: {backup_path_str})"
            return res_msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    def delete_file(self, path: str, agent_name: str) -> str:
        """Delete a file — auto-approved for agent-owned files."""
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not resolved.exists():
            return f"File not found: {path}"

        if not self._is_auto_approved(path, agent_name):
            description = f"Delete: {path}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='delete_file',
                tool_args={'path': path},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""


        try:
            resolved = self._resolve_path(path, mode="rw")
            if resolved.is_dir():
                import shutil
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
            if str(resolved) in self.file_ownership:
                del self.file_ownership[str(resolved)]
            msg = f"APPROVED: Deleted {path}"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    def copy_file(self, source: str, destination: str, agent_name: str) -> str:
        """Copy a file — auto-approved if destination is new or agent-owned."""
        try:
            src_path = self._resolve_path(source, mode="ro")
            dest_path_check = self._resolve_path(destination, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not src_path.exists():
            return f"Source not found: {source}"

        if not self._is_auto_approved(destination, agent_name, creating_new=True):
            description = f"Copy: {source} → {destination}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='copy_file',
                tool_args={'source': source, 'destination': destination},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""


        try:
            dest_path = self._resolve_path(destination, mode="rw")
            import shutil
            if src_path.is_dir():
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
            self.file_ownership[str(dest_path)] = agent_name
            msg = f"APPROVED: Copied {source} to {destination}"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    def move_file(self, source: str, destination: str, agent_name: str) -> str:
        """Move a file — auto-approved if source is agent-owned."""
        try:
            src_path = self._resolve_path(source, mode="rw")
            dest_path_check = self._resolve_path(destination, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not src_path.exists():
            return f"Source not found: {source}"

        if not self._is_auto_approved(source, agent_name):
            description = f"Move: {source} → {destination}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='move_file',
                tool_args={'source': source, 'destination': destination},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""


        try:
            dest_path = self._resolve_path(destination, mode="rw")
            import shutil
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_path, dest_path)
            if str(src_path) in self.file_ownership:
                del self.file_ownership[str(src_path)]
            self.file_ownership[str(dest_path)] = agent_name
            msg = f"APPROVED: Moved {source} to {destination}"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    def execute_shell_command(self, command: str, justification: str, agent_name: str, cwd: str = ".", char_limit: int = 2000) -> str:
        """Execute a shell command — NEVER auto-approved, always requires user approval."""
        try:
            resolved_cwd = self._resolve_path(cwd, mode="rw") # shell commands usually need RW for artifacts
        except Exception as e:
            return f"ERROR: Invalid working directory: {str(e)}"

        description = (
            f"⚠️ **SECURITY WARNING**: This is a host shell command. It can potentially bypass folder restrictions!\n\n"
            f"**CWD**: {resolved_cwd}\n"
            f"**Execute Shell Command**:\n```bash\n{command}\n```\n**Justification**: {justification}"
        )
        
        approved, reason = self.request_user_approval(
            agent_name=agent_name,
            tool_name='shell_cmd',
            tool_args={'command': command, 'justification': justification, 'cwd': cwd},
            description=description,
        )
        
        if not approved:
            return f"REJECTED BY USER: {reason}"
        justification_text = reason
            
        try:
            import subprocess
            
            # Execute the command in the workspace directory
            result = subprocess.run(
                command,
                cwd=str(resolved_cwd),
                shell=True,
                capture_output=True,
                text=True,
                timeout=120  # Prevent hanging indefinitely
            )
            
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr}\n"
                
            if result.returncode == 0:
                status = "Command completed successfully."
            else:
                status = f"Command exited with return code {result.returncode}."
                
            if not output.strip():
                output = "No output produced."
            
            final_output = output
            if char_limit != -1 and len(output) > char_limit:
                # Save full result to spill file
                log_dir = self.base_dir / 'logs'
                log_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_agent = agent_name.replace('/', '_').replace('\\', '_')
                spill_filename = f"{safe_agent}_shell_{timestamp}.txt"
                spill_path = log_dir / spill_filename
                
                try:
                    spill_path.write_text(output, encoding='utf-8')
                    try:
                        rel_spill = str(spill_path.relative_to(self.base_dir))
                    except ValueError:
                        rel_spill = str(spill_path)
                except Exception as e:
                    rel_spill = f"ERROR SAVING SPILL: {e}"

                final_output = output[:char_limit] + f"\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. Full output saved to: {rel_spill}]"
                status += " [TRUNCATED]"

            final_msg = f"APPROVED: {status}\n"
            if justification_text:
                final_msg += f"Security Justification: {justification_text}\n"
            return final_msg + f"\n{final_output}"
            
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 120 seconds. If the process is expected to take a long time, consider using a background command (e.g. using '&' on linux or 'Start-Job' on windows) or optimizing the task."
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Context Compression (still internal, auto-approved) ──────────────

    def apply_context_compression(self, agent_name: str, summary: str, fraction: float, num_to_remove: int = None, agent_obj: Optional[Any] = None):
        """Apply context compression — this is internal so no user approval needed."""
        if not self.agent_pool:
            raise ValueError("agent_pool not connected to OperationManager")
        self.agent_pool._apply_context_compression(agent_name, summary, fraction, num_to_remove=num_to_remove, agent_obj=agent_obj)

    # ─── Utilities ────────────────────────────────────────────────────────

    def get_file_owner(self, path: str) -> Optional[str]:
        """Get the owner of a file."""
        return self.file_ownership.get(path)
