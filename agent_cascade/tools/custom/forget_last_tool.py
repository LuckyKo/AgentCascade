"""ForgetLast tool - retroactively truncates previous tool call responses."""

import logging
from typing import Union

from agent_cascade.tools.base import BaseTool
from agent_cascade.settings import DEFAULT_FORGET_LAST_TRUNCATE_MAX_CHARS, DEFAULT_FORGET_LAST_MIN_CHAR_LIMIT
from agent_cascade.compression import rebuild_working_set

logger = logging.getLogger(__name__)


class ForgetLast(BaseTool):
    """Truncates the output of the last N tool call responses in the agent's conversation history.
    
    This is useful when a tool read too much data (e.g., a large file) and the output
    is consuming context with mostly useless information. Rather than compressing,
    this retroactively shortens the stored content to a configurable maximum length.
    
    Affects both the in-memory agent pool and the JSONL log file.
    
    NOTE: If the log write fails after in-memory truncation succeeds, the pool and log
    will be temporarily inconsistent until the next sync cycle. This is a known limitation
    of the fail-open design.
    """
    
    name = 'forget_last'
    description = (
        "Retroactively truncate the output of the last N tool call responses in the "
        "active conversation history. Each truncated response is shortened to "
        f"{DEFAULT_FORGET_LAST_TRUNCATE_MAX_CHARS} characters max, with a marker indicating "
        f"truncation. Responses already ≤ {DEFAULT_FORGET_LAST_MIN_CHAR_LIMIT} chars are skipped as they're short enough. "
        "An optional justification parameter appends a reason to the truncation marker for context awareness. "
        "This frees up context space without losing the fact that the tool was called. "
        "Affects both the in-memory pool and the log file."
    )
    
    parameters = {
        'type': 'object',
        'properties': {
            'count': {
                'type': 'integer',
                'description': (
                    'Number of recent tool call responses to truncate. '
                    'Counts backwards from the most recent function result, '
                    'skipping non-function messages. Default is 1.'
                ),
                'minimum': 1,
                'maximum': 100,  # Guardrail against runaway truncation
                'default': 1,
            },
            'justification': {
                'type': 'string',
                'description': (
                    'Optional reason for truncation. Appended to the truncation marker '
                    'for context awareness.'
                ),
            },
        },
        'required': [],
    }
    
    def __init__(self, cfg: dict = None):
        super().__init__(cfg)
        self.agent_pool = None  # Injected at registration time
        self.agent_name = ''    # Injected at registration time
    
    def _get_role(self, msg) -> str:
        """Extract role from a message (handles both dict and Message object)."""
        return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', None)
    
    def _get_content(self, msg):
        """Extract content from a message."""
        return msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
    
    def _set_content(self, msg, new_content):
        """Set content on a message in-place."""
        if isinstance(msg, dict):
            msg['content'] = new_content
        else:
            msg.content = new_content
    
    def call(self, params: Union[str, dict], **kwargs) -> str:
        if isinstance(params, str):
            try:
                import json
                params = json.loads(params) if params.strip() else {}
            except json.JSONDecodeError:
                params = {}
        
        # Defensive guard against n <= 0 (even though schema validates this)
        n = max(1, int(params.get('count', 1)))
        justification = params.get('justification', '')
        max_chars = self.cfg.get('truncate_max_chars', DEFAULT_FORGET_LAST_TRUNCATE_MAX_CHARS)
        min_char_limit = self.cfg.get('min_char_limit', DEFAULT_FORGET_LAST_MIN_CHAR_LIMIT)
        
        # Resolve agent_name using multi-source fallback (same pattern as CompressContext)
        agent_obj = kwargs.get('agent_obj')
        agent_name = (
            kwargs.get('agent_instance_name') or
            (getattr(agent_obj, 'instance_name', None) if agent_obj else None) or
            self.agent_name or
            ''  # Fail-fast: prefer no guess over wrong agent (unlike CompressContext which defaults to 'orchestrator')
        )
        
        if not self.agent_pool or not agent_name:
            return "Error: ForgetLast tool requires agent_pool and agent_name to be set."
        
        # Get the conversation history (returns a copy — we need to write back)
        history = self.agent_pool.get_conversation(agent_name)
        if not history:
            return "Error: No conversation history found."
        
        # Identify function result messages to truncate (counting backwards)
        indices_to_truncate = []
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            if self._get_role(msg) == 'function':
                indices_to_truncate.append(i)
                if len(indices_to_truncate) >= n:
                    break
        
        # Reverse so we process in forward order (index stability)
        indices_to_truncate.reverse()
        
        if not indices_to_truncate:
            return f"No tool responses found (searched {n})."
        
        # Collect tool names before truncation (for the report)
        tool_names = []
        for idx in indices_to_truncate:
            msg = history[idx]
            name = msg.get('name') if isinstance(msg, dict) else getattr(msg, 'name', None)
            if name:
                tool_names.append(name)
        
        # Truncate content of identified messages (in-place mutation on the copy)
        truncated_count = 0
        total_chars_saved = 0
        
        for idx in indices_to_truncate:
            msg = history[idx]
            original_content = self._get_content(msg)
            
            if not isinstance(original_content, str):
                original_content = str(original_content)
            
            original_len = len(original_content)
            
            if original_len <= min_char_limit:
                continue  # Already short enough, no point truncating small messages
            
            # Build truncation marker (conditional justification for context awareness)
            chars_trimmed = original_len - max_chars
            if justification:
                marker = f" ... [TRUNCATED] Forgotten because {justification}. Trimmed ~{chars_trimmed} chars."
            else:
                marker = f" ... [TRUNCATED] Forget_last trimmed ~{chars_trimmed} characters."
            new_content = original_content[:max_chars] + marker
            
            # Update in-place in the history list
            self._set_content(msg, new_content)
            
            truncated_count += 1
            total_chars_saved += (original_len - len(new_content))
        
        if truncated_count == 0:
            return f"All {len(indices_to_truncate)} response(s) ≤ {min_char_limit} chars. Nothing to truncate."
        
        # Sync truncated messages back to the pool (get_conversation returns a shallow copy,
        # so in-place mutations on history[] already affect inst.conversation — just invalidate caches)
        inst = self.agent_pool.get_instance(agent_name)
        if inst:
            with inst._compression_lock:
                # Invalidate token count cache — content changed
                inst._cached_token_count = 0
                inst._last_token_count_conversation_length = -1
        
        # Sync to log file using update_history (lightweight timestamp-based sync)
        if agent_name in self.agent_pool.instance_loggers:
            logger_inst = self.agent_pool.instance_loggers[agent_name]
            try:
                logger_inst.update_history(history)
        
            except Exception as e:
                return f"Truncated {truncated_count}/{n} [-{total_chars_saved} chars]. Log sync error: {e}"
        
        # Sync caller's working set (same pattern as CompressContext)
        if 'messages' in kwargs:
            rebuild_working_set(kwargs['messages'], self.agent_pool, agent_name)
        
        logger.info(
            f"ForgetLast[{agent_name}]: truncated {truncated_count}/{n} responses, "
            f"~{total_chars_saved} chars freed. Tools: {', '.join(tool_names)}"
        )
        
        # Build success message
        return f"Truncated {truncated_count}/{n} response(s) [-{total_chars_saved} chars]. Tools: {', '.join(tool_names) or '<none>'}"