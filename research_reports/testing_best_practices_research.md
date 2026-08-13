# Testing Best Practices Research Report for Agent Cascade

**Date:** 2026-08-13  
**Prepared for:** Skill creation team  
**Purpose:** Provide evidence-based recommendations for creating a reusable "Testing Best Practices" skill for the Agent Cascade system.

---

## Executive Summary

This research synthesizes observations from the existing Agent Cascade test suite, general Python testing best practices, and domain-specific challenges of testing AI agent systems. The findings produce practical, implementable guidelines that can be directly translated into a reusable skill document.

**Key Findings:**
- Agent Cascade uses `pytest` with extensive mocking for isolation, especially for LLM calls and external services
- Test organization is hybrid: root `tests/` contains both unit AND integration tests; subdirectories (`agents/`, `llm/`, `tools/`) contain specialized tests, some requiring local LLM servers
- Critical testing patterns include: session-scoped fixtures for LLM auto-detection, `MockAgentPool` for compression testing (with important caveats), and programmable mock HTTP servers for E2E testing
- The codebase demonstrates strong practices in test isolation, deterministic behavior, and regression test documentation

**CRITICAL WARNING:** `MockAgentPool` is a simulation, not the real AgentPool. It only implements the subset of methods used by compress_context and helpers. Tests validate against the mock's behavior, not production code. Use actual AgentPool tests (`test_compression_no_duplication.py`) for higher fidelity.

---

## 1. General Unit Testing Best Practices

### 1.1 What Makes a Good Unit Test

**Evidence from Agent Cascade:**

- **Isolation**: Tests never touch production config files. The `e2e_config_isolation` fixture (in `test_e2e_agent_calls.py`, lines 46-58) uses `tmp_path_factory` to create isolated directories per test module.
- **Determinism**: Use fixed seeds for randomness (`random.Random(42)` in `loop_test_utils.py`, lines 168, 214). Mock time with custom fixtures instead of real `time.time()` (see `_fake_time_module` in `test_async_shell_cmd.py`, lines 27-34).
- **Speed**: Tests avoid network calls where possible. Mock LLM calls, filesystem operations, and subprocesses. Unit tests typically run in milliseconds.
- **Clarity**: Test names describe the scenario, not just the function. Example: `test_enqueue_and_drain` in `test_agent_pool.py`, line 62.

**Recommended Practices:**

```python
# GOOD: Clear, isolated, deterministic
def test_retry_policy_exponential_backoff():
    """Verify exponential backoff with jitter caps at max_delay."""
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0)
    delays = [calculate_backoff(policy, attempt=i) for i in range(5)]
    expected = [1.0, 2.0, 4.0, 8.0, 8.0]
    assert all(abs(d - e) < 0.1 for d, e in zip(delays, expected))

# BAD: Relies on real time, not isolated
def test_retry_works():
    time.sleep(1)  # Slow, non-deterministic
    result = do_something()
    assert result is not None
```

### 1.2 Naming Conventions and Structure

**Agent Cascade Patterns:**

- Tests are organized in classes matching the component being tested: `TestComputeDiscardCount`, `TestRetryPolicyDefaults` in `test_retry_policy.py`.
- Method names describe the behavior: `test_normal_fraction`, `test_force_mode_bypasses_tail_guard` (see `test_compression.py`, lines 71-99).
- Some tests use "scenario" format: `test_t2_async_spawn_reservation_regression` in `test_e2e_agent_calls.py`, line 655.

**Recommended Structure:**

```python
class TestComponentName:
    """Test the ComponentName class."""

    def test_normal_case(self):
        """When given valid input, returns expected output."""
        # Arrange
        input_data = {"key": "value"}
        component = Component(input_data)

        # Act
        result = component.process()

        # Assert
        assert result == expected

    def test_edge_case_with_empty_input(self):
        """Handles empty input gracefully."""
        # ...
```

### 1.3 Handling Dependencies and Mocking

**Agent Cascade Approaches:**

- **External Services**: Mock at the import level using `patch` from `unittest.mock`. Example: `patch('agent_cascade.operation_manager.OperationManager')` in `test_agent_pool.py`, lines 20-23.
- **Filesystem**: Use `tmp_path` fixtures to create temporary directories. Never write to production paths.
- **Network**: Use custom HTTP mock servers (see `ProgrammableMockLLMHandler` in `test_e2e_agent_calls.py`, lines 75-160).
- **LLM Calls**: Create fixture `local_llm_cfg` that points to a local server with lightweight models, or fully mock LLM interfaces.

