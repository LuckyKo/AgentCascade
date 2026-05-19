"""
Agent Orchestrator — OrchestratorAgent and sub-agent streaming.

This module contains only the OrchestratorAgent class (the supervisor that
intercepts sub-agent tool calls as streaming generators) and its supporting
schemas / utilities.

Extracted modules:
- agent_pool.py      — AgentPool (agent lifecycle + conversation persistence)
- agent_factory.py   — Tool registration + agent loading
- agent_logger.py    — AgentInstanceLogger (JSONL session logs)

Backward-compatible re-exports are at the bottom of this file.
"""

import copy
import json
import datetime
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import copy
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import (
    Any, Dict, Iterator, List, Optional, Tuple, Union,
)

from agent_cascade.agents import Assistant
from agent_cascade.agent import strip_thinking_blocks
from agent_cascade.utils.thinking_block import (
    _THINK_BLOCK_RE, _THINK_BLOCK_UNCLOSED_RE, _THINK_BLOCK_BRACKET_RE,
    _TOOL_TRUNCATED_RE, _IMAGE_DATA_RE, _GEMMA_THOUGHT_RE,
)
from agent_cascade.log import logger
from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, FUNCTION, IMAGE, ROLE, SYSTEM, USER, Message,
)
import agent_cascade.settings
agent_cascade.settings.MAX_LLM_CALL_PER_RUN = 100
from agent_cascade.settings import MAX_LLM_CALL_PER_RUN, DEFAULT_MAX_INPUT_TOKENS
from agent_cascade.tools.base import BaseTool
from agent_cascade.utils.utils import (
    extract_text_from_message,
    get_basename_from_url,
    json_loads,
)

from agent_pool import AgentPool

class LoopDetectedError(Exception):
    """Raised when a repetitive loop is detected in agent turns."""
    def __init__(self, reason, agent_name=None, pop_count=None, turn_pop_count=0, resp_snapshot=None):
        self.reason = reason
        self.agent_name = agent_name
        self.pop_count = pop_count
        self.turn_pop_count = turn_pop_count
        self.resp_snapshot = resp_snapshot or []
        super().__init__(f"Loop detected for {agent_name or 'agent'}: {reason}")

def detect_loop(messages: List[Union[dict, Message]]) -> Optional[Tuple[str, int]]:
    """
    Detect if the agent is stuck in a loop.
    Returns (reason, pop_count) if found, else None.
    pop_count is the number of messages from the end that belong to the loop.
    """
    if len(messages) < 6:
        return None
    
    # Extract identifying features, ignoring SYSTEM messages
    def get_feature(m):
        if hasattr(m, 'model_dump'):
            m = m.model_dump()
        elif not isinstance(m, dict):
            # Fallback for other objects
            m = {
                ROLE: getattr(m, 'role', ''),
                CONTENT: getattr(m, 'content', ''),
                'reasoning_content': getattr(m, 'reasoning_content', getattr(m, 'thought', '')),
                'function_call': getattr(m, 'function_call', None)
            }
            
        role = m.get(ROLE)
        content = m.get(CONTENT, '')
        if isinstance(content, list):
            # For multimodal content, just use the text parts
            text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text']
            content = " ".join(text_parts)
        content = str(content)
        
        reasoning = str(m.get('reasoning_content', '') or m.get('thought', ''))
        
        # Combine reasoning and content for better loop detection
        if reasoning and not content.startswith('<think>'):
            text_feature = f"{reasoning}\n{content}"
        else:
            text_feature = content or reasoning
            
        text_feature = _TOOL_TRUNCATED_RE.sub('[TOOL RESPONSE TRUNCATED]', text_feature)
            
        fc = m.get('function_call')
        if fc:
            # Handle both dict and object function calls
            name = fc.get('name') if isinstance(fc, dict) else getattr(fc, 'name', '')
            args = fc.get('arguments') if isinstance(fc, dict) else getattr(fc, 'arguments', '')
            # Strip thinking tags from args for comparison consistency
            args = strip_thinking_blocks(args)
            return f"{role}:{name}:{args}"
        
        # For plain messages, use first 3000 chars of content to distinguish long reasoning
        return f"{role}:{text_feature[:3000]}"

    # Only look at the last 40 messages to detect recent loops
    window = messages[-40:]
    features = []
    feature_to_window_idx = []
    for i, m in enumerate(window):
        role = m.get(ROLE) if isinstance(m, dict) else getattr(m, 'role', '')
        if role != SYSTEM:
            features.append(get_feature(m))
            feature_to_window_idx.append(i)
    
    if len(features) < 4:
        return None

    # Generic loop detection for pattern length L repeating K times
    for L in range(1, 21):
        K = 3 if L < 5 else 2
        
        if len(features) < L * K:
            continue
            
        for i in range(len(features) - (L * K), -1, -1):
            pattern = features[i : i + L]
            is_loop = True
            for k in range(1, K):
                if features[i + k * L : i + (k + 1) * L] != pattern:
                    is_loop = False
                    break
            if is_loop:
                # Double check that the loop is still happening at the very end
                if features[-L:] == pattern:
                    roles = [p.split(':')[0] for p in pattern]
                    
                    # Skip False Positives: 
                    # L=1 pattern of FUNCTION or USER role is usually parallel tool responses
                    # or consecutive user inputs, which are NOT agent loops.
                    if L == 1 and roles[0] in (FUNCTION, USER):
                        continue
                        
                    # Calculate how many messages to pop from the end of 'messages'.
                    # We only want to remove the DUPLICATE repetitions, not the original pattern.
                    # The first occurrence (features[i:i+L]) is valid work; only the
                    # K-1 extra copies that follow it should be rolled back.
                    # feature_to_window_idx[i + L] is the window-level start of the 2nd repetition.
                    second_rep_feature_idx = i + L
                    second_rep_window_idx = feature_to_window_idx[second_rep_feature_idx]
                    # Convert window index to a pop-count from the end of 'messages'
                    pop_count = len(window) - second_rep_window_idx
                    
                    reason = f"Detected repeated sequence loop ({', '.join(roles)} repeating {K} times)"
                    return reason, pop_count
            
    return None

# ─── Sub-agent function schemas ────────────────────────────────────────────────
# These are NOT called via _call_tool; OrchestratorAgent._run intercepts them
# and handles them as streaming generators. They exist only so the LLM sees
# them in the function list.

CALL_AGENT_SCHEMA = {
    'name': 'call_agent',
    'description': (
        'Delegate a task to a specialized sub-agent. '
        'If the instance_name already exists, the session continues with the existing context. '
        'Otherwise, a new session is started using the specified agent_class.\n\n'
        'Example usage:\n'
        '{"name": "call_agent", "arguments": {"agent_class": "coder", "instance_name": "worker1", "task": "Write a script", "parallel_launch": true}}'
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'agent_class': {
                'type': 'string',
                'description': 'The class of agent to call (e.g. "coder", "researcher"). Only required when starting a NEW instance.'
            },
            'instance_name': {
                'type': 'string',
                'description': 'A unique name for this agent instance. If this name exists, the existing session is continued regardless of agent_class.'
            },
            'task': {
                'type': 'string',
                'description': 'The task or question to delegate'
            },
            'context': {
                'type': 'string',
                'description': 'Optional background context for the sub-agent'
            },
            'parallel_launch': {
                'type': 'boolean',
                'description': 'Set to true to run the agent asynchronously in the background. Defaults to false (sequential).'
            },
            'log_file': {
                'type': 'string',
                'description': 'Path to a JSONL log file to restore the agent session from before starting. Useful for resuming old sessions. If provided and the instance_name does not already exist in the pool, the session will be loaded from this log file.'
            },
        },
        'required': ['agent_class', 'instance_name', 'task'],
    },
}

DISMISS_AGENT_SCHEMA = {
    'name': 'dismiss_agent',
    'description': (
        "End a sub-agent's current task and clear its conversation context. "
        "Use when you're done with a sub-agent instance and don't need its context anymore."
    ),
    'parameters': {
        'type': 'object',
        'properties': {
            'instance_name': {
                'type': 'string',
                'description': 'Name of the sub-agent instance to dismiss'
            },
        },
        'required': ['instance_name'],
    },
}


# ─── OrchestratorAgent ─────────────────────────────────────────────────────────

class _SubAgentFunctionProxy(BaseTool):
    """
    Placeholder tool so the LLM sees call_agent / continue_with_agent
    in the function list. These are never actually executed via _call_tool;
    OrchestratorAgent._run intercepts them first.
    """

    def __init__(self, schema: dict, **kwargs):
        self.name = schema['name']
        self.description = schema['description']
        self.parameters = schema['parameters']
        super().__init__(**kwargs)

    def call(self, params: str, **kwargs) -> str:
        # Should never be reached — intercepted in _run
        return 'ERROR: This tool should be intercepted by OrchestratorAgent._run'

import concurrent.futures

