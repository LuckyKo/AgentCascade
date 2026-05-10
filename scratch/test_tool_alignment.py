import sys
import os
import json

# Add the workspace to sys.path
sys.path.append(r'n:\work\WD\AgentCascade')

from agent_cascade.tools.custom.file_ops import ReadFile, WriteFile, EditFile, ListDir
from agent_cascade.tools.code_interpreter import CodeInterpreter
from agent_cascade.utils.utils import repair_invalid_json

class MockOpManager:
    base_dir = "scratch"
    def write_file(self, **kwargs):
        print(f"DEBUG: write_file called with {kwargs}")
        return "SUCCESS"
    def edit_file(self, **kwargs):
        print(f"DEBUG: edit_file called with {kwargs}")
        return "SUCCESS"
    def read_file(self, **kwargs):
        print(f"DEBUG: read_file called with {kwargs}")
        return "SUCCESS"
    def list_dir(self, **kwargs):
        print(f"DEBUG: list_dir called with {kwargs}")
        return "SUCCESS"

class MockAgentPool:
    def __init__(self):
        self.operation_manager = MockOpManager()

def test_write_file():
    print("--- Testing WriteFile ---")
    tool = WriteFile(agent_pool=MockAgentPool(), agent_name="test_agent")
    
    # 1. Standard JSON with new parameters
    print("1. Standard JSON (file_path)")
    tool.call('{"file_path": "test.py", "content": "print(\'hello\')"}')
    
    # 2. Backward compatibility (path)
    print("2. Backward compatibility (path)")
    tool.call('{"path": "test.py", "content": "print(\'hello\')"}')
    
    # 3. Robust fallback (Raw string)
    print("3. Robust fallback (Raw string)")
    tool.call("test.py\n```python\nprint('hello')\n```")
    
    # 4. JSON with markdown block inside content
    print("4. JSON with markdown block (\\n escaped)")
    input_str = '{"file_path": "test.py", "content": "```python\\nprint(\'hello\')\\n```"}'
    print(f"Repaired output: {repr(repair_invalid_json(input_str))}")
    tool.call(input_str)

    # 5. JSON with LITERAL newlines
    print("5. JSON with LITERAL newlines")
    tool.call('{"file_path": "test.py", "content": "line 1\nline 2"}')

def test_edit_file():
    print("\n--- Testing EditFile ---")
    tool = EditFile(agent_pool=MockAgentPool(), agent_name="test_agent")
    
    # 1. Standard JSON with new parameters
    print("1. Standard JSON (file_path, old_string, new_string)")
    tool.call('{"file_path": "test.py", "old_string": "old", "new_string": "new"}')
    
    # 2. Backward compatibility (path, old_content, new_content)
    print("2. Backward compatibility (path, old_content, new_content)")
    tool.call('{"path": "test.py", "old_content": "old", "new_content": "new"}')

def test_read_file():
    print("\n--- Testing ReadFile ---")
    tool = ReadFile(agent_pool=MockAgentPool(), agent_name="test_agent")
    
    # 1. Standard JSON with new parameters
    print("1. Standard JSON (absolute_path, offset)")
    tool.call('{"absolute_path": "test.py", "offset": 10, "limit": 5}')
    
    # 2. Backward compatibility (path, start_line)
    print("2. Backward compatibility (path, start_line)")
    tool.call('{"path": "test.py", "start_line": 1, "limit": 5}')

def test_code_interpreter():
    print("\n--- Testing CodeInterpreter ---")
    # We need to mock _check_docker_availability and _check_host_deps to avoid errors
    import agent_cascade.tools.code_interpreter as ci
    ci._check_docker_availability = lambda: None
    ci._check_host_deps = lambda: None
    
    tool = CodeInterpreter(cfg={'work_dir': 'scratch/tmp'})
    # Mock _execute_code to just return the code
    tool._execute_code = lambda kc, code: f"EXECUTED: {code}"
    # Mock _start_kernel
    tool._start_kernel = lambda kernel_id: (None, "container_id")
    
    # Test JSON with literal newlines
    print("1. JSON with LITERAL newlines")
    res = tool.call('{"code": "print(\'line 1\')\nprint(\'line 2\')"}', timeout=None)
    print(res)

if __name__ == "__main__":
    try:
        test_write_file()
        test_edit_file()
        test_read_file()
        test_code_interpreter()
    except Exception as e:
        import traceback
        traceback.print_exc()
