#!/usr/bin/env python3
"""Detailed analysis of brace matching around lines 2660-2760."""

with open('N:\\work\\WD\\AgentCascade\\web_ui\\app.js', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

print("=== Detailed view (lines 2660-2760) ===\n")

func_start = None
for i, line in enumerate(lines[2659:2760], start=2660):
    print(f"{i:4d}: {line.rstrip()}")
    
    if 'function renderAgentMessages()' in line:
        func_start = 2660
        brace_count = 1
        print(f"\n--- STARTING FUNCTION: renderAgentMessages ---")
    
    if func_start is not None and i > func_start:
        for ch in line:
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0:
                    print(f"Function closes at line {i}")
                    func_start = None

print("\n=== Checking for template literals with braces ===\n")
# Check the specific template literal at lines 2668-2676
for i, line in enumerate(lines[2667:2677], start=2668):
    print(f"{i:4d}: {line.rstrip()}")
    if '{' in line:
        print(f"   ^ Contains {{ character")
    if '}' in line:
        print(f"   ^ Contains }} character")

print("\n=== Check the arrow function at lines 2723-2726 ===\n")
for i, line in enumerate(lines[2722:2728], start=2722):
    print(f"{i:4d}: {line.rstrip()}")
    if '{' in line:
        print(f"   ^ Contains {{ character")
    if '}' in line:
        print(f"   ^ Contains }} character")
