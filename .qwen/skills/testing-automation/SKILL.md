---
name: testing-automation
description: Write and run unit tests, integration tests, test coverage analysis with pytest patterns, mock strategies, and edge case identification
source: manual
version: "1.0.0"
triggers:
  - "unit test"
  - "integration test"
  - "pytest"
  - "test coverage"
  - "mock"
  - "edge case"
  - "write tests"
  - "testing"
---

## Goal

Write comprehensive, maintainable tests using pytest that cover unit logic, integration paths, and edge cases. Achieve meaningful test coverage (not just percentage) by targeting critical code paths and failure modes.

## Procedure

### Step 1 — Test structure and naming conventions

**Directory layout:**
```
project/
├── src/
│   └── mymodule/
│       ├── __init__.py
│       └── processor.py
├── tests/
│   ├── conftest.py           # Shared fixtures
│   ├── unit/
│   │   └── test_processor.py  # Unit tests (fast, isolated)
│   └── integration/
│       └── test_api.py        # Integration tests (slower, real deps)
├── pytest.ini                 # Pytest configuration
└── requirements-test.txt      # Test dependencies
```

**Naming convention:** `test_<function_or_behavior>_<scenario>`
- ✅ `test_parse_csv_handles_empty_rows`
- ✅ `test_connect_retries_on_timeout`
- ❌ `test_1`, `my_test`, `test_stuff`

### Step 2 — Pytest fixture patterns

```python
import pytest

# Simple fixture: reusable test data
@pytest.fixture
def sample_user():
    return {"id": 1, "name": "Alice", "email": "alice@test.com"}

# Parametrized fixture: multiple variations
@pytest.fixture(params=["json", "csv", "xml"])
def format_type(request):
    return request.param

# Fixture with autouse for setup/teardown
@pytest.fixture(autouse=True)
def clean_temp_dir(tmp_path):
    """Ensure temp directory exists and is clean before each test"""
    (tmp_path / "uploads").mkdir()
    yield tmp_path
    # Teardown runs here automatically

# Fixture dependency chain
@pytest.fixture
def db_connection():
    conn = create_db_connection()
    yield conn
    conn.close()

@pytest.fixture
def seeded_database(db_connection):
    """Database with test data — depends on connection"""
    seed_test_data(db_connection)
    return db_connection
```

### Step 3 — Mock strategies

**Mocking external dependencies:**

```python
from unittest.mock import patch, MagicMock, mock_open
import requests

# Patch a function at call site
@patch("mymodule.requests.get")
def test_fetch_data(mock_get):
    mock_get.return_value.json.return_value = {"items": [1, 2, 3]}
    result = mymodule.fetch_data()
    assert len(result) == 3

# Patch with side_effect for multiple calls returning different values
@patch("mymodule.requests.get")
def test_retry_on_failure(mock_get):
    mock_get.side_effect = [
        requests.exceptions.ConnectionError(),  # First call fails
        MagicMock(json=lambda: {"status": "ok"}),  # Second succeeds
    ]
    result = mymodule.fetch_data(retries=2)
    assert result["status"] == "ok"
    assert mock_get.call_count == 2

# Mock file operations
@patch("builtins.open", mock_open(read_data="line1\nline2"))
def test_read_file():
    content = mymodule.read_config("/path/to/config")
    assert "line1" in content

# Patch object methods (not the class itself)
def test_instance_method(sample_user):
    sample_user.save = MagicMock(return_value=True)
    assert sample_user.save() is True
```

**When to mock vs. when not to:**
| Mock | Don't Mock |
|---|---|
| External APIs (HTTP, databases) | Pure functions and local logic |
| File system operations | Simple data transformations |
| Time-dependent behavior (`datetime.now`) | Constants and configuration values |

### Step 4 — Edge case identification checklist

For every function, test these categories:

