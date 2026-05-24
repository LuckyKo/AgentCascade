import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

path = r"N:\work\WD\AgentCascade_unified\web_ui\app.js"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

lines_with_1 = []
for i, line in enumerate(lines, 1):
    count = line.count("`")
    if count == 1:
        lines_with_1.append((i, line.rstrip()[:100]))

print(f"Lines with exactly 1 backtick ({len(lines_with_1)} total):")
for lineno, snippet in lines_with_1:
    print(f"  Line {lineno}: {snippet}")