**Mocking Strategy:**

```python
from unittest.mock import patch, MagicMock

@patch('module.ClassName')
def test_with_mock(mock_class):
    """Mock the dependency completely."""
    mock_instance = mock_class.return_value
    mock_instance.method.return_value = "mocked"
    
    # Test code
```

### 1.4 Handling Randomness, Time, and Environment

**Evidence:**

- `loop_test_utils.py` uses `random.Random(42)` for reproducible randomness (lines 168, 214).
- Time is mocked using `patch.dict('sys.modules', {'time': fake_mod})` as seen in `test_async_shell_cmd.py`, lines 252, 263, 274, 283, 562.
- Environment variables are set via fixtures: `_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", "test_e2e")` before imports (see `test_e2e_agent_calls.py`, line 24).

**Best Practices:**

```python
import os
from pytest import fixture
from unittest.mock import patch, MagicMock

@fixture
def controlled_env():
    """Temporarily set environment variables."""
    original = os.environ.get("MY_VAR")
    os.environ["MY_VAR"] = "test_value"
    yield
    if original:
        os.environ["MY_VAR"] = original
    else:
        del os.environ["MY_VAR"]

@fixture
def frozen_time(monkeypatch):
    """Freeze time for deterministic tests."""
    import time
    fake_time_mod = MagicMock()
    fake_time_mod.time.return_value = 1234567890.0
    monkeypatch.dict('sys.modules', {'time': fake_time_mod})

# OR, more robustly (as in test_async_shell_cmd.py):
def _fake_time_module(initial=1000.0):
    """Create a fake time module for mocking polls without real delays."""
    state = {'time': initial, 'elapsed': 0.0}
    mod = MagicMock()
    mod.time.side_effect = lambda: state['time']
    mod.sleep.side_effect = lambda secs: state.__setitem__('time', state['time'] + secs) or state.__setitem__('elapsed', state['elapsed'] + secs)
    return mod, state

def test_with_faked_time():
    fake_time_mod, state = _fake_time_module()
    with patch.dict('sys.modules', {'time': fake_time_mod}):
        # Test code that uses time.time()
```

---

## 2. Regression Testing Best Practices

### 2.1 Turning Bugs into Regression Tests

**Agent Cascade Example:**

- `test_session_load_regression.py`: Explicitly documents the design requirements being tested (5 specific points from the design doc, lines 7-16).
- `test_compression_boundary_fix.py`: Tests a specific bug fix with clear before/after behavior.
- `test_inner_loop_regression.py`: Simple test that ensures a bug doesn't return.

**Process:**

1. **Reproduce the bug** with minimal code.
2. **Write a failing test** that captures the incorrect behavior.
3. **Fix the bug** to make the test pass.
4. **Document** the issue reference (e.g., `#123`, GitHub issue, TODO comment).

**Example:**

```python
def test_issue_123_nested_call_timeout():
    """Regression: nested agent calls should not exceed timeout.
    
    Related: #123
    Bug: Parent call would hang indefinitely when child failed.
    Fix: Added timeout check in _execute_llm_call.
    """
    # ...
```

### 2.2 Where Regression Tests Should Live

**Agent Cascade Structure:**

- **Unit test files** in `tests/` root for logic regression tests (e.g., `test_retry_policy.py`).
- **Regression-specific files**: `test_*_regression.py` for focused regression suites (e.g., `test_session_load_regression.py`, `test_inner_loop_regression.py`).
- **Integration/regression**: Files like `test_e2e_agent_calls.py` contain both E2E and regression scenarios.

**Recommendation:**

- **Quick regression tests** (10-20 lines) → add to existing unit test file in the same module.
- **Large regression suites** → create dedicated `test_*_regression.py` file.
- **E2E regressions** → `tests/` root with clear `"""Regression tests for X"""` header.

### 2.3 Naming Regression Tests for Traceability

**Patterns Observed in Codebase:**

- `test_t2_async_spawn_reservation_regression` - uses task reference (T2) (see `test_e2e_agent_calls.py`, line 655)
- `test_session_load_regression.py` - file-level regression focus
- `test_inner_loop_regression.py` - simple name with `_regression` suffix
- Some tests include bug numbers in docstrings but not names

