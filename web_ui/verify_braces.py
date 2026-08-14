#!/usr/bin/env python3
"""Verify brace matching in app.js, ignoring template literals."""

import re

def check_braces(filename):
    with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Remove all template literals (backticks) to avoid CSS interference
    # This is a simplification - we replace them with empty strings
    # But we must be careful not to break actual JS logic
    
    # Strategy: Track if we're inside a backtick string, and when we exit, skip content
    clean_content = []
    i = 0
    in_template = False
    while i < len(content):
        if not in_template and content[i] == '`':
            in_template = True
            i += 1
            continue
        elif in_template and content[i] == '`':
            in_template = False
            i += 1
            continue
        if in_template:
            # Skip characters inside template literal
            i += 1
            continue
        
        # Outside template literals, keep character
        clean_content.append(content[i])
        i += 1
    
    clean_content = ''.join(clean_content)
    
    # Now check braces
    stack = []
    for i, char in enumerate(clean_content):
        if char in '({[':
            stack.append((char, i+1))
        elif char in ')}]':
            if not stack:
                print(f"Line {i+1}: Unexpected closing '{char}'")
                return False
            last = stack.pop()
            opens = {'(':')', '{':'}', '[':']'}
            if opens[last[0]] != char:
                print(f"Line {i+1}: '{char}' does not match '{last[0]}' from line {last[1]}")
                return False
    
    if stack:
        for open_char, line_num in stack:
            print(f"Unclosed '{open_char}' from line {line_num}")
    
    return len(stack) == 0

if __name__ == '__main__':
    result = check_braces('N:\\work\\WD\\AgentCascade\\web_ui\\app.js')
    print("Result:", "OK" if result else "ERROR")