class ParallelAgentManager:
    """
    Manages parallel execution of sub-agents using a thread pool.
    Results are queued back to the originating agent via AgentPool.enqueue_message.
    """
    def __init__(self, agent_pool: AgentPool, max_workers: int = 10):
        self.agent_pool = agent_pool
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.active_tasks = {} # instance_name -> (Future, owner_session, agent_class)
    
    def has_active_tasks(self, session_name: str) -> bool:
        """Check if there are any active parallel tasks owned by the given session."""
        return any(owner == session_name for _, owner, _ in self.active_tasks.values())

    def count_active_tasks_by_class(self, agent_class: str) -> int:
        """Count how many active parallel tasks are running for a given agent class."""
        return sum(1 for _, _, a_class in self.active_tasks.values() if a_class == agent_class)

    def submit_task(self, orchestrator, tool_name: str, tool_args: dict, current_response: List[Message], manager_history: List[Message]) -> str:
        """Submit a sub-agent stream to the background pool and return immediately."""
        instance_name = tool_args.get('instance_name', 'unknown')
        agent_class = tool_args.get('agent_class', 'unknown')
        
        # We need a safe copy of the history to prevent thread mutation issues
        # (Though _stream_sub_agent_call itself makes copies, passing references 
        # from a live orchestrator turn can be risky).
        safe_response = copy.deepcopy(current_response)
        safe_history = copy.deepcopy(manager_history)

        def task_wrapper():
            # Because _stream_sub_agent_call is a generator for the WebUI, 
            # we must iterate it to completion to get the final return value.
            try:
                gen = orchestrator._stream_sub_agent_call(tool_name, tool_args, safe_response, safe_history)
                result = None
                try:
                    while True:
                        next(gen)
                except StopIteration as e:
                    result = e.value
                
                # Format the parallel completion message
                completion_msg = f"[Parallel Sub-Agent '{instance_name}' Finished]:\n{result}"
                
                # Push the result asynchronously into the caller's message queue
                self.agent_pool.enqueue_message(orchestrator.session_name, completion_msg)
                
            except Exception as e:
                logger.error(f"Parallel sub-agent {instance_name} failed: {e}", exc_info=True)
                error_msg = f"[Parallel Sub-Agent '{instance_name}' Failed]:\n{str(e)}"
                self.agent_pool.enqueue_message(orchestrator.session_name, error_msg)
            finally:
                if instance_name in self.active_tasks:
                    del self.active_tasks[instance_name]

        future = self.executor.submit(task_wrapper)
        self.active_tasks[instance_name] = (future, orchestrator.session_name)
        return f"[Started agent '{instance_name}' in parallel. You will be notified via an async message when it finishes. You may continue with other tasks.]"


