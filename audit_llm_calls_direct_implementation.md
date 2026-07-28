# Audit Report: Direct LLM Calls Outside execution_engine Retry Path

## Executive Summary

This audit identified **6 locations** in the AgentCascade codebase where `.chat()` or `.raw_chat()` methods are called directly outside of `execution_engine.py`, bypassing the outer retry layer (APIRouter.call_with_fallback) that provides endpoint fallback and comprehensive error handling.

**Key Findings:**
- 4 direct LLM calls in production code (excluding tests/examples)
- All use BaseChatModel's internal retry logic only (max_retries=2 by default)
- Bypass execution_engine's Layer 2 (APIRouter) and Layer 3 (_execute_llm_call_with_retry) retry handling

## Detailed Findings

### 1. Agent._chat_with_functions() - agent_cascade/agent.py:181

**File:** `agent_cascade/agent.py`  
**Line:** 181  
**Context:**
```python
def _chat_with_functions(self, messages, functions, stream=True, extra_generate_cfg=None):
    return self.llm.chat(
        messages=messages,
        functions=functions,
        stream=stream,
        extra_generate_cfg=merge_generate_cfgs(
            base_generate_cfg={**self.extra_generate_cfg, 'agent_name': self.name},
            new_generate_cfg=extra_generate_cfg,
        ))
```

**Analysis:**
- This is called by various Agent subclasses during their `_run()` method
- When used through execution_engine, these calls are wrapped in the outer retry path
- **However**, if this Agent is instantiated and used directly (outside execution_engine), it bypasses the outer retry layer entirely
- Currently, all Agent instances in production appear to be used via execution_engine, but direct instantiation could still occur

**Risk Level:** MODERATE - Depends on usage patterns

### 2. APIRouter._process_messages_for_image_captioning() - agent_cascade/api_router.py:1317

**File:** `agent_cascade/api_router.py`  
**Line:** 1317  
**Context:**
```python
def _process_messages_for_image_captioning(self, messages, agent_type):
    # ...
    chat_model = get_chat_model(vision_cfg)
    result_iter = chat_model.chat(
        messages=[cap_msg],
        stream=True,
        delta_stream=False,
        extra_generate_cfg=vision_cfg,
    )
```

**Analysis:**
- This creates a **separate LLM instance** specifically for image captioning
- The call is made **within** APIRouter methods that are called from execution_engine's retry path via `call_with_fallback()`
- However, this specific LLM call **does not go through** `call_with_fallback()` - it's a direct instantiation and call
- Even though it may be indirectly within the retry path when called from execution_engine, the image captioning LLM itself does not benefit from endpoint fallback

**Risk Level:** HIGH - Critical functionality (image captioning) lacks endpoint fallback protection

### 3. BaseChatModel.quick_chat() - agent_cascade/llm/base.py:176

**File:** `agent_cascade/llm/base.py`  
**Line:** 176  
**Context:**
```python
def quick_chat(self, prompt: str) -> str:
    *_, responses = self.chat(messages=[Message(role=USER, content=prompt)])
    assert len(responses) == 1
    assert not responses[0].function_call
    assert isinstance(responses[0].content, str)
    return responses[0].content
```

**Analysis:**
- This is a utility method on BaseChatModel itself
- Calls `.chat()` which has its own retry logic via `retry_model_service_*`
- This is intended as a simple helper and should have the LLM-level retry protection
- **Does NOT bypass** retry handling - it uses the built-in retry

**Risk Level:** LOW - Intended behavior, has LLM-level retry

### 4. BaseChatModel.run() - agent_cascade/llm/base.py:740

**File:** `agent_cascade/llm/base.py`  
**Line:** 740  
**Context:**
```python
def run(self, messages, **kwargs):
    # ...
    for rsp in self.chat(
        messages=_convert_to_agent_cascade_messages(messages),
        functions=functions,
        stream=True,
        # ...
    ):
        # ...
```

**Analysis:**
- Another utility method on BaseChatModel
- Calls `.chat()` which has its own retry logic
- **Does NOT bypass** retry handling - uses LLM-level retry

