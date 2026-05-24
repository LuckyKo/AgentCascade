import sys

path = r"N:\work\WD\AgentCascade_unified\web_ui\app.js"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Count brackets
pairs = {'[': ']', '{': '}', '(': ')'}
label_map = {'[': 'bracket', '{': 'brace', '(': 'paren'}

for ch in ['[', '{', '(']:
    open_count = content.count(ch)
    close_count = content.count(pairs[ch])
    status = "BALANCED" if open_count == close_count else f"MISMATCH (open={open_count}, close={close_count})"
    print(f"{label_map[ch]}s: {status}")

sq = content.count("'")
dq = content.count('"')
bt = content.count("`")
print(f"\nSingle quotes: {sq} (odd? {sq % 2 == 1})")
print(f"Double quotes: {dq} (odd? {dq % 2 == 1})")
print(f"Backticks: {bt}")

if bt % 2 == 1:
    print("WARNING: Odd number of backticks - possible unmatched template literal")