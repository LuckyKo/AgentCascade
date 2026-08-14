#!/usr/bin/env python3
"""Parse app.js and check function structure."""

import re

def parse_functions(filename):
    with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    # Find all function declarations
    func_pattern = r'function\s+(\w+)\s*\([^)]*\)\s*\{'
    funcs = []
    for m in re.finditer(func_pattern, content):
        name = m.group(1)
        start = m.end()  # position after opening brace
        funcs.append((name, start))
    
    print(f"Found {len(funcs)} functions")
    
    # For each function, try to find its closing brace at the top level
    for i, (name, start) in enumerate(funcs):
        # Find matching closing brace
        brace_count = 1
        pos = start
        while brace_count > 0 and pos < len(content):
            if content[pos] == '{':
                brace_count += 1
            elif content[pos] == '}':
                brace_count -= 1
            pos += 1
        
        end = pos - 1
        # Calculate line number of closing brace
        lines = content[:end].split('\n')
        line_num = len(lines)
        
        print(f"{name}: starts after line {content[:start].count(chr(10))+1}, ends at line {line_num}")
        
        # Check if this function contains the error line
        error_line = 2721
        start_line = content[:start].count(chr(10)) + 1
        if start_line <= error_line <= line_num:
            print(f"  -> Function '{name}' spans line {error_line}")
        
        # Print a snippet around the function end to see structure
        if i < 5 or name in ['renderAgentMessages', 'createAgentMessageEl']:
            snippet = content[max(0, start-200):end+200]
            print(f"\n--- Snippet for {name} ---")
            print(snippet[:1000])
            print("--- End snippet ---\n")

if __name__ == '__main__':
    parse_functions('N:\\work\\WD\\AgentCascade\\web_ui\\app.js')
