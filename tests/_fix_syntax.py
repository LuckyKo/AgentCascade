"""Quick script to fix remaining syntax errors in test_compression_no_duplication.py"""
import os

path = r'N:\work\WD\AgentCascade_unified\tests\test_compression_no_duplication.py'
with open(path) as f:
    lines = f.readlines()

# Fix tail mismatch extra parens (line 2765, 0-indexed)
lines[2765] = '            f"Tail mismatch after reload: JSONL {len(jsonl_tail)}, pool {len(pool_tail)}"\n'

# Fix range(5 missing colon (line 2817)
lines[2817] = '        for i in range(5):\n'

# Fix mode="manual",)) -> mode="manual", (line 2822)
lines[2822] = '                mode="manual",\n'

# Fix summary_text line (line 2824) - broken string split across lines
lines[2824] = "                summary_text=f\"Marker content {i} with unique padding {'M' * (i + 1)}\")\n"

# Fix assert len(set...) missing closing paren (line 2842)
lines[2842] = '        assert len(all_marker_contents) == len(set(all_marker_contents)), \\\n'

# Fix extra parens on f-string (line 2845)
lines[2845] = '            f"{len(set(all_marker_contents))}"\n'

# Fix summary_text for system test round (line 2894)
lines[2894] = '                summary_text=f"System test round {i + 1}.")\n'

# Fix conv assignment (line 2897)
lines[2897] = '        conv = pool.get_conversation(inst_name)\n'

# Fix range(3 missing colon for system test (line 2932)
lines[2932] = '        for i in range(3):\n'

# Fix summary_text extra parens (line 2939)
lines[2939] = '                summary_text=f"JSONL system test round {i + 1}.")\n'

# Fix get_logger missing paren (line 2942)
lines[2942] = '        log_inst = pool.get_logger(inst_name, "coder")\n'

# Fix extra paren on final assert (line 2948)
lines[2948] = '        assert sys_count == 1, f"Expected exactly 1 system message in JSONL, found {sys_count}"\n'

with open(path, 'w') as f:
    f.writelines(lines)

print("Fixes applied")