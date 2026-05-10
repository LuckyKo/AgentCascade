import re
import json

def repair_invalid_json(text: str) -> str:
    def escape_newlines(match):
        prefix = match.group(1)
        content = match.group(2)
        return prefix + '"' + content.replace('\n', '\\n') + '"'
    
    # Simple version for testing
    repaired = re.sub(r'(:\s*)"((?:[^"\\]|\\.)*?)"(?=\s*[,}\]])', escape_newlines, text, flags=re.DOTALL)
    return repaired

test_json = '{"file_path": "test.py", "content": "```python\nprint(\'hello\')\n```"}'
repaired = repair_invalid_json(test_json)
print(f"Original: {repr(test_json)}")
print(f"Repaired: {repr(repaired)}")

try:
    json.loads(repaired)
    print("JSON valid!")
except Exception as e:
    print(f"JSON invalid: {e}")
