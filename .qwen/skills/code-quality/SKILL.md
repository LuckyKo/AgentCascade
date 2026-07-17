---
name: code-quality
description: Lint code with ruff/flake8, format with black, type check with mypy including common style guides, import ordering, and naming conventions
source: manual
version: "1.0.0"
triggers:
  - "lint"
  - "format code"
  - "ruff"
  - "black"
  - "mypy"
  - "type check"
  - "code style"
  - "flake8"
---

## Goal

Enforce consistent, high-quality Python code through automated linting (ruff), formatting (black), and type checking (mypy). Catch style issues, import ordering problems, naming convention violations, and type mismatches before they reach review.

## Procedure

### Step 1 — Ruff configuration and execution

**pyproject.toml setup:**
```toml
[tool.ruff]
line-length = 88
target-version = "py310"
src = ["src"]

[tool.ruff.lint]
select = [
    "E",      # pycodestyle errors
    "W",      # pycodestyle warnings
    "F",      # Pyflakes
    "I",      # isort (import ordering)
    "N",      # pep8-naming
    "UP",     # pyupgrade (modern Python syntax)
    "B",      # flake8-bugbear (common bugs)
    "C4",     # flake8-comprehensions
    "SIM",    # flake8-simplify
]
ignore = [
    "E501",   # line length (black handles this)
    "B006",   # default arguments as lists (common pattern)
]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["N"]  # Relaxed naming in tests
```

**Run ruff:**
```bash
# Check only (no modifications)
ruff check src/

# Auto-fix what can be fixed
ruff check --fix src/

# Show which rules triggered
ruff check --show-fixes src/

# Specific file with detailed output
ruff check --output-format=full path/to/file.py
```

### Step 2 — Black formatting

**pyproject.toml:**
```toml
[tool.black]
line-length = 88
target-version = ["py310"]
include = '\.pyi?$'
exclude = '''
/(
    \.git
  | \.venv
  | build
  | dist
)/
'''
```

**Run black:**
```bash
# Format all Python files
black src/ tests/

# Check without modifying (CI use)
black --check src/ tests/

# Diff mode: show what would change
black --diff src/
```

### Step 3 — Mypy type checking

**pyproject.toml:**
```toml
[tool.mypy]
python_version = "3.10"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = false   # Gradual typing: start lenient, tighten later
show_error_codes = true
strict_optional = true

[[tool.mypy.overrides]]
module = ["tests.*"]
disable_error_code = ["arg-type"]  # Less strict in test code
```

**Run mypy:**
```bash
# Basic type check
mypy src/

# Strict mode (for final review)
mypy --strict src/

# Check specific module with error codes
mypy --show-error-codes src/mymodule.py
```

### Step 4 — Common style guide rules

**Naming conventions:**

| Style | Example | Use For |
|---|---|---|
| `snake_case` | `user_count`, `get_data()` | Variables, functions, methods |
| `PascalCase` | `UserProfile`, `DataProcessor` | Classes, exceptions |
| `UPPER_SNAKE_CASE` | `MAX_RETRIES`, `API_KEY` | Module-level constants |
| `_leading_underscore` | `_internal_method()` | Private/protected members |
| `__dunder__` | `__init__`, `__str__` | Special Python methods only |

**Import ordering (enforced by ruff I rules):**
```python
# 1. Standard library imports (alphabetical)
import json
import os
from pathlib import Path

# 2. Third-party imports (alphabetical)
import httpx
import numpy as np
from pydantic import BaseModel

# 3. Local application imports (relative or absolute, consistent)
from .config import Settings
from ..utils.helpers import format_date
```

### Step 5 — Common issues and fixes

**Bug patterns caught by ruff:**

| Rule | Issue | Fix |
|---|---|---|
| `F401` | Unused imports | Remove or use in code |
| `E731` | Lambda assigned to variable | Use `def` instead: `fn = lambda x: x → def fn(x): return x` |
| `C416` | Unnecessary list comprehension | `[x for x in y] → list(y)` |
| `SIM904` | Multiple identical expressions | Extract to variable |
| `UP038` | f-string with single expression | Remove redundant `f""`: `f"{x}" → str(x)` or just use the value directly |

**Type annotation patterns:**
```python
from typing import Any, Optional, Union
import json

# Good: explicit return types and parameter types
def parse_config(path: str) -> dict[str, Any]:
    """Parse a JSON config file."""
    with open(path) as f:
        return json.load(f)

# Good: optional parameters
def get_user(user_id: int, include_history: bool = False) -> Optional[dict]:
    ...

# Good: Union types (Python 3.10+ use | syntax)
def process(value: str | int) -> str:
    return str(value)

# Avoid: overuse of Any — be specific when possible
def compute(items: list[Any]) -> float:   # OK but could be better
    ...
```

### Step 6 — Automated quality pipeline (CI-friendly)

**Combined check script:**
```bash
#!/bin/bash
set -e

echo "=== Formatting ==="
black --check src/ tests/

echo "=== Linting ==="
ruff check src/ tests/

echo "=== Type Checking ==="
mypy src/

echo "✅ All quality checks passed"
```

**Pre-commit hooks (optional but recommended):**
```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/psf/black
    rev: 24.1.0
    hooks: [{id: black}]
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.3.0
    hooks: [{id: ruff, args: [--fix]}]
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| `line-length` | 88 (black default) | Industry standard; longer lines reduce readability |
| `ruff select` rules | E, W, F, I, N, UP, B minimum | Covers style + bugs + modernization |
| `mypy strict` | Off during development, on for PRs | Gradual typing is more practical for large codebases |

## What NOT to do

- Do not disable linting rules without a comment explaining why
- Do not run formatters with custom line lengths — stick to 88 unless the team has a strong reason otherwise
- Do not skip type checking on new modules — add annotations incrementally but don't ignore them entirely
- Do not mix import styles (relative and absolute) within the same project
- Do not suppress mypy errors with `# type: ignore` without specifying the error code: `# type: ignore[arg-type]`