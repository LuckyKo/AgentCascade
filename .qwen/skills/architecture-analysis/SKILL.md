---
name: architecture-analysis
description: System design review including dependency mapping, SOLID principles check, design pattern identification, coupling/cohesion analysis, and scalability assessment
source: manual
version: "1.0.0"
triggers:
  - "architecture"
  - "SOLID"
  - "design pattern"
  - "coupling"
  - "cohesion"
  - "scalability"
  - "dependency map"
  - "system design"
---

## Goal

Review and evaluate software architecture by mapping dependencies, checking SOLID principles, identifying design patterns, analyzing coupling and cohesion, and assessing scalability characteristics.

## Procedure

### Step 1 — Dependency mapping

**Discover module-level dependencies:**

```python
import ast, os
from collections import defaultdict

def map_dependencies(source_dir: str) -> dict[str, set[str]]:
    """Build a dependency graph from Python source files."""
    deps = defaultdict(set)

    for root, _, files in os.walk(source_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            filepath = os.path.join(root, fname)
            module = filepath.replace("/", ".").replace("\\", ".").removesuffix(".py")

            with open(filepath) as f:
                tree = ast.parse(f.read())

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        deps[module].add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        deps[module].add(node.module.split(".")[0])

    return dict(deps)

# Usage
deps = map_dependencies("src/")
for module, dependencies in sorted(deps.items()):
    print(f"{module} → {', '.join(sorted(dependencies))}")
```

**Identify circular dependencies:**
```python
def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find all circular dependency chains."""
    cycles = []
    visited = set()

    def dfs(node, path):
        if node in path:
            cycle_start = path.index(node)
            cycles.append(path[cycle_start:] + [node])
            return
        if node in visited:
            return
        path.append(node)
        for neighbor in graph.get(node, set()):
            dfs(neighbor, path[:])
        visited.add(node)

    for node in graph:
        dfs(node, [])
    return cycles
```

### Step 2 — SOLID principles checklist

**S — Single Responsibility Principle:**

| Sign of violation | Fix |
|---|---|
| Module handles both DB queries AND HTTP responses | Split into `repository.py` and `serializer.py` |
| Class does data validation, persistence, AND business logic | Extract each concern into separate classes |
| Function has >3 distinct purposes (check with comments) | Split into smaller, named functions |

**O — Open/Closed Principle:**

```python
# ❌ Violation: modifying class to add new behavior
class ReportGenerator:
    def generate(self, fmt="pdf"):
        if fmt == "pdf":
            return self._to_pdf()
        elif fmt == "csv":
            return self._to_csv()  # Adding formats requires editing this class
        elif fmt == "json":
            return self._to_json()

# ✅ Fix: strategy pattern — extend without modifying
from abc import ABC, abstractmethod

class ReportFormatter(ABC):
    @abstractmethod
    def format(self, data: dict) -> str: ...

class PDFFormatter(ReportFormatter):
    def format(self, data): return f"<pdf>{data}</pdf>"

class CSVFormatter(ReportFormatter):
    def format(self, data): return ",".join(str(v) for v in data.values())

# New formats = new class, no modification to existing code
```

**L — Liskov Substitution Principle:**

```python
# Check: can subclasses be used interchangeably with their parent?
class Bird:
    def fly(self): ...  # All birds fly?

class Penguin(Bird):
    def fly(self):  # Overrides but penguins don't fly!
        raise NotImplementedError("Penguins can't fly")

# ✅ Fix: more specific hierarchy or default behavior
class Bird:
    pass

class FlyingBird(Bird):
    def fly(self): ...

class Penguin(Bird):
    def swim(self): ...
```

**I — Interface Segregation Principle:**

| Sign of violation | Fix |
|---|---|
| Classes implement interfaces with unused methods | Split into smaller, focused protocols/interfaces |
| `typing.Protocol` with 10+ required methods | Break into multiple protocols (e.g., `Readable`, `Writable`) |

**D — Dependency Inversion Principle:**

```python
# ❌ High-level module depends on low-level detail
class OrderService:
    def __init__(self):
        self.db = PostgreSQLDatabase()  # Concrete dependency

# ✅ Depend on abstractions (protocols/interfaces)
from typing import Protocol

class Database(Protocol):
    def query(self, sql: str) -> list[dict]: ...
    def insert(self, table: str, data: dict) -> int: ...

class OrderService:
    def __init__(self, db: Database):  # Depends on abstraction
        self.db = db
```

### Step 3 — Design pattern identification

**Common patterns and when to spot them:**

