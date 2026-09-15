---
name: testing-best-practices
description: Practical Python/pytest testing guidelines for any project — test organization, mocking strategies (incl. time/randomness/worker pools), determinism, regression tests, and E2E isolation. Use when writing new tests or adding regression coverage.
triggers:
  - "write unit tests"
  - "add regression test"
  - "test this component"
  - "write a test for"
  - "pytest fixture"
  - "mock the LLM"
  - "mock external service"
  - "regression test for bug"
  - "E2E test"
  - "integration test"
---

# Python Testing (pytest)

Determinism, isolation, maintainability. Test observable behavior, not internals — a refactor that changes internals but not behavior must not break tests.

## What's worth testing

- **Skip:** trivial getters/setters, pass-through wrappers, empty `__init__` with only defaults. If a change can't affect user-visible behavior, rely on higher-level tests.
- **Prioritize:** business logic/decisions; edge cases (zero, negative, max, off-by-one); error paths (invalid input, timeout, partial failure); concurrency/async (ordering, cancellation, shared state); config parsing/validation; serialization round-trips.

## Probe for hidden issues

Stress inputs: `None`/`""`/`[]`/`{}`, huge strings/lists, malformed types (str where int expected), unicode/control chars, paths with spaces/quotes. Failure modes: timeouts/retries exhausted, partial batch failures, concurrent access to shared state, I/O errors (permissions, missing files, disk full — mock where practical). Property-based testing (`hypothesis`) auto-generates edge cases for parsers/numeric logic.

## Organization

- Unit tests → `tests/test_<component>.py`
- Regression suites → `tests/test_<topic>_regression.py` (or append to existing file if <20 lines)
- Integration tests → `tests/<domain>/test_<module>.py`
- External-dependency tests → gate with markers (`@pytest.mark.live_api`, `@pytest.mark.integration`) so the default run excludes them; opt in via `pytest -m live_api`.

Naming: `test_<behavior>_when_<condition>`; regression `test_gh123_<desc>` or `test_t2_<desc>_regression`.

## Mocking

- **Mock at import level**, not instances:
  ```python
  @patch('mypackage.service.ServiceClient')
  def test_with_mock(mock_class):
      mock_class.return_value.fetch.return_value = {"data": "mocked"}
  ```
- `MagicMock` for simple interfaces; custom fakes for complex ones. Verify calls when behavior matters: `mock.assert_called_once_with(arg)`.
- **External services (LLM/API):** unit tests inject a minimal mock (scripted responses, track `call_count`); integration/E2E spin up a local mock HTTP server (`http.server`/`aiohttp`) for real round-trips without external calls.
- **Time** — patch the module with a controllable clock:
  ```python
  def _fake_time(initial=1000.0):
      state = {'time': initial}
      mod = MagicMock()
      mod.time.side_effect = lambda: state['time']
      mod.sleep.side_effect = lambda s: state.__setitem__('time', state['time']+s)
      return mod, state
  # with patch.dict('sys.modules', {'time': _fake_time()[0]}): ...
  ```
- **Randomness/env:** seeded instances `rng = random.Random(42)`; env-var fixtures that restore originals after yield.

## Testing complex systems (orchestrators/workers/agents)

**Simplified mock pools are simulations, not the real thing.** If production changes its target-set calc, scheduling, or failure handling, the mocks must be updated in parallel — divergence goes undetected because tests validate against the mock's behavior. Use simplified mocks only for fast unit tests; use integration tests with the real pool/orchestrator + mocked leaf services for fidelity. Verify coordination (who calls whom, order) and failure propagation (worker timeout/crash handling).

## Regression tests

Reproduce bug minimally → write a failing test capturing the wrong behavior → fix to pass → document in the docstring. Keep <50 lines, self-contained, mock latency sources. Template:
```python
def test_issue_123_nested_call_timeout():
    """Regression: nested calls must not exceed parent timeout.

    Related: #123
    Bug: parent call hung indefinitely when a child failed.
    Fix: added timeout check in _execute_call.
    """
    ...
```

## E2E isolation

Never touch production config. Per-module isolated dir via `tmp_path_factory`:
```python
@pytest.fixture(scope="module")
def shared_tmp_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("e2e_module")
```
Mock external services (real APIs only behind `@pytest.mark.live_api`); reset shared state between tests.

## Fixtures

Narrow scope (function > module > session); clear names; always clean up in teardown (yield pattern).

## Running

```bash
pytest -v
pytest -n auto --timeout=60 -v    # parallel + timeout (needs pytest-xdist, pytest-timeout)
pytest --durations=10             # slowest tests
pytest -m live_api                # only marked external tests
```

## Pitfalls

Oversimplified mocks whose limits you don't understand; tests depending on each other's state (each must run standalone); real network in unit tests; non-determinism (fix seeds, mock time, isolate env); writing to production paths (use `tmp_path`).

## When in doubt

Mirror the project's best-tested module. Check how it: mocks dependencies (a well-tested core module), handles E2E isolation (`tmp_path_factory` usage), controls time/randomness (`patch('time')`, `Random(seed)`), and covers edge cases (tests with the most assertions per line of code).
