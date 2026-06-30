# File Operation Tool — Feedback Message Design Plan v2

**Date:** 2026-06-30  
**Scope:** `edit_file`, `write_file`, `read_file`, `re_indent`, `delete_file`, `copy_file`, `syntax_check` (+ dead code: `move_file`)  
**Codebase:** AgentCascade Unified (`agent_cascade/`)

---

## Design Principles

| # | Principle | Meaning |
|---|-----------|---------|
| 1 | **Tighter** | More information in fewer words. No filler phrases like "Please check the file..." |
| 2 | **More useful** | LLM should NOT need to re-read the file to verify changes |
| 3 | **Consistent** | Same structural patterns across all tools |
| 4 | **Token-efficient** | Every word earns its place. No emoji, no fluff |

---

## P1: Prefix Standardization

Replace the inconsistent mix of `APPROVED:`, `Valid (lang)`, bare text with two simple prefixes:

| Status | Prefix | When Used |
|--------|--------|-----------|
| Success | `OK:` | Operation completed successfully |
| Error | `ERROR:` | Something went wrong |
| Rejected | `REJECTED:` | User denied the operation (kept as-is, rare case) |

**Rationale:** No Emoji these operations are either OK or not. The LLM just needs to parse the status quickly.

---

## Message Format Convention

All success messages follow this structure:

```
OK: <verb> <path> (<key-facts>)
```

- **`<verb>`** — action taken (Created, Overwrote, Edited, Deleted, Copied, Re-indented, etc.)
- **`<path>`** — the file path as provided by the caller
- **`(<key-facts>)`** — comma-separated diagnostic details in parentheses

All error messages follow:

```
ERROR: <reason> (<context>)
```

### M3. Standardized Backup Line (Across All 6 Tools)

Backup info is appended on a separate line only when present, using this **exact format** across all tools (`write_file`, `edit_file`, `re_indent`, `delete_file`, `copy_file`):

```
  backup → <absolute-path>
```

(M5: Paths are always absolute. The code uses `str(backup_path)` directly instead of `backup_path.relative_to(self.base_dir)`. This eliminates the try/except fallback and produces consistent output like `C:\workspace\logs\backups\agent_name\file.py.1719600000.bak`.)

**Current inconsistencies being fixed:**
| Tool | Current Format | New Format |
|------|---------------|------------|
| `write_file` | `. Backup created: path...` (appended to main line with period) | `  backup → path...` (separate indented line) |
| `edit_file` | `(Backup saved to: path...)` (in parentheses at end of line) | `  backup → path...` (separate indented line) |
| `re_indent` | `(Backup saved to: path...)` (in parentheses at end of line) | `  backup → path...` (separate indented line) |
| `delete_file` | `. Backup created: path...` (appended with period) | `  backup → path...` (separate indented line) |
| `copy_file` | `. Backup created: path...` (appended with period) | `  backup → path...` (separate indented line) |

### M4. Size Formatting Convention

The `_format_size()` helper at line 116 returns strings **with a space** between number and unit: `"1.2 KB"`, `"48.6 MB"`, `"73 B"`. All design examples must use this exact format (space included). Design examples using `"1.2KB"` are corrected throughout.

---

## Tool-by-Tool Design

### 1. `edit_file` (Highest Priority — Biggest Pain Point)

**Current output:**
```
APPROVED: Edited path/to/file.py
```
or for heuristic:
```
APPROVED: Edited path/to/file.py (Heuristic match similarity: 87.5%) [NOTE: This file has been edited 4 times in heuristic mode this session. Indentation drift may have accumulated.]
  ⚠ Indentation anomaly at line 15 in path/to/file.py: indent increased from 4 to 12 (jump of 8 spaces, threshold=8)
Please check the file to ensure the insertion was applied correctly.
```

#### New Success Format — Exact Match

```
OK: Edited src/main.py lines 42-48 (exact, -3 +5 = +2net)
--- a/src/main.py
+++ b/src/main.py
@@ -40,10 +40,12 @@
 def foo():
-    x = 1
-    y = 2
-    return x + y
+    x = 1
+    y = 2
+    z = 3
+    result = x + y + z
+    if result > 10:
+        result = 10
+    return result
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

**Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `lines X-Y` | Line range of the matched/replaced block (1-based, inclusive) | `lines 42-48` |
| `exact` | Match mode label | `exact` |
| `-N +M = ±Knet` | Lines removed, lines added, net delta | `-3 +5 = +2net` |
| unified diff | Compact diff of actual changes (limited to 20 lines max) | see example above |

**C1. Line range computation for exact mode — implementation note:**  
At line 564, after the match succeeds (`count == 1`), store the byte offset of the matched block before it gets replaced. The response construction at line ~940 needs this data but `file_content` is re-read fresh there (line 934). So we must capture the position **at match time** and carry it forward:

```python
# At line 564 in file_operations.py, after count check passes:
match_start_pos = file_content.index(old_content)   # byte offset of matched block
match_end_pos = match_start_pos + len(actual_old_content)
# Convert to 1-based line numbers by counting newlines before the match:
match_start_line = file_content[:match_start_pos].count('\n') + 1
match_end_line = file_content[:match_end_pos].count('\n') + 1
```

These values (`match_start_line`, `match_end_line`) must be stored as instance variables or passed through to the response construction block. The cleanest approach: assign them alongside `actual_old_content` and `match_ratio` at module scope within the method, so they're available at line ~940 when building `res_msg`.

#### New Success Format — Heuristic Match

```
OK: Edited src/main.py lines 42-48 (heuristic 91%, -2 +4 = +2net, indent adjusted 4→8sp)
--- a/src/main.py
+++ b/src/main.py
@@ -40,6 +40,8 @@
 def foo():
     x = 1
+    y = 2
+    z = 3
     return x + y