**Best Practice:**

```python
def test_gh123_nested_agent_call_timeout():
    """GitHub #123: nested calls timeout correctly."""

def test_issue_456_compression_duplication():
    """Issue #456: prevents compression from duplicating messages."""

# OR using task references:
def test_t2_async_spawn_reservation_regression():
    """Task T2: ensure async spawn doesn't cause reservation issues."""
```

### 2.4 Keeping Regression Suites Fast and Meaningful

**Agent Cascade Practices:**

- Regression tests are self-contained, requiring no LLM or network (see `test_session_load_regression.py` comment, lines 17-17).
- They test specific failure modes, not full functionality.
- Often use `MockAgentPool` to simulate production behavior without the overhead.

**Guidelines:**

- Keep each regression test under 50 lines if possible.
- Use mocks for anything that introduces latency (DB, network, LLM).
- Run regression tests separately in CI if they are slow.

---

## 3. Practices Specific to Agent/Orchestration Systems

### 3.1 Testing Orchestration Logic Without Real LLMs

**Evidence from Agent Cascade:**

- **MockAgentPool**: A minimal mock that replicates `AgentPool` behavior for compression and state tests (`conftest.py`, lines 325-472). It implements `instance_conversations` as a synced mapping to mirror production. ⚠️ **WARNING**: This is a SIMULATION, not the real AgentPool. If production code changes its target-set calculation or marker detection logic, this mock must be updated in parallel — divergence will go undetected because tests validate against the mock's behavior, not the real pool's. For higher-fidelity compression testing that uses the actual AgentPool, see `test_compression_no_duplication.py`.
- **Programmable Mock LLM Server**: `test_e2e_agent_calls.py` spins up an HTTP server that returns scripted responses, allowing full E2E testing without real LLMs (lines 75-160).
- **Dependency Injection**: Fixtures like `local_llm_cfg` provide config dicts instead of hardcoded values, making it easy to swap in mock implementations.

**Implementation Pattern:**

```python
class MockLLM:
    """Minimal LLM interface for unit tests."""
    
    def __init__(self, responses=None):
        self.responses = responses or ["mock response"]
        self.call_count = 0
        
    def chat(self, messages):
        self.call_count += 1
        return self.responses.pop(0)

def test_orchestrator_logic(mock_llm):
    orchestrator = Orchestrator(llm=mock_llm)
    result = orchestrator.dispatch("task")
    assert result == "expected"
```

### 3.2 Mocking/Stubbing Agents, Tools, and Message Flows

**Agent Cascade Patterns:**

- **Agent Templates**: Mocked with `MagicMock` having `name`, `agent_class`, `llm`, `function_map` attributes (`test_nested_agent_calls.py`, lines 27-40).
- **Tools**: Often patched at import level: `patch('agent_cascade.tools.some_tool.ToolClass')`.
- **Message Flows**: Use synthetic conversation histories built with helper functions (`_make_msg`, `_build_pool_with_history`).

**Best Practices:**

```python
def _make_mock_agent(name, responses=None):
    """Create a mock agent with minimal interface."""
    agent = MagicMock()
    agent.name = name
    agent.run.return_value = responses or ["done"]
    return agent

def test_agent_communication():
    agent_a = _make_mock_agent("researcher")
    agent_b = _make_mock_agent("reviewer")
    
    orchestrator = Orchestrator()
    orchestrator.connect(agent_a, agent_b)
    
    # Simulate message passing
    orchestrator.send_message(agent_a, "task")
    assert agent_b.run.called
```

### 3.3 Testing Tool Call Handling, Error Paths, Retries, Timeouts

**Evidence:**

- `test_retry_policy.py`: Comprehensive testing of error classification, backoff calculations, and policy defaults.
- `test_async_shell_cmd.py`: Tests heartbeat routing, control commands, edge cases with mocked time (lines 249-287).
- `test_tool_chain_boundary.py`: Tests tool call boundaries and transitions.

**Testing Strategies:**

