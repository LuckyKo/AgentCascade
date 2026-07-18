---
name: fix_line_number_prefix_corruption
description: Detect and remove line-number prefix corruption (e.g. "1: ", "2: ") from files
source: auto-skill
extracted_at: '2026-06-05T18:35:41.425Z'
---

## Problem

A script or tool accidentally prepends line numbers to every line of a file, corrupting it. The pattern is: `<number>: <original content>` at the start of each line.

Example:
```
1: {
2:   "key": "value"
3: }
```

## Detection

- File content starts with `<digits>: ` prefix on every line
- Diff against HEAD shows massive changes (hundreds/thousands of lines) even though content is identical minus prefixes
- File may fail validation (e.g., JSON decode errors, Python syntax errors)

## Fix Procedure

1. **For tracked files** — restore from git HEAD first:
   ```
   git checkout HEAD -- <file>
   ```

2. **For untracked / external files** — strip line-number prefixes:
   - Write a small Python script to a temp file (multiline `-c` often fails on Windows)
   - Use this pattern:
     ```python
     import re
     with open(filepath, 'r') as f:
         content = f.read()
     lines = content.splitlines()
     cleaned = [re.sub(r'^\d+: ', '', line) for line in lines]
     with open(filepath, 'w', newline='') as f:
         f.write('\n'.join(cleaned))
     ```
   - Validate the result (e.g., `json.load()` for JSON files)
   - Delete the temp script

## Why This Happens

A script that processes files (e.g., for display, logging, or diffing) writes line-number prefixes into the file content itself instead of just for display purposes.