class OrchestratorAgent(Assistant):
    """
    An orchestrator agent whose _run method intercepts sub-agent tool calls
    and executes them as streaming generators so the WebUI can display
    real-time sub-agent output.
    """

    STREAMING_TOOLS = {'call_agent', 'continue_with_agent'}

    def __init__(self, agent_pool: AgentPool, agent_type: str = 'Orchestrator', **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool
        self.agent_type = agent_type
        self.session_name: str = "Maine"
        self.auto_continue_enabled = True # Toggleable in UI
        from agent_cascade.utils.tokenization_qwen import count_tokens
        self._count_tokens = count_tokens
        
        # Tool result character limits
        self.tool_result_max_chars = getattr(agent_cascade.settings, 'DEFAULT_TOOL_RESULT_MAX_CHARS', 10000)
        if 'tool_result_max_chars' in kwargs:
            self.tool_result_max_chars = kwargs['tool_result_max_chars']

    def _call_llm(
        self,
        messages: List[Message],
        functions: List[dict] = None,
        stream: bool = True,
        extra_generate_cfg: dict = None,
    ) -> Iterator[List[Message]]:
        """
        Injected LLM call wrapper that routes through APIRouter for multi-endpoint
        failover and ensures correct model selection.
        """
        if not hasattr(self.agent_pool, 'api_router') or not self.agent_pool.api_router:
            return super()._call_llm(messages, functions, stream, extra_generate_cfg)

        # 1. Base generation config from the agent's internal state (held by the LLM object)
        merged_cfg = copy.deepcopy(getattr(self.llm, 'generate_cfg', {}))
        
        # 2. Merge UI overrides (temperature, etc.)
        if extra_generate_cfg:
            merged_cfg.update(extra_generate_cfg)

        def _execute_llm(llm_cfg: dict) -> Iterator[List[Message]]:
            # 3. Final Merge: The Router's specific endpoint config (llm_cfg) 
            # ALWAYS takes priority for infrastructure (model, base, key).
            final_cfg = copy.deepcopy(merged_cfg)
            final_cfg.update(llm_cfg)
            
            # Use the high-level chat() method to ensure functions/tools are properly
            # handled and passed to the model server.
            return self.llm.chat(
                messages=messages,
                functions=functions,
                stream=stream,
                delta_stream=False,
                extra_generate_cfg=final_cfg
            )

        # Delegate to the router for failover
        return self.agent_pool.api_router.call_with_fallback(
            self.agent_type.lower(),
            _execute_llm
        )

    def _count_message_tokens(self, msg: Union[Message, dict]) -> int:
        from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
        
        # Consistent with base.py's `_count_tokens`
        if isinstance(msg, dict):
            role = msg.get('role', '')
            function_call = msg.get('function_call')
            # Important: if an assistant message has a function call, base.py counts ONLY the function call string.
            if role == ASSISTANT and function_call:
                return qwen_count(f'{function_call}')
            msg_obj = Message(**msg)
        else:
            if msg.role == ASSISTANT and msg.function_call:
                return qwen_count(f'{msg.function_call}')
            msg_obj = msg
            
        text = extract_text_from_message(msg_obj, add_upload_info=True)
        image_tokens = 0
        def repl(match):
            nonlocal image_tokens
            image_tokens += 255
            return f"[Image: {match.group(1)}]"
        text = _IMAGE_DATA_RE.sub(repl, text)
        return qwen_count(text) + image_tokens

    def _get_history_tokens(self, messages: List[Message]) -> int:
        """Calculate total tokens in a message list."""
        total = 0
        for msg in messages:
            total += self._count_message_tokens(msg)
        return total

    def _get_max_tokens(self) -> int:
        """Resolve the effective max_input_tokens from LLM config.
        
        NOTE: We read from self.llm.cfg (the immutable original), NOT from
        self.llm.generate_cfg, because generate_cfg.pop('max_input_tokens')
        is called during the first chat() call, removing it permanently.
        """
        # 1. Try the API Router if available (it handles per-endpoint MIN logic)
        if hasattr(self, 'agent_pool') and self.agent_pool and hasattr(self.agent_pool, 'api_router') and self.agent_pool.api_router:
            router_limit = self.agent_pool.api_router.get_effective_max_tokens(self.agent_type.lower())
            if router_limit > 0:
                return router_limit

        # 2. Try the LLM's original cfg (immutable source of truth)
        if hasattr(self, 'llm') and hasattr(self.llm, 'cfg'):
            cfg = self.llm.cfg
            # Check generate_cfg sub-dict first, then top-level
            agent_max = cfg.get('generate_cfg', {}).get('max_input_tokens') or cfg.get('max_input_tokens')
            if agent_max:
                return int(agent_max)

        # 3. Fallback to pool-level config
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            pool_max = (
                llm_cfg.get('generate_cfg', {}).get('max_input_tokens')
                or llm_cfg.get('max_input_tokens')
            )
            if pool_max:
                return int(pool_max)
        
        return DEFAULT_MAX_INPUT_TOKENS

    def _truncate_tool_result(
        self,
        tool_result: str,
        tool_name: str,
        messages: List[Message],
        instance_name: str,
        tool_args: Optional[dict] = None,
    ) -> str:
        """Truncate a tool result if it would push context past 95% capacity.
        
        Token accounting mirrors base.py's _truncate_input_messages_roughly:
          available_tokens = max_input_tokens - system_message_tokens
          all_tokens = sum of non-system message tokens
        This ensures our percentage matches the 'ALL tokens / Available tokens'
        log in base.py.
        """
        if not isinstance(tool_result, str):
            return tool_result  # multimodal results pass through
            
        if tool_name in ['compress_context']:
            return tool_result

        max_tokens = self._get_max_tokens()

        # Mirror base.py token accounting: separate system vs non-system
        from agent_cascade.utils.tokenization_qwen import count_tokens
        system_tokens = 0
        non_system_tokens = 0
        for msg in messages:
            tokens = self._count_message_tokens(msg)
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == SYSTEM:
                system_tokens += tokens
            else:
                non_system_tokens += tokens

        available_tokens = max_tokens - system_tokens
        if available_tokens <= 0:
            available_tokens = max_tokens  # fallback if system prompt is huge

        total_threshold = int(available_tokens * 0.95)
        per_tool_threshold = int(available_tokens * 0.25)

        # Inline image content (e.g. from screenshot): ![image/png](iVBOR...) — skip truncation since it's already compact markdown data, not prose the LLM parses as text tokens.
        if '![image/' in tool_result:
            return tool_result

        # --- Wild Read Detection ---
        # 1. Check instance-level override
        # 2. Check shared agent_pool config (updated by UI)
        # 3. Fallback to global setting
        wild_read_limit = getattr(self, 'tool_result_max_chars', 10000)
        if hasattr(self, 'agent_pool') and self.agent_pool:
            wild_read_limit = self.agent_pool.llm_cfg.get('tool_result_max_chars', wild_read_limit)
        
        if not wild_read_limit or wild_read_limit == 10000:
             wild_read_limit = getattr(agent_cascade.settings, 'DEFAULT_TOOL_RESULT_MAX_CHARS', 10000)
        is_wild_read = len(tool_result) > wild_read_limit
        
        # Bypass "wild read" for controlled read_file calls
        if is_wild_read and tool_name == 'read_file' and isinstance(tool_args, dict):
            # If the model specified limit, offset, full_read, or line ranges, we assume it's a controlled read
            if any(tool_args.get(k) for k in ['limit', 'offset', 'full_read', 'start_line', 'end_line']):
                is_wild_read = False
        
        result_tokens = max(1, len(tool_result) // 3)
        
        # Check if truncation is needed for ANY reason:
        # 1. Individual tool output exceeds 25% of total context
        # 2. Total context would exceed 95% capacity
        # 3. Wild read (over limit and not a controlled read)
        if (result_tokens <= per_tool_threshold) and (non_system_tokens + result_tokens <= total_threshold) and not is_wild_read:
            return tool_result  # Fits fine, no truncation needed
        
        # --- Truncation required ---
        # Determine target token count for the result
        target_tokens = result_tokens
        reason = ""
        
        if is_wild_read:
            target_tokens = 500
            reason = f"A possible wild read without defined limits (over {wild_read_limit} chars)"
            
        if target_tokens > per_tool_threshold:
            target_tokens = per_tool_threshold
            if not reason:
                reason = f"Individual tool limit (used {result_tokens/available_tokens*100:.0f}% of context)"
        
        # Final safety check against total context budget
        if non_system_tokens + target_tokens > total_threshold:
            target_tokens = max(200, total_threshold - non_system_tokens)
            if not reason:
                reason = f"Total context safety (capacity at {(non_system_tokens+result_tokens)/available_tokens*100:.0f}%)"
            
        # Convert tokens back to chars (use 2.5 multiplier to be safe)
        char_budget = int(target_tokens * 2.5)
        
        # Reserve space for the truncation notice itself (~300 chars)
        char_budget = max(100, char_budget - 300)
        
        # Save full result to spill file
        log_dir = Path('workspace/logs')
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_tool = tool_name.replace('/', '_').replace('\\', '_')
        safe_instance = instance_name.replace('/', '_').replace('\\', '_')
        spill_filename = f"{safe_instance}_{safe_tool}_{timestamp}.txt"
        spill_path = log_dir / spill_filename
        
        try:
            with open(spill_path, 'w', encoding='utf-8') as f:
                f.write(tool_result)
        except Exception as e:
            logger.error(f"Failed to write spill file {spill_path}: {e}")
            # Even if spill fails, still truncate to prevent context overflow
        
        truncated = tool_result[:char_budget]
        
        # If no specific reason was set yet, use a fallback
        if not reason:
            if result_tokens > per_tool_threshold:
                reason = f"Individual tool limit (used {result_tokens/available_tokens*100:.0f}% of context)"
            else:
                reason = f"Total context safety (capacity at {(non_system_tokens+result_tokens)/available_tokens*100:.0f}%)"

        notice = (
            f"\n\n[TOOL RESPONSE TRUNCATED — {reason}. "
            f"Full output ({len(tool_result)} chars) saved to: {spill_path}\n"
            f"You can read it with read_file if needed. "
            f"Consider compressing context before continuing.]"
        )
        
        logger.info(
            f"Truncated '{tool_name}' result for {instance_name}: "
            f"{len(tool_result)} chars -> {len(truncated)} chars. Reason: {reason}. "
            f"Spill file: {spill_path}"
        )
        
        return truncated + notice

    def _inject_compression_warning(self, messages: List[Message]) -> bool:
        """Legacy helper for the orchestrator itself. Returns True if forced compression ran."""
        return self._inject_compression_warning_for_agent(self, self.session_name, messages)

    def _inject_compression_warning_for_agent(self, agent, instance_name: str, messages: List[Message]) -> bool:
        """Inject a warning or force compression if context is getting full for any agent.
        
        Returns True if forced compression was triggered (caller should halt current turn).
        Returns False otherwise (safe to continue with LLM call).
        """
        max_tokens = self._get_max_tokens()

        current_tokens = self._get_history_tokens(messages)
        usage_pct = (current_tokens / max_tokens) * 100
        
        # 1. Critical Threshold Check (> 95%)
        if usage_pct > 95.0:
            # Skip if compress_context already ran this turn — prevents double-compression when
            # LLM-initiated compression mutates the pool, but _inject_compression_warning checks
            # a stale deep-copied llm_messages that still has all old messages.
            if getattr(self, '_compress_context_ran_this_turn', False):
                logger.debug(f"Skipping forceful compression for {instance_name} — already ran this turn.")
                return True  # Still signal halt — something already compressed this turn
            
            # Halt all other agents before compressing so no new content is added during compression.
            # The agent being compressed (instance_name) and the compression agent itself are exempt.
            self.agent_pool.halt_all_instances(except_instances=[instance_name, 'compression_agent'])
            
            logger.info(f"Context usage at {usage_pct:.1f}% for {instance_name} - Halting other agents and triggering FORCEFUL compression.")
            compress_tool = agent.function_map.get('compress_context')
            if compress_tool:
                # Programmatically call the tool's internal compression logic

                # Call tool directly with 50% fraction
                params = json.dumps({
                    'fraction': 0.5,
                    'justification': f'CRITICAL THRESHOLD REACHED ({usage_pct:.1f}%)'
                })

                # Pass necessary kwargs for direct application
                self._compress_context_ran_this_turn = True
                result = compress_tool.call(
                    params,
                    messages=messages,
                    agent_instance_name=instance_name,
                    agent_obj=agent
                )
                
                # Check for failure
                is_error = isinstance(result, str) and result.startswith('ERROR')
                if is_error:
                    logger.error(f"Forceful compression failed for {instance_name}: {result}")
                    notification = (
                        f"\n\n[SYSTEM NOTIFICATION: Context window exceeded 95% capacity ({usage_pct:.1f}%), "
                        f"but automatic compression failed ({result}). The upcoming API call will likely fail due to length.]"
                    )
                else:
                    # Add a system message to inform the agent that it happened
                    agent_class = self.agent_pool.instance_classes.get(instance_name, 'Unknown')
                    logger_inst = self.agent_pool.get_logger(instance_name, agent_class)
                    notification = (
                        f"\n\n[SYSTEM NOTIFICATION: Context window exceeded 95% capacity ({usage_pct:.1f}%). "
                        "Forceful compression (50% ratio) has been effectuated to prevent errors. "
                        f"Use your logs at `{logger_inst.log_path}` if you need to restore details from turns that were removed.]"
                    )
                
                if messages:
                    last_msg = messages[-1]
                    if isinstance(last_msg.content, str):
                        # Prevent duplicate notifications from stacking if the loop repeats
                        if "[SYSTEM NOTIFICATION: Context window exceeded 95%" not in last_msg.content:
                            last_msg.content += notification
                    elif isinstance(last_msg.content, list):
                        from agent_cascade.llm.schema import ContentItem
                        
                        # Prevent duplicate notifications from stacking
                        has_notification = any(
                            isinstance(item, ContentItem) and "[SYSTEM NOTIFICATION: Context window exceeded 95%" in getattr(item, 'text', '')
                            for item in last_msg.content
                        )
                        if not has_notification:
                            last_msg.content.append(ContentItem(text=notification))
            # Resume all halted agents after compression completes
            self.agent_pool.resume_all_instances()
            
            return True  # Signal caller to halt current turn

        # 2. Warning Threshold (> 85%)
        if usage_pct > 85.0:
            warning_text = "[SYSTEM WARNING: Context window at"
            warning = (
                f"\n\n{warning_text} {usage_pct:.1f}% capacity ({current_tokens}/{max_tokens} tokens). "
                "Consider using the `compress_context` tool to summarize old history and free up space. "
                "Propose a fraction (e.g. 0.4 for 40%) and a justification. The summary will be sent for approval.]"
            )
            # Find the most recent message to append warning (temporarily)
            if messages:
                # We append to the last message's content if it's text, or add a system message
                # Note: This is NOT saved to the permanent AgentPool history
                last_msg = messages[-1]
                if isinstance(last_msg.content, str):
                    if warning_text not in last_msg.content:
                        last_msg.content += warning
                elif isinstance(last_msg.content, list):
                    from agent_cascade.llm.schema import ContentItem
                    
                    # Prevent duplicate notifications from stacking
                    has_notification = any(
                        isinstance(item, ContentItem) and warning_text in getattr(item, 'text', '')
                        for item in last_msg.content
                    )
                    if not has_notification:
                        last_msg.content.append(ContentItem(text=warning))

        return False  # No forced compression, safe to continue with LLM call


    @property
    def support_multimodal_input(self) -> bool:
        """
        Signal that the orchestrator itself can handle multimodal messages
        and should not have them stripped before reaching _run.
        """
        return True

    def _run(
        self,
        messages: List[Message],
        lang: str = 'en',
        knowledge: str = '',
        **kwargs,
    ) -> Iterator[List[Message]]:
        # Clear active stack only for the root turn to avoid state leakage
        if not kwargs.get('agent_instance_name'):
            self.agent_pool.active_stack.clear()
            
        instance = kwargs.get('agent_instance_name') or self.session_name
        self.session_name = instance # Track current instance name
        
        # Prepend knowledge like Assistant does
        messages = self._prepend_knowledge_prompt(
            messages=messages, lang=lang, knowledge=knowledge, **kwargs
        )
        
        # Using the base agent_type for the class metadata field keeps logs clean.
        logger_inst = self.agent_pool.get_logger(instance, self.agent_type)
        
        # Log the latest turn
        if messages:
            # Robust identity insertion into the system prompt regardless of line position
            m0 = messages[0]
            m0_role = m0.get('role') if isinstance(m0, dict) else getattr(m0, 'role', '')
            if m0_role == SYSTEM:
                m0_content = m0.get('content', '') if isinstance(m0, dict) else getattr(m0, 'content', '')
                if isinstance(m0_content, str) and instance:
                    import re
                    # 1. Update identity "You are [instance]."
                    pattern = rf"(?i)You are {self.agent_type}\."
                    if re.search(pattern, m0_content):
                        m0_content = re.sub(pattern, f"You are {instance}.", m0_content, count=1)
                    
                    # 2. Insert Session Metadata section (Stable)
                    if '## Session Metadata' not in m0_content:
                        meta_lines = [
                            "## Session Metadata",
                            f"- Supervisor: User",
                            f"- Log Path: {logger_inst.log_path}",
                            "Use your logs to recall details from turns that were compressed.\n"
                        ]
                        content_lines = m0_content.split('\n')
                        insert_pos = 2 if len(content_lines) > 1 and not content_lines[1].startswith("#") else 1
                        for i, ml in enumerate(meta_lines): content_lines.insert(insert_pos + i, ml)
                        m0_content = '\n'.join(content_lines)
                    
                    # 3. Inject available resources (Stable sort for caching)
                    if '--- CURRENT AVAILABLE RESOURCES' not in m0_content:
                        res_append = "\n\n--- CURRENT AVAILABLE RESOURCES (Auto-Injected) ---\n"
                        res_append += "\nAvailable Sub-Agents (call via call_agent):\n"
                        has_agents = False
                        for name in sorted(self.agent_pool.list_agents()):
                            if name.lower() != self.name.lower():
                                info = self.agent_pool.get_agent_info(name)
                                if info:
                                    res_append += f"- **{info['name']}**: {info['tagline']}\n"
                                    has_agents = True
                        if not has_agents: res_append += "- None currently available.\n"
                        
                        res_append += "\nEnabled Tools (can change per interaction):\n"
                        if self.function_map:
                            disabled_tools = self._get_disabled_tool_names()
                            for t_name in sorted(self.function_map.keys()):
                                if t_name in disabled_tools:
                                    continue
                                desc = getattr(self.function_map[t_name], 'description', 'No description provided')
                                res_append += f"- **{t_name}**: {desc}\n"
                        else: res_append += "- None currently enabled.\n"
                        m0_content += res_append
                    
                    # 4. Inject Argument Reuse instructions (Static version for caching)
                    if '### Advanced Feature: Argument Reuse' not in m0_content:
                        m0_content += (
                            "\n\n### Advanced Feature: Argument Reuse\n"
                            "To reuse a LARGE argument value (like full file content or path) from any previous successful tool call in this session, "
                            "use the exact placeholder: \"__USE_PREV_ARG__\". This saves tokens and processing time."
                        )
                    
                    if isinstance(m0, dict): m0['content'] = m0_content
                    else: m0.content = m0_content
        # Sync initial state if history is empty
        if not logger_inst.data["history"]:
            logger_inst.update_history(messages)
        elif not kwargs.get('agent_instance_name'):
            # Only log the last message for the main orchestrator session.
            # Use update_history to avoid duplicates if api_server.py already logged it during a retry.
            # Skip if compression ran last turn — the log is already in sync via
            # insert_compression_marker + individual log_message calls.
            if not getattr(self, '_compress_context_ran_this_turn', False):
                logger_inst.update_history([messages[-1]])

        # --- Check for manual commands ---
        last_msg = messages[-1] if messages else None
        last_role = last_msg.get('role') if isinstance(last_msg, dict) else getattr(last_msg, 'role', '')
        
        cmd_text = ""
        if last_msg:
            try:
                msg_obj = Message(**last_msg) if isinstance(last_msg, dict) else last_msg
                cmd_text = extract_text_from_message(msg_obj, add_upload_info=False).strip()
            except Exception:
                pass
        
        if last_role == USER and cmd_text:
            # --- /compress command ---
            if cmd_text.startswith('/compress'):
                parts = cmd_text.split()
                fraction = 0.5
                if len(parts) > 1:
                    try:
                        fraction = float(parts[1])
                    except ValueError:
                        pass
                
                compress_tool = self.function_map.get('compress_context')
                if compress_tool:
                    # We no longer pop the command. It should remain in history for traceability.
                    # The cumulative log and tool logic will handle it correctly.
                    
                    yield [Message(role=ASSISTANT, content=f"Generating context summary for {int(fraction*100)}% of history...")]
                    
                    params = json.dumps({
                        'fraction': fraction,
                        'justification': 'MANUAL USER COMMAND (Preview)'
                    })
                    
                    # Generate summary without applying
                    summary = compress_tool.call(
                        params,
                        messages=messages,
                        agent_instance_name=instance,
                        agent_obj=self,
                        dry_run=True  # Ensure it doesn't apply yet
                    )
                    
                    if summary and not summary.startswith("ERROR"):
                        description = f"Proposed Compression Summary ({int(fraction*100)}% of history)"
                        approved, reason = self.agent_pool.operation_manager.request_user_approval(
                            agent_name=instance,
                            tool_name='compress_context',
                            tool_args={'fraction': fraction, 'summary': summary},
                            description=description,
                            )
                        
                        if approved:
                            # Apply the compression
                            params = json.dumps({
                                'fraction': fraction,
                                'justification': 'MANUAL USER COMMAND (Approved)'
                            })
                            result = compress_tool.call(
                                params,
                                messages=messages,
                                agent_instance_name=instance,
                                agent_obj=self,
                                precomputed_summary=summary # Skip generation
                            )
                            yield [Message(role=ASSISTANT, content=f"Context compressed successfully.\nResult: {result}")]
                        else:
                            yield [Message(role=ASSISTANT, content=f"Context compression cancelled: {reason}")]
                    else:
                        yield [Message(role=ASSISTANT, content=f"Failed to generate summary: {summary}")]
                else:
                    yield [Message(role=ASSISTANT, content="Error: compress_context tool is not available.")]
                return

        # --- Custom FnCallAgent-style loop with streaming sub-agent support ---
        # messages[0] now contains all stabilized instructions, so caches will hit across turns.
        llm_messages = copy.deepcopy(messages)

        # Initialize/Reset turn state
        self.turn_final_messages = None
        self._compress_context_ran_this_turn = False  # prevent double-compression this turn
        
        # Robustness: Read turn limit and auto-continue settings
        # These may be set on the instance by api_server.py or passed in kwargs
        max_turns = getattr(self, 'max_turns', None) or kwargs.get('max_turns') or 50
        self.auto_continue_enabled = getattr(self, 'auto_continue_enabled', None)
        if self.auto_continue_enabled is None:
            self.auto_continue_enabled = True # default
        
        num_llm_calls_available = max(int(max_turns), MAX_LLM_CALL_PER_RUN)
        response: List[Message] = []

        while num_llm_calls_available > 0:
            if self.agent_pool.stopped:
                logger.info(f"Agent {self.name} stopped by user.")
                yield response
                break
            
            # Check per-instance halt — agent is paused (e.g. during forced compression or manual pause)
            if self.agent_pool.is_halted(self.session_name):
                yield response
                continue  # Skip this turn, will resume when halted flag is cleared
                
            num_llm_calls_available -= 1
    
            extra_generate_cfg = {'lang': lang}
    
            # --- ASYNC MESSAGE INJECTION (per-agent routed) ---
            pending = self.agent_pool.drain_queue(self.session_name)
            if pending:
                for async_msg_text in pending:
                    if not async_msg_text.strip():
                        continue  # Skip empty messages
                    async_msg = Message(role=USER, content=async_msg_text)
                    messages.append(async_msg)
                    llm_messages.append(async_msg)
                    response.append(async_msg)
                    logger_inst.log_message(async_msg)  # Log to JSONL file
                    logger.info(f"Injected async user message into {self.session_name}: {async_msg_text}")
                yield response  # Update UI with the injected message
                
            # Inject warning or force compression (only for the LLM call, doesn't affect saved history)
            forced_compression_ran = self._inject_compression_warning(llm_messages)

            # If forced compression ran, halt this turn — don't proceed with the LLM call.
            # The agent must not add more content on top of a just-compressed context in the same turn;
            # otherwise we risk still being over 95% after compression + new LLM output.
            # Re-sync both `messages` and `llm_messages` from the compressed pool state before continuing.
            # Use slice_history_for_llm to get the actual working set (not full cumulative history),
            # otherwise token count stays >95% and we'd loop forever compressing the same full history.
            if forced_compression_ran:
                # Restore the turn budget — we didn't actually make an LLM call this turn,
                # and we don't want to penalize the agent for compression consuming a turn.
                num_llm_calls_available += 1
                
                compressed = self.agent_pool.get_conversation(self.session_name)
                if compressed:
                    # messages stays as canonical full history (used by loop detection, etc.)
                    messages.clear()
                    messages.extend(compressed)
                    # llm_messages gets the sliced working set — what actually goes to the LLM
                    sliced = self.agent_pool.slice_history_for_llm(compressed) if hasattr(self.agent_pool, 'slice_history_for_llm') else compressed
                    llm_messages.clear()
                    llm_messages.extend(copy.deepcopy(sliced))
                yield response
                # Reset flag so BUG-7 block below doesn't redundantly re-sync every iteration
                self._compress_context_ran_this_turn = False
                continue

            # BUG-7 fix: If compress_context ran via LLM tool call, llm_messages was compressed in-place
            # but `messages` (the canonical history) still holds the old un-compressed state.
            # Re-sync `messages` from the Pool to prevent divergence.
            # NOTE: cumulative compression INSERTS a summary marker (pool grows), so we check
            # the flag rather than comparing lengths. `messages` stays as full cumulative history;
            # `llm_messages` is synced to the sliced working set by the compression tool itself.
            if getattr(self, '_compress_context_ran_this_turn', False):
                compressed = self.agent_pool.get_conversation(self.session_name)
                if compressed:
                    messages.clear()
                    messages.extend(compressed)
            
            # --- LOOP DETECTION ---
            loop_info = detect_loop(messages)
            if loop_info:
                loop_reason, pop_count = loop_info
                logger.warning(f"Loop detected for {self.name}: {loop_reason}")
                try:
                    if hasattr(self.agent_pool, 'telemetry'):
                        self.agent_pool.telemetry.record_loop_detected(
                            self.session_name, 
                            loop_reason, 
                            auto_rolled_back=True, 
                            pop_count=pop_count
                        )
                except Exception:
                    pass
                raise LoopDetectedError(loop_reason, agent_name=self.session_name, pop_count=pop_count)

            active_functions = self._get_active_functions()
            
            # --- Call LLM and handle streaming ---
            # Telemetry: estimate input tokens for this call (non-blocking)
            _llm_first_token_recorded = False
            try:
                _llm_input_est = sum(self._count_message_tokens(m) for m in llm_messages)
                _llm_model = getattr(self.llm, 'model', 'unknown') if hasattr(self, 'llm') else 'unknown'
                if hasattr(self.agent_pool, 'telemetry'):
                    self.agent_pool.telemetry.record_llm_call_start(instance, input_tokens_est=_llm_input_est, model=_llm_model)
            except Exception:
                pass  # Telemetry must never block agent execution

            turn_output: List[Message] = []
            for output in self._call_llm(llm_messages, functions=active_functions, stream=True, extra_generate_cfg=extra_generate_cfg):
                if self.agent_pool.stopped:
                    logger.info(f"Agent {self.name} stopped during LLM stream.")
                    break
                # Check per-instance halt — pause takes effect mid-stream
                if self.agent_pool.is_halted(self.session_name):
                    logger.info(f"Agent {self.name} halted during LLM stream.")
                    break
                # Telemetry: record first token time
                if not _llm_first_token_recorded and output:
                    try:
                        if hasattr(self.agent_pool, 'telemetry'):
                            self.agent_pool.telemetry.record_llm_first_token(instance)
                    except Exception:
                        pass
                    _llm_first_token_recorded = True
                turn_output = output
                yield response + turn_output
            
            # Telemetry: estimate output tokens and record LLM call end (non-blocking)
            try:
                _llm_output_est = sum(
                    self._count_message_tokens(m) for m in turn_output
                    if (m.get('role') if isinstance(m, dict) else getattr(m, 'role', '')) == ASSISTANT
                ) if turn_output else 0
                if hasattr(self.agent_pool, 'telemetry'):
                    self.agent_pool.telemetry.record_llm_call_end(instance, output_tokens_est=_llm_output_est)
            except Exception:
                pass  # Telemetry must never block agent execution

            if self.agent_pool.stopped:
                yield response
                break
            
            # Check per-instance halt after LLM turn — pause takes effect between turns
            if self.agent_pool.is_halted(self.session_name):
                yield response
                continue  # Don't process this turn's output, wait for resume

            # Process the FINAL output of this LLM turn
            output = turn_output
            
            # --- NORMALIZE AND UPDATE HISTORY ---
            # Extract reasoning from model-specific tags (like Gemma's) and standardize to <think>
            history_output = []
            for msg in output:
                # Normalize Gemma-style thinking blocks into reasoning_content if not already present
                content = msg.get(CONTENT, '')
                if not msg.get('reasoning_content') and isinstance(content, str) and '<|channel>thought' in content.lower():
                    # Only strip if it's at the very beginning of the content to avoid stripping 
                    # markers inside file content or tutorial text.
                    match = _GEMMA_THOUGHT_RE.search(content)
                    if match:
                        msg['reasoning_content'] = match.group(1).strip()
                        msg[CONTENT] = _GEMMA_THOUGHT_RE.sub("", content, count=1).strip()

                m = copy.deepcopy(msg)
                # Strip thinking blocks from reasoning_content to prevent tag pollution in history
                if m.get('reasoning_content') and isinstance(m['reasoning_content'], str):
                    m['reasoning_content'] = strip_thinking_blocks(m['reasoning_content'])
                
                # FIX: In newer AgentCascade, tool calls should NOT have reasoning injected 
                # into 'content' if it's already in 'reasoning_content' and there's a tool call,
                # as the UI/orchestrator handles the combined display and it confuses history trackers.
                reasoning_content = m.get('reasoning_content')
                if reasoning_content:
                    if isinstance(reasoning_content, list):
                        # Convert list of ContentItem to string for regex cleanup
                        reasoning_str = " ".join([str(item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')) for item in reasoning_content])
                    else:
                        reasoning_str = str(reasoning_content)

                    reasoning_clean = _THINK_BLOCK_RE.sub("", reasoning_str)
                    reasoning = f"<think>\n{reasoning_clean}\n</think>\n"
                    old_content = m.get(CONTENT)
                    
                    # Only inject if there's NO function call (standard message)
                    if not m.get('function_call'):
                        if old_content is None:
                            m[CONTENT] = reasoning
                        elif isinstance(old_content, list):
                            m[CONTENT] = [ContentItem(text=reasoning)] + old_content
                        else:
                            m[CONTENT] = reasoning + str(old_content)
                
                # Clean thinking blocks from function call arguments in history to prevent UI/parsing issues
                if m.get('function_call'):
                    fc = m['function_call']
                    if isinstance(fc, dict) and fc.get('arguments'):
                        fc['arguments'] = strip_thinking_blocks(fc['arguments'])
                    elif hasattr(fc, 'arguments'):
                        fc.arguments = strip_thinking_blocks(fc.arguments)
                        
                history_output.append(m)

            response.extend(output)
            messages.extend(output)
            llm_messages.extend(history_output)
    
            # Log generated messages
            is_truncated = False
            for msg in output:
                logger_inst.log_message(msg)
                # Detection: finish_reason is often in extra
                if msg.extra and msg.extra.get('finish_reason') == 'length':
                    is_truncated = True
            
            # --- AUTO-CONTINUE ON LENGTH LIMIT ---
            if is_truncated and self.auto_continue_enabled and not self.agent_pool.stopped:
                logger.info(f"Detected message truncation (length limit) for {self.name}. Auto-triggering continuation.")
                # Increment budget to allow the continuation
                num_llm_calls_available += 1
                # Inject a system prompt to continue
                cont_msg = Message(role=USER, content="[SYSTEM]: Your previous response was cut off by the length limit. Please continue exactly from where you left off, without repeating yourself.")
                messages.append(cont_msg)
                response.append(cont_msg)
                llm_messages.append(cont_msg)
                logger_inst.log_message(cont_msg)
                # Yield with a hint
                yield response + [Message(role=ASSISTANT, content="... (Continuing output due to length limit)")]
    
            used_any_tool = False
            for out in output:
                use_tool, tool_name, tool_args, _ = self._detect_tool(out)
                if not use_tool:
                    continue
                used_any_tool = True
                # Yield the tool call request immediately so UI sees "calling tool..."
                yield response
                _tool_success = True
                _tool_error = ""
                try:
                    if tool_name in self.STREAMING_TOOLS:
                        # ── Sub-agent call ──
                        parsed_args = tool_args
                        if isinstance(tool_args, str):
                            try:
                                parsed_args = json_loads(tool_args)
                            except Exception:
                                parsed_args = {}

                        if isinstance(parsed_args, dict) and parsed_args.get('parallel_launch') is True:
                            # ── Check Concurrency Limits ──
                            agent_class = parsed_args.get('agent_class')
                            is_parallel_allowed = True
                            
                            if agent_class and hasattr(self.agent_pool, 'api_router'):
                                limit = self.agent_pool.api_router.get_concurrency_limit(agent_class)
                                if limit == 0:
                                    # User explicitly sets 0 to mean sequential delegation
                                    is_parallel_allowed = False
                                    logger.info(f"Forcing sequential launch for {agent_class} (concurrency_limit=0)")
                                elif limit > 0:
                                    # Count how many active parallel tasks are using this same agent class
                                    active_count = self.agent_pool.parallel_manager.count_active_tasks_by_class(agent_class)
                                    if active_count >= limit:
                                        is_parallel_allowed = False
                                        logger.info(f"Forcing sequential launch for {agent_class} (limit {limit} reached, active {active_count})")
                            
                            if is_parallel_allowed:
                                # ── Parallel background execution ──
                                tool_result = self.agent_pool.parallel_manager.submit_task(
                                    self, tool_name, parsed_args, response, messages
                                )
                            else:
                                # Fallback to sequential if limit reached or 0
                                tool_result = yield from self._stream_sub_agent_call(
                                    tool_name, tool_args, response, messages
                                )
                        else:
                            # ── Synchronous streaming execution ──
                            tool_result = yield from self._stream_sub_agent_call(
                                tool_name, tool_args, response, messages
                            )
                    else:
                        # ── Normal synchronous tool ──
                        
                        # --- Handle __USE_PREV_ARG__ Placeholder Replacement ---
                        if isinstance(tool_args, str):
                            tool_args = tool_args.strip()
                            if tool_args:
                                try:
                                    # Use relaxed json_loads to handle trailing commas or other LLM quirks
                                    tool_args = json_loads(tool_args)
                                except Exception:
                                    pass # Let _call_tool handle standard verification
                            else:
                                tool_args = {} # Guard against empty string arguments
                        
                        if isinstance(tool_args, dict):
                            # Use the current instance name as the scope for the last_tool_args cache
                            instance_scope = self.session_name
                            
                            # Resolve placeholders
                            placeholders_found = []
                            for arg_key, arg_val in tool_args.items():
                                if arg_val == "__USE_PREV_ARG__":
                                    placeholders_found.append(arg_key)
                                    
                            if placeholders_found:
                                # 1. Try tool-specific cache first
                                prev_args = self.agent_pool.last_tool_args.get(instance_scope, {}).get(tool_name)
                                
                                # 2. Fallback to global cache for common parameters like 'path'
                                global_args = self.agent_pool.last_tool_args.get(instance_scope, {}).get("__GLOBAL__", {})
                                
                                if not prev_args and not global_args:
                                    tool_result = f"Error: Cannot use __USE_PREV_ARG__ for '{tool_name}' because no previous call to this tool was recorded for instance '{instance_scope}'."
                                    # Skip tool execution if placeholder fails
                                    skip_execution = True
                                else:
                                    skip_execution = False
                                    for arg_key in placeholders_found:
                                        # Prefer tool-specific, then global
                                        if prev_args and arg_key in prev_args:
                                            tool_args[arg_key] = prev_args[arg_key]
                                        elif arg_key in global_args:
                                            tool_args[arg_key] = global_args[arg_key]
                                        else:
                                            tool_result = f"Error: Cannot use __USE_PREV_ARG__ for argument '{arg_key}' because it was not found in previous calls (neither specific to '{tool_name}' nor globally)."
                                            skip_execution = True
                                            break
                            else:
                                skip_execution = False
                                
                            if not skip_execution:
                                call_kwargs = kwargs.copy()
                                if 'agent_instance_name' not in call_kwargs:
                                    call_kwargs['agent_instance_name'] = self.session_name
                                
                                # Pass the agent itself so tools (like compress_context) can sync 
                                # back to its base system_message for persistence across turns.
                                call_kwargs['agent_obj'] = self
                                    
                                # Telemetry: track tool call
                                try:
                                    if hasattr(self.agent_pool, 'telemetry'):
                                        self.agent_pool.telemetry.record_tool_call_start(self.session_name, tool_name)
                                except Exception:
                                    pass
                                
                                try:
                                    tool_result = self._call_tool(
                                        tool_name, tool_args, messages=llm_messages, 
                                        **call_kwargs
                                    )
                                except Exception as e:
                                    logger.error(f"Error calling tool {tool_name}: {e}")
                                    tool_result = f"Error: {e}"
                                    _tool_success = False
                                    _tool_error = str(e)
                                    if "valid JSON" in str(e) and isinstance(tool_args, str):
                                        tool_result += f"\nYour arguments: {tool_args[:200]}..."
                                
                                # Caching: Save successful tool args for future reuse
                                if instance_scope not in self.agent_pool.last_tool_args:
                                    self.agent_pool.last_tool_args[instance_scope] = {}
                                
                                self.agent_pool.last_tool_args[instance_scope][tool_name] = copy.deepcopy(tool_args)
                                if "__GLOBAL__" not in self.agent_pool.last_tool_args[instance_scope]:
                                    self.agent_pool.last_tool_args[instance_scope]["__GLOBAL__"] = {}
                                self.agent_pool.last_tool_args[instance_scope]["__GLOBAL__"].update(copy.deepcopy(tool_args))
                        else:
                            # Fallback for non-dict tool_args
                            call_kwargs = kwargs.copy()
                            if 'agent_instance_name' not in call_kwargs:
                                call_kwargs['agent_instance_name'] = self.session_name
                            call_kwargs['agent_obj'] = self
                            try:
                                if hasattr(self.agent_pool, 'telemetry'):
                                    self.agent_pool.telemetry.record_tool_call_start(self.session_name, tool_name)
                            except Exception:
                                pass
                            
                            try:
                                tool_result = self._call_tool(
                                    tool_name, tool_args, messages=llm_messages, 
                                    **call_kwargs
                                )
                            except Exception as e:
                                logger.error(f"Error calling tool {tool_name}: {e}")
                                tool_result = f"Error: {e}"
                                _tool_success = False
                                _tool_error = str(e)
                except Exception as e:
                    # Catch high-level errors (like LoopDetectedError) to ensure telemetry is closed
                    _tool_success = False
                    _tool_error = str(e)
                    tool_result = f"Error: {e}"
                    # If it's a LoopDetectedError, we should re-raise after recording telemetry 
                    # so the orchestrator turn stops as intended.
                    if "Loop detected" in str(e):
                        # Ensure telemetry is recorded before re-raising
                        try:
                            if hasattr(self.agent_pool, 'telemetry'):
                                self.agent_pool.telemetry.record_tool_call_end(
                                    self.session_name, tool_name,
                                    success=False,
                                    result_chars=len(tool_result),
                                    truncated=False,
                                    error=_tool_error,
                                )
                        except Exception:
                            pass
                        raise e
    
                # --- Generic truncation: protect ALL tool results ---
                _was_truncated = False
                if isinstance(tool_result, str):
                    _pre_trunc_len = len(tool_result)
                    tool_result = self._truncate_tool_result(
                        tool_result, tool_name, llm_messages, self.session_name, tool_args=tool_args
                    )
                    _was_truncated = len(tool_result) < _pre_trunc_len

                # Track that compress_context ran this turn to prevent _inject_compression_warning from triggering a second one
                if tool_name == 'compress_context':
                    self._compress_context_ran_this_turn = True
                    # Sync llm_messages from the pool immediately so subsequent operations 
                    # in this turn see the compressed state (prevents desync).
                    compressed_conv = self.agent_pool.get_conversation(self.session_name)
                    if compressed_conv:
                        llm_messages.clear()
                        llm_messages.extend(copy.deepcopy(compressed_conv))
                
                # --- Post-execution success detection ---
                # Many tools return an error message as a string instead of raising an exception.
                if _tool_success and isinstance(tool_result, str):
                    # Only check the first non-empty line — error markers always appear at the start.
                    first_line = ''
                    for line in tool_result.split('\n'):
                        stripped = line.strip()
                        if stripped:
                            first_line = stripped.lower()
                            break
                    error_indicators = [
                        'error:', 'rejected by user:', 'failed:', 'invalid:', 
                        'permission denied:', 'an error occurred', 'does not exist'
                    ]
                    if any(first_line.startswith(ind) for ind in error_indicators) or 'failed to' in first_line:
                        _tool_success = False
                        _tool_error = tool_result[:500]  # Capture the start of the error message
                
                # Telemetry: record tool call end
                try:
                    if hasattr(self.agent_pool, 'telemetry'):
                        _result_chars = len(tool_result) if isinstance(tool_result, str) else 0
                        self.agent_pool.telemetry.record_tool_call_end(
                            self.session_name, tool_name,
                            success=_tool_success,
                            result_chars=_result_chars,
                            truncated=_was_truncated,
                            error=_tool_error,
                        )
                except Exception:
                    pass  # Telemetry must never block agent execution
    
                fn_msg = Message(
                    role=FUNCTION,
                    name=tool_name,
                    content=tool_result,
                    extra={
                        'function_id': out.extra.get('function_id', '1')
                        if out.extra else '1',
                        'tool_success': _tool_success,  # Pass to frontend so isToolFailure() can use it directly
                    },
                )
                messages.append(fn_msg)
                llm_messages.append(fn_msg)
                response.append(fn_msg)
                
                # Log just the function result (LLM output already logged above)
                logger_inst.log_message(fn_msg)
                
                # --- ASYNC MESSAGE INJECTION (URGENT, per-agent routed) ---
                urgent_msgs = self.agent_pool.drain_queue(instance)
                if urgent_msgs:
                    for async_msg_text in urgent_msgs:
                        if not async_msg_text.strip():
                            continue  # Skip empty messages
                        async_msg = Message(role=USER, content=async_msg_text)
                        messages.append(async_msg)
                        llm_messages.append(async_msg)
                        response.append(async_msg)
                        logger_inst.log_message(async_msg)  # Log to JSONL file
                        logger.info(f"Injected urgent async user message mid-tool-loop into {instance}: {async_msg_text}")
                    yield response
                    break  # CRITICAL: Stop executing the rest of the batched tools!
                    
                yield response
    
            # Check if the turn is truly finished.
            has_real_content = any(out.get('content') and not out.get('content').startswith('<think>') for out in output)
            has_thinking = any(out.get('thought') or out.get('reasoning_content') for out in output)
            
            if not used_any_tool:
                if is_truncated:
                    # Truncated but no tool used? This means the model was cut off mid-sentence.
                    # We already handled the budget and system prompt injection above,
                    # so we just need to make sure we don't 'break' here.
                    pass
                elif has_thinking and not has_real_content:
                    # It's a pure thinking turn. Continue to the next turn.
                    logger.info(f"Pure reasoning turn detected for {self.name}. Continuing to next turn.")
                    pass
                else:
                    # Final answer or real content provided, or no thinking at all.
                    # WAIT if there are active parallel tasks for this instance
                    if self.agent_pool.parallel_manager.has_active_tasks(instance):
                        # Poll for either a new message or all tasks completion
                        while not self.agent_pool.has_messages(instance) and \
                              self.agent_pool.parallel_manager.has_active_tasks(instance):
                            if self.agent_pool.stopped: break
                            time.sleep(0.5)
                        
                        if self.agent_pool.has_messages(instance):
                            continue # Loop back to drain_queue
                    
                    # Post-generation queue drain: If the agent finished its work but there
                    # are queued messages waiting, loop back instead of breaking out.
                    # This prevents injected messages from sitting idle after the agent goes idle.
                    if self.agent_pool.has_messages(instance):
                        logger.info(f"Queued messages detected for {instance} after turn completion. Looping back to process them.")
                        continue # Loop back to drain_queue at the top of the while loop
                    
                    break
    
            # No final update_history needed: all messages are logged individually
            # via log_message above (LLM output at line 502, fn results at line 542).
            
            # Expose the final context for the WebUI so it can detect if a compression 
            # occurred and mutated the history mid-turn.
            self.turn_final_messages = messages

        if num_llm_calls_available <= 0:
            logger.warning(f"Agent {self.name} reached turn limit. Stopping.")
            term_msg = Message(role=ASSISTANT, content="\n\n[SYSTEM: Turn limit reached. If the task is incomplete, please ask me to continue.]")
            response.append(term_msg)
            yield response

        yield response

    # ------------------------------------------------------------------ #
    #  Streaming sub-agent execution                                      #
    # ------------------------------------------------------------------ #

    def _stream_sub_agent_call(
        self,
        tool_name: str,
        tool_args: Union[str, dict],
        current_response: List[Message],
        manager_history: List[Message],
    ) -> Iterator[List[Message]]:
        """
        Generator that runs a sub-agent instance, yields intermediate results to the
        WebUI (so streaming is visible), and *returns* the final tool-result
        string via ``return``.  Called via ``yield from`` in ``_run``.
        """
        if isinstance(tool_args, str):
            try:
                args = json.loads(tool_args)
            except json.JSONDecodeError:
                return f'Error: Invalid JSON arguments: {tool_args}'
        else:
            args = tool_args

        instance_name = args.get('instance_name', '')
        agent_class = args.get('agent_class', '')

        # Prevent state corruption when an agent calls ITSELF recursively.
        # If the instance is already in the stack, cloning its state ensures
        # that the outer caller's 'conv' doesn't get polluted by inner messages, 
        # which causes out-of-order UI rendering when 'conv + resp' is joined.
        if instance_name in self.agent_pool.active_stack:
            original_instance = instance_name
            count = self.agent_pool.active_stack.count(instance_name)
            instance_name = f"{instance_name}_child{count}"
            
            # Clone base conversation and append the current turn's context
            base_conv = self.agent_pool.get_conversation(original_instance)
            clone_conv = copy.deepcopy(base_conv)
            if current_response:
                clone_conv.extend(copy.deepcopy(current_response))
            self.agent_pool.instance_conversations[instance_name] = clone_conv

        # 1. Resolve agent class and isolation
        existing_class = self.agent_pool.instance_classes.get(instance_name)
        
        # If the requested class is different from the existing one,
        # we MUST clear history to avoid confusing context mix-ups (stacking).
        if existing_class and agent_class and existing_class != agent_class:
            logger.info(f"Class mismatch for '{instance_name}': {existing_class} -> {agent_class}. Clearing history.")
            self.agent_pool.clear_conversation(instance_name)
            existing_class = None # Trigger fresh initialization
            
        if existing_class:
            agent_class = existing_class
        elif not agent_class:
            return f"Error: No active instance named '{instance_name}'. Provide agent_class to start one."
        
        # Register/Confirm instance class
        self.agent_pool.instance_classes[instance_name] = agent_class

        agent = self.agent_pool.get_agent(agent_class)
        if not agent:
            return (
                f"Error: Agent class '{agent_class}' not found. "
                f"Available classes: {self.agent_pool.list_agents()}"
            )

        if self.agent_pool.stopped:
            return f"Operation cancelled by user."
        
        # If log_file is provided and instance doesn't exist yet, restore session from log
        log_file = args.get('log_file')
        if log_file and instance_name not in self.agent_pool.instance_conversations:
            load_result = self.agent_pool.load_session_from_log(log_file, target_instance=instance_name)
            if load_result.startswith("Error"):
                return f"Failed to restore session from log '{log_file}': {load_result}"
        
        # Prepare sub-agent logger
        logger_inst = self.agent_pool.get_logger(
            instance_name, 
            agent_class, 
            base_metadata={'supervisor': self.session_name}
        )

        # Build the final system message FIRST (before conversation init)
        # so there's only ever ONE system prompt in the conversation and logs.
        base_sys = getattr(agent, 'base_system_message', agent.system_message)
        lines = base_sys.strip().split('\n')
        
        if lines:
            # 1. Update the first line: "You are [Role]." -> "You are [Instance]."
            if lines[0].startswith("You are") and f" {instance_name}" not in lines[0]:
                lines[0] = f"You are {instance_name}."
            
            # 2. Insert session metadata after the tagline (usually line 2)
            metadata_block = [
                "## Session Metadata",
                f"- Supervisor: {self.session_name}",
                f"- Working Dir: {logger_inst.data['metadata'].get('working_dir', 'Unknown')}",
                f"- Log Path: {logger_inst.log_path}",
            ]
            
            # Add extra paths to prompt if they exist
            extra_ro = logger_inst.data['metadata'].get('extra_paths_ro', [])
            extra_rw = logger_inst.data['metadata'].get('extra_paths_rw', [])
            if extra_ro:
                metadata_block.append(f"- Extra Paths (Read-Only): {', '.join(extra_ro)}")
            if extra_rw:
                metadata_block.append(f"- Extra Paths (Read-Write): {', '.join(extra_rw)}")
                
            metadata_block.append("Use your logs to recall details from turns that were compressed.\n")
            
            # Insert after the tagline if line 2 exists and isn't a header, otherwise after line 1
            insert_pos = 2 if len(lines) > 1 and not lines[1].startswith("#") else 1
            for i, metadata_line in enumerate(metadata_block):
                lines.insert(insert_pos + i, metadata_line)
                
        final_sys_content = "\n".join(lines)
        agent.system_message = final_sys_content

        if tool_name == 'call_agent':
            # Persistent sessions: if instance exists, we just append to it.
            if instance_name not in self.agent_pool.instance_conversations:
                # New instance: use the FINAL system message (with metadata already integrated)
                self.agent_pool.instance_conversations[instance_name] = [
                    Message(role=SYSTEM, content=final_sys_content)
                ]
                # Record the initial state in the log
                logger_inst.update_history(self.agent_pool.instance_conversations[instance_name])
            else:
                # Existing instance: update the system message in-place with latest metadata
                conv_existing = self.agent_pool.instance_conversations[instance_name]
                if conv_existing and conv_existing[0].get(ROLE) == SYSTEM:
                    conv_existing[0].content = final_sys_content
                if not logger_inst.data["history"]:
                    logger_inst.update_history(conv_existing)
            
        task = args.get('task', '')
        context = args.get('context', '')
        
        caller_prefix = f"This is a message from {self.session_name}."
        if context:
            context = f"{caller_prefix}\n{context}"
        else:
            context = caller_prefix

        msg_text = f'Context: {context}\n\nTask: {task}\n\nPlease help with this task.'
        if not msg_text.strip():
            msg_text = "Please proceed with your task."

        # --- Resolve multimodal content (Images) ---
        sub_agent_msg_content = [{'text': msg_text}]
        
        seen_images = {}
        for msg in manager_history:
            content = msg.get(CONTENT)
            if isinstance(content, list):
                for item in content:
                    # Content item might be dict or object
                    item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                    item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                    if item_type == IMAGE:
                        img_url = item_value
                        basename = get_basename_from_url(img_url)
                        seen_images[basename] = img_url
                        idx = len([v for k, v in seen_images.items() if not k.startswith("image_")]) - 1
                        seen_images[f"image_{idx}"] = img_url

        added_to_sub = set()
        for ref, img_url in seen_images.items():
            if ref in msg_text and img_url not in added_to_sub:
                sub_agent_msg_content.append({IMAGE: img_url})
                added_to_sub.add(img_url)
        
        if len(sub_agent_msg_content) == 1 and tool_name == 'call_agent':
            last_user_msg = next((m for m in reversed(manager_history) if m.get(ROLE) == USER), None)
            if last_user_msg:
                content = last_user_msg.get(CONTENT)
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == IMAGE and item_value not in added_to_sub:
                            sub_agent_msg_content.append({IMAGE: item_value})
                            added_to_sub.add(item_value)

        # streaming status is updated during the run

        # Initialize sub-agent chat for new turn
        # This block is from web_ui.py, not agent_orchestrator.py.
        # The instruction to reset self._last_active_sa, _last_stack_top, and _last_rendered_sa
        # applies to the agent_run method in web_ui.py, not here.
        # The provided code snippet for the change is a mix of files.
        # I will only apply the change to agent_orchestrator.py as per the first part of the instruction.
        # The second part of the instruction for web_ui.py cannot be applied here.

        conv = self.agent_pool.get_conversation(instance_name)
        user_msg = {ROLE: USER, CONTENT: sub_agent_msg_content}
        conv.append(user_msg)
        
        # Record user message in persistent log
        logger_inst.log_message(user_msg)
        
        # Track this call in the active stack for UI context switching
        self.agent_pool.active_stack.append(instance_name)

        # Telemetry: record sub-agent delegation start time
        _sub_agent_start = time.time()

        # Initialize streaming state for the WebUI
        state = {
            'active': True,
            'agent_name': f"{instance_name} ({agent_class})",
            'messages': copy.deepcopy(conv),
        }
        # Overwrite any existing state for this instance
        self.agent_pool.sub_agent_state[instance_name] = state

        # Force an immediate yield so the WebUI detects the new active_stack entry
        # and switches the subagent window context immediately.
        yield current_response

        # ── Propagate disabled_tools to sub-agent's LLM ──
        # The API server only patches the orchestrator's llm.generate_cfg with
        # disabled_tools from the frontend. Sub-agents have their own LLM instances,
        # so we must copy the policy over before running them.
        # ── Propagate agent settings to sub-agent ──
        # Always synchronize from the current orchestrator's settings to ensure
        # mid-session setting changes (e.g. from the UI) are respected.
        agent.max_turns = getattr(self, 'max_turns', 50)
        agent.auto_continue_enabled = getattr(self, 'auto_continue_enabled', True)
        
        # Propagate context window limit from supervisor to sub-agent
        if hasattr(self.llm, 'generate_cfg'):
            supervisor_max = self.llm.generate_cfg.get('max_input_tokens') or (self.llm.cfg and self.llm.cfg.get('max_input_tokens'))
            if supervisor_max:
                agent.llm.generate_cfg['max_input_tokens'] = supervisor_max

        orchestrator_disabled = getattr(self.llm, 'generate_cfg', {}).get('disabled_tools')
        if orchestrator_disabled and hasattr(agent, 'llm') and agent.llm:
            if not hasattr(agent.llm, 'generate_cfg') or agent.llm.generate_cfg is None:
                agent.llm.generate_cfg = {}
            agent.llm.generate_cfg['disabled_tools'] = orchestrator_disabled

        # Run the sub-agent as a generator
        final_resp: list = []
        try:
            # Run the sub-agent as a generator with an internal retry loop for loop recovery
            max_internal_retries = 3
            internal_retries = 0
            while internal_retries <= max_internal_retries:
                try:
                    # ── Monkey-patch sub-agent's _call_llm to enforce compression ──
                    if not hasattr(agent, '_original_call_llm'):
                        agent._original_call_llm = agent._call_llm

                        def hooked_call_llm(self_agent, messages: List[Message], **kwargs_llm):
                            # --- ASYNC MESSAGE INJECTION (per-agent routed) ---
                            hook_pending = self.agent_pool.drain_queue(instance_name)
                            for async_msg_text in hook_pending:
                                if not async_msg_text.strip():
                                    continue  # Skip empty messages
                                async_msg = Message(role=USER, content=async_msg_text)
                                messages.append(async_msg)
                                logger_inst.log_message(async_msg)  # Log to JSONL file
                                logger.info(f"Injected async user message into sub-agent {instance_name} via LLM hook: {async_msg_text}")

                            hook_forced = self._inject_compression_warning_for_agent(self_agent, instance_name, messages)
                            
                            # If forced compression ran, halt this LLM call — don't proceed.
                            if hook_forced:
                                logger.info(f"Forced compression for sub-agent {instance_name} — halting LLM call.")
                                # Sync messages from pool so the sub-agent's next iteration sees compressed state
                                compressed = self.agent_pool.get_conversation(instance_name)
                                if compressed:
                                    messages.clear()
                                    messages.extend(compressed)
                                return
                            
                            # --- CALL LLM WITH AUTO-CONTINUE ---
                            # We wrap the generator to detect truncation (finish_reason='length')
                            last_output = []
                            for output in self_agent._original_call_llm(messages, **kwargs_llm):
                                if self.agent_pool.stopped:
                                    logger.info(f"Sub-agent {instance_name} LLM call interrupted by stop flag.")
                                    break
                                # Check per-instance halt — pause takes effect mid-stream
                                if self.agent_pool.is_halted(instance_name):
                                    logger.info(f"Sub-agent {instance_name} halted during LLM stream.")
                                    break
                                last_output = output
                                yield output
                            
                            # Detect truncation
                            is_truncated = False
                            for msg in last_output:
                                if msg.extra and msg.extra.get('finish_reason') == 'length':
                                    is_truncated = True
                                    break
                            
                            # Read auto-continue setting from current orchestrator config
                            # Note: self is the OrchestratorAgent instance
                            auto_continue_enabled = getattr(self, 'auto_continue_enabled', True)
                            
                            if is_truncated and auto_continue_enabled and not self.agent_pool.stopped:
                                logger.info(f"Sub-agent {instance_name} truncated by length limit. Auto-triggering continuation...")
                                cont_msg = Message(role=USER, content="[SYSTEM]: Your previous response was cut off by the length limit. Please continue exactly from where you left off, without repeating yourself.")
                                messages.append(cont_msg)
                                # Log it
                                try:
                                    logger_inst.log_message(cont_msg)
                                except Exception:
                                    pass
                                # Recursive call to continue the same LLM turn
                                yield from hooked_call_llm(self_agent, messages, **kwargs_llm)

                        import types
                        agent._call_llm = types.MethodType(hooked_call_llm, agent)
                    
                    # agent.run mutates the passed list, so we pass a copy to avoid double-appending
                    # when we do state['messages'] = conv + resp
                    # Extract the optimized working set for the LLM
                    working_history = self.agent_pool.slice_history_for_llm(conv)
                    
                    # Removed base_len logic as it interferes with legitimate external edits.
                    
                    # Pass instance name through kwargs so tools (like compress_context) know who they are contextually
                    for resp in agent.run(working_history, agent_instance_name=instance_name):
                        if self.agent_pool.stopped:
                            logger.info(f"Sub-agent {instance_name} interrupted by user stop signal.")
                            yield current_response
                            break
                        
                        # --- SUB-AGENT LOOP DETECTION ---
                        # Check the full history of the sub-agent to detect loops across turns
                        loop_info = detect_loop(list(conv) + list(resp))
                        if loop_info:
                            loop_reason, pop_count = loop_info
                            logger.warning(f"Loop detected for sub-agent {instance_name}: {loop_reason}")
                            raise LoopDetectedError(loop_reason, agent_name=instance_name, pop_count=pop_count, turn_pop_count=len(resp), resp_snapshot=list(resp))
                            
                        final_resp = resp

                        # Sync stream state using the definitive 'conv' reference
                        # If 'conv' was edited externally (UI/compression), it is already updated since it's a reference to instance_conversations.
                        state['messages'] = list(conv) + list(resp)
                        yield current_response
                        
                        # Efficient logging: check if a tool call was just completed
                        if resp and (resp[-1].get(ROLE) == FUNCTION or resp[-1].get('function_call')):
                            # Incremental logging via log_message (already called in agent loop) 
                            # ensures history is persistent. update_history is redundant here.
                            pass
                    
                    # Turn successfully completed
                    break
                    
                except LoopDetectedError as e:
                    internal_retries += 1
                    if internal_retries > max_internal_retries:
                        logger.error(f"Sub-agent {instance_name} hit hard internal retry limit for loop: {e.reason}. Kicking back to main.")
                        raise e
                    
                    logger.warning(f"Sub-agent {instance_name} loop detected internally ({internal_retries}/{max_internal_retries}). Surgically rolling back {e.pop_count} messages...")
                    
                    # Telemetry: Record the internal loop and rollback
                    try:
                        if hasattr(self.agent_pool, 'telemetry'):
                            self.agent_pool.telemetry.record_loop_detected(
                                instance_name, 
                                e.reason, 
                                auto_rolled_back=True, 
                                pop_count=e.pop_count
                            )
                    except Exception:
                        pass
                    
                    # New Logic: Commit any prefix of the turn that was NOT part of the loop
                    if e.resp_snapshot:
                        if e.pop_count < len(e.resp_snapshot):
                            keep_count = len(e.resp_snapshot) - e.pop_count
                            if keep_count > 0:
                                good_messages = e.resp_snapshot[:keep_count]
                                conv.extend(good_messages)
                                logger.info(f"Partial loop recovery: Committed {keep_count} successful messages from turn to history.")
                            # No pool rollback needed as loop was entirely in resp
                        else:
                            # Loop spans back into pool history
                            pool_pop = e.pop_count - len(e.resp_snapshot)
                            if pool_pop > 0:
                                self.agent_pool.surgical_rollback(instance_name, pool_pop, soft=True, reason=e.reason)
                    elif e.pop_count > 0:
                        # Fallback for when resp_snapshot is missing (shouldn't happen with new code)
                        self.agent_pool.surgical_rollback(instance_name, e.pop_count, soft=True, reason=e.reason)
                    
                    # Ensure the persistent log is truncated to match the exact rolled-back state of conv
                    # This guarantees that if the loop was entirely within resp_snapshot (no pool rollback),
                    # the incrementally logged bad messages are removed.
                    logger_inst.truncate_to(len(conv), soft=True, reason=e.reason)

                    # Inject a corrective hint for the sub-agent
                    sub_hint = f"[SYSTEM]: Your last actions resulted in a repetitive loop ({e.reason}). Please try a different approach to solve the task."
                    conv.append({ROLE: USER, CONTENT: sub_hint})
                    
                    # Sync log and UI state immediately so user sees the hint and rollback in history
                    logger_inst.update_history(conv)
                    state['messages'] = list(conv)
                    self.agent_pool.sub_agent_state[instance_name] = state
                    
                    # Restart the turn for this sub-agent
                    continue

            if instance_name in self.agent_pool.terminated_instances:
                self.agent_pool.terminated_instances.remove(instance_name)
                return f"[User]: Sub-agent {instance_name} was terminated by user."

            if final_resp:
                # IMPORTANT: Update the persistent conversation instance with the FULL TURN result.
                # This ensures the next turn (or next 'call_agent') sees the tool results.
                conv.extend(final_resp)
                
                # Messages were already logged incrementally via log_message in the turn loop.
                # update_history(conv) is redundant here.
                pass
                
                # Extraction logic: get only text blocks from the last successful turn 
                # to avoid repeating the whole task/context history in the manager's prompt.
                result_str = extract_sub_agent_feedback(final_resp, instance_name)
                # Telemetry: record sub-agent call completion
                try:
                    if hasattr(self.agent_pool, 'telemetry'):
                        _sa_latency = (time.time() - _sub_agent_start) * 1000
                        self.agent_pool.telemetry.record_sub_agent_call(
                            self.session_name, agent_class, instance_name, latency_ms=_sa_latency
                        )
                except Exception:
                    pass
                return f"[{instance_name}'s output]:\n{result_str}"
            else:
                return f"[{instance_name}] finished with no output."

        except LoopDetectedError:
            # Re-raise so the orchestrator's _run can catch it and propagate to api_server
            raise
        except Exception as e:
            logger.error(f"Error in sub-agent {instance_name}: {str(e)}", exc_info=True)
            return f"Error executing sub-agent {instance_name}: {str(e)}"
        finally:
            # Clean up state
            state['active'] = False
            # Ensure the UI state reflects the full history including the final turn results
            state['messages'] = list(conv)
            self.agent_pool.sub_agent_state[instance_name] = state
            
            # Remove from active stack (pop the most recent occurrence)
            removed = False
            for i in range(len(self.agent_pool.active_stack) - 1, -1, -1):
                if self.agent_pool.active_stack[i] == instance_name:
                    self.agent_pool.active_stack.pop(i)
                    removed = True
                    break
            
            # If the instance was marked for termination (dismissed from UI), clear it now that the loop is done
            if removed and instance_name in self.agent_pool.terminated_instances:
                self.agent_pool.clear_conversation(instance_name)
                self.agent_pool.terminated_instances.discard(instance_name)




# ─── Utility ───────────────────────────────────────────────────────────────────

def extract_sub_agent_feedback(messages: List[Dict], instance_name: str) -> str:
    """
    Extracts text output from sub-agent messages.
    Only includes text generated AFTER the last tool call ended.
    """
    last_tool_idx = -1
    for i, msg in enumerate(messages):
        if msg.get(ROLE) == FUNCTION or msg.get('function_call'):
            last_tool_idx = i

    relevant_msgs = messages[last_tool_idx + 1:] if last_tool_idx != -1 else messages

    collected_text = []
    for msg in relevant_msgs:
        if isinstance(msg, dict):
            msg_role = msg.get('role', '')
        else:
            msg_role = msg.role

        if msg_role == ASSISTANT:
            text = extract_text_from_message(msg, add_upload_info=False)
            if text:
                collected_text.append(text)

    result_str = "\n\n".join(collected_text).strip()

    if not result_str:
        if last_tool_idx != -1:
            return f"WARNING: Sub-agent {instance_name} performed tool calls but provided no final summary."
        return f"Sub-agent {instance_name} finished but provided no text output."

    return result_str


# ─── Backward-compatible re-exports ────────────────────────────────────────────
# These were extracted into their own modules during the restructure.
# Existing imports like `from agent_orchestrator import AgentPool` still work.

from agent_pool import AgentPool  # noqa: F401
from agent_logger import AgentInstanceLogger  # noqa: F401
from agent_factory import load_orchestrator_agent, load_sub_agent_with_tools  # noqa: F401


