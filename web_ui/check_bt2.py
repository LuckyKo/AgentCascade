import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

path = r"N:\work\WD\AgentCascade_unified\web_ui\app.js"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

total = 0
lines_with_1 = []
for i, line in enumerate(lines, 1):
    count = line.count("`")
    total += count
    if count == 1:
        lines_with_1.append(i)

print(f"Total backticks: {total}")
print(f"Lines with exactly 1 backtick: {len(lines_with_1)}")
if len(lines_with_1) % 2 != 0:
    print(f"WARNING: Odd number of single-backtick lines ({len(lines_with_1)}) - this indicates unbalanced template literals!")
else:
    print("Single-backtick lines are balanced (even count)")