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
from typing import Dict, List, Set, Optional, Tuple
from datetime import datetime
from agent_cascade.settings import DEFAULT_WORKSPACE


class FileManager:
    """Manages file operations with a permission system."""
    
    def __init__(self, base_dir: str = DEFAULT_WORKSPACE):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(exist_ok=True)
        
        # Track which files belong to which agent
        # Format: {file_path: agent_name}
        self.file_ownership: Dict[str, str] = {}
        
        # Pending write requests awaiting approval
        # Format: {request_id: {'agent': str, 'path': str, 'content': str, 'timestamp': datetime}}
        self.pending_requests: Dict[str, dict] = {}
        
        # Request counter for generating IDs
        self.request_counter = 0
    
    def _resolve_path(self, path: str) -> Path:
        """Resolve a path to be within the base directory (security)."""
        # Prevent path traversal attacks
        resolved = (self.base_dir / path).resolve()
        if not str(resolved).startswith(str(self.base_dir.resolve())):
            raise ValueError(f"Path '{path}' is outside the allowed directory")
        return resolved
    
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
            resolved = self._resolve_path(path)
            
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
                result += "📁 Directories:\n"
                for d in sorted(dirs):
                    result += f"  📂 {d}/\n"
            
            if files:
                result += "\n📄 Files:\n"
                for f in sorted(files):
                    # Show file size
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
            resolved = self._resolve_path(path)
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
            resolved = self._resolve_path(path)
            return resolved.exists() and resolved.is_file()
        except:
            return False
    
    def get_file_owner(self, path: str) -> Optional[str]:
        """Get the owner of a file."""
        try:
            resolved = self._resolve_path(path)
            return self.file_ownership.get(str(resolved))
        except:
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