```python
import pytest

def test_edge_cases_example():
    """Test the parse_number function against edge cases"""
    # Empty / None inputs
    assert parse_number(None) == 0
    assert parse_number("") == 0

    # Boundary values
    assert parse_number("0") == 0
    assert parse_number("-1") == -1
    assert parse_number("999999999") == 9_999_999

    # Whitespace and formatting
    assert parse_number(" 42 ") == 42
    assert parse_number("42.0") == 42

    # Invalid inputs (should raise or return default)
    with pytest.raises(ValueError):
        parse_number("abc")

# Edge case categories to always consider:
EDGE_CASE_CATEGORIES = [
    ("empty_input", "None, empty string, empty list/dict"),
    ("single_element", "Lists with one item, single key dicts"),
    ("boundary_values", "0, -1, MAX_INT, empty collections"),
    ("whitespace", "Leading/trailing spaces, newlines, tabs"),
    ("unicode", "Non-ASCII characters, emoji, mixed scripts"),
    ("type_variation", "int vs float, str vs bytes, list vs tuple"),
    ("order_dependent", "Reversed lists, unsorted inputs"),
    ("duplicate_data", "Repeated values, overlapping ranges"),
]
```

### Step 5 — Parametrized testing for coverage

```python
@pytest.mark.parametrize("input_val,expected", [
    (0, "zero"),
    (1, "one"),
    (-5, "negative"),
    (100, "large"),
    ("abc", None),       # Invalid input → None
])
def test_classify_number(input_val, expected):
    assert classify_number(input_val) == expected

# Parametrize with indirect fixtures for complex setup
@pytest.mark.parametrize("db_type", ["sqlite", "postgresql"])
def test_database_query(db_type, request):
    conn = create_connection(request.getfixturevalue(f"{db_type}_conn"))
    result = conn.query("SELECT 1")
    assert result == [(1,)]
```

### Step 6 — Coverage analysis and reporting

**pytest.ini configuration:**
```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    integration: marks tests as integration tests
addopts = --tb=short -v

[tool:pytest]
# Coverage config
filterwarnings =
    ignore::DeprecationWarning
```

**Run with coverage:**
```bash
# Basic coverage report
pytest --cov=mymodule --cov-report=term-missing tests/

# HTML report for visual inspection
pytest --cov=mymodule --cov-report=html:coverage_html tests/

# Fail if below threshold (enforce in CI)
pytest --cov=mymodule --cov-fail-under=80 tests/

# Exclude integration tests from unit test runs
pytest -m "not slow and not integration" tests/unit/
```

**Interpreting coverage output:**
- **Lines missing**: Add targeted tests for uncovered branches
- **Branches missed**: Test both `if` and `else` paths explicitly
- **100% on trivial code is fine**: Focus coverage effort on complex logic

### Step 7 — Integration test patterns

```python
import pytest
import time

@pytest.mark.integration
def test_full_pipeline():
    """Test end-to-end: read → process → write"""
    # Setup with real (but isolated) resources
    input_file = create_test_input("/tmp/test_input.csv")
    
    # Execute the pipeline
    result = run_pipeline(input_file)
    
    # Assert on output, not internal state
    assert result.row_count == 100
    assert result.errors == []

@pytest.mark.integration
def test_api_roundtrip():
    """Test API call → response parsing → data validation"""
    import httpx
    client = httpx.Client(base_url="http://localhost:8080")
    
    # POST to create resource
    resp = client.post("/api/items", json={"name": "test"})
    assert resp.status_code == 201
    
    item_id = resp.json()["id"]
    
    # GET to verify it was stored
    resp = client.get(f"/api/items/{item_id}")
    assert resp.json()["name"] == "test"
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| Coverage threshold (CI) | 80% minimum | Balances quality against test maintenance cost |
| Test timeout | 30s for unit, 60s for integration | Catches hanging tests without being too strict |
| Mock granularity | Mock at the boundary (API calls, DB queries) | Keeps internal logic real and testable |

## What NOT to do

- Do not mock the thing you're testing — only mock external dependencies
- Do not assert on implementation details (private methods, internal state) — assert on behavior
- Do not skip edge case tests because "it works in practice" — edge cases are where bugs hide
- Do not write tests after production bugs — aim for test-driven development on new features
- Do not ignore flaky tests — they erode trust in the entire test suite