| Pattern | Recognition Signal | When It's Appropriate |
|---|---|---|
| **Singleton** | Module-level global state, shared instance | Single shared resource (DB pool, config) |
| **Factory** | Multiple `if type == X: return ClassX()` branches | Object creation varies by input/condition |
| **Strategy** | Algorithm selected at runtime via enum/config | Swappable behaviors (sorting, formatting) |
| **Observer** | Callback lists, event emitters, signal handlers | Decoupled notification of state changes |
| **Decorator** | Wrapper functions/classes adding behavior | Cross-cutting concerns (logging, caching, auth) |
| **Adapter** | Translation layer between incompatible interfaces | Integrating third-party APIs with internal models |
| **Repository** | Data access abstraction behind a query interface | Decoupling business logic from data source |

### Step 4 — Coupling and cohesion analysis

**Coupling assessment:**

```python
# Measure: count distinct imports per module
def measure_coupling(module_path: str) -> int:
    """Count unique external dependencies of a module."""
    with open(module_path) as f:
        tree = ast.parse(f.read())

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, 'names', [])]
            module_name = getattr(node, 'module', '')
            imports.update(names)
            if module_name:
                imports.add(module_name.split(".")[0])

    return len(imports)

# High coupling (>15 unique deps per file) suggests the module does too much
```

**Cohesion assessment (manual checklist):**

| Cohesion Level | Indicator | Action |
|---|---|---|
| **High** ✅ | All functions in a file work on the same data/concept | No action needed — this is ideal |
| **Medium** ⚠️ | Some related, some tangential functions | Consider splitting into submodules |
| **Low** ❌ | Unrelated utilities dumped together (e.g., `utils.py` with 50+ functions) | Split by domain: `string_utils.py`, `date_utils.py`, etc. |

### Step 5 — Scalability assessment

**Checklist for scaling evaluation:**

```python
# Pattern to check in code for scalability concerns:
SCALABILITY_CHECKS = {
    "N+1 queries": [
        # Look for query-inside-loop patterns
        r"for .* in .*:\s*\n\s*(?:query|execute|\.get)",
    ],
    "unbounded growth": [
        # Lists/dicts that grow without limits
        r"(?:results|items|cache)\.append",  # Without size checks
        r"(?:_CACHE|_MEMO).*pop",           # Cache eviction? Check for it
    ],
    "blocking calls": [
        # Synchronous I/O in hot paths
        r"requests\.get\(|httpx\.get\(",     # Sync HTTP in loops
        r"time\.sleep",                      # Sleep instead of async wait
    ],
    "memory leaks": [
        # Growing collections without cleanup
        r"(?:global|GLOBAL)\s*=\s*\[",       # Global mutable state
    ],
}
```

**Scalability dimensions to evaluate:**

| Dimension | What to check | Red flags |
|---|---|---|
| **Horizontal scaling** | Statelessness, shared config | In-memory state without sync mechanism |
| **Database scaling** | Query patterns, indexes, connection pooling | N+1 queries, no connection limit |
| **Memory scaling** | Data loading patterns, caching strategy | Loading entire datasets into memory |
| **Concurrency** | Thread safety, lock granularity | Global locks, shared mutable state |

### Step 6 — Architecture review report template

When completing an architecture analysis, structure findings as:

```markdown
## Architecture Review: [Module/Project Name]

### Dependency Map
- Core modules and their dependencies (list top-level)
- Circular dependencies found: [none / list them]

### SOLID Assessment
- **SRP**: [Pass/Fail with examples]
- **OCP**: [Pass/Fail with examples]
- **LSP**: [Pass/Fail with examples]
- **ISP**: [Pass/Fail with examples]
- **DIP**: [Pass/Fail with examples]

### Coupling Analysis
- Average dependencies per module: X
- Highest coupling module: `module_name` (Y deps)
- Recommendation: [if any]

### Scalability Notes
- N+1 query patterns: [found / none found]
- Memory concerns: [list if any]
- Blocking I/O in hot paths: [list if any]

### Recommendations (Priority Ordered)
1. **High**: [Most impactful change with estimated effort]
2. **Medium**: [Secondary improvement]
3. **Low**: [Nice-to-have refinement]
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| Max deps per module | < 15 unique imports | Higher suggests the module has too many responsibilities |
| Function length | < 30 lines ideally | Longer functions are harder to reason about architecturally |
| File size | < 400 lines | Larger files indicate low cohesion — split into submodules |

## What NOT to do

- Do not add design patterns where simple code suffices — YAGNI (You Aren't Gonna Need It)
- Do not refactor architecture without understanding the current dependency graph first
- Do not enforce SOLID principles dogmatically — practical readability beats theoretical purity
- Do not ignore circular dependencies — they create tight coupling and make testing harder
- Do not scale prematurely — optimize architecture when you have evidence of bottlenecks, not speculation