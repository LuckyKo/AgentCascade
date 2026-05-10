import pytest
from agent_cascade.tools import CodeInterpreter

def test_code_interpreter_dict_input():
    tool = CodeInterpreter()
    # This should not raise TypeError
    params = {"code": "print('hello world')"}
    result = tool.call(params)
    assert "hello world" in result

def test_code_interpreter_string_input():
    tool = CodeInterpreter()
    # This should work as before
    params = "print('hello world')"
    result = tool.call(params)
    assert "hello world" in result

if __name__ == "__main__":
    test_code_interpreter_dict_input()
    test_code_interpreter_string_input()
