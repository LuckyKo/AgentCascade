import json

log_path = r"n:\work\WD\AgentWorkspace\logs\Security_advisor_security_advisor_20260516_214342.jsonl"
with open(log_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '"role": "assistant"' in line:
        data = json.loads(line)
        content = data.get('content', '')
        print(f"Line {i+1} content: {repr(content)}")
        print(f"Content hex: {content.encode('utf-8').hex()}")