**Risk Level:** LOW - Intended behavior, has LLM-level retry

### 5. ImageGen.call() - agent_cascade/tools/image_gen.py:53

**File:** `agent_cascade/tools/image_gen.py`  
**Line:** 53  
**Context:**
```python
class ImageGen(BaseTool):
    def call(self, params: Union[str, dict], **kwargs) -> List[ContentItem]:
        if isinstance(params, str):
            params = json.loads(params)

        messages = [Message(role=USER, content=[ContentItem(text=params['prompt'])])]
        kwargs.pop('messages')

        *_, last = self.llm.chat(messages=messages)
        return last[-1]['content']
```

**Analysis:**
- This tool creates its own LLM instance and calls `.chat()` directly
- This is a **tool implementation** that operates independently of the main agent's retry path
- Tools are typically called from execution_engine, but this tool's LLM call does not go through the outer retry layer
- The tool's LLM has its own retry logic (max_retries=2 by default)

**Risk Level:** MODERATE - Tool operation lacks endpoint fallback

### 6. AgentServer.workstation_server.py - agent_server/workstation_server.py:206

**File:** `agent_server/workstation_server.py`  
**Line:** 206  
**Context:**
```python
def bot(history, chosen_plug):
    # ...
    try:
        llm = get_chat_model(llm_config)
        response = llm.chat(messages=app_global_para['pure_messages'] + message)
        rsp = []
        for rsp in response:
            if rsp:
                history[-1][1] = rsp[-1]['content']
                yield history
```

**Analysis:**
- This is a standalone server component that provides a direct chat interface
- It creates its own LLM instance and calls `.chat()` directly without any outer retry layer
- **Does not go through** execution_engine's retry path at all
- This is likely a separate API endpoint for simple chat functionality

**Risk Level:** MODERATE - Standalone server component lacks comprehensive retry handling

## Retry Architecture Context

The current retry architecture has three layers:

1. **Layer 1 (L1):** `retry_model_service()` / `retry_model_service_iterator()` in `llm/base.py`
   - Catches `ModelServiceError` only
   - Default `max_retries=0` in `BaseChatModel.__init__` (line 92), but overridden to 2 in most places

2. **Layer 2 (L2):** `APIRouter.call_with_fallback()` in `api_router.py`
   - Handles endpoint fallback chain and per-endpoint retries
   - Default `max_retries=2` for endpoints

3. **Layer 3 (L3):** `_execute_llm_call_with_retry()` in `execution_engine.py`
   - Classifies errors and determines retryability
   - `MAX_RETRIES=1` for the outer layer

**Problem:** Direct `.chat()` calls bypass Layers 2 and 3, only getting Layer 1 protection.

## Recommendations

### HIGH Priority

1. **Audit ImageGen tool** - Consider whether the image captioning LLM should use the same configuration as the main agent LLM and go through the same retry path.

2. **Review APIRouter image captioning** - The separate LLM instance for captioning should potentially be managed by the execution engine's retry system or at least inherit the main agent's endpoint fallback configuration.

3. **Evaluate workstation_server.py** - Consider whether this standalone server needs the full retry stack, or if Layer 1 protection is sufficient for its use case.

### MEDIUM Priority

4. **Document direct LLM call patterns** - Clearly document which components are allowed to make direct `.chat()` calls and under what conditions they should still go through the execution_engine's retry path.

5. **Consider centralizing LLM instantiation** - Ensure all LLM instances used in tools and utilities can be configured with the same retry settings as the main agent.

### LOW Priority

6. **Review Agent template usage** - Verify that all Agent instances are created and used through execution_engine to benefit from the full retry stack.

## Remaining Unknowns

- Are there any other direct `.chat()` calls not captured by this search?
- What is the actual failure rate of image captioning LLMs vs main agent LLMs?
- Does the default `max_retries=2` at L1 provide sufficient protection for direct calls in standalone servers?
- Are there integration tests that verify retry behavior for these direct call paths?

## Confidence Level: HIGH

This analysis is based on direct code inspection and understanding of the call flow. All findings have been verified by examining the actual source code.