---
name: testing-best-practices
description: Practical Python/pytest testing guidelines for any project. Covers test organization, mocking strategies, determinism, regression tests, E2E isolation, and patterns for complex systems with orchestration or external dependencies. Use when writing new tests or adding regression coverage.
triggers:
  - write unit tests
  - add regression test
  - test this component
  - test this function
  - test this method
  - write a test for
  - create tests for
  - how should I test
  - add test coverage
  - pytest fixture
  - mock the LLM
  - mock external service
  - regression test for bug
  - E2E test
  - integration test
---

# Python Testing Best Practices (pytest)

Practical, step-by-step guidelines for writing tests in any Python project using `pytest`. Emphasizes determinism, isolation, and maintainability.

## Quick Start Checklist

Before writing a test:

- [ ] Unit or integration/E2E? Determine the scope.
- [ ] Identify external dependencies (APIs, network, filesystem, time, randomness) and plan to mock them.
- [ ] Choose the correct file location per the organization rules below.
- [ ] Ensure deterministic behavior: fixed seeds, mocked time, isolated config.

## What's Worth Testing

Not every function needs a dedicated test. Tests should earn their keep by providing confidence or catching regressions.

**Skip tests for:**
- Trivial getters/setters that just return an attribute.
- Pass-through wrappers that do nothing but call another function with the same arguments.
- Pure boilerplate (e.g., empty `__init__` with defaults only).

If changing a function would not affect any user-visible behavior, it probably doesn't need its own test. Rely on higher-level tests that cover the calling path instead.

**Prioritize tests for:**
- Business logic and decision-making code.
- Edge cases and boundary conditions (zero, negative, max values, off-by-one).
- Error handling paths (invalid input, timeouts, partial failures).
- Concurrency and async behavior (ordering, cancellation, shared state).
- Configuration parsing and validation.
- Data transformations and serialization/deserialization.

**General principle:** Test observable behavior over internal implementation details. If a refactor changes internals but not behavior, your tests shouldn't break.

## Uncovering Hidden Issues

Code that passes basic tests may still fail in production under unusual conditions. Systematically probe for non-obvious failure modes.

**Input stress testing:**
- Empty inputs: `""`, `[]`, `{}`, `None`.
- Huge inputs: very long strings, massive lists, deep nesting.
- Malformed types: pass a string where an int is expected, wrong enum values.
- Unicode and special characters: emojis, combining chars, control characters, paths with spaces or quotes.
- Mutate "normal" inputs slightly (extra field, missing optional field) to see if the code is truly robust or just works for the happy path.

**Failure mode testing:**
- Timeouts and retries exhausted.
- Partial failures: some items succeed while others fail in a batch operation.
- Concurrent access to shared state — test race conditions by adding delays or running operations in parallel.
- For I/O-bound code: permissions denied, missing files/directories, disk full (mock where practical).

