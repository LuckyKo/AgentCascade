"""
Analyze all .settings.* assignments and check for save calls.
"""

import re
import os
from pathlib import Path

# Files to analyze
files_to_check = [
    "agent_cascade/agent_instance.py",
    "agent_cascade/agent_pool.py",
    "agent_cascade/api_integration.py",
    "agent_cascade/api_server.py",
    "agent_cascade/shared_init.py",
    "agent_cascade/config_handlers.py",
]

base_path = Path(__file__).parent.resolve()

# Pattern to match: .settings.attribute_name =
assignment_pattern = re.compile(r'\.settings\.(\w+)\s*=')
save_call_patterns = [
    re.compile(r'_save_pool_settings\s*\('),
    re.compile(r'_save_pool_settings_if_available\s*\('),
]

results = []

for file_path in files_to_check:
    full_path = base_path / file_path
    if not full_path.exists():
        print(f"File not found: {full_path}")
        continue
    
    with open(full_path, 'r', encoding='utf-8') as f:
        content = f.read()
        lines = content.splitlines()
    
    print(f"\n{'='*60}")
    print(f"File: {file_path}")
    print(f"{'='*60}")
    
    for i, line in enumerate(lines):
        if assignment_pattern.search(line):
            # Extract the attribute name
            match = assignment_pattern.search(line)
            attr_name = match.group(1) if match else "unknown"
            
            # Look ahead for save calls in next 3 lines
            has_save_call = False
            start_idx = max(0, i - 1)  # Also check same line
            end_idx = min(len(lines), i + 4)
            
            for j in range(start_idx, end_idx):
                for pattern in save_call_patterns:
                    if pattern.search(lines[j]):
                        has_save_call = True
                        break
                if has_save_call:
                    break
            
            # Show context
            context_start = max(0, i - 2)
            context_end = min(len(lines), i + 3)
            
            print(f"\nLine {i+1}:")
            print(f"  Assignment: {line.strip()}")
            print(f"  Attribute: {attr_name}")
            print(f"  Has save call: {'YES' if has_save_call else 'NO'}")
            
            if not has_save_call:
                print("  Context:")
                for k in range(context_start, context_end):
                    marker = ">>>" if k == i else "   "
                    print(f"  {marker} {k+1}: {lines[k]}")
            
            results.append({
                'file': file_path,
                'line': i+1,
                'assignment': line.strip(),
                'attribute': attr_name,
                'has_save_call': has_save_call,
            })

print(f"\n{'='*60}")
print(f"SUMMARY")
print(f"{'='*60}")
total = len(results)
without_save = [r for r in results if not r['has_save_call']]
print(f"Total assignments found: {total}")
print(f"Assignments without save call: {len(without_save)}")

if without_save:
    print("\nDetailed list of assignments MISSING save calls:")
    for r in without_save:
        print(f"- {r['file']}:{r['line']} - {r['assignment']}")