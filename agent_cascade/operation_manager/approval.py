"""Approval subsystem — types, constants, and mixin for user-approval operations."""

import uuid
import threading
import time
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Any, Tuple


# ─── Types ────────────────────────────────────────────────────────────────

class OperationType(Enum):
    """Types of operations that can require user approval."""
    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    FILE_DELETE = "file_delete"
    FILE_COPY = "file_copy"
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
    # Extracted justification from tool_args (e.g., shell_cmd passes 'justification')
    justification: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    # Threading primitives for blocking
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    outcome_reason: str = ""


# ─── Constants (imported by api_server.py — must remain importable from package level) ──

# Timeout for security advisor checks (seconds). If the security advisor takes longer
# than this, it is terminated and the operation is auto-rejected to prevent AFK rejection cascades.
SECURITY_ADVISOR_TIMEOUT_SECONDS = 180   # 3 minutes — gives slow models breathing room
SECURITY_ADVISOR_WARNING_SECONDS = 120   # Warn at 2 minutes — agent gets a nudge via message queue


# ─── Mixin: Approval methods for OperationManager ────────────────────────

class ApprovalMixin:
    """Approval-related instance methods. Expects self to have __init__-set attributes."""

    def set_approval_timeout(self, seconds):
        """Set the approval timeout duration in seconds (clamped 10s–2h)."""
        self.approval_timeout_seconds = max(10, min(int(seconds), 7200))

    def set_enable_timeout(self, enabled):
        """Enable or disable approval timeout."""
        self.enable_timeout = bool(enabled)

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

        # Extract justification from tool_args if present (e.g., shell_cmd passes 'justification')
        just = ""
        if isinstance(tool_args, dict):
            just = tool_args.get("justification", "") or ""

        # str() guards against non-string values (e.g., integers from malformed tool_args)
        approval = PendingApproval(
            request_id=request_id,
            agent_name=agent_name,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description,
            justification=str(just),
        )

        with self._lock:
            self.pending[request_id] = approval

        # Block until user responds, timeout, or agent is stopped (FIX 5)
        timeout_val = self.approval_timeout_seconds if self.enable_timeout else 3600
        start_time = time.time()
        got_response = False

        while time.time() - start_time < timeout_val:
            # FIX 5: Check _stopped_event to unblock threads waiting on approval
            # REVIEWER FIX: Use .stopped directly instead of getattr - it's a well-defined property
            if self.agent_pool and self.agent_pool.stopped:
                from agent_cascade.log import logger
                logger.debug(f"[APPROVAL_STOPPED] Approval wait interrupted for {agent_name} due to pool stop")
                break

            # MINOR-3 FIX: Reduce polling from 1s to 0.1s for more responsive stop detection
            if approval.event.wait(timeout=0.1):
                got_response = True
                break

        # Clean up
        with self._lock:
            self.pending.pop(request_id, None)

        if not got_response:
            # Check if we exited due to stop (FIX 5) or timeout
            # REVIEWER FIX: Use .stopped directly instead of getattr
            if self.agent_pool and self.agent_pool.stopped:
                return False, "Session stopped by user"
            else:
                # Timed out - user is AFK
                return False, "User is AFK, try another method if possible"

        if approval.approved:
            return True, approval.outcome_reason
        else:
            return False, approval.outcome_reason or "Rejected by user."

    def user_approve(self, request_id: str, reason: str = "") -> str:
        """Called by WebUI when user clicks Approve."""
        with self._lock:
            approval = self.pending.pop(request_id, None)

        if not approval:
            return f"ERROR: Request '{request_id}' not found or already resolved."

        approval.approved = True
        approval.outcome_reason = reason
        approval.event.set()
        return f"Approved: {request_id}"

    def user_reject(self, request_id: str, reason: str = "") -> str:
        """Called by WebUI when user clicks Reject."""
        with self._lock:
            approval = self.pending.pop(request_id, None)

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
                    'justification': getattr(a, 'justification', ''),
                    'timestamp': a.timestamp,
                }
                for a in self.pending.values()
            ]