**Advanced techniques:**
- Property-based testing (e.g., [`hypothesis`](https://hypothesis.readthedocs.io/)) can auto-generate edge cases for critical logic like parsers or numeric algorithms.
- Fuzz inputs around known-good values to discover boundary bugs you didn't think of.

Example — basic input stress test:
```python
@pytest.mark.parametrize("input_val", [None, "", [], {}, "a" * 1_000_000, "\u0000\u007F"])
def test_parser_handles_edge_inputs(input_val):
    with pytest.raises((ValueError, TypeError)):
        parse_input(str(input_val))
```

## Test Organization Rules

Use a hybrid structure that separates concerns by scope:

- **Unit tests for core logic** → `tests/test_<component>.py` (root of test suite)
- **Regression suites** → `tests/test_<topic>_regression.py`, or add to existing file if small (under 20 lines)
- **Integration tests for subsystems** → `tests/<domain>/test_<module>.py` (e.g., `tests/api/`, `tests/workers/`)
- **External-dependency tests** → Use markers like `@pytest.mark.live_api` or `@pytest.mark.integration`; default pytest run should exclude these.

Markers allow you to gate slow/expensive/flaky tests behind explicit opt-in:
```python
@pytest.mark.live_api
def test_real_endpoint():
    ...
```
Run with: `pytest -m live_api`

## Test Structure and Naming

```python
class TestComponentName:
    def test_normal_case_returns_expected(self):
        """When given valid input, returns expected output."""
        # Arrange
        component = Component(valid_input)
        # Act
        result = component.process()
        # Assert
        assert result == expected_value

    def test_edge_case_with_empty_input(self):
        """Handles empty input gracefully."""
        ...
```

Naming: `test_<behavior>_when_<condition>` for unit tests. For regression: `test_gh123_<description>` or `test_t2_<description>_regression`.

## Mocking Strategy

### General Rules

- **Mock at the import level**, not instances:
  ```python
  @patch('mypackage.service.ServiceClient')
  def test_with_mock(mock_class):
      mock_class.return_value.fetch.return_value = {"data": "mocked"}
  ```
- Use `MagicMock` for simple interfaces, custom fakes for complex ones.
- Verify calls when behavior matters: `mock.assert_called_once_with(arg)`.

### Mocking External Services (e.g., LLMs, APIs)

**Unit tests:** Inject a minimal mock:
```python
class MockServiceClient:
    def __init__(self, responses=None):
        self.responses = responses or ["mock response"]
        self.call_count = 0
    def call(self, request):
        self.call_count += 1
        return self.responses.pop(0)
```

**Integration/E2E tests:** For higher fidelity, spin up a local mock HTTP server with scripted responses (e.g., using `http.server` or `aiohttp`). This lets you test real HTTP round-trips without hitting external services.

### Mocking Time

Use module-level patching to control time-dependent behavior:
```python
def _fake_time_module(initial=1000.0):
    state = {'time': initial}
    mod = MagicMock()
    mod.time.side_effect = lambda: state['time']
    mod.sleep.side_effect = lambda secs: state.__setitem__('time', state['time'] + secs)
    return mod, state

def test_with_faked_time():
    fake_mod, state = _fake_time_module()
    with patch.dict('sys.modules', {'time': fake_mod}):
        # time.time() now uses the fake
        ...
```

### Mocking Randomness and Environment

- Use seeded instances: `rng = random.Random(42)` for reproducibility.
- For env vars, use fixtures that restore original values after yield.

## Testing Complex Systems (Orchestration, Workers, Agents)

When testing systems with multiple cooperating components (orchestrators, workers, agents), extra care is needed to avoid brittle or misleading tests.

### Mocking Worker/Agent Pools

Be cautious when using simplified mock pools: they are SIMULATIONS that implement only a subset of real behavior. If production code changes its target-set calculation, scheduling logic, or failure handling, these mocks must be updated in parallel — divergence goes undetected because tests validate against the mock's behavior.

- **Use simplified mocks for:** Quick unit tests needing worker-like behavior without overhead.
- **For higher fidelity:** Write integration tests that use the actual pool/orchestrator with mocked leaf services.

### Mocking Individual Workers/Agents

```python
def _make_mock_worker(name, responses=None):
    worker = MagicMock()
    worker.name = name
    worker.run.return_value = responses or ["done"]
    return worker
```

Key considerations:
- Verify coordination logic (who calls whom, in what order).
- Test failure propagation: does the orchestrator handle worker timeouts/crashes?
- Avoid testing implementation details that may change; focus on observable behavior.

## Regression Testing

1. Reproduce the bug with minimal code.
2. Write a failing test capturing the incorrect behavior.
3. Fix the bug to make it pass.
4. Document the issue reference in the docstring:

```python
def test_issue_123_nested_call_timeout():
    """Regression: nested calls should not exceed parent timeout.

    Related: #123
    Bug: Parent call would hang indefinitely when child failed.
    Fix: Added timeout check in _execute_call.
    """
    ...
```

Keep tests under 50 lines, self-contained, using mocks for latency sources.

## E2E Test Isolation

Never touch production config. Use `tmp_path_factory` for isolated directories per module:

```python
@pytest.fixture(scope="module")
def shared_tmp_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("e2e_module")
```

Key rules:
- Each E2E test module gets its own temporary workspace.
- Mock external services; never call real APIs unless explicitly marked (e.g., `@pytest.mark.live_api`).
- Reset shared state between tests when possible.

## Fixture Guidelines

- Scope narrowly: function > module > session.
- Name clearly: the name should indicate what is provided.
- Always clean up in teardown (yield pattern).

## Running Tests

```bash
pytest -v                              # Default run
pytest -n auto --timeout=60 -v         # Parallel with timeout (requires pytest-xdist, pytest-timeout)
pytest --durations=10                  # Check slow tests
pytest -m "live_api"                   # Only marked external-dependency tests
```

## Common Pitfalls to Avoid

- Using oversimplified mocks without understanding their limitations vs real behavior.
- Tests depending on each other's state — each must be independently runnable.
- Real network calls in unit tests — always mock external services.
- Non-deterministic tests — use fixed seeds, mock time, isolate environment.
- Writing to production paths — always use `tmp_path` or isolated directories.

## When in Doubt

Look at existing tests in the project for patterns:
- How are dependencies mocked? Check a well-tested core module.
- How is E2E isolation handled? Look for `tmp_path_factory` usage.
- How is time/randomness controlled? Search for `patch('time')`, `Random(seed)`.
- How are edge cases covered? Look at tests with the most assertions per line of code.
