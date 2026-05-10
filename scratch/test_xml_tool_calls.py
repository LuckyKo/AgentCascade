"""Test the XML-delimited content field extraction in tool call parsing.

This verifies that:
1. XML content fields are correctly extracted
2. JSON portion is correctly isolated after XML stripping
3. Preprocess builds XML-delimited tool calls for history
4. Postprocess correctly merges XML fields into parsed arguments
5. Pure JSON tool calls still work (backward compatibility)
"""
import sys
import json

sys.path.insert(0, r'n:\work\WD\AgentCascade')

from agent_cascade.llm.fncall_prompts.nous_fncall_prompt import (
    _extract_xml_content_fields,
    _strip_xml_content_fields,
    _build_xml_tool_call,
    NousFnCallPrompt,
    XML_CONTENT_FIELDS,
)
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM, USER, ContentItem, FunctionCall, Message


def test_extract_xml_fields():
    """Test extracting XML fields from tool call text."""
    print("--- test_extract_xml_fields ---")

    # Basic content extraction
    text = '{"name": "write_file", "arguments": {"file_path": "/tmp/test.py"}}\n<content>\ndef hello():\n    print("Hello, world!")\n</content>'
    fields = _extract_xml_content_fields(text)
    assert 'content' in fields, f"Expected 'content' field, got: {fields}"
    assert 'def hello():' in fields['content'], f"Expected code in content, got: {fields['content']}"
    assert 'print("Hello, world!")' in fields['content']
    print(f"  PASS: Basic content extraction. Got {len(fields['content'])} chars")

    # Multiple XML fields (edit_file)
    text2 = '{"name": "edit_file", "arguments": {"file_path": "/tmp/test.py"}}\n<old_string>\n    print("old")\n</old_string>\n<new_string>\n    print("new")\n</new_string>'
    fields2 = _extract_xml_content_fields(text2)
    assert 'old_string' in fields2 and 'new_string' in fields2, f"Expected both fields, got: {list(fields2.keys())}"
    assert '    print("old")' in fields2['old_string']
    assert '    print("new")' in fields2['new_string']
    print(f"  PASS: Multiple XML fields extraction")

    # No XML fields (pure JSON)
    text3 = '{"name": "list_dir", "arguments": {"path": "/home/user"}}'
    fields3 = _extract_xml_content_fields(text3)
    assert fields3 == {}, f"Expected empty dict for pure JSON, got: {fields3}"
    print(f"  PASS: Pure JSON returns empty fields")

    # Code with quotes, backslashes, and special chars
    text4 = '<code>\nimport re\npattern = r"\\d+\\.\\d+"\nresult = re.findall(pattern, "3.14 and 2.71")\nprint(f"Found: {result}")\n</code>'
    fields4 = _extract_xml_content_fields(text4)
    assert 'code' in fields4
    assert 'import re' in fields4['code']
    assert 'r"\\d+\\.\\d+"' in fields4['code']
    print(f"  PASS: Code with special chars extracted correctly")


def test_strip_xml_fields():
    """Test stripping XML fields from text."""
    print("\n--- test_strip_xml_fields ---")

    text = '{"name": "write_file", "arguments": {"file_path": "/tmp/test.py"}}\n<content>\ndef hello():\n    print("Hello")\n</content>'
    stripped = _strip_xml_content_fields(text)
    assert '<content>' not in stripped
    assert '</content>' not in stripped
    assert '"file_path"' in stripped
    print(f"  PASS: XML stripped, JSON preserved. Result: {stripped[:80]}...")


def test_build_xml_tool_call():
    """Test building tool call strings with XML-delimited fields."""
    print("\n--- test_build_xml_tool_call ---")

    # write_file with content
    result = _build_xml_tool_call('write_file', {
        'file_path': '/tmp/test.py',
        'content': 'def hello():\n    print("Hello, world!")\n'
    })
    assert '<tool_call>' in result
    assert '</tool_call>' in result
    assert '<content>' in result
    assert '</content>' in result
    assert '"content"' not in result  # content should NOT be in JSON
    assert '"file_path"' in result    # file_path SHOULD be in JSON
    print(f"  PASS: write_file builds XML content block")
    print(f"    Result preview:\n{result[:300]}")

    # Short content stays in JSON
    result2 = _build_xml_tool_call('write_file', {
        'file_path': '/tmp/test.py',
        'content': 'hello'
    })
    assert '<content>' not in result2  # too short, stays in JSON
    assert '"content": "hello"' in result2
    print(f"  PASS: Short content stays in JSON")

    # edit_file with old/new strings
    result3 = _build_xml_tool_call('edit_file', {
        'file_path': '/tmp/test.py',
        'old_string': 'def hello():\n    print("old message")\n    return None\n',
        'new_string': 'def hello():\n    print("new message")\n    return True\n',
    })
    assert '<old_string>' in result3
    assert '<new_string>' in result3
    assert '"old_string"' not in result3
    assert '"new_string"' not in result3
    print(f"  PASS: edit_file builds XML old/new blocks")


def test_postprocess_xml():
    """Test that postprocess correctly handles XML-delimited tool calls."""
    print("\n--- test_postprocess_xml ---")

    prompt = NousFnCallPrompt()

    # Simulate an assistant message with XML-delimited write_file call
    xml_tool_call = '''<tool_call>
{"name": "write_file", "arguments": {"file_path": "/tmp/test.py"}}
<content>
def hello():
    x = "Hello, world!"
    print(f"Message: {x}")
    # This has "quotes" and special characters and newlines
</content>
</tool_call>'''

    msg = Message(
        role=ASSISTANT,
        content=[ContentItem(text=xml_tool_call)]
    )

    result = prompt.postprocess_fncall_messages([msg])

    # Should produce a single assistant message with function_call
    fn_call_msgs = [m for m in result if m.function_call]
    assert len(fn_call_msgs) == 1, f"Expected 1 function call, got {len(fn_call_msgs)}"

    fc = fn_call_msgs[0].function_call
    assert fc.name == 'write_file', f"Expected write_file, got {fc.name}"

    args = json.loads(fc.arguments) if isinstance(fc.arguments, str) else fc.arguments
    assert args['file_path'] == '/tmp/test.py'
    assert 'def hello():' in args['content']
    assert '"quotes"' in args['content']  # Quotes preserved!
    print(f"  PASS: XML write_file parsed correctly. Content length: {len(args['content'])}")

    # Test edit_file with XML
    xml_edit = '''<tool_call>
{"name": "edit_file", "arguments": {"file_path": "/tmp/test.py"}}
<old_string>
    x = "Hello, world!"
    print(f"Message: {x}")
</old_string>
<new_string>
    x = "Goodbye, world!"
    print(f"Farewell: {x}")
</new_string>
</tool_call>'''

    msg2 = Message(role=ASSISTANT, content=[ContentItem(text=xml_edit)])
    result2 = prompt.postprocess_fncall_messages([msg2])
    fn_call_msgs2 = [m for m in result2 if m.function_call]
    assert len(fn_call_msgs2) == 1

    fc2 = fn_call_msgs2[0].function_call
    args2 = json.loads(fc2.arguments) if isinstance(fc2.arguments, str) else fc2.arguments
    assert '"Hello, world!"' in args2['old_string']
    assert '"Goodbye, world!"' in args2['new_string']
    print(f"  PASS: XML edit_file parsed correctly")


def test_postprocess_pure_json():
    """Test that pure JSON tool calls still work (backward compatibility)."""
    print("\n--- test_postprocess_pure_json ---")

    prompt = NousFnCallPrompt()

    json_tool_call = '''<tool_call>
{"name": "list_dir", "arguments": {"path": "/home/user/project"}}
</tool_call>'''

    msg = Message(role=ASSISTANT, content=[ContentItem(text=json_tool_call)])
    result = prompt.postprocess_fncall_messages([msg])

    fn_call_msgs = [m for m in result if m.function_call]
    assert len(fn_call_msgs) == 1
    fc = fn_call_msgs[0].function_call
    assert fc.name == 'list_dir'
    args = json.loads(fc.arguments)
    assert args['path'] == '/home/user/project'
    print(f"  PASS: Pure JSON tool call still works")


def test_preprocess_round_trip():
    """Test that preprocess correctly emits XML format for history messages."""
    print("\n--- test_preprocess_round_trip ---")

    prompt = NousFnCallPrompt()

    # Create a history with a function call containing code content
    messages = [
        Message(role=SYSTEM, content='You are helpful.'),
        Message(role=USER, content=[ContentItem(text='Write a Python file')]),
        Message(
            role=ASSISTANT,
            content=[],
            function_call=FunctionCall(
                name='write_file',
                arguments=json.dumps({
                    'file_path': '/tmp/test.py',
                    'content': 'def hello():\n    print("Hello, world!")\n'
                })
            )
        ),
        Message(role=FUNCTION, content=[ContentItem(text='File written successfully')]),
    ]

    functions = [{
        'name': 'write_file',
        'description': 'Write a file',
        'parameters': {'type': 'object', 'properties': {'file_path': {'type': 'string'}, 'content': {'type': 'string'}}}
    }]

    preprocessed = prompt.preprocess_fncall_messages(messages, functions, 'en')

    # Find the assistant message with the tool call
    for msg in preprocessed:
        if msg.role == ASSISTANT:
            text = ''.join(item.text for item in msg.content if item.text)
            if '<tool_call>' in text:
                assert '<content>' in text, f"Expected XML <content> tag in preprocessed output, got: {text[:300]}"
                assert 'def hello():' in text
                assert '"content"' not in text.split('<content>')[0]  # content should NOT be in JSON portion
                print(f"  PASS: Preprocess emits XML format for content field")
                print(f"    Preview:\n{text[:400]}")
                return

    assert False, "No assistant message with <tool_call> found in preprocessed output"


if __name__ == '__main__':
    try:
        test_extract_xml_fields()
        test_strip_xml_fields()
        test_build_xml_tool_call()
        test_postprocess_xml()
        test_postprocess_pure_json()
        test_preprocess_round_trip()
        print("\n>> ALL TESTS PASSED")
    except Exception as e:
        import traceback
        print(f"\n>> TEST FAILED: {e}")
        traceback.print_exc()
