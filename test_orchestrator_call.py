import sys
import logging
from agent_cascade.agent_pool import AgentPool
from agent_cascade.agent_factory import load_orchestrator_agent
from agent_cascade.llm.schema import Message

# No basicConfig to avoid deadlock

llm_cfg = {
    'model': 'qwopus3.6-27b-v1-preview',
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
    'max_input_tokens': 65536,
    'log_api_post': True,
}

print("Initializing pool...")
# Passing operation_manager=None for the test
pool = AgentPool(llm_cfg, 'agents', workspace_dir='./workspace')
pool.start()

print("Loading orchestrator...")
orchestrator = load_orchestrator_agent(pool, "Orchestrator")

from agent_cascade.execution_engine import ExecutionEngine
print("Sending message...")
messages = [Message(role="user", content="Hello test!")]
try:
    engine = ExecutionEngine(pool)
    instance = pool.create_instance(
        instance_name="Orchestrator",
        agent_class="orchestrator",
        conversation=messages
    )
    for chunk in engine.run(instance):
        pass
except Exception as e:
    import traceback
    print(f"Exception during run: {e}")
    traceback.print_exc()

pool.stopped = True
