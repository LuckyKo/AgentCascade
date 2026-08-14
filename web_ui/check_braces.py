#!/usr/bin/env python3
"""Check brace matching in app.js and report errors."""

def check_braces(filename):
    with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    # Track positions of opening braces
    stack = []  # (char, line_num, char_index)
    in_template_literal = False
    template_start_line = None
    
    for i, line in enumerate(lines, 1):
        j = 0
        while j < len(line):
            ch = line[j]
            
            # Handle template literals (basic check - may fail with nested quotes)
            if ch == '`' and (j == 0 or line[j-1] != '\\'):
                in_template_literal = not in_template_literal
                template_start_line = i if in_template_literal else None
            
            # Skip if inside template literal for brace matching (simplification)
            if in_template_literal:
                j += 1
                continue
                
            if ch in '({[':
                stack.append((ch, i, j))
            elif ch in ')}]':
                if not stack:
                    print(f"Line {i}, col {j}: Unexpected closing '{ch}'")
                    return False
                last = stack.pop()
                opens = {'(':')', '{':'}', '[':']'}
                if opens[last[0]] != ch:
                    print(f"Line {i}, col {j}: '{ch}' does not match '{last[0]}' from line {last[1]}")
                    return False
            
            j += 1
        
        # Check for unclosed template literals at end of line
        if in_template_literal and not line.strip().endswith('`'):
            # Could be continuing to next line - just set flag
            pass
    
    if stack:
        for open_char, line_num, _ in stack:
            print(f"Unclosed '{open_char}' from line {line_num}")
    
    return len(stack) == 0

if __name__ == '__main__':
    result = check_braces('N:\\work\\WD\\AgentCascade\\web_ui\\app.js')
    print("Result:", "OK" if result else "ERROR")
