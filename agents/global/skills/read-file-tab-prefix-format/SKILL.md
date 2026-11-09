---
name: read-file-tab-prefix-format
description: "read_file uses NNN:<TAB>content; the TAB is the separator, not indentation — strip it, match the target file's real indent style, and syntax_check after indentation-sensitive edits"
source: auto-generated
version: "1.2.0"
triggers:
  - "read_file transcription"
  - "edit_file old_content from read_file"
  - "strip line number prefix"
  - "read_file format"
  - "inconsistent use of tabs and spaces in indentation"
  - "mixed tabs and spaces indentation error"
  - "re_indent convert tabs to spaces"
generated_by: coder
generated_from_task: "list_agents soft-deprecate — repeated tab/space edit_file mistakes in agent_factory.py and engine/helpers.py"
---

## Goal

Correctly transcribe lines from `read_file` output into `edit_file` `old_content`/`new_content` without whitespace errors, and recover quickly when an edit mixes tabs and spaces.

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

### Step 3 — Compose old_content / new_content with the TARGET file's real indentation

When transcribing multiple lines, join their raw contents with `\n` (matching how the file stores them). Exact match mode requires byte-for-byte equality. The indentation you TYPE in `new_content` must match the target file's existing style for that block — not the separator tab.

### Step 4 — Verify after any indentation-sensitive edit

After `edit_file`/`re_indent` on a block that could change leading whitespace, `syntax_check` the file. If it reports `inconsistent use of tabs and spaces in indentation` (a hard SyntaxError, fatal on hot paths like tool-offering code), recover with `re_indent` `mode=convert`, `indent=4`, `indent_type=space` over the affected line range. Do not trust the diff rendering — verify byte-wise.

## Tips

- **Never assume the number of leading spaces from the visual display.** There is exactly one separator character (the tab); the bytes after it are the file's raw content.
- **The separator tab *renders* as leading indentation (BUG_0176).** A content line with **no** leading whitespace (`6:\tfor attempt:`) *looks* tab-indented, and a 4-space line (`7:\t    # ...`) looks like tab+4spaces. Strip everything up to and including the first TAB — that tab is the separator, NOT content. Confirm leading whitespace byte-wise (`repr(line)` via code_interpreter) before transcribing.
- **Match the block, not your assumption.** A file that uses tabs in one region and spaces in another (or 4-space throughout) must be matched exactly; the Python "tabs vs spaces" error surfaces at the first mismatched line, which may be far from where you typed. Copy a verified neighbor line's indentation.
- **grep output uses a space separator** (`path:LINE: content`). For exact content from a grep hit, re-read with `read_file`.
- **read_logs output** uses the same space separator (`NNN: content`) — same ambiguity applies.
- **The `NNN:` includes the colon.** Strip up to and including the first `\t` (`line.split('\t', 1)[1]`), not just the digits.

## Anti-patterns

| Pattern | Why it fails |
|---|---|
| Paste the read_file `NNN:\t` prefix into `new_content` | The separator tab becomes a literal tab in the file → mixed-indent SyntaxError |
| Assume a block is tab- (or space-) indented from the fence rendering alone | Visual tabs/spaces are ambiguous; verify byte-wise or copy a neighbor |
| Skip `syntax_check` after an indentation-sensitive edit | A hot-path SyntaxError ships silently until first import/run |
| Hand-edit a mixed-indent block again with the wrong whitespace | Repeats the error; use `re_indent mode=convert` instead |
