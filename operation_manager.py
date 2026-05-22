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
import shutil
import subprocess
import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
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

# Maximum size for spill files (grep/shell output saved to disk). Prevents disk exhaustion.
MAX_SPILL_SIZE = 50 * 1024 * 1024  # 50MB


# ─── Module-level cached helpers (P1-1, P3-1) ─────────────────────────────

@lru_cache(maxsize=256)
def _compile_grep_pattern(pattern: str, *, flags: int = 0):
    """Cache compiled regex patterns for grep to avoid recompiling on each call.
    
    Args:
        pattern: The regex pattern string.
        flags: Optional re.IGNORECASE flag for case-insensitive matching (smart_case).
            Keyword-only to prevent cache key collisions between positional and keyword calls.
    """
    return re.compile(pattern, flags)


@lru_cache(maxsize=512)
def _path_is_contained_cached(path_str: str, container_str: str) -> bool:
    """Cached path containment check using os.path.commonpath().
    
    Prevents sibling-directory escape. Case-insensitive on all platforms.
    Cached to avoid repeated commonpath() calls during file operations.
    """
    try:
        common = os.path.commonpath([path_str, container_str])
        return common.lower() == container_str.lower()
    except ValueError:
        # Different drive letters on Windows (e.g., C:\ vs D:\)
        return False


