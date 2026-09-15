---
name: python-codebase-static-analysis
description: Static analysis of Python codebases using AST parsing to audit functions, imports, docstrings, and code patterns across a project directory — without executing the code.
source: auto-generated
version: "1.0.0"
triggers:
  - "scan all python files"
  - "find functions with no docstrings"
  - "most frequently imported undocumented"
  - "python static analysis"
  - "AST parse codebase"
  - "audit code quality"
---

AST-based static analysis of Python codebases. Inspect functions, imports, docstrings, and patterns without executing the code.

## Procedure

**1 — Collect target files.**
```python
from pathlib import Path
def get_python_files(root_dir: str) -> list[Path]:
    return sorted(Path(root_dir).rglob("*.py"))
```

**2 — Parse with AST, handle errors gracefully.**
```python
import ast
def parse_file(filepath: Path):
    try:
        source = filepath.read_text(encoding="utf-8", errors="ignore")
        return ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return None
```

**3 — Extract functions and docstrings.**
```python
def has_docstring(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node) is not None
documented = {node.name for node in ast.walk(tree) if has_docstring(node)}
all_funcs = {node.name for node in ast.iter_child_nodes(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
undocumented = all_funcs - documented
```

**4 — Extract imports.** Collect names from `import X` and `from M import Y`:
```python
imported_names = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names: imported_names.append(alias.name.split(".")[-1])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names: imported_names.append(alias.asname or alias.name)
```

**5 — Correlate and rank.** Use `collections.Counter` to find frequently-used undocumented functions:
```python
from collections import Counter
counter = Counter()
for name in all_imports_across_codebase:
    if name in all_undocumented_functions: counter[name] += 1
for func_name, count in counter.most_common(10):
    print(f"{func_name}: imported {count}x")
```

**6 — Run via `code_interpreter`** (Docker container); verify workspace path mapping with `system_info`.

## Tips

- Use `ast.iter_child_nodes(tree)` for top-level definitions only; `ast.walk(tree)` for all nested nodes.
- Always wrap AST parsing in try/except — real codebases contain syntax errors or non-Python `.py` files.
- For large codebases, process files incrementally rather than loading everything into memory.
- Combine with `grep` for quick pattern discovery before writing full AST analysis; use `code_map` for a file-structure overview first.
