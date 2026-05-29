import sys
import logging
from agent_cascade.llm.qwenvl_oai import QwenVLChatAtOAI
from agent_cascade.llm.schema import Message

# Configure logging to show all logs
logging.basicConfig(level=logging.DEBUG)

cfg = {
    'model': 'qwopus3.6-27b-v1-preview',
    'api_base': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
    'max_input_tokens': 65536,
}

print("Creating LLM instance...")
llm = QwenVLChatAtOAI(cfg)

print("Starting chat...")
messages = [Message(role="user", content="Hello, test.")]
try:
    # Use direct chat (non-streaming or streaming) to see where it fails
    # Let's try non-streaming first
    res = llm._chat_no_stream(messages, {})
    print("Direct non-streaming chat success! Output:")
    print(res)
except Exception as e:
    print(f"Direct non-streaming chat failed with exception of type {type(e)}:")
    print(e)
    import traceback
    traceback.print_exc()

try:
    print("\nStarting streaming chat...")
    res_stream = llm._chat_stream(messages, delta_stream=False, generate_cfg={})
    print("Direct streaming chat started. Reading stream...")
    for chunk in res_stream:
        print("Chunk:", chunk)
except Exception as e:
    print(f"Direct streaming chat failed with exception of type {type(e)}:")
    print(e)
    import traceback
    traceback.print_exc()