# Cache tool availability at module level — doesn't change at runtime
_RIPGREP_AVAILABLE = shutil.which('rg') is not None
# On Windows, standard grep may be a Git Bash wrapper that hangs
_GREP_AVAILABLE = (shutil.which('grep') is not None) and (os.name != 'nt')


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
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                agent_backup_dir = backup_base / safe_agent
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

    @staticmethod
    def _path_is_contained(path: Path, container: Path) -> bool:
        """Check if *path* is inside *container* using the cached containment check."""
        return _path_is_contained_cached(str(path), str(container))

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
        if self._path_is_contained(resolved, self.base_dir):
            return resolved

        # 2. Check extra RW folders (allowed for both RO and RW)
        for extra in self.extra_work_folders_rw:
            if self._path_is_contained(resolved, extra):
                return resolved

        # 3. Check extra RO folders (allowed only if mode is "ro")
        if mode == "ro":
            for extra in self.extra_work_folders_ro:
                if self._path_is_contained(resolved, extra):
                    return resolved

        raise ValueError(f"Path '{path}' is outside the allowed {mode.upper()} directories")


    # ─── Read Operations (Free Access) ────────────────────────────────────

    def list_directory(self, path: str = ".") -> str:
        """List contents of a directory using os.scandir() for cached stat info."""
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"
            if not resolved.is_dir():
                return f"Not a directory: {path}"

            result = f"Contents of {path}/ (Absolute path: {resolved}):\n\n"
            dirs = []
            files = []
            with os.scandir(str(resolved)) as it:
                for entry in it:
                    if entry.is_dir():
                        dirs.append(entry.name)
                    else:
                        try:
                            size = entry.stat().st_size  # stat is cached from scandir
                        except Exception:
                            size = None
                        files.append((entry.name, size))

            if dirs:
                result += "Directories:\n"
                for d in sorted(dirs):
                    result += f"  {d}/\n"

            if files:
                result += "\nFiles:\n"
                for fname, size in sorted(files):
                    if size is not None:
                        size_str = f"{size:,} bytes" if size > 1000 else f"{size} bytes"
                    else:
                        size_str = "?"
                    result += f"  {fname} ({size_str})\n"

            if not dirs and not files:
                result += "  (empty directory)"

            return result
        except Exception as e:
            return f"Error listing directory: {str(e)}"

    def read_file(self, path: str, start_line: int = 1, limit: int = 1000) -> str:
        """Read a file. Uses line-by-line iteration for memory efficiency when range is specified."""
        try:
            resolved = self._resolve_path(path, mode="ro")
            if not resolved.exists():
                return f"File not found: {path}"
            if not resolved.is_file():
                return f"Not a file: {path}"

            end_line = start_line + limit - 1
            total_lines = 0
            hit_end = False
            
            with open(resolved, 'r', encoding='utf-8', errors='ignore') as f:
                lines = []
                for line_num, line in enumerate(f, 1):
                    total_lines = line_num
                    if line_num < start_line:
                        continue  # skip to start
                    if line_num > end_line:
                        hit_end = True
                        break     # stop at limit
                    lines.append(line.rstrip('\n'))
            
            # If we didn't hit the limit, total_lines is accurate. Otherwise file is longer.
            if hit_end:
                total_lines_str = f">{total_lines}"
            else:
                total_lines_str = str(total_lines)
            # Format output with line numbers (1-indexed from start_line)
            content = "".join([f"{start_line + i}: {lines[i]}" for i in range(len(lines))])
            header = f"File content ({path}), lines {start_line} to {start_line + len(lines) - 1} of {total_lines_str}:"
            if hit_end:
                header += " [TRUNCATED]"

            return f"{header}\n```\n{content}\n```"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    # ── Subprocess grep fast path (P0-1) ────────────────────────────────────

    def _try_subprocess_grep(self, pattern: str, path: Path, include: str, char_limit: int, timeout: float,
                             exclude: str = "", ignore_vcs: bool = True, context: int = 0, smart_case: bool = True):
        """Fast-path grep using system ripgrep or grep via subprocess.
        
        Returns (results_list, count, was_timed_out) on success, or (None, 0, False) on failure.
        Output format matches Python fallback: "relative_path:line_number: content"
        """
        # Only try subprocess path if at least one tool is available (cached at module level)
        if not _RIPGREP_AVAILABLE and not _GREP_AVAILABLE:
            return None, 0, False
        
        try:
            if _RIPGREP_AVAILABLE:
                # ripgrep command — supports Perl regex, fast recursive search
                cmd = [
                    'rg',
                    '-r',           # recursive
                    '--no-heading', # don't print filename before each match group
                    '-n',           # line numbers
                    '--color', 'never',  # no ANSI color codes
                    '--no-mmap',    # disable mmap — handle binary files gracefully like Python fallback
                ]
                
                # H1: VCS/ignore support — default ripgrep respects .gitignore; disable with --no-ignore
                if not ignore_vcs:
                    cmd.extend(['--no-ignore'])
                
                # H3: Context lines
                if context > 0:
                    cmd.extend(['-C', str(context)])
                
                # M1: Smart case — ripgrep default is smart_case; only add -i when pattern has no uppercase
                # Issue 4: Check for inline flags like (?-i:) that explicitly set case sensitivity
                has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
                if smart_case:
                    # Smart case: only add -i if pattern has no uppercase letters
                    if not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                        cmd.append('-i')
                # else: Not smart case — always case-sensitive (no -i flag)
                
                # H1: Exclude glob pattern
                if exclude:
                    cmd.extend(['--glob', f'!{exclude}'])
                
                cmd.extend([
                    '--glob', include,  # file filter (include pattern)
                    pattern,
                ])
            else:
                # Standard grep — only reached on Unix-like systems (Windows grep may hang)
                cmd = [
                    'grep',
                    '-r',           # recursive
                    '--include=' + include,  # file filter
                    '-n',           # line numbers
                ]
                
                # M1: Smart case for standard grep too
                has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
                if smart_case:
                    # Smart case: only add -i if pattern has no uppercase letters
                    if not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                        cmd.append('-i')
                # else: Not smart case — always case-sensitive (no -i flag)
                
                # H3: Context lines (standard grep supports -C)
                if context > 0:
                    cmd.extend(['-C', str(context)])
                
                # H1: Exclude glob for standard grep
                if exclude:
                    cmd.append('--exclude=' + exclude)
                
                cmd.append(pattern)
            
            result = subprocess.run(
                cmd,
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            if result.returncode == 0:
                lines = result.stdout.split('\n') if result.stdout.strip() else []
                # Convert grep/rg output (file:line:content) to our format (file:line: content)
                formatted = []
                # H3: When context is active, we need to distinguish match lines from context lines.
                # ripgrep uses space prefix for context; standard grep uses "-" in filename part.
                _match_re = re.compile(r'^(.+?):(\d+):(.*)$')  # file:linenum:content
                _ctx_re = re.compile(r'^(.+?)-(\d+)-(.*)$')   # file-linenum-content (std grep context)
                
                for line in lines:
                    if not line:
                        continue
                    # H3: When context is active, ripgrep outputs "---" separators and includes
                    # line numbers for context lines. Standard grep uses "--" as separator.
                    if line == "---" or line == "--":
                        formatted.append("---")  # Normalize both to "---"
                        continue
                    
                    # First try to parse as a match line (file:linenum:content)
                    m = _match_re.match(line)
                    if m:
                        raw_path, linenum, content = m.groups()
                        normalized_path = raw_path.replace('\\', '/')
                        # M3: Don't strip content — preserve whitespace (important for Python/YAML)
                        if _RIPGREP_AVAILABLE and context > 0 and normalized_path.startswith(' '):
                            # Context line from ripgrep (path starts with space)
                            normalized_path = normalized_path[1:]
                            formatted.append(f"{normalized_path}:{linenum}:     {content}")
                        elif context > 0:
                            # Match line in context mode (ripgrep or standard grep)
                            formatted.append(f"{normalized_path}:{linenum}: >>>{content}")
                        else:
                            # No context mode — just normalize
                            formatted.append(f"{normalized_path}:{linenum}: {content}")
                    elif not _RIPGREP_AVAILABLE and context > 0:
                        # Standard grep with context: try to parse as context line (file-linenum-content)
                        c = _ctx_re.match(line)
                        if c:
                            ctx_path, ctx_linenum, ctx_content = c.groups()
                            normalized_ctx_path = ctx_path.replace('\\', '/')
                            formatted.append(f"{normalized_ctx_path}:{ctx_linenum}:     {ctx_content}")
                        else:
                            # Can't parse — keep raw line
                            formatted.append(line)
                    else:
                        formatted.append(line)
                
                # Count only actual match lines, not context lines or separators
                if context > 0:
                    count = sum(1 for l in formatted if ">>>" in l)
                else:
                    count = sum(1 for l in formatted if l != "---")
                
                # If char_limit is set and output exceeds it, truncate within subprocess path too
                if char_limit != -1 and count > 0:
                    output_size = sum(len(l) for l in formatted) + count  # +count for newlines
                    if output_size > char_limit:
                        # Truncate to fit within char_limit
                        byte_budget = char_limit
                        truncated = []
                        for line in formatted:
                            if byte_budget < len(line) + 1:
                                break
                            truncated.append(line)
                            byte_budget -= len(line) + 1
                        formatted = truncated
                        # Recount after truncation (count only match lines)
                        if context > 0:
                            count = sum(1 for l in formatted if ">>>" in l)
                        else:
                            count = sum(1 for l in formatted if l != "---")
                
                return formatted, count, False
            
            # Non-zero return code (e.g., grep returns 1 for no matches) — still valid
            if result.returncode == 1:
                return [], 0, False
                
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # Fall through to Python implementation
        
        return None, 0, False

    def _grep_single_file(self, file_path: Path, pattern: str, char_limit: int,
                          include: str = "*", exclude: str = "", context: int = 0, smart_case: bool = True) -> str:
        """Search a single file for a regex pattern. Used when path is a file instead of directory."""
        # Compute normalized relative path once (used for glob matching and output formatting)
        try:
            normalized_rel_path = str(file_path.relative_to(self.base_dir)).replace('\\', '/')
        except ValueError:
            normalized_rel_path = file_path.name

        # Check include/exclude globs against the relative path (consistent with Python fallback)
        if not fnmatch.fnmatch(normalized_rel_path, include):
            return f"No matches found for pattern '{pattern}' in {file_path.name}"
        if exclude and fnmatch.fnmatch(normalized_rel_path, exclude):
            return f"No matches found for pattern '{pattern}' in {file_path.name} (excluded by {exclude})"

        # Determine case-sensitivity flags
        has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
        if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
            flags = 0  # Case-sensitive (smart case, pattern has uppercase)
        elif smart_case:
            flags = re.IGNORECASE  # Smart case, pattern is all lowercase
        else:
            flags = 0  # Not smart case — always case-sensitive

        try:
            pattern_re = _compile_grep_pattern(pattern, flags=flags)
        except re.error as e:
            return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            return f"Error reading file {file_path.name}: {str(e)}"

        lines = content.split('\n')
        results = []
        match_count = 0
        hit_result_limit = False
        was_timed_out = False
        start_time = time.time()
        timeout = 30.0  # seconds

        if context > 0:
            for line_num, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    match_count += 1
                    start = max(1, line_num - context)
                    end = min(len(lines), line_num + context)
                    for ctx_line in range(start - 1, end):
                        prefix = ">>>" if ctx_line + 1 == line_num else "    "
                        results.append(f"{normalized_rel_path}:{ctx_line + 1}: {prefix}{lines[ctx_line]}")
                    results.append("---")
                # Fix 4: Periodic timeout check inside context mode loop
                if len(results) % 200 == 0 and time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if len(results) > 5000:
                    hit_result_limit = True
                    break
        else:
            for line_num, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    match_count += 1
                    results.append(f"{normalized_rel_path}:{line_num}: {line}")
                # Fix 4: Periodic timeout check inside non-context mode loop
                if len(results) % 500 == 0 and time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if len(results) > 5000:
                    hit_result_limit = True
                    break

        if not results:
            return f"No matches found for pattern '{pattern}' in {file_path.name}"

        summary = f"Found {match_count} matches for '{pattern}'"
        if context > 0:
            summary += f" (with {context} line(s) of context)"
        output_text = '\n'.join(results)

        if was_timed_out:
            summary += f" [TIMED OUT after {int(timeout)}s]"
        elif hit_result_limit:
            summary += " [TRUNCATED at 5000 results]"

        if char_limit != -1 and len(output_text) > char_limit:
            output_text = output_text[:char_limit] + "\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded.]"
            summary += " [TRUNCATED]"

        return f"{summary}:\n\n" + output_text

    def grep(self, pattern: str, path: str = ".", include: str = "*", char_limit: int = 2000, agent_name: str = "unknown",
             exclude: str = "", ignore_vcs: bool = True, context: int = 0, smart_case: bool = True) -> str:
        """Search for text pattern in files.
        
        Args:
            pattern: Regex pattern to search for (Python regex syntax).
            path: Directory to search in (relative to workspace root, default ".").
            include: Glob pattern for files to include (default "*").
            char_limit: Maximum character count before truncation (default 2000, -1 for unlimited).
            agent_name: Name of the calling agent (for logging).
            exclude: Glob pattern for files/directories to exclude (default "").
            ignore_vcs: When True (default), ripgrep respects .gitignore. Set False to search all files.
            context: Number of lines to show before/after each match (default 0, like -C N in grep).
            smart_case: When True (default), case-insensitive unless pattern has uppercase letters.
        
        Uses subprocess-based grep (ripgrep or system grep) as a fast path,
        falling back to pure Python if the subprocess approach fails/times out.
        """
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"

            # Handle file paths — search the single file directly
            if resolved.is_file():
                return self._grep_single_file(resolved, pattern, char_limit, include=include,
                                             exclude=exclude, context=context, smart_case=smart_case)

            # ── Fast path: try subprocess-based grep (ripgrep or system grep) ──
            results, count, was_timed_out = self._try_subprocess_grep(
                pattern=pattern, path=resolved, include=include,
                char_limit=char_limit, timeout=30.0,
                exclude=exclude, ignore_vcs=ignore_vcs, context=context, smart_case=smart_case
            )
            if results is not None:
                # Subprocess grep succeeded — format and return
                if count == 0:
                    # Don't return early — fall through to Python fallback which may find matches
                    # in hidden directories or handle globs differently
                    logger.debug(f"grep: subprocess found no matches for '{pattern}', trying Python fallback")
                else:
                    output_text = '\n'.join(results)
                    summary = f"Found {count} matches for '{pattern}'"
                    if context > 0:
                        summary += f" (with {context} line(s) of context)"
                    if was_timed_out:
                        summary += f" [TIMED OUT after 30s]"

                    # Truncate if needed (no spill file — orchestrator handles its own)
                    if char_limit != -1 and len(output_text) > char_limit:
                        output_text = output_text[:char_limit] + "\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded.]"
                        summary += " [TRUNCATED]"

                    return f"{summary}:\n\n" + output_text

            # ── Slow path: pure Python fallback ──
            logger.debug(f"grep: subprocess fast path unavailable (rg={_RIPGREP_AVAILABLE}, grep={_GREP_AVAILABLE}), falling back to Python")
            results = []
            
            # M1: Smart case — compile with or without IGNORECASE based on pattern content
            # Issue 4: Respect inline regex flags like (?-i:) for explicit case sensitivity
            has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
            if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                flags = 0  # Case-sensitive (smart case, pattern has uppercase)
            elif smart_case:
                flags = re.IGNORECASE  # Smart case, pattern is all lowercase
            else:
                flags = 0  # Not smart case — always case-sensitive
            try:
                pattern_re = _compile_grep_pattern(pattern, flags=flags)
            except re.error as e:
                return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

            start_time = time.time()
            timeout = 30.0  # seconds
            was_timed_out = False
            hit_result_limit = False
            file_count = 0
            match_count = 0  # Track actual matches (not context/separator lines)
            
            # H1: Directories to skip (VCS/build artifacts) in Python fallback
            skip_dirs = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.tox'}
            
            for file_path in resolved.rglob(include):
                if time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if file_path.is_file():
                    # H1: Skip files in VCS/build directories (only when ignore_vcs=True)
                    if ignore_vcs:
                        parts = file_path.relative_to(resolved).parts
                        if any(p in skip_dirs for p in parts):
                            continue
                    # H1: Skip files matching the exclude glob pattern (use fnmatch for ** support)
                    if exclude:
                        try:
                            rel = file_path.relative_to(resolved)
                            if fnmatch.fnmatch(str(rel), exclude):
                                continue
                        except ValueError:
                            pass  # Fallback — can't determine relative path
                    try:
                        content = file_path.read_text(encoding='utf-8', errors='ignore')
                        lines = content.split('\n')
                        
                        if context > 0:
                            # H3: Context lines mode — store extra lines around each match
                            for line_num, line in enumerate(lines, 1):
                                if pattern_re.search(line):
                                    match_count += 1  # Count actual matches
                                    start = max(1, line_num - context)
                                    end = min(len(lines), line_num + context)
                                    for ctx_line in range(start - 1, end):
                                        # >>> prefix on matched line, spaces for context lines
                                        prefix = ">>>" if ctx_line + 1 == line_num else "    "
                                        try:
                                            normalized_rel_path = str(file_path.relative_to(self.base_dir)).replace('\\', '/')
                                        except ValueError:
                                            normalized_rel_path = file_path.name
                                        # M3: Don't strip — preserve whitespace; H2: normalize path separators
                                        results.append(f"{normalized_rel_path}:{ctx_line + 1}: {prefix}{lines[ctx_line]}")
                                    # Separator between context groups
                                    results.append("---")
                                # Issue 6: Periodic timeout check inside context mode loop
                                if len(results) % 200 == 0 and time.time() - start_time > timeout:
                                    was_timed_out = True
                                    break
                                if len(results) > 5000:
                                    hit_result_limit = True
                                    break
                        else:
                            # Standard mode (no context)
                            for line_num, line in enumerate(lines, 1):
                                if pattern_re.search(line):
                                    match_count += 1  # Count actual matches
                                    try:
                                        rel_path = file_path.relative_to(self.base_dir)
                                    except ValueError:
                                        rel_path = file_path.name  # Fallback
                                    normalized_rel_path = str(rel_path).replace('\\', '/')
                                    # M3: Don't strip — preserve whitespace; H2: normalize path separators
                                    results.append(f"{normalized_rel_path}:{line_num}: {line}")
                                # Periodic timeout check inside line loop to prevent single huge files from bypassing it
                                if len(results) % 500 == 0 and time.time() - start_time > timeout:
                                    was_timed_out = True
                                    break
                            if was_timed_out:
                                break
                        
                        file_count += 1  # Count only successfully processed files
                        if len(results) > 5000:  # Safety limit to prevent OOM
                            hit_result_limit = True
                            break
                    except Exception:
                        continue

            # Fix 3: Debug log when Python fallback also finds no matches (subprocess already confirmed)
            if not results and not was_timed_out:
                logger.debug(f"grep: Python fallback also found no matches for '{pattern}' (subprocess already confirmed)")

            if not results:
                if was_timed_out:
                    return f"Search timed out after {int(timeout)}s before finding any matches for '{pattern}'. Narrow your pattern or scope."
                exclude_info = f", excluding {exclude}" if exclude else ""
                return f"No matches found for pattern '{pattern}' in {path}/**/{include}{exclude_info}"

            summary = f"Found {match_count} matches for '{pattern}'"
            if context > 0:
                summary += f" (with {context} line(s) of context)"
            output_text = '\n'.join(results)
            
            if was_timed_out:
                summary += f" [TIMED OUT after {int(timeout)}s]"
                output_text += f"\n\n[TOOL RESPONSE TIMED OUT — Searched {file_count} files before exceeding {int(timeout)} second limit. Narrow your pattern or scope to a specific directory.]"
            elif hit_result_limit:
                summary += " [TRUNCATED at 5000 results]"

            if char_limit != -1 and len(output_text) > char_limit:
                # P0-2: Truncate silently — no spill file (orchestrator handles its own)
                output_text = output_text[:char_limit] + "\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded.]"
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
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / "logs" / "backups" / safe_agent
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
                return f"ERROR: Pattern not found in {path}. The 'old_content' string must exactly match the existing file content character-for-character, including whitespace and indentation, or consider using heuristic match mode."
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
            
            # Auto-align indentation if match_mode is heuristic
            def get_leading_whitespace(s: str) -> str:
                for line in s.splitlines():
                    if line.strip():
                        return line[:len(line) - len(line.lstrip())]
                return ""

            file_indent = get_leading_whitespace(actual_old_content)
            old_indent = get_leading_whitespace(old_content)

            if file_indent != old_indent:
                def get_indent_width(indent_str: str) -> int:
                    width = 0
                    for char in indent_str:
                        if char == '\t':
                            width += 4
                        elif char == ' ':
                            width += 1
                    return width

                file_width = get_indent_width(file_indent)
                old_width = get_indent_width(old_indent)
                delta = file_width - old_width

                indent_char = ' '
                if '\t' in file_indent or (not file_indent and '\t' in old_indent):
                    indent_char = '\t'
                    delta = delta // 4

                adjusted_lines = []
                for line in new_content.splitlines(keepends=True):
                    if not line.strip():
                        adjusted_lines.append(line)
                        continue

                    current_indent = line[:len(line) - len(line.lstrip())]
                    current_width = get_indent_width(current_indent)
                    
                    if indent_char == '\t':
                        current_tabs = current_width // 4
                        new_tabs = max(0, current_tabs + delta)
                        adjusted_lines.append(('\t' * new_tabs) + line.lstrip())
                    else:
                        new_spaces = max(0, current_width + delta)
                        adjusted_lines.append((' ' * new_spaces) + line.lstrip())
                
                new_content = "".join(adjusted_lines)

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
            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            backup_dir = self.base_dir / "logs" / "backups" / safe_agent
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

    @staticmethod
    def _is_safe_readonly_shell_command(command: str) -> bool:
        """
        Check if a shell command is purely read-only (directory listing/search).

        Safe commands: find, dir, ls, tree — without dangerous piggybacking.

        Returns False (requires approval) if the command contains any of:
          - Command chaining: && ; ||
          - Pipes to anything other than safe pager/sort/head/tail/grep
          - Subshell execution: $(...) or backticks
          - Redirections that write: > >>
          - Background processes: & (not inside &&)
          - Any non-read-only commands
        """
        cmd = command.strip()
        if not cmd:
            return False

        # Check for dangerous patterns first (these always require approval)
        # Command chaining with && ; ||
        # We need to be careful: 'find ... -name "*.py" | grep "foo"' is fine, but
        # 'find . ; rm -rf /' is not.
        if '&&' in cmd or ';' in cmd or '||' in cmd:
            return False

        # Subshell execution: $(...) or backticks
        if '$(' in cmd or '`' in cmd:
            return False

        # Redirections that write to files (but not 2>/dev/null for suppressing errors)
        # Match > or >> that are NOT part of /dev/null or NUL patterns
        redirect_match = re.search(r'>[^>]', cmd)
        if redirect_match:
            # Check if it redirects to /dev/null or NUL (which is safe - just discards output)
            redir_target = redirect_match.group(0)
            if '/dev/null' not in redir_target and 'NUL' not in redir_target.upper():
                return False

        # Background processes: & that's not part of && (already checked above)
        # Simple check: if there's a standalone & not preceded by another &
        if re.search(r'(?<!&)&(?!&)', cmd):
            return False

        # Extract the primary command(s) — split on pipe
        pipeline = cmd.split('|')
        
        # Safe secondary commands in a pipe (sorting, filtering, paging)
        SAFE_PIPE_COMMANDS = {
            'grep', 'egrep', 'fgrep', 'head', 'tail', 'sort', 'uniq', 
            'wc', 'cat', 'more', 'less', 'awk', 'sed', 'cut', 'tr',
            'tee', 'xargs', 'comm', 'diff', 'nl', 'rev', 'fold'
        }

        # Safe primary commands (read-only filesystem operations)
        SAFE_PRIMARY_COMMANDS = {
            'find', 'dir', 'ls', 'tree', 'dir', 'directory',
            'vfd', 'where', 'whereis', 'locate', 'which', 'type',
            'pwd', 'stat', 'file', 'du', 'df',
        }

        # Parse the first command in the pipeline (the primary action)
        first_cmd_part = pipeline[0].strip()
        
        # Handle Windows commands with / switches: "dir /s /b"
        # Also handle "cmd /c dir ..." patterns
        words = first_cmd_part.split()
        if not words:
            return False

        primary_cmd = words[0].lower()

        # Check for "cmd /c" or "powershell -c" wrappers — not auto-approvable
        if primary_cmd in ('cmd', 'command.com'):
            # cmd /c could run anything, so reject
            return False
        if primary_cmd in ('powershell', 'pwsh'):
            return False

        # Check if the primary command is a safe read-only command
        if primary_cmd not in SAFE_PRIMARY_COMMANDS:
            return False

        # For "find" commands, check that they don't use -exec with dangerous actions
        if primary_cmd == 'find':
            # find -exec is dangerous if it runs arbitrary commands
            if '-exec' in cmd or '-ok' in cmd:
                return False

        # Check all subsequent pipeline stages are safe
        for i, stage in enumerate(pipeline[1:], 1):
            stage_words = stage.strip().split()
            if not stage_words:
                continue
            stage_cmd = stage_words[0].lower()
            if stage_cmd not in SAFE_PIPE_COMMANDS:
                return False

        return True

    def execute_shell_command(self, command: str, justification: str, agent_name: str, cwd: str = ".", char_limit: int = 2000) -> str:
        """Execute a shell command — auto-approved for safe read-only commands (find, dir, ls), requires user approval for everything else."""
        try:
            resolved_cwd = self._resolve_path(cwd, mode="rw") # shell commands usually need RW for artifacts
        except Exception as e:
            return f"ERROR: Invalid working directory: {str(e)}"

        # Check if this is a safe read-only command that can be auto-approved
        is_safe = self._is_safe_readonly_shell_command(command)
        
        if is_safe:
            # Auto-approve safe read-only commands without user interaction
            approved = True
            reason = "Auto-approved: safe read-only filesystem operation"
            justification_text = reason
        else:
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
                log_dir = self.base_dir / 'logs' / 'spillover'
                log_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                spill_filename = f"{safe_agent}_shell_{timestamp}.txt"
                spill_path = log_dir / spill_filename
                
                try:
                    # Cap spill file to prevent disk exhaustion from massive shell output
                    if len(output) > MAX_SPILL_SIZE:
                        output = output[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"
                    spill_path.write_text(output, encoding='utf-8')
                    try:
                        rel_spill = str(spill_path.relative_to(self.base_dir))
                    except ValueError:
                        rel_spill = str(spill_path)
                except Exception as e:
                    rel_spill = f"ERROR SAVING SPILL: {e}"

                final_output = output[:char_limit] + f"\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. Full output saved to: {rel_spill}]"
                status += " [TRUNCATED]"

            # Format output differently for auto-approved vs user-approved
            if is_safe:
                final_msg = f"AUTO-APPROVED: {status}\n"
            else:
                final_msg = f"APPROVED: {status}\n"
                if justification_text:
                    final_msg += f"Security Justification: {justification_text}\n"
            return final_msg + f"\n{final_output}"
            
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 120 seconds. If the process is expected to take a long time, consider using a background command (e.g. using '&' on linux or 'Start-Job' on windows) or optimizing the task."
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Utilities ────────────────────────────────────────────────────────

    def get_file_owner(self, path: str) -> Optional[str]:
        """Get the owner of a file."""
        return self.file_ownership.get(path)