```python
def test_retry_on_network_error():
    """Retry policy should retry on network errors."""
    policy = RetryPolicy()
    
    # Simulate a function that fails then succeeds
    call_count = 0
    
    def flaky_function():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("Network failed")
        return "success"
    
    result = with_retry(flaky_function, policy)
    assert result == "success"
    assert call_count == 3

def test_timeout_cancels_long_running():
    """Timeout should cancel operation after specified duration."""
    fake_time_mod, state = _fake_time_module()
    with patch.dict('sys.modules', {'time': fake_time_mod}):
        # Simulate time passing by advancing state
        state['time'] = 1020
        with pytest.raises(TimeoutError):
            long_running_operation(timeout=5)
```

### 3.4 Testing Configuration and Settings Behavior

**Agent Cascade Approach:**

- Fixtures create isolated config directories (`e2e_config_isolation` in `test_e2e_agent_calls.py`, lines 46-58).
- Settings are passed as dicts or objects to functions, not hardcoded.
- Default values are tested explicitly (`test_policy_default_values` in `test_retry_policy.py`, lines 30-51).

**Best Practices:**

```python
def test_settings_merge_disabled_tools():
    """Merging settings should combine disabled_tools instead of overwriting."""
    base_settings = {"disabled_tools": {"agent1": ["tool_a"]}}
    override_settings = {"disabled_tools": {"agent2": ["tool_b"]}}
    
    merged = merge_settings(base_settings, override_settings)
    
    assert "agent1" in merged["disabled_tools"]
    assert "agent2" in merged["disabled_tools"]

def test_import_export_preserves_data():
    """Export to JSON and import back should preserve all values."""
    settings = Settings(...)
    exported = settings.to_json()
    imported = Settings.from_json(exported)
    
    assert imported == settings
```

---

## 4. Practical Guidelines for Agent Cascade Codebase

### 4.1 Recommended Test Structure/Layout

**Current Structure (Effective):**

```
tests/
├── conftest.py                    # Shared fixtures, mocks, configuration
├── test_unit_module.py            # Unit tests for core logic
├── test_integration_*.py          # Integration tests (may require local LLM)
├── test_regression_*.py           # Focused regression suites
├── agents/                        # Agent-specific tests (integration)
│   ├── test_article_agent.py      # Note: skipped but present
│   └── test_assistant.py          # Uses local_llm_cfg
├── llm/                           # LLM integration tests
│   ├── test_continue.py           # Uses skip_if_no_local marker
│   ├── test_local_llm.py          # Uses skip_if_no_local
│   └── test_oai.py                # Uses skip_if_no_local
├── tools/                         # Tool tests
│   ├── test_edit_file_modes.py    # Unit tests
│   └── test_tools.py              # Hybrid: some mocked, some live external API
└── examples/                      # Example integration tests (live APIs)
    └── test_examples.py           # Uses extra_examples marker
```

**Important Notes:**

- Root `tests/` contains BOTH unit AND integration tests (e.g., `test_e2e_agent_calls.py`, `test_api_endpoints.py`).
- Subdirectories don't exclusively contain integration tests requiring local LLM servers.
- pytest.ini (`addopts = -n auto --timeout=60 -m "not live_api and not skip_if_no_local..."`) excludes most network-dependent tests by default.

### 4.2 Minimal Checklist for New Tests

**Before Writing a Test:**

- [ ] Isolate the test from production state (use fixtures, tmp_path, mocks)
- [ ] Determine if this is a unit or integration test
- [ ] Identify external dependencies (LLM, network, filesystem) and plan to mock them
- [ ] Ensure deterministic behavior (fixed seeds, frozen time if needed)

**When Writing the Test:**

- [ ] Use descriptive names: `test_<scenario>_when_<condition>_returns_<result>`
- [ ] Follow arrange-act/assert structure
- [ ] Keep tests independent; no test should depend on another's state
- [ ] For regression tests, include docstring linking to issue/bug reference
- [ ] Add appropriate pytest markers if needed (`@pytest.mark.skip_if_no_local`, `@pytest.mark.extra_tools`, etc.)

**Mocking Dependencies:**

- [ ] Mock at the highest level possible (patch imports, not instances)
- [ ] Use `MagicMock` for simple interfaces, custom fakes for complex ones
- [ ] Verify mocks were called correctly when needed (`assert_called_once_with`)
- [ ] For time mocking, use `patch.dict('sys.modules', {'time': fake_mod})` as in `test_async_shell_cmd.py`

**Running Tests:**

- [ ] Run with `pytest -v` to see detailed output
- [ ] Use markers appropriately for network-dependent tests
- [ ] Ensure tests pass in isolation, not just as a suite
- [ ] Check test duration with `--durations=10` if performance is a concern

