import re

FILE_PATH = 'N:/work/WD/AgentCascade/agent_orchestrator.py'

with open(FILE_PATH, 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i in range(len(lines)):
    if 'reasoning = f' in lines[i] and 'reasoning_content' in lines[i]:
        indent = len(lines[i]) - len(lines[i].lstrip())
        pre_strip = ' ' * indent + 'reasoning_clean = re.sub(r\\s*<(think|thought>), '', m["reasoning_content"], flags=re.IGNORECASE | re.DOTALL)\n'
        lines.insert(i, pre_strip)
        lines[i+1] = lines[i+1].replace('m["reasoning_content"]', 'reasoning_clean')
        print(f'Fixed at line {i+2}')
        break

with open(FILE_PATH, 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('Done')