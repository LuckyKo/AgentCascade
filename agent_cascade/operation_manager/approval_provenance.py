"""Shared provenance label for approver-supplied reasons in approval result messages (BUG_0039).

Single source of truth so the five emitter sites in file_operations.py and the
loop-detect normalization regex in tool_loop_detect.py can never drift apart.
"""

#: Label line emitted after "Security Justification: <caller>" when the security
#: approver supplied its own reason. The label is deliberately explicit about
#: provenance — the text that follows was NOT authored by the caller.
APPROVER_REASON_LABEL = 'Auto-Approval Reason (approver-supplied, not caller-authored):'
