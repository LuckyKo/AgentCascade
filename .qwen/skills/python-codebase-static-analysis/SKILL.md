---
name: python-codebase-static-analysis
description: Static analysis of Python codebases using AST parsing to audit functions, imports, docstrings, and code patterns across a project directory
source: auto-generated
version: "1.0.0"
triggers:
  - "scan all python files"
  - "find functions with no docstrings"
  - "most frequently imported undocumented"
  - "codebase analysis script"
  - "python static analysis"
  - "AST parse codebase"
  - "audit code quality"
generated_by: auto_skill_tester3
generated_from_task: "Write a Python script that scans all Python files in agent_cascade/, finds functions with no docstrings, and prints the top 10 most frequently imported undocumented functions across the codebase."
---

## Goal

Perform static analysis on Python codebases using AST parsing to inspect functions, imports, docstrings, and other code patterns without executing the code.

## Procedure

### Step 1 — Collect Target Files
Recursively gather all `.py` files from the target directory:

```python
from pathlib import Path
def get_python_files(root_dir: str) -> list[Path]:
    return sorted(Path(root_dir).rglob("*.py"))
```

### Step 2 — Parse Files with AST
Use Python's `ast` module to safely parse source files. Handle syntax errors gracefully:

```python
import ast
def parse_file(filepath: Path):
    try:
        source = filepath.read_text(encoding="utf-8", errors="ignore")
        return ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return None
```

### Step 3 — Extract Functions and Docstrings
Walk the AST to find functions with/without docstrings:

```python
def has_docstring(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node) is not None

documented = {node.name for node in ast.walk(tree) if has_docstring(node)}
all_funcs = {node.name for node in ast.iter_child_nodes(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
undocumented = all_funcs - documented
```

### Step 4 — Extract Imports
Collect imported names from `import X` and `from M import Y`:

```python
imported_names = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            imported_names.append(alias.name.split(".")[-1])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            imported_names.append(alias.asname or alias.name)
```

### Step 5 — Correlate and Rank
Use `collections.Counter` to find frequently used undocumented functions:

```python
from collections import Counter
counter = Counter()
for name in all_imports_across_codebase:
    if name in all_undocumented_functions:
        counter[name] += 1

for func_name, count in counter.most_common(10):
    print(f"{func_name}: imported {count}x")
```

### Step 6 — Run via code_interpreter
Execute the analysis script in a Docker container using `code_interpreter`, ensuring correct workspace path mapping (use `system_info` to verify mount points).

## Tips

- Use `ast.iter_child_nodes(tree)` for top-level definitions only; use `ast.walk(tree)` for all nested nodes.
- Always wrap AST parsing in try/except — real codebases contain syntax errors or non-Python `.py` files.
- For large codebases, process files incrementally rather than loading everything into memory.
- Combine with `grep` tool for quick pattern discovery before writing full AST analysis.
- Use `code_map` tool to get file structure overview before deep analysis.