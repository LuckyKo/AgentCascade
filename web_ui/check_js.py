#!/usr/bin/env python3
"""Check JavaScript syntax structure by tracking braces, parentheses, and brackets."""

def check_structure(filename):
    with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Track line numbers properly
    lines = content.split('\n')
    
    # State tracking
    stack = []  # (char, line_number)
    in_string = False
    in_template = False
    escape = False
    string_char = None
    
    for line_num, line in enumerate(lines, 1):
        i = 0
        while i < len(line):
            ch = line[i]
            
            # Handle escape sequences
            if escape:
                escape = False
                i += 1
                continue
            
            if ch == '\\':
                escape = True
                i += 1
                continue
            
            # Toggle string modes
            if not in_string and not in_template and (ch == '"' or ch == "'"):
                in_string = True
                string_char = ch
                i += 1
                continue
            elif in_string and ch == string_char:
                in_string = False
                string_char = None
                i += 1
                continue
            
            if not in_string and not in_template and ch == '`':
                in_template = True
                i += 1
                continue
            elif in_template and ch == '`':
                in_template = False
                i += 1
                continue
            
            # Skip if inside string or template
            if in_string or in_template:
                i += 1
                continue
            
            # Track braces, parens, brackets
            if ch in '({[':
                stack.append((ch, line_num))
            elif ch in ')}]':
                if not stack:
                    print(f"Line {line_num}: Unexpected closing '{ch}'")
                    return False
                last = stack.pop()
                opens = {'(':')', '{':'}', '[':']'}
                if opens[last[0]] != ch:
                    print(f"Line {line_num}: '{ch}' does not match '{last[0]}' from line {last[1]}")
                    return False
            
            i += 1
    
    if stack:
        for open_char, line_num in stack:
            print(f"Unclosed '{open_char}' from line {line_num}")
    
    return len(stack) == 0

if __name__ == '__main__':
    result = check_structure('N:\\work\\WD\\AgentCascade\\web_ui\\app.js')
    print("Result:", "OK" if result else "ERROR")
