---
name: read-file-tab-prefix-format
description: read_file output uses NNN:\\t as line-number prefix; split on first TAB to extract raw content for edit_file transcription
source: auto-generated
version: "1.1.0"
triggers:
  - "read_file transcription"
  - "edit_file old_content from read_file"
  - "strip line number prefix"
  - "read_file format"
generated_by: orchestrator
generated_from_task: "BUG_0158/BUG_0170 fix — read_file tab separator"
---

## Goal

Correctly transcribe lines from `read_file` output into `edit_file` `old_content` without off-by-one whitespace errors.

## Procedure

### Step 1 — Understand the format

`read_file` output is wrapped in code fences. Each content line has the format:

```\
NNN:\t<raw file content>
```

- `NNN` = 1-based line number
- `:` = literal colon (always present)
- `\t` = single TAB character (the separator)
- After the tab = the raw file content with original indentation preserved

Example output:
````
OK: Read src/main.py lines 1-3/10 (text, 250 B)
```
1:\tdef hello():
2:\t    print("world")
3:\t    return True
```
````

### Step 2 — Strip the prefix to get raw content

To extract line N's content for `edit_file`:
- Find the first TAB on the line
- Everything after the tab is the raw content (including leading spaces/tabs)

In Python: `content = line.split('\t', 1)[1]`

### Step 3 — Compose old_content

When transcribing multiple lines, join their raw contents with `\n` (matching how the file stores them). The exact match mode requires byte-for-byte equality.

## Tips

- **Never assume the number of leading spaces from the visual display.** With the tab separator, there is exactly one separator character (the tab); the bytes after it are the file's raw content.
- **The separator tab *renders* as a leading tab of indentation (BUG_0176).** Especially inside a code fence, a line whose content has **no** leading whitespace (e.g. `6:\tfor attempt:`) *looks* like it starts with a tab, and a 4-space line (`7:\t    # ...`) looks like a tab plus 4 spaces. When composing `old_content`, strip everything up to and including the first TAB — that tab is the separator, NOT content. For a block that sits inside a fenced code block (or otherwise looks indented), confirm the leading whitespace **byte-wise** (`repr(line)` via code_interpreter, or `grep` raw output) before transcribing.
- **grep output still uses a space separator** (`path:LINE: content`). If you need exact content from a grep hit, re-read the file with `read_file` to get the tab-prefixed lines.
- **read_logs output** uses the same space separator (`NNN: content`) — same ambiguity applies if transcribing into edit_file.
- **The `NNN:` includes the colon.** When parsing, strip everything up to and including the first `\t` (i.e., `line.split('\t', 1)[1]`), not just the digits.
