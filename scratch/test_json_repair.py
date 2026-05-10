import re
import json
import json5

def repair_invalid_json(text: str) -> str:
    if not isinstance(text, str):
        return text
    
    # 1. Handle triple quotes in values: """content""" -> "content" (with escaped newlines)
    # This regex looks for """ specifically as a value after a colon.
    repaired = re.sub(r'(?<=": )"""(.*?)"""(?=[,}\s])', 
                      lambda m: json.dumps(m.group(1).replace('\\n', '\n')).replace('\\\\n', '\\n'), 
                      text, flags=re.DOTALL)
    
    # 2. Handle literal newlines in double-quoted values (very common failure mode)
    # We look for content between double quotes that contains a literal newline and escape it.
    # Note: This is a bit risky if there are escaped quotes, but handles most cases.
    def escape_newlines(match):
        return match.group(0).replace('\n', '\\n')
    
    repaired = re.sub(r'":\s*"[^"]*[\n][^"]*"', escape_newlines, repaired)
    
    return repaired

test_cases = [
    '{"code": """import os\nprint("hello")"""}',
    '{"msg": "hello\nworld"}',
]

for tc in test_cases:
    print(f"Original: {repr(tc)}")
    repaired = repair_invalid_json(tc)
    print(f"Repaired: {repr(repaired)}")
    try:
        parsed = json5.loads(repaired)
        print(f"Parsed:   {parsed}")
    except Exception as e:
        print(f"FAILED:   {e}")
    print("-" * 20)