+    return result
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### M2. Heuristic vs Heuristic-Agnostic Mode Discrimination

The `match_mode` parameter accepts `'heuristic'`, `'heuristic_agnostic'`, and `'exact'`. The two heuristic variants must be distinguishable in feedback:

| match_mode value | Label in feedback |
|-----------------|-------------------|
| `'heuristic'` | `heuristic XX%` |
| `'heuristic_agnostic'` | `heur_ag XX%` (shorter to save tokens; whitespace-only normalization) |

**Heuristic-agnostic example:**
```
OK: Edited src/main.py lines 42-48 (heur_ag 91%, -2 +4 = +2net, indent adjusted 4→8sp)
--- a/src/main.py
+++ b/src/main.py
@@ -40,6 +40,8 @@
 def foo():
     x = 1
+    y = 2
+    z = 3
     return x + y
+    return result
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

**Implementation note:** At line ~947 the current code checks `match_mode in ('heuristic', 'heuristic_agnostic')` as a group. Split this into two branches for label generation:
```python
if match_mode == 'heuristic':
    mode_label = f"heuristic {match_ratio:.0%}"
elif match_mode == 'heuristic_agnostic':
    mode_label = f"heur_ag {match_ratio:.0%}"
```

**Additional fields vs exact:**
| Field | Description | Example |
|-------|-------------|---------|
| `heuristic XX%` / `heur_ag XX%` | Match mode + similarity score (M2: discriminated) | `heuristic 91%`, `heur_ag 87%` |
| `indent adjusted X→Ysp` | Indentation delta detected (only if non-zero) | `indent adjusted 4→8sp` or `4→2tabs` |

**Indent drift warning (3+ heuristic edits on same file):**
```
OK: Edited src/main.py lines 50-55 (heuristic 87%, -1 +3 = +2net, indent adjusted 4→8sp) [edit #4 this session]
--- a/src/main.py
+++ b/src/main.py
@@ -48,5 +48,7 @@
 def bar():
     pass
+    x = compute()
+    return x
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

**Indentation anomaly warning:**
```
OK: Edited src/main.py lines 50-55 (heuristic 87%, -1 +3 = +2net) [edit #4 this session] line 15 indent jump 4→12sp
--- a/src/main.py
+++ b/src/main.py
@@ -48,5 +48,7 @@
 def bar():
     pass
+    x = compute()
+    return x
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Success Format — Delete & Insert Mode

```
OK: Edited src/main.py lines 3-7 (d&i, deleted 5 lines, inserted at line 3, -5 +4 = -1net)
--- a/src/main.py
+++ b/src/main.py
@@ -1,6 +1,5 @@
 # header
-def old_func():
-    pass
-
-def another():
-    return True
+# replaced with new content below
+def new_func():
+    return False
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

**Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `d&i` | Short for delete_and_insert | `d&i` |
| `deleted N lines` | How many lines were removed from the range | `deleted 5 lines` |
| `inserted at line X` | Where new content was placed (1-based) | `inserted at line 3` |

**Delete-only variant (no insertion):**
```
OK: Edited src/main.py lines 10-20 (d&i, deleted 11 lines, -11 +0 = -11net)
--- a/src/main.py
+++ b/src/main.py
@@ -8,13 +8,2 @@
 # before
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

**Insert-only variant (empty delete range):**
```
OK: Edited src/main.py line 5 (d&i, inserted at line 5, -0 +3 = +3net)
--- a/src/main.py
+++ b/src/main.py
@@ -3,2 +3,5 @@
 # before
+def new_func():
+    pass
+
 # after
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Error Format — Pattern Not Found (Exact)

**Current:**
```
ERROR: Pattern not found in src/main.py. The 'old_content' string must exactly match the existing file content character-for-character, including whitespace and indentation, or consider using heuristic match mode.
```

**New:**
```
ERROR: Pattern not found in src/main.py (exact) — try heuristic match_mode or include more context lines
```

#### New Error Format — Multiple Matches

**Current:**
```
ERROR: Pattern found 3 times in src/main.py. The 'old_content' block must be unique. Please include more surrounding lines in 'old_content' to make it unique.
```

**New:**
```
ERROR: Pattern found 3 times in src/main.py — add more context lines to old_content to disambiguate
```

#### New Error Format — Heuristic Too Ambiguous / Not Found

**Current:**
```
ERROR: Heuristic pattern is too ambiguous (found 47 candidate locations). Please include more unique surrounding lines of context.
```

**New:**
```
ERROR: Heuristic match found 47 candidates in src/main.py — add more unique context to narrow down
```

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| Line range `lines X-Y` | LLM knows exactly WHERE the edit landed without re-reading |
| Line delta `-N +M = ±Knet` | LLM can verify the expected change magnitude |
| Indent adjustment info | LLM knows if whitespace was modified (critical for Python) |
| Heuristic edit count `[edit #N]` | Early warning before drift accumulates |
| d&i specifics (deleted N, inserted at X) | Clear picture of what happened in range-based mode |
| Unified diff snippet | LLM sees exactly WHAT changed without re-reading the file — most impactful addition |

#### C5. Unified Diff Generation — Implementation Note

At line ~934–940 in `file_operations.py`, both `file_content` (original) and `new_file_content` (after edit) are available. Use `difflib.unified_diff()` (already imported at line 489) to generate a compact diff:

```python
# At line ~938, after new_file_content is computed but before resolved.write_text():
old_lines = file_content.splitlines(keepends=True)
new_lines = new_file_content.splitlines(keepends=True)
diff_lines = list(difflib.unified_diff(
    old_lines, new_lines,
    fromfile=f'a/{path}', tofile=f'b/{path}', lineterm=''
))

# diff_lines structure: ['--- a/path', '+++ b/path', '@@ ... @@', content lines ...]
# Skip the --- and +++ header lines (first 2), keep @@ headers and content
diff_content = '\n'.join(diff_lines[2:]) if len(diff_lines) > 2 else ''

# Limit to 20 lines max; truncate with ellipsis if longer
if diff_content:
    all_diff_lines = diff_content.splitlines()
    if len(all_diff_lines) > 20:
        first_lines = all_diff_lines[:8]
        last_lines = all_diff_lines[-8:]
        diff_content = '\n'.join(first_lines + ['...'] + last_lines)

# Then at response construction (line ~944), insert the diff between the main line and backup line:
res_msg = f"OK: Edited {path}"
if match_mode == 'exact':
    res_msg += f" lines {exact_start_line}-{exact_end_line} (exact, -{old_lc} +{new_lc} = {'+' if net_delta >= 0 else ''}{net_delta}net)"
# ... other modes ...

if diff_content:
    res_msg += f'\n--- a/{path}\n+++ b/{path}'
    # Re-add the @@ headers that were stripped above — they're in diff_lines[2:] already
    # Actually, keep it simpler: use the full diff_lines but skip --- and +++
    res_msg += '\n' + diff_content

if backup_path_str:
    res_msg += f'\n  backup → {backup_path_str}'
```

**Token budget note:** A typical edit produces ~5–10 lines of diff output. Even with the 20-line cap, this is cheaper than a full `read_file` call (which costs 50–250+ tokens for content). The diff gives the LLM exactly what it needs to verify changes.

---

### 2. `write_file`

**Current output:**
```
APPROVED: Created path/to/file.py (1234 characters)
APPROVED: Created path/to/file.py (1234 characters). Backup created: C:\full\absolute\path\to\logs\backups\agent_name\file.py.1719600000.bak
```

#### New Success Format — New File

```
OK: Created src/main.py (87 lines, 1.2 KB)
```

#### New Success Format — Overwrite

```
OK: Overwrote src/main.py (87 lines, 1.2 KB)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

**Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `Created` / `Overwrote` | Distinct verbs for new vs overwrite | `Created`, `Overwrote` |
| `N lines, X.X KB` | Line count + file size via `_format_size()` (M4: space before unit) | `87 lines, 1.2 KB` |

#### New Error Format — Path Resolution

```
ERROR: Cannot write src/main.py — path outside workspace
```

#### m3. Additional Error Formats for write_file

| Condition | Message |
|-----------|---------|
| Execution failure after approval | `ERROR: Write failed for src/main.py — {error detail}` |
| User rejection | `REJECTED: src/main.py — {reason}` (already handled, kept as-is) |

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| "Created" vs "Overwrote" | LLM knows if it replaced something |
| Line count (not just chars) | LLM can verify expected line count |
| File size via `_format_size()` | Uses existing unused helper — easy win |

---

### 3. `read_file`

**Current output:**
```
File content (src/main.py), lines 1 to 250 of 500: [TRUNCATED]
```
with line-numbered content and pagination note.

#### New Success Format — Text File

```
OK: Read src/main.py lines 1-250/500 (text, 14.2 KB) [TRUNCATED]
```
followed by the same line-numbered content block as today.

**Pagination footer:**
```
→ continue at start_line=251
```

#### m2. Pagination Note Consistency Across Both Implementations

Both the tools-layer `ReadFile.call()` (`file_ops.py` line ~276) and the operation_manager `read_file()` (`file_operations.py`) must use the **same** compact pagination format:

| Current (tools layer, line ~276) | New Format |
|----------------------------------|------------|
| `[PAGINATION NOTE: This file is large. Use read_file with start_line=251 to read the next 250 lines.]` | `→ continue at start_line=251` |

The operation_manager version currently has no pagination guidance — it should also emit this footer when content is truncated.

#### New Success Format — Empty File

```
OK: Read src/empty.txt (0 lines, 0 B)
```

#### New Success Format — Binary File

```
OK: Read image.png (binary, 48.6 KB) showing first 1024 bytes as hex dump
```
followed by the hex dump block.

**Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `lines X-Y/TOTAL` | Range being shown + total file lines | `lines 1-250/500` |
| `(text, SIZE)` / `(binary, SIZE)` | File type + size (M4: space before unit) | `(text, 14.2 KB)` |
| `[TRUNCATED]` | Present only when content was cut short | `[TRUNCATED]` |
| `→ continue at start_line=N` | Replaces verbose pagination note (m2: consistent across both impls) | `→ continue at start_line=251` |

#### New Error Format — Start Line Beyond EOF

```
ERROR: start_line 300 exceeds file length (src/main.py has 250 lines)
```

#### m1. Encoding Warning with Replacement Character Detection

When non-UTF-8 bytes are detected and replaced, append to the header:
```
OK: Read src/data.txt lines 1-200/200 (text, 4.1 KB) [encoding: utf-8 with replacements]
```

**Implementation note:** After reading text content in `_read_text_file()` (line ~257 of `file_ops.py`), count replacement characters to detect encoding issues:
```python
# After building the content string at line ~257:
repl_count = content.count('\ufffd')  # Unicode replacement character U+FFFD
if repl_count > 0:
    encoding_note = f" [encoding: utf-8 with {repl_count} replacement(s)]"
else:
    encoding_note = ""
```

This makes the hidden corruption risk visible to the LLM. The count gives it a sense of how many bytes were affected.

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| `OK:` prefix | Consistent status marker across all tools |
| File size for text files | LLM knows scope of what it read |
| Encoding warning flag | Hidden corruption risk becomes visible (reviewer finding #5) |
| Compact pagination | "→ continue at start_line=251" vs a full sentence |

---

### 4. `re_indent`

**Current output:**
```
APPROVED: Re-indented lines 5:20 in src/main.py. Block had base indent of 8 spaces, now indented to 4 spaces.
```

#### M1. Lines Modified Format — "N total / M changed"

The phrase "lines modified" is ambiguous: does it count blank lines? To be precise, report **total lines in range** and **non-blank lines actually re-indented**:

```
OK: Re-indented src/main.py lines 5-20 (min, base 8sp→4sp, 16 total / 12 changed)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

- **`N total`** = number of lines in the requested range (`end - start + 1`)
- **`M changed`** = non-blank lines that had their indentation adjusted (blank lines pass through unchanged)

#### New Success Format — Min Mode

```
OK: Re-indented src/main.py lines 5-20 (min, base 8sp→4sp, 16 total / 12 changed)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Success Format — Shift Mode (+N)

```
OK: Re-indented src/main.py lines 5-20 (shift +4sp, 16 total / 12 changed)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Success Format — Shift Mode (-N)

```
OK: Re-indented src/main.py lines 5-20 (shift -4sp, 16 total / 12 changed)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Success Format — Flat Mode

```
OK: Re-indented src/main.py lines 5-20 (flat→4sp, 16 total / 14 changed)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Success Format — Convert Mode

```
OK: Re-indented src/main.py lines 5-20 (convert, tab-width=4→tabs, 16 total / 16 changed)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Edge Case — No Non-Blank Lines (Reviewer Finding #4)

**Current:** `APPROVED: No non-blank lines found in block 5:20 of src/main.py. Block unchanged.`

**New:**
```
OK: Re-indented src/main.py lines 5-20 (min, no-op: 16 total / 0 changed)
```

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| `N total / M changed` count (M1) | LLM knows exact scope and how many lines were actually touched; blank lines are distinguished |
| Compact mode label `(min)` / `(shift +4sp)` etc. | Same info, fewer words |
| Clear no-op message for blank-only blocks | Less confusing than "No non-blank lines found" |

---

### 5. `delete_file`

**Current output:**
```
APPROVED: Deleted path/to/file.py. Backup created: C:\full\absolute\path\to\logs\backups\agent_name\file.py.1719600000.bak
```

#### New Success Format — File Deletion (M4: + file size)

```
OK: Deleted src/main.py (file, 87 lines, 1.2 KB)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

#### New Success Format — Directory Deletion

```
OK: Deleted src/tests/ (directory, 12 files)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\tests.1719600000.bak
```

**Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `(file, N lines, SIZE)` / `(directory, N files)` | Type + scope + size (M4: file includes size via `_format_size()`) | `(file, 87 lines, 1.2 KB)` |

#### C2. Pre-Deletion Stats — Implementation Note

The file is moved/deleted at line 1273-1281, so stats must be captured **before** that point. At line ~1254, `resolved` path and `is_directory` flag are already available:

```python
# In delete_file() at line ~1254, before shutil.move():
if is_directory:
    file_count = sum(1 for _ in resolved.rglob('*') if _.is_file())
    scope_info = f"(directory, {file_count} files)"
else:
    with open(resolved) as f:
        line_count = sum(1 for _ in f)
    scope_info = f"(file, {line_count} lines)"
```

Then at line ~297 when building the message:
```python
msg = f"OK: Deleted {path} {scope_info}"
```

#### m3. Additional Error Formats for delete_file

| Condition | Message |
|-----------|---------|
| File not found | `ERROR: src/main.py not found` (already handled at line 1237) |
| Execution failure after approval | `ERROR: Delete failed for src/main.py — {error detail}` |
| User rejection | `REJECTED: src/main.py — {reason}` (already handled, kept as-is) |

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| File vs directory distinction | LLM knows if it deleted a tree or a single file |
| Line/file count | Scope of what was removed |

---

### 6. `copy_file`

**Current output:**
```
APPROVED: Copied path/to/source to path/to/destination. Backup created: C:\full\absolute\path\to\logs\backups\agent_name\file.py.1719600000.bak
```

#### New Success Format — New Copy (Destination Didn't Exist) (M4: + file size)

```
OK: Copied src/utils.py → src/lib/utils.py (file, 234 lines, 4.8 KB)
```

#### New Success Format — Overwrite Copy

```
OK: Copied src/utils.py → src/lib/utils.py (overwrote, 234 lines, 4.8 KB)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\utils.py.1719600000.bak
```

#### New Success Format — Directory Copy

```
OK: Copied src/tests/ → src/integration_tests/ (directory, 8 files)
```

**Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `→` arrow notation | Replaces verbose "to" for compactness | `src/utils.py → src/lib/utils.py` |
| `(file, N lines, SIZE)` / `(directory, N files)` | Type + scope + size (M4: file includes size) | `(file, 234 lines, 4.8 KB)` |
| `(overwrote, ...)` | Explicit overwrite indicator | `(overwrote, 234 lines, 4.8 KB)` |

#### C3. Pre-Copy Source Stats — Implementation Note

Source stats must be captured **before** the copy operation at line ~1362-1366. At that point `src_path` is already resolved:

```python
# In copy_file() after src_path resolution (line ~1310), before copy:
if src_path.is_dir():
    file_count = sum(1 for _ in src_path.rglob('*') if _.is_file())
    scope_info = f"(directory, {file_count} files)"
else:
    with open(src_path) as f:
        line_count = sum(1 for _ in f)
    scope_info = f"(file, {line_count} lines)"

# Then at response construction (line ~1368):
if dest_path.exists():   # was overwritten
    msg = f"OK: Copied {source} → {destination} (overwrote{scope_info})"
else:
    msg = f"OK: Copied {source} → {destination} {scope_info}"
```

**Note:** The `dest_path.exists()` check at line 1335 already determines whether it's an overwrite — reuse that boolean flag rather than re-checking.

#### m3. Additional Error Formats for copy_file

| Condition | Message |
|-----------|---------|
| Source not found | `ERROR: src/utils.py source not found` (already handled at line 1314) |
| Execution failure after approval | `ERROR: Copy failed — {error detail}` |
| User rejection | `REJECTED: {source} → {destination} — {reason}` (already handled, kept as-is) |

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| "Copied" vs "Copied (overwrote)" | LLM knows if destination was replaced |
| File/directory distinction + count | Scope of the operation |
| Arrow notation `→` | Saves tokens vs verbose "to" |

---

### 7. `syntax_check`

**Current output:**
```
Valid (python)
```
or for empty files:
```
Valid
```
(no language tag — reviewer finding #3 inconsistency)

#### New Success Format — Valid File

```
OK: src/main.py syntax valid (python, 142 lines)
```

#### C4. Empty File Fix — Implementation Note

**Current code at line 443-444 of `syntax_check.py`:**
```python
if not content.strip():
    return 'Valid'  # Empty files are syntactically valid — no language tag!
```

The `lang` variable is computed earlier (line 429) but the early return at line 443 skips it. Fix by including lang and line count in the empty-file path:

```python
# At line 443, replace:
if not content.strip():
    return f'OK: {rel_path} syntax valid ({lang}, 0 lines)'

# For non-empty files at line 456-457, replace:
line_count = len(content.splitlines())
if result == 'Valid':
    return f"OK: {rel_path} syntax valid ({lang}, {line_count} lines)"
```

This ensures **every** success response (empty or not) includes the language tag and line count.

#### New Success Format — Empty File (Fixes Inconsistency)

```
OK: src/empty.py syntax valid (python, 0 lines)
```

Both empty and non-empty files now include the language tag consistently.

#### New Error Format — Syntax Error

**Current:**
```
[python] Syntax Error: invalid syntax at line 42, column 7
```

**New:**
```
ERROR: src/main.py syntax error (python) line 42 col 7: invalid syntax
```

#### New Error Format — Unsupported Type

```
ERROR: src/data.csv unsupported extension .csv in src/data.py
```

#### What's New vs Before

| Added | Why It Matters |
|-------|---------------|
| Consistent `OK:` / `ERROR:` prefix | Same pattern as all other tools |
| Language tag on ALL valid responses (including empty files) | Fixes reviewer finding #3 |
| File path in response | LLM knows which file was checked |
| Line count | Scope of the check |

---

## Dead Code Flag: `move_file`

**Reviewer Finding #1:** The `move_file` method exists at line 1379 of `file_operations.py` but has no corresponding tool wrapper in `file_ops.py`. It's never called through the tool registry.

**Recommendation:** Either register a `MoveFile` tool class or remove the method. If kept, its feedback format would mirror `copy_file`:
```
OK: Moved src/old_name.py → src/new_name.py (file)
  backup → C:\full\absolute\path\to\logs\backups\agent_name\old_name.py.1719600000.bak
```

---

## Unused Helpers: `_format_size()` and `_format_mtime()`

**Reviewer Finding #2:** These static helpers exist at lines 115-138 of `file_operations.py` but are used only by `list_dir`. They should be reused in feedback messages.

### M4. Size Format Alignment

The `_format_size()` helper returns strings **with a space** between number and unit:
```python
# Line 121: f"{size_bytes} B"       → "73 B"
# Line 128: f"{val:.1f} {units[idx]}" → "1.2 KB", "48.6 MB"
```

All design examples use this exact format (space before unit). No changes needed to the helper itself — just consistent usage in all feedback messages.

**Adoption plan:**
| Helper | Used By (New) | Example Output |
|--------|---------------|----------------|
| `_format_size()` | `write_file`, `read_file`, `copy_file`, `delete_file` | `"1.2 KB"`, `"48.6 MB"` |
| `_format_mtime()` | Optional: could be added to `read_file` header | `"2026-06-30 09:15"` |

---

## Implementation Checklist

### Phase 1 — Core Changes (file_operations.py)

**CRITICAL fixes:**
- [ ] C1: Capture `match_start_pos` / line range in edit_file exact mode at line ~568
- [ ] C2: Compute pre-deletion stats in delete_file() before shutil.move() at line ~1254
- [ ] C3: Compute pre-copy source stats in copy_file() before the copy at line ~1331
- [ ] C5: Generate unified diff snippet in edit_file response using `difflib.unified_diff()`

**MAJOR fixes:**
- [ ] M1: re_indent — use "N total / M changed" format instead of ambiguous "lines modified"
- [ ] M2: Discriminate `heuristic` vs `heur_ag` labels in edit_file feedback
- [ ] M3: Standardize backup line to `  backup → <absolute-path>` across all 6 tools (write, edit, re_indent, delete, copy)
- [ ] M4: Use `_format_size()` output format consistently (`"1.2 KB"` with space — matches actual helper output)
- [ ] M5: Use absolute paths for backups — replace `backup_path.relative_to(self.base_dir)` with `str(backup_path)` at lines 452, 930, 1190, 1284, 1358, 1430

**General updates:**
- [ ] Replace all `"APPROVED:"` prefixes with `"OK:"` in success messages
- [ ] **edit_file exact mode:** Add line range, match mode label, and line delta to response string
- [ ] **edit_file heuristic mode:** Add similarity %, indent adjustment info, edit count label
- [ ] **edit_file delete_and_insert mode:** Add deleted line count, insertion point
- [ ] **write_file:** Distinguish "Created" vs "Overwrote", add line count and file size via `_format_size()`
- [ ] **read_file (operation_manager version):** Add `OK:` prefix, file size for text files
- [ ] **re_indent:** Add "N total / M changed" count, compact mode labels
- [ ] **delete_file:** Add type indicator (file/directory) + scope
- [ ] **copy_file:** Distinguish new copy vs overwrite, add arrow notation, type+scope

### Phase 2 — Tool Layer Changes (file_ops.py / syntax_check.py)

**CRITICAL fix:**
- [ ] C4: syntax_check empty file returns bare `'Valid'` — include language tag and "0 lines" at line 443

**Minor fixes:**
- [ ] m1: read_file encoding replacement detection — count `\ufffd` chars after reading (line ~257)
- [ ] m2: Pagination note format consistency across both implementations (`→ continue at start_line=N`)
- [ ] **read_file (tools layer):** Update header to include `OK:` prefix, file size for text files, encoding warning

### Phase 3 — Cleanup

- [ ] Register `move_file` tool wrapper OR remove the dead method
- [ ] Wire `_format_size()` into write/edit/delete/copy feedback messages
- [ ] Consider wiring `_format_mtime()` into read_file header (nice-to-have)

---

## m4. Message Size Budget (Token Awareness — Revised Estimates)

Initial estimates were slightly optimistic on savings and understated costs where new data fields are added. Revised figures below use conservative real-world measurements:

| Tool | Current Avg Tokens | New Avg Tokens | Delta | Justification |
|------|-------------------|----------------|-------|---------------|
| `edit_file` exact | ~6 | ~24 | +18 | Line range + delta + unified diff; eliminates follow-up read_file calls (50-250 tokens) |
| `edit_file` heuristic | ~15-25 | ~25 | 0 to -10 | More info but tighter wording + diff replaces "Please check..." |
| `edit_file` d&i | ~8 | ~24 | +16 | Deleted/inserted specifics + unified diff are essential |
| `write_file` new | ~8 | ~10 | +2 | Line count is cheap and valuable |
| `write_file` overwrite | ~15-20 | ~13 | -2 to -7 | More compact, more info; backup on separate line |
| `read_file` text | ~10 | ~13 | +3 | File size + encoding flag + OK: prefix |
| `re_indent` | ~14 | ~14 | 0 | Same token count but "N total / M changed" is more precise |
| `delete_file` | ~10-15 | ~12 | -1 to -3 | Type+scope replaces vague message; backup on separate line |
| `copy_file` | ~12-18 | ~12 | 0 to -6 | Arrow notation saves tokens vs verbose "to" |
| `syntax_check` valid | ~4-6 | ~10 | +4 to +6 | Path + lang tag + line count add value |

**Net effect:** edit_file (most commonly called) gets 3x more useful info — line range, delta, AND a unified diff showing exactly what changed. The +18 token increase is justified because it eliminates follow-up read_file calls that cost 50-250+ tokens. All other tools stay roughly the same or become more compact while adding information.

---

## Examples: Before vs After Comparison

### edit_file — Exact Match
```
BEFORE: APPROVED: Edited src/main.py
AFTER:  OK: Edited src/main.py lines 42-48 (exact, -3 +5 = +2net)
         --- a/src/main.py
         +++ b/src/main.py
         @@ -40,10 +40,12 @@
          def foo():
         -    x = 1
         -    y = 2
         -    return x + y
         +    x = 1
         +    y = 2
         +    z = 3
         +    result = x + y + z
         +    if result > 10:
         +        result = 10
         +    return result
          backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

### edit_file — Heuristic with Indent Change
```
BEFORE: APPROVED: Edited src/main.py (Heuristic match similarity: 91%) [NOTE: This file has been edited 4 times in heuristic mode this session. Indentation drift may have accumulated.] Please check the file to ensure the insertion was applied correctly.
AFTER:  OK: Edited src/main.py lines 50-55 (heuristic 87%, -1 +3 = +2net, indent adjusted 4→8sp) [edit #4 this session]
         --- a/src/main.py
         +++ b/src/main.py
         @@ -48,5 +48,7 @@
          def bar():
              pass
         +    x = compute()
         +    return x
          backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

### edit_file — Heuristic-Agnostic (M2: Discriminated from heuristic)
```
BEFORE: APPROVED: Edited src/main.py (Heuristic match similarity: 85%) ...
AFTER:  OK: Edited src/main.py lines 10-15 (heur_ag 85%, -2 +3 = +1net)
         --- a/src/main.py
         +++ b/src/main.py
         @@ -8,4 +8,5 @@
          def init():
              pass
         +    setup_config()
          backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

### edit_file — Delete & Insert
```
BEFORE: APPROVED: Edited src/main.py (delete_and_insert mode)
AFTER:  OK: Edited src/main.py lines 3-7 (d&i, deleted 5 lines, inserted at line 3, -5 +4 = -1net)
         --- a/src/main.py
         +++ b/src/main.py
         @@ -1,6 +1,5 @@
          # header
         -def old_func():
         -    pass
         -
         -def another():
         -    return True
         +# replaced with new content below
         +def new_func():
         +    return False
          backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

### write_file — New File
```
BEFORE: APPROVED: Created src/main.py (1234 characters)
AFTER:  OK: Created src/main.py (87 lines, 1.2 KB)
```

### write_file — Overwrite
```
BEFORE: APPROVED: Created src/main.py (1234 characters). Backup created: C:\full\absolute\path\to\logs\backups\agent_name\file.py.1719600000.bak
AFTER:  OK: Overwrote src/main.py (87 lines, 1.2 KB)
         backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

### read_file — Text File
```
BEFORE: File content (src/main.py), lines 1 to 250 of 500: [TRUNCATED]
         ...content...
         
         [PAGINATION NOTE: This file is large. Use read_file with start_line=251 to read the next 250 lines.]
AFTER:  OK: Read src/main.py lines 1-250/500 (text, 14.2 KB) [TRUNCATED]
         ...content...
         
         → continue at start_line=251
```

### delete_file — File Deletion (M4: + file size)
```
BEFORE: APPROVED: Deleted src/main.py. Backup created: C:\full\absolute\path\to\logs\backups\agent_name\file.py.1719600000.bak
AFTER:  OK: Deleted src/main.py (file, 87 lines, 1.2 KB)
         backup → C:\full\absolute\path\to\logs\backups\agent_name\main.py.1719600000.bak
```

### copy_file — File Copy (M4: + file size)
```
BEFORE: APPROVED: Copied src/utils.py to src/lib/utils.py
AFTER:  OK: Copied src/utils.py → src/lib/utils.py (file, 234 lines, 4.8 KB)
```

### syntax_check — Empty File (C4: Inconsistency Fix + Error Prefix)
```
BEFORE: Valid                              ← no language tag!
AFTER:  OK: src/empty.py syntax valid (python, 0 lines)   ← consistent with non-empty files
```

### syntax_check — Syntax Error (C4: Error prefix fix)
```
BEFORE: [python] Syntax Error: invalid syntax at line 42, column 7
AFTER:  ERROR: src/main.py syntax error (python) Syntax Error: invalid syntax at line 42, column 7
```

---

## Implementation Approach — Critical Issues

Each critical issue requires specific code changes in `agent_cascade/operation_manager/file_operations.py`. Line numbers reference the current file.

### C1. edit_file Exact Mode — Line Range at Match Time

**Problem:** The exact match path (line 563-568) uses `file_content.count(old_content)` but never records WHERE the match occurred. By line ~940 when building `res_msg`, the original `file_content` has been re-read fresh (line 934), so we can't compute the position from it anymore.

**Fix:** Capture byte offset at match time and carry it through to response construction:

```python
# At method entry (after line 563), initialize variables that will be set by each branch:
exact_start_line = None
exact_end_line = None

# At line 563-568, replace the exact match block:
if match_mode == 'exact':
    count = file_content.count(old_content)
    if count == 0:
        return f"ERROR: Pattern not found in {path} (exact) — try heuristic match_mode or include more context lines"
    if count > 1:
        return f"ERROR: Pattern found {count} times in {path} — add more context lines to old_content to disambiguate"
    
    # C1: Record match position for feedback message
    match_start_pos = file_content.index(old_content)
    match_end_pos = match_start_pos + len(actual_old_content)
    exact_start_line = file_content[:match_start_pos].count('\n') + 1
    exact_end_line = file_content[:match_end_pos].count('\n') + 1   # +1: line numbers are 1-based inclusive
```

Then at line ~940-960, build the response using these captured values:
```python
# M1. Line delta computation — use splitlines() for correct handling of trailing newlines:
old_lc = len(actual_old_content.splitlines()) if actual_old_content else 0
new_lc = len(new_content.splitlines()) if new_content else 0
net_delta = new_lc - old_lc

res_msg = f"OK: Edited {path}"

if match_mode == 'exact':
    res_msg += f" lines {exact_start_line}-{exact_end_line} (exact, -{old_lc} +{new_lc} = {'+' if net_delta >= 0 else ''}{net_delta}net)"
elif match_mode in ('heuristic', 'heuristic_agnostic'):
    # M2: Discriminated labels; use resolved.as_posix() consistently for dict key
    mode_label = "heuristic" if match_mode == 'heuristic' else "heur_ag"
    res_msg += f" lines {orig_start_idx+1}-{orig_end_idx+1} ({mode_label} {match_ratio:.0%}, -{old_lc} +{new_lc} = {'+' if net_delta >= 0 else ''}{net_delta}net)"
elif match_mode == 'delete_and_insert':
    # BUG FIX: Use end_idx (from _parse_range) not the non-existent 'end_line'
    # Also handle insert-only case where start_idx == end_idx
    d_start = start_idx + 1   # 0-based to 1-based
    d_end = end_idx           # end_idx is exclusive in slice semantics, so it's already the display end line
    if start_idx == end_idx:
        # Insert-only: no lines deleted, use singular "line N"
        res_msg += f" line {d_start} (d&i, inserted at line {d_start}, -{old_lc} +{new_lc} = {'+' if net_delta >= 0 else ''}{net_delta}net)"
    elif new_content:
        # Normal delete+insert
        res_msg += f" lines {d_start}-{d_end} (d&i, deleted {old_lc} lines, inserted at line {d_start}, -{old_lc} +{new_lc} = {'+' if net_delta >= 0 else ''}{net_delta}net)"
    else:
        # Delete-only: no insertion
        res_msg += f" lines {d_start}-{d_end} (d&i, deleted {old_lc} lines, -{old_lc} +{new_lc} = {'+' if net_delta >= 0 else ''}{net_delta}net)"
```

**Variable threading note:** `exact_start_line`, `exact_end_line` are initialized as `None` at method entry so they're defined in all code paths. The heuristic branch uses `orig_start_idx`/`orig_end_idx` (already computed at line 640-641). The d&i branch uses `start_idx`/`end_idx` from `_parse_range()` (line 877).

### C2. delete_file — Pre-Deletion Stats Before Move (M4: + file size)

**Problem:** At line 1273, `shutil.move()` relocates the file before we can count its lines/files/size.

**Fix:** Compute stats at line ~1254 where `resolved` and `is_directory` are already set:

```python
# In delete_file() after is_directory check (line ~1255), add:
if is_directory:
    file_count = sum(1 for _ in resolved.rglob('*') if _.is_file())
    scope_info = f"(directory, {file_count} files)"
else:
    with open(resolved) as f:
        line_count = sum(1 for _ in f)
    # M4: Include file size via _format_size() for consistency with write_file
    file_size_str = self._format_size(resolved.stat().st_size)
    scope_info = f"(file, {line_count} lines, {file_size_str})"

# ... (shutil.move happens at line 1273)

# At response construction (line ~1297), replace:
msg = f"OK: Deleted {path} {scope_info}"
```

### C3. copy_file — Pre-Copy Source Stats (M4: + file size)

**Problem:** Need source file stats before the copy operation completes.

**Fix:** Compute at line ~1331 where `src_path` is already resolved and available:

```python
# In copy_file() after src_path resolution (line ~1331), add:
if src_path.is_dir():
    file_count = sum(1 for _ in src_path.rglob('*') if _.is_file())
    scope_info = f"(directory, {file_count} files)"
else:
    with open(src_path) as f:
        line_count = sum(1 for _ in f)
    # M4: Include file size via _format_size() for consistency with write_file
    file_size_str = self._format_size(src_path.stat().st_size)
    scope_info = f"(file, {line_count} lines, {file_size_str})"

# At response construction (line ~1368), replace:
if backup_path_str:  # destination existed → overwrite
    msg = f"OK: Copied {source} → {destination} (overwrote{scope_info})"
else:
    msg = f"OK: Copied {source} → {destination} {scope_info}"
```

### C4. syntax_check — Empty File Language Tag + Error Prefix Fix

**Problem:** At line 443-444 of `syntax_check.py`, empty files return bare `'Valid'` without the language tag that non-empty files get at line 457. Also, error returns use `[lang] {result}` instead of the design-specified `ERROR:` prefix.

**Fix in `agent_cascade/tools/custom/syntax_check.py`:**

```python
# Line 443, replace:
if not content.strip():
    return f'OK: {rel_path} syntax valid ({lang}, 0 lines)'

# Line 456-457, replace:
line_count = len(content.splitlines())
if result == 'Valid':
    return f"OK: {rel_path} syntax valid ({lang}, {line_count} lines)"
return f"ERROR: {rel_path} syntax error ({lang}) {result}"
```

**BUG FIX:** The error path now use `ERROR:` prefix with file path and language tag, matching the design spec. Previously returned `[lang] {result}` which was inconsistent.

### M5. Absolute Backup Paths — Implementation Note

**Problem:** At 6 locations (lines 452, 930, 1190, 1284, 1358, 1430), backup paths are converted to relative via `backup_path.relative_to(self.base_dir)` with a fallback to absolute on `ValueError`. The design now requires absolute paths everywhere.

**Fix:** Replace the try/except block at all 6 locations:

```python
# Before (at each of lines 452, 930, 1190, 1284, 1358, 1430):
try:
    backup_path_str = str(backup_path.relative_to(self.base_dir))
except ValueError:
    backup_path_str = str(backup_path)

# After (simpler — always absolute):
backup_path_str = str(backup_path)
```

This eliminates the try/except overhead entirely and produces consistent output. The `backup_path` is a `Path` object constructed from `self.base_dir / "logs" / "backups" / ...`, so `str(backup_path)` yields a full absolute path like `C:\workspace\logs\backups\agent_name\file.py.1719600000.bak`.

### C5. Unified Diff in edit_file — Implementation Detail

At line ~934–940, both `file_content` (original) and `new_file_content` (after edit) are available. `difflib` is already imported at line 489. The diff generation code:

```python
# After new_file_content is computed (line ~940), before resolved.write_text():
old_lines = file_content.splitlines(keepends=True)
new_lines = new_file_content.splitlines(keepends=True)
diff_lines = list(difflib.unified_diff(
    old_lines, new_lines,
    fromfile=f'a/{path}', tofile=f'b/{path}', lineterm=''
))

# diff_lines: ['--- a/path', '+++ b/path', '@@ ... @@', content lines ...]
# Skip --- and +++ headers (first 2), keep @@ headers and content
diff_content = '\n'.join(diff_lines[2:]) if len(diff_lines) > 2 else ''

# Limit to 20 lines max; truncate with ellipsis if longer
if diff_content:
    all_diff_lines = diff_content.splitlines()
    if len(all_diff_lines) > 20:
        first_lines = all_diff_lines[:8]
        last_lines = all_diff_lines[-8:]
        diff_content = '\n'.join(first_lines + ['...'] + last_lines)

# At response construction (line ~944), insert between main line and backup line:
if diff_content:
    res_msg += f'\n--- a/{path}\n+++ b/{path}' + '\n' + diff_content
```

### Data Availability Summary

| New Field | Where Computed | Already Available? | Extra Cost |
|-----------|---------------|-------------------|------------|
| edit_file exact line range | Line ~568 (match time) | `file_content` already in scope | 1 `.index()` + 2 `.count('\n')` calls |
| edit_file line delta | Line ~940 (response build) | `actual_old_content`, `new_content` already in scope | 2 `.splitlines()` calls (M1: replaced `.count('\n')`) |
| edit_file unified diff (C5) | Line ~940 (response build) | `file_content`, `new_file_content` already in scope; `difflib` imported at line 489 | 2 `.splitlines()` + 1 `unified_diff()` call (~5-10 lines output, capped at 20) |
| delete_file stats + size | Line ~1254 (before move) | `resolved`, `is_directory` already set | 1 file read or rglob + 1 `.stat().st_size` (M4) |
| copy_file stats + size | Line ~1331 (before copy) | `src_path` already resolved | 1 file read or rglob + 1 `.stat().st_size` (M4) |
| syntax_check empty fix | Line 443-444 | `lang`, `rel_path` already in scope | 0 extra cost |

**Variable threading note:** Variables set inside if/elif branches (`exact_start_line`, `orig_start_idx`, etc.) must be initialized as `None` at method entry before the match-mode dispatch, so they're defined when building `res_msg` at line ~940.

---

## Backward Compatibility Notes

- The response format is plain text consumed by LLMs, not a structured API. Changing the format is safe as long as:
  1. Success messages still start with a recognizable status prefix (`OK:` replaces `APPROVED:`)
  2. Error messages still start with `ERROR:`
  3. The file path is always included in the message body

- No tool call contracts are affected — only the response string format changes.