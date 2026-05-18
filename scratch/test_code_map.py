import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(os.getcwd())

from agent_cascade.tools.custom.code_map import CodeMap

tool = CodeMap()
# Mock agent_pool for path resolution
class MockPool:
    class MockOps:
        base_dir = Path(os.getcwd())
    operation_manager = MockOps()

tool.agent_pool = MockPool()

print("--- Python Map (agent_pool.py) ---")
print(tool.call('{"path": "agent_pool.py"}'))

print("\n--- JavaScript Map (web_ui/app.js) ---")
print(tool.call('{"path": "web_ui/app.js"}'))