---

## 5. Additional Testing Domains

### 5.1 Test Parallelization and Performance

**Agent Cascade Setup:**

- Uses pytest-xdist (`-n auto`) for parallel execution (see `pytest.ini`, line 10).
- Per-test timeout of 60 seconds (`--timeout=60`).
- Session-scoped fixtures are used to avoid redundant setup (e.g., `local_llm_available` in `conftest.py`, scope="session").

**Recommendations:**

- Keep test fixtures as narrow in scope as possible (function > module > session).
- Use `--durations=10` to identify slow tests.
- Consider marking long-running tests with a custom marker to exclude from default runs.

### 5.2 CI/CD Integration Patterns

**Current Setup:**

- Tests are excluded by default based on markers (`live_api`, `skip_if_no_local`, `extra_examples`, `extra_tools`, `extra_vl`).
- This allows quick feedback on core functionality while still enabling full test runs when needed.

**Best Practices:**

- Create separate CI jobs for different test categories (unit, integration, E2E).
- Use environment variables to control test selection in CI.
- Fail fast on unit tests before running integration tests.

### 5.3 Code Coverage Requirements

**Current State:**

- No explicit coverage enforcement in the project (based on file inspection).
- However, test coverage is a valuable metric to track over time.

**Recommendations:**

- Use `pytest-cov` to measure coverage.
- Set minimum coverage thresholds in CI.
- Focus on critical paths: orchestration logic, retry policies, tool calling.

### 5.4 Performance and Load Testing

**Examples in Codebase:**

- `test_endpoint_scheduler_stress.py`: Tests scheduler under stress conditions.
- `test_rate_limiting_concurrency.py`: Tests rate limiting with concurrent requests.
- `run_perf_tests.py`: Script for performance benchmarking.

**Approach:**

- Create synthetic load using mocked components.
- Measure wall-clock time and resource usage.
- Use fixtures to control concurrency levels.

### 5.5 Security Testing Considerations

**Agent Cascade Practices:**

- `test_security_parser.py`: Tests security parser for dangerous commands.
- `test_security_endpoint_inheritance.py`: Tests endpoint security inheritance.
- Environment variable isolation (e.g., `AGENT_CASCADE_TEST_CONFIG_DIR`).

**Recommendations:**

- Test input sanitization and validation.
- Verify that dangerous operations are properly controlled.
- Ensure test data doesn't leak into production.

---

## 6. Key Takeaways for Skill Creation

Based on this research, the "Testing Best Practices" skill should include:

1. **Fixture Design**: How to create isolated test environments and reusable fixtures
2. **Mocking Strategy**: When and how to mock external dependencies, with examples from Agent Cascade (including proper time mocking)
3. **Test Organization**: Clear structure for unit vs integration vs regression tests (with accurate understanding of current hybrid structure)
4. **Determinism**: Techniques for controlling randomness, time, and environment (with correct implementation patterns)
5. **Agent-Specific Patterns**: Mocking agents, tools, message flows, and orchestration logic (with warnings about MockAgentPool limitations)
6. **Regression Testing**: Turning bugs into maintainable tests with traceability
7. **Additional Domains**: Parallelization, CI/CD, coverage, performance, security

The skill should be practical, with code snippets directly applicable to the Agent Cascade codebase. All examples must accurately reflect actual patterns used in the codebase.

---

## 7. References

- Agent Cascade test suite: 
  - `tests/conftest.py` (lines 325-472 for MockAgentPool warning)
  - `tests/test_agent_pool.py` (mocking pattern)
  - `tests/test_e2e_agent_calls.py` (lines 46-58 for e2e_config_isolation, lines 75-160 for programmable mock server)
  - `tests/test_retry_policy.py` (default value testing)
  - `tests/test_async_shell_cmd.py` (lines 27-34 for _fake_time_module, lines 252-287 for time mocking)
  - `tests/loop_test_utils.py` (lines 168, 214 for random seed usage)
- pytest configuration: `pytest.ini` (lines 10-21 for markers and options)
- Existing documentation: `docs/test_suite_live_vs_regression_analysis.md`

---

**Next Steps:** Use this research to draft the "Testing Best Practices" skill document in `.qwen/skills/testing-best-practices/SKILL.md`.
