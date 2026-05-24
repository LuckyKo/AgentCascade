import re

content = open(r'N:\work\WD\AgentCascade\agent_cascade\tools\code_interpreter.py', encoding='utf-8').read()

# Count triple quotes
dq_count = content.count('"""')
sq_count = content.count("'''")
print(f'Triple double quotes: {dq_count}')
print(f'Triple single quotes: {sq_count}')

# Find positions of all triple-double-quotes
lines = content.split('\n')
pos_to_line = {}
current_pos = 0
for i, line in enumerate(lines):
    pos_to_line[current_pos] = i + 1
    current_pos += len(line) + 1  # +1 for newline

for m in re.finditer(r'"""', content):
    line_num = pos_to_line.get(m.start(), -1)
    context = content[max(0, m.start()-30):m.end()+30]
    print(f'Line {line_num}: ...{repr(context)}...')