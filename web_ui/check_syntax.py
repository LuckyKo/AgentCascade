import ast
import sys

try:
    with open('N:\\work\\WD\\AgentCascade\\web_ui\\app.js', 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Basic brace matching
    stack = []
    for i, char in enumerate(content):
        if char in '({[':
            stack.append((char, i+1))
        elif char in ')}]':
            if not stack:
                print(f"Line {i+1}: Unexpected closing '{char}'")
                sys.exit(1)
            last = stack.pop()
            opens = {'(':')', '{':'}', '[':']'}
            if opens[last[0]] != char:
                print(f"Line {i+1}: ')'{char} does not match '{{' from line {last[1]}")
    
    if stack:
        for open_char, line in stack:
            print(f"Unclosed '{open_char}' from line {line}")
    
    # Try to parse with JavaScript parser if available
    try:
        import jsparser
        jsparser.parse(content)
        print("Parsed successfully with jsparser")
    except Exception as e:
        print(f"jsparser failed: {e}")

except Exception as e:
    print(f"Error: {e}")
