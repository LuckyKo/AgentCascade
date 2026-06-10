"""
File Manager - Handles file operations with permission system
- Reads: Free access
- Writes to new files: Requires manager approval
- Edits to agent's own files: Auto-approved
- CLI operations: list_dir, grep, delete, etc.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple, Literal
from datetime import datetime
from agent_cascade.settings import DEFAULT_WORKSPACE

# Import the cached path containment check from operation_manager for unified security
from operation_manager import _path_is_contained_cached


class FileManager:
    """Manages file operations with a permission system."""
    
    def __init__(self, base_dir: str = DEFAULT_WORKSPACE):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(exist_ok=True)
        
        # Extra work folders (mirrors OperationManager's tiered access system)
        # Tier 1 (RO): Read-only extra directories
        # Tier 2 (RW): Read-write extra directories (base_dir is always RW)
        self.extra_work_folders_ro: List[Path] = []
        self.extra_work_folders_rw: List[Path] = []
        
        # Track which files belong to which agent
        # Format: {file_path: agent_name}
        self.file_ownership: Dict[str, str] = {}
        
        # Pending write requests awaiting approval
        # Format: {request_id: {'agent': str, 'path': str, 'content': str, 'timestamp': datetime}}
        self.pending_requests: Dict[str, dict] = {}
        
        # Request counter for generating IDs
        self.request_counter = 0
    
    def set_extra_work_folders(self, folders_ro: List[str], folders_rw: List[str]):
        """
        Set extra directories that agents can access.
        
        This mirrors OperationManager's set_extra_work_folders for unified path resolution.
        
        Args:
            folders_ro: List of read-only directory paths (Tier 1)
            folders_rw: List of read-write directory paths (Tier 2)
        """
        # Clear and rebuild RO folders list
        self.extra_work_folders_ro = []
        for folder in folders_ro:
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                self.extra_work_folders_ro.append(p)
            except Exception as e:
                # Silently skip invalid paths (can add logging if needed)
                pass
        
        # Clear and rebuild RW folders list
        self.extra_work_folders_rw = []
        for folder in folders_rw:
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                self.extra_work_folders_rw.append(p)
            except Exception as e:
                # Silently skip invalid paths (can add logging if needed)
                pass
    
    def _path_is_contained(self, path: Path, container: Path) -> bool:
        """Check if *path* is inside *container* using the cached containment check."""
        return _path_is_contained_cached(str(path), str(container))
    
    def _resolve_path(self, path: str, mode: Literal["ro", "rw"] = "ro") -> Path:
        """
        Resolve a path to be within the allowed directories (security).
        
        This method implements unified path resolution matching operation_manager's logic.
        It supports tiered access (base_dir is RW, extra folders can be RO or RW),
        virtual /workspace/ prefix handling for Docker containers, and proper containment checks.
        
        Args:
            path: The path string to resolve (can be relative, absolute, or have /workspace/ prefix)
            mode: "ro" for read-only access (allows Tier 1 + Tier 2), 
                  "rw" for write access (requires Tier 2 only)
                  
        Returns:
            The resolved Path object (absolute path)
            
        Raises:
            ValueError: If the path is outside allowed directories for the given mode
            
        Security Tiers (matched in priority order):
            - Tier 2 (RW): Path contained in base_dir or any extra_work_folders_rw
            - Tier 1 (RO): Path contained in extra_work_folders_ro (mode must be "ro")
            - Tier 0: Path not contained in any allowed directory
        """
        # Validate mode parameter
        if mode not in ("ro", "rw"):
            raise ValueError(f"Invalid mode '{mode}': must be 'ro' or 'rw'")
        
        # Handle virtual /workspace/ prefix (Docker container convention)
        clean_path = path
        
        # Normalize double slashes before prefix stripping to avoid edge cases
        # e.g., "workspace//test.txt" should not become "/test.txt"
        while "//" in clean_path:
            clean_path = clean_path.replace("//", "/")
        
        if clean_path.startswith('/workspace/'):
            clean_path = clean_path[len('/workspace/'):]
        elif clean_path.startswith('workspace/'):
            clean_path = clean_path[len('workspace/'):]
        elif clean_path == '/workspace' or clean_path == 'workspace':
            clean_path = '.'
        
        # If the path is already absolute, use it directly instead of joining with base_dir
        # On Windows, Path(base) / abs_path replaces base entirely
        if Path(clean_path).is_absolute():
            resolved = Path(clean_path).resolve()
        else:
            resolved = (self.base_dir / clean_path).resolve()
        
        # Determine the security tier
        
        # Tier 2: Base directory is always RW (and thus RO)
        if self._path_is_contained(resolved, self.base_dir):
            return resolved
        
        # Tier 2: Check extra RW folders (allowed for both RO and RW modes)
        for extra in self.extra_work_folders_rw:
            if self._path_is_contained(resolved, extra):
                return resolved
        
        # Tier 1: Check extra RO folders (allowed only if mode is "ro")
        if mode == "ro":
            for extra in self.extra_work_folders_ro:
                if self._path_is_contained(resolved, extra):
                    return resolved
        
        # Tier 0: Path is outside all allowed directories
        # Build a helpful error message listing permitted directories
        allowed = [str(self.base_dir)] + [str(f) for f in self.extra_work_folders_rw]
        if mode == "ro":
            allowed.extend(str(f) for f in self.extra_work_folders_ro)
        raise ValueError(
            f"Path '{path}' is outside the allowed {mode.upper()} directories. "
            f"Permitted: {allowed}"
        )
    
    def read_file(self, path: str, agent_name: str = None) -> Tuple[bool, str]:
        """
        Read a file (free access - no approval needed).
        
        Returns:
            (success: bool, content_or_error: str)
        """
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return False, f"File not found: {path}"
            
            content = resolved.read_text(encoding='utf-8')
            return True, content
        except Exception as e:
            return False, f"Error reading file: {str(e)}"
    
    def request_write(self, path: str, content: str, agent_name: str, is_edit: bool = False) -> str:
        """
        Request to write/edit a file (requires manager approval for others' files).
        
        Args:
            is_edit: If True, this is explicitly an edit operation (clearer intent)
        
        Returns:
            request_id if approval needed, or success message if auto-approved
        """
        try:
            resolved = self._resolve_path(path, mode="rw")
            
            # Check if file exists and who owns it
            file_key = str(resolved)
            existing_owner = self.file_ownership.get(file_key)
            file_exists = resolved.exists()
            
            # Auto-approve if:
            # 1. Agent owns the file already, OR
            # 2. File doesn't exist yet (new file in workspace is OK)
            if existing_owner == agent_name or not file_exists:
                # Auto-approve: write directly
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(content, encoding='utf-8')
                self.file_ownership[file_key] = agent_name
                action = "edited" if (is_edit and file_exists) else "created"
                return f"AUTO_APPROVED: Successfully {action} {path} ({len(content)} characters)"
            
            # Needs approval - create a pending request
            self.request_counter += 1
            request_id = f"req_{self.request_counter}"
            
            self.pending_requests[request_id] = {
                'agent': agent_name,
                'path': path,
                'content': content,
                'timestamp': datetime.now().isoformat(),
                'resolved_path': str(resolved),
                'is_edit': is_edit and file_exists,
            }
            
            action = "edit" if (is_edit and file_exists) else "write to"
            return f"PENDING_APPROVAL: Request ID {request_id}. File '{path}' exists and is owned by '{existing_owner}'. Awaiting manager approval."
        
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def list_directory(self, path: str = ".", agent_name: str = None) -> str:
        """List contents of a directory."""
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"
            if not resolved.is_dir():
                return f"Not a directory: {path}"
            
            result = f"Contents of {path}/:\n\n"
            
            # List directories first
            dirs = []
            files = []
            for item in resolved.iterdir():
                if item.is_dir():
                    dirs.append(item.name)
                else:
                    files.append(item.name)
            
            if dirs:
                result += "Directories:\n"
                for d in sorted(dirs):
                    result += f"  [dir] {d}\n"
            
            if files:
                result += "\nFiles:\n"
                for f in sorted(files):
                    # Show file size
                    try:
                        size = (resolved / f).stat().st_size
                        size_str = f"{size:,} bytes" if size > 1000 else f"{size} bytes"
                    except:
                        size_str = "?"
                    result += f"  [file] {f} ({size_str})\n"
            
            if not dirs and not files:
                result += "  (empty directory)"
            
            return result
        except Exception as e:
            return f"Error listing directory: {str(e)}"
    
    def grep(self, pattern: str, path: str = ".", include_pattern: str = "*", agent_name: str = None) -> str:
        """
        Search for a pattern in files (like grep).
        
        Args:
            pattern: Regex or text pattern to search for
            path: Directory to search in
            include_pattern: Glob pattern for which files to search (e.g., "*.py")
        """
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"
            
            results = []
            pattern_re = re.compile(pattern, re.IGNORECASE)
            
            for file_path in resolved.rglob(include_pattern):
                if file_path.is_file():
                    try:
                        content = file_path.read_text(encoding='utf-8')
                        lines = content.split('\n')
                        
                        for line_num, line in enumerate(lines, 1):
                            if pattern_re.search(line):
                                rel_path = file_path.relative_to(self.base_dir)
                                results.append(f"{rel_path}:{line_num}: {line.strip()}")
                    except:
                        continue
            
            if not results:
                return f"No matches found for pattern '{pattern}' in {path}/**/{include_pattern}"
            
            return f"Found {len(results)} matches for '{pattern}':\n\n" + '\n'.join(results[:50])  # Limit to 50 results
        
        except Exception as e:
            return f"Error searching: {str(e)}"
    
    def delete_file(self, path: str, agent_name: str) -> str:
        """Delete a file (only if agent owns it, or needs manager approval)."""
        try:
            resolved = self._resolve_path(path, mode="rw")
            if not resolved.exists():
                return f"File not found: {path}"
            
            file_key = str(resolved)
            owner = self.file_ownership.get(file_key)
            
            # Can delete if:
            # 1. Agent owns the file, OR
            # 2. File has no owner (wasn't created by an agent)
            if owner == agent_name or owner is None:
                resolved.unlink()
                if file_key in self.file_ownership:
                    del self.file_ownership[file_key]
                return f"Deleted: {path}"
            
            # Needs approval
            self.request_counter += 1
            request_id = f"req_{self.request_counter}"
            
            self.pending_requests[request_id] = {
                'agent': agent_name,
                'path': path,
                'action': 'delete',
                'timestamp': datetime.now().isoformat(),
                'resolved_path': str(resolved),
            }
            
            return f"PENDING_APPROVAL: Request ID {request_id}. File '{path}' is owned by '{owner}'. Awaiting manager approval for deletion."
        
        except Exception as e:
            return f"Error deleting file: {str(e)}"
    
    def file_exists(self, path: str) -> bool:
        """Check if a file exists."""
        try:
            resolved = self._resolve_path(path, mode="ro")
            return resolved.exists() and resolved.is_file()
        except Exception:
            return False
    
    def get_file_owner(self, path: str) -> Optional[str]:
        """Get the owner of a file."""
        try:
            resolved = self._resolve_path(path, mode="ro")
            return self.file_ownership.get(str(resolved))
        except Exception:
            return None
    
    def approve_request(self, request_id: str, approver_name: str) -> str:
        """
        Approve a pending write/delete request.
        
        Returns:
            Success or error message
        """
        if request_id not in self.pending_requests:
            return f"ERROR: Request ID '{request_id}' not found"
        
        request = self.pending_requests.pop(request_id)
        
        try:
            resolved = Path(request['resolved_path'])
            
            if request.get('action') == 'delete':
                # Delete operation
                resolved.unlink()
                file_key = str(resolved)
                if file_key in self.file_ownership:
                    del self.file_ownership[file_key]
                return f"APPROVED: Manager '{approver_name}' approved deletion of {request['path']} by {request['agent']}"
            else:
                # Write operation
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(request['content'], encoding='utf-8')
                self.file_ownership[str(resolved)] = request['agent']
                
                action = "edit" if request.get('is_edit') else "write to"
                return f"APPROVED: Manager '{approver_name}' approved {action} {request['path']} by {request['agent']}"
        
        except Exception as e:
            return f"ERROR: Failed to execute request: {str(e)}"
    
    def reject_request(self, request_id: str, approver_name: str) -> str:
        """Reject a pending write/delete request."""
        if request_id not in self.pending_requests:
            return f"ERROR: Request ID '{request_id}' not found"
        
        request = self.pending_requests.pop(request_id)
        action = request.get('action', 'write to')
        return f"REJECTED: Manager '{approver_name}' rejected {action} {request['path']} by {request['agent']}"
    
    def list_pending_requests(self) -> List[dict]:
        """List all pending write/delete requests."""
        return [
            {
                'request_id': req_id,
                'agent': req['agent'],
                'path': req['path'],
                'action': req.get('action', 'write'),
                'timestamp': req['timestamp'],
            }
            for req_id, req in self.pending_requests.items()
        ]
    
    def list_agent_files(self, agent_name: str) -> List[str]:
        """List all files owned by an agent."""
        return [
            path for path, owner in self.file_ownership.items()
            if owner == agent_name
        ]
    
    def get_file_info(self) -> dict:
        """Get information about file ownership and pending requests."""
        return {
            'file_ownership': dict(self.file_ownership),
            'pending_requests_count': len(self.pending_requests),
            'pending_requests': self.list_pending_requests(),
        }
