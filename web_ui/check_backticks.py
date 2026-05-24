import re

path = r"N:\work\WD\AgentCascade_unified\web_ui\app.js"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Count total backticks per line
total = 0
odd_lines = []
for i, line in enumerate(lines, 1):
    count = line.count('`')
    total += count
    if count > 0:
        odd_lines.append((i, count, line.rstrip()[:100]))

print(f"Total backticks: {total}")
print(f"Odd? {total % 2 == 1}")
print(f"\nLines with backticks ({len(odd_lines)} lines):")
for lineno, cnt, snippet in odd_lines:
    print(f"  Line {lineno}: {cnt} backtick(s) - {snippet}")

# Check for potential unbalanced template literals
# A template literal starts with ` and ends with ` (not escaped as \`)
in_template = False
template_start = 0
for i, line in enumerate(lines, 1):
    j = 0
    while j < len(line):
        ch = line[j]
        if ch == '\\' and j + 1 < len(line):
            j += 2  # skip escaped character
            continue
        if ch == '`':
            if not in_template:
                in_template = True
                template_start = i
            else:
                in_template = False
        j += 1
    
    # Check for unterminated template at end of file
    if in_template and i == len(lines):
        print(f"\nWARNING: Unterminated template literal starting at line {template_start}")

if not in_template:
    print("\nAll template literals appear properly closed.")