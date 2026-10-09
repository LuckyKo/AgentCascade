# CI Test Selection (AgentCascade)

This document is the single source of truth for **what the test suite runs in CI,
why, and how to reproduce it locally**. It is written for anyone doing regression
triage or planning new tests: read it before touching the test infra.

**Design principle — minimum separation.** CI runs the *same* suite the Windows
dev machine runs: the real `pytest.ini` and the real `tests/` tree. There is **no
parallel manifest, no CI-only ini, no divergent test list.** The only CI-specific
artifacts are `.github/workflows/tests.yml` (the runner) and the three
*capability* markers below, which keep the suite green on a runner that is missing
a resource the dev machine has.

---

## 1. What CI runs

`.github/workflows/tests.yml` runs three jobs on `ubuntu-latest`, Python 3.12:

| job | Docker | Node | command | purpose |
|---|---|---|---|---|
| `tests-normal` | available | — | `pytest -n auto -m "not fullstack_e2e and …"` | Full suite should be **green** (minus `fullstack_e2e`). `requires_docker` tests **PASS** (not skip); `windows_only` tests **SKIP** on Linux. |
| `tests-no-docker` | forced off | — | `pytest -n 2 -m "not fullstack_e2e and …"` | Proves the capability skip-guards turn would-be failures into clean **skips** (minus `fullstack_e2e`). |
| `tests-e2e-fullstack` | available | **24** | `python -m pytest tests/test_streaming_fullstack_e2e.py -v --timeout=540 -o addopts=""` | The full-stack streaming E2E test, run **serially** in its own job (see §1.2). |

The two parallel jobs share this shape:

```bash
pip install -r requirements.txt          # adds the 3 pytest plugins (see §1.1)
export AGENT_WORKSPACE="$PWD/.ci_workspace"   # REQUIRED — see §2
mkdir -p "$AGENT_WORKSPACE"
pytest -n <N> -m "<addopts filter> and not fullstack_e2e" -p no:cacheprovider
```

The `-m` filter is the **`addopts` filter from `pytest.ini` repeated verbatim, plus
`not fullstack_e2e`** — not a divergent test list. It is set on the command line because
the CLI `-m` **replaces** (does not combine with) the `addopts` `-m`, so the full filter
must be restated; the only *change* from `addopts` is the added `not fullstack_e2e`
clause, which keeps the timing-sensitive full-stack E2E test out of the parallel runs
(it runs once, in the dedicated serial `tests-e2e-fullstack` job, §1.2). `-n` is the only
other override and is allowed because CLI args are appended after `addopts`. The shared
`pytest.ini` `addopts` line is left **untouched** (minimum separation).

### 1.1 The three pytest plugins

`requirements.txt` carries a `# ── Test Suite (pytest plugins required by pytest.ini addopts/markers) ──` section with exactly three plugins,
required by the `addopts`/markers:

| plugin | provides | used by |
|---|---|---|
| `pytest-xdist` | `-n` (parallel execution) | `addopts = -n auto ...` |
| `pytest-timeout` | `--timeout=60` | `addopts = ... --timeout=60` |
| `pytest-asyncio` | `@pytest.mark.asyncio` | `tests/test_queue_warnings.py` (the only `async def test_` in the suite) |

Without them, `pytest` fails at startup with `unrecognized arguments: -n --timeout=60`
and the two async tests in `test_queue_warnings.py` fail with
"async def functions are not natively supported" (2 failed, 2 passed → 4 passed with the plugin).

`gradio` is **deliberately not added** here — see §4 and §8.

### 1.2 The dedicated serial E2E job (`tests-e2e-fullstack`)

`tests/test_streaming_fullstack_e2e.py` (marker `fullstack_e2e`) is the repo's most
complete E2E test: a real uvicorn server + real agent loop + the **real frontend**
`web_ui/app.js` loaded in Node over a live WebSocket. It uses a scripted local mock
LLM (`http.server.HTTPServer`), so it needs **no** live LLM / API key / browser.

It runs in its **own serial job** (not under `-n auto`) for two reasons:

1. **Timing-sensitive.** It asserts incremental streaming growth over a 12s window;
   in-process frame capture doesn't work under xdist, and parallelism would flake it.
   So the invocation is serial and uses `-o addopts=""` — a *single in-scope file*, so
   dropping `-n auto` and the default `-m` filter is intentional (see §8.5 for why
   `-o addopts=""` is otherwise a trap).
2. **Hard Node 24 requirement.** The harness does `subprocess.run(['node', ...])`
   (`test_streaming_fullstack_e2e.py:804`, called unconditionally at `:1488`, asserted
   at `:1490`) with **no** skipif, and the frontend uses `const WebSocket =
   globalThis.WebSocket` (`:610`) — Node's **native** WebSocket, which requires
   **Node 24** (not 18). The job therefore installs `actions/setup-node@v4` with
   `node-version: '24'` before the pytest step.

**Dependency:** the test does `import websocket` (`:833`), which is the package
**`websocket-client`** (a sync WebSocket client) — **not** `websockets` (the asyncio
library already in `requirements.txt`). `websocket-client` was added to
`requirements.txt`; both packages are needed and both remain.

**Excluded from the parallel jobs.** So the test runs exactly once, `fullstack_e2e` is
excluded from `tests-normal` and `tests-no-docker` via a **command-line `-m` override
only** (the shared `pytest.ini` `addopts` is untouched — minimum separation). Because
the CLI `-m` *replaces* the `addopts` `-m`, both jobs repeat the **full** `pytest.ini`
filter verbatim plus `not fullstack_e2e`:

```
-m "not fullstack_e2e and not live_api and not skip_if_no_local and not extra_examples and not extra_tools and not extra_vl and not stress"
```

This does **not** change the Windows dev-machine default run (`pytest`), which still
includes `fullstack_e2e` (§3). It only stops the CI parallel jobs from trying to run a
Node test on a runner that has no Node.

---

## 2. Required environment variables

| variable | required? | why |
|---|---|---|
| `AGENT_WORKSPACE` | **Yes (on Linux)** | `tests/test_regression_logging_refinement.py:117,183,208,276` do `Path(os.environ.get('AGENT_WORKSPACE', 'N:\\work\\WD\\AgentWorkspace'))`. Unset on Linux → 4 hard failures pointing at a Windows path. CI sets it. (Changing the fallback is a separate follow-up.) |
| `OPENBLAS_NUM_THREADS` / `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `NUMEXPR_NUM_THREADS` (all `=1`) | Yes on **constrained** runners | Prevents an `OpenBLAS blas_thread_init: pthread_create failed` interpreter crash at process startup on a low-thread-limit runner. Harmless on a normal runner. |
| `AGENT_CASCADE_DISABLE_DOCKER` | CI-only (no-docker job) | `=1` forces the `docker_available()` probe (§4) to report unavailable, so CI can deterministically exercise the skip path on a Docker-capable GitHub runner. |
| `AGENT_CASCADE_RUN_LOCAL_TESTS` | Opt-in | `=1` enables the live local-LLM tests (`skip_if_no_local`). Off by default (production-safety). |
| `DASHSCOPE_API_KEY` | Opt-in | Enables the DashScope/`live_api`/`extra_*` tests. Off by default. |

---

## 3. Marker semantics

Markers registered in `pytest.ini` `[markers]`. The `addopts` `-m` filter excludes
`live_api, skip_if_no_local, extra_examples, extra_tools, extra_vl, stress` **by default**.

| marker | meaning | default-run behaviour | how to run it explicitly |
|---|---|---|---|
| `live_api` | live network calls to external APIs | **excluded** | `pytest -m live_api` |
| `skip_if_no_local` | needs a local LLM server (LM Studio / Ollama) | **excluded** | `pytest -m "skip_if_no_local"` (+ `AGENT_CASCADE_RUN_LOCAL_TESTS=1`) |
| `extra_examples` | example integration tests (DashScope, weather APIs) | **excluded** | `pytest -m extra_examples` |
| `extra_tools` | tool tests needing external APIs (SERPER, langchain, image_gen, amap) | **excluded** | `pytest -m extra_tools` |
| `extra_vl` | vision-language model tests | **excluded** | `pytest -m extra_vl` |
| `stress` | heavy-concurrency breaker stress tests | **excluded** | `pytest -m stress` |
| `fullstack_e2e` | full-stack E2E streaming test | **INCLUDED in the default run** (a live-server streaming test that works under xdist; timing-variable, can flake — re-run single-process to confirm a failure) | to skip it: add `"and not fullstack_e2e"` to the `-m` filter; to run alone: `pytest -m fullstack_e2e -o addopts=""`. **CI:** runs in the dedicated serial `tests-e2e-fullstack` job (§1.2) and is excluded from the two parallel jobs via a command-line `-m` override (not `pytest.ini`). |
| `requires_docker` | constructs `CodeInterpreter` (Docker daemon required) | **included, but skipped when no Docker daemon** (capability guard, §4) | runs automatically on any Docker-capable runner |
| `heavy_concurrency` | spawns ≥100 raw OS threads | **included, but skipped when the OS thread limit is too low** (capability guard, §4) | runs automatically when the thread limit allows |
| `windows_only` | exercises Windows-only OS APIs (`ctypes.WINFUNCTYPE` / console Ctrl+C handler) | **included, but skipped off Windows** (capability guard, §4) | runs automatically on Windows |

> **Correction (this is a real doc bug that was fixed):** `fullstack_e2e` is **NOT**
> excluded by default. The `addopts` filter (`pytest.ini`) does not mention it, and the
> comment block above it says it is *intentionally included*. An earlier `[markers]`
> description claimed "excluded by default" — that was stale and is now corrected.

The three capability markers (`requires_docker`, `heavy_concurrency`, `windows_only`)
are **not** in the `-m` filter. They are *capability* markers: the test still runs by
default **when the resource exists**, and is only skipped when it is missing. This is
what keeps the Windows default run unchanged (minimum separation).

---

## 4. Capability skips (why they exist)

`tests/conftest.py` extends the existing `pytest_collection_modifyitems` hook with
three capability-based skip branches. The probes are **cached once per session**
(probed at collection, not per test) to keep collection fast — same idiom as the
existing local-LLM detector.

| probe | returns False when | skips marker | reason string |
|---|---|---|---|
| `docker_available()` | no Docker CLI **or** no reachable daemon, **or** `AGENT_CASCADE_DISABLE_DOCKER=1` | `requires_docker` | `Docker daemon not available (requires_docker)` |
| `thread_limit_ok(min_threads=256)` | `RLIMIT_NPROC` **or** cgroup `pids.max` (v2 `/sys/fs/cgroup/pids.max`, v1 `pids/pids.max`) < 256. Windows: neither exists → always True. | `heavy_concurrency` | `OS thread limit too low for heavy concurrency (heavy_concurrency)` |

> **Why both limits:** a sandbox can have an *infinite* `RLIMIT_NPROC` yet a low
> cgroup `pids.max` (e.g. `100`), which is what actually kills a 100-thread test with
> `RuntimeError: can't start new thread`. Checking `RLIMIT_NPROC` alone misses it. (A local
> project memory `.agent_lessons/ac-ci-sandbox-thread-limit.md` has more detail; it is
> gitignored, so the explanation above is self-contained.)
| `sys.platform != 'win32'` | not on Windows | `windows_only` | `Windows-only OS API (ctypes.WINFUNCTYPE) — runs on Windows only` |

### The `CodeInterpreter.__init__` root cause (the non-obvious bit)

**Do not "fix" a `requires_docker` failure by mocking `subprocess` in the test.** The
gate is in the **constructor**: `agent_cascade/tools/code_interpreter.py:730` calls
`_check_docker_availability()` **unconditionally in `CodeInterpreter.__init__`**. It
runs `subprocess.run(['docker', '--version'])` and raises
`RuntimeError('Docker is not installed...')` on `FileNotFoundError`. So **any** test
that constructs a `CodeInterpreter` needs a real Docker daemon — even if the test
patches `subprocess.run` for its *own* calls, the constructor's check runs first and
cannot be mocked away from the test body. That is exactly why the guard is
**capability-based at collection time** (the conftest hook), not a per-test mock.

`AGENT_CASCADE_DISABLE_DOCKER=1` is a small, documented opt-out added to
`docker_available()` so CI can force the "unavailable" branch on a Docker-capable
runner (GitHub Actions always has Docker) and thus deterministically prove the skip
path works.

---

## 5. Running locally to match CI

```bash
# 1. deps (installs the 3 pytest plugins from requirements.txt)
pip install -r requirements.txt

# 2. env — AGENT_WORKSPACE is REQUIRED on Linux
export AGENT_WORKSPACE="$PWD/.ci_workspace"
mkdir -p "$AGENT_WORKSPACE"

# 3. (constrained runner only) cap math-library threads to avoid the startup crash
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1

# 4. test — reuses pytest.ini addopts verbatim
pytest -n auto -p no:cacheprovider        # normal machine
# pytest -n 2  -p no:cacheprovider        # constrained / low-thread-limit machine

# Exercise the no-Docker skip path deliberately:
AGENT_CASCADE_DISABLE_DOCKER=1 pytest tests/test_code_interpreter_image_tag.py -v
```

---

## 6. How to add a new test

1. **Default: write a plain test, no marker.** It runs everywhere. **Do not add a
   marker by default.**
2. Add `requires_docker` **iff** the test constructs `CodeInterpreter` (or any tool
   that shells out to `docker`). The gate is the constructor, so even a test that
   mocks `subprocess` for its own calls still needs the marker.
3. Add `heavy_concurrency` **iff** the test spawns ≥ ~100 raw `threading.Thread`s.
   The suite norm is ≤ 8 workers via `ThreadPoolExecutor`. **This marker requires a
   comment stating the thread count** (it is the one a reader cannot infer).
4. Add `windows_only` **iff** the test touches `ctypes.windll` / `WINFUNCTYPE` /
   `winreg` / `msvcrt`, or asserts a Windows path literal.
5. **Prefer a capability probe over a platform check** when both would work — it keeps
   the test running on the platform where the resource *is* present.

Comment/annotation convention (state the *gate*, not the marker name):

```python
@pytest.mark.requires_docker   # CodeInterpreter.__init__ -> _check_docker_availability()
def test_...(self): ...

@pytest.mark.heavy_concurrency  # spawns 100 raw OS threads (suite norm is <=8)
def test_...(self): ...
```

---

## 7. Known environment-gated tests

The inventory below lets an agent triaging a **CI-only** failure recognise it
instantly instead of re-investigating.

### 7.1 Docker-daemon gated → `requires_docker`

Root gate: `code_interpreter.py:730` (`CodeInterpreter.__init__`). These **pass** on a
Docker-capable runner and **skip** on a no-Docker runner:

| file | marked scope |
|---|---|
| `tests/test_code_interpreter_image_tag.py` | 3 constructor tests (`TestCodeInterpreterImageName::test_docker_image_name_is_content_derived`, `TestBuildDockerImage::test_short_circuits_when_image_exists`, `TestBuildDockerImage::test_builds_when_image_missing`). The 5 `TestComputeDockerImageTag` tests are pure-helper and are **not** marked. |
| `tests/test_code_interpreter_extra_mounts.py` | class-level on `TestExtraMounts`, `TestPathMappingWrittenAfterDockerSuccess`, `TestWorkDirAttributeExists`, `TestWorkDirPriorityChain`; test-level on the 3 `TestIopubIdleTimeout` tests that drive `_execute_code` via `self._ci()`. (The pure-helper `TestWatchdogTypeSafety` / `TestTimeoutMessageFormatting` classes are **not** marked.) |
| `tests/test_zmq_cleanup.py` | 4 tests that construct `CodeInterpreter` (`test_close_shuts_down_kernel_clients`, `test_close_noop_when_no_kernels`, `test_close_clears_all_kernel_state`, `test_close_removes_temp_files`). |
| `tests/tools/test_tools.py` | `test_code_interpreter` (parametrized, 2 cases). |
| `tests/tools/test_issue_repro.py` | `test_code_interpreter_dict_input`, `test_code_interpreter_string_input`. |

### 7.2 Thread/concurrency gated → `heavy_concurrency`

| test | reason |
|---|---|
| `tests/test_slot_queue.py::TestMassCancellation::test_mass_cancel_performance` | spawns **100** raw OS threads (suite-wide max is 8 via `ThreadPoolExecutor`). Order-fragile when a worker is near its thread ceiling. `tests/test_slot_queue_deadmans_switch.py` is **not** marked — it is fully green. |

### 7.3 Windows-only API → `windows_only`

| test | cause |
|---|---|
| `tests/test_console_ctrl_guard.py::TestConsoleCtrlGuard::test_guard_install_idempotent_with_fake_kernel32` | reaches `shared_init.py:409` `ctypes.WINFUNCTYPE(...)`, which exists only on Windows |
| `tests/test_console_ctrl_guard.py::TestConsoleCtrlGuard::test_guard_callback_redispatches_sigint` | same |

> **Pending portability fix (separate follow-up):** the file's own docstring says these
> tests are meant to run on non-Windows CI, but the production code
> (`shared_init.py:409`, `ctypes.WINFUNCTYPE`) makes that impossible. The proper fix is
> to make `shared_init.py` portable (e.g. `getattr(ctypes, 'WINFUNCTYPE',
> ctypes.CFUNCTYPE)` and skip registration when absent) — that is a **production code
> change** and is deliberately out of scope for this test-infra diff. Until then the
> two tests are `windows_only`.

### 7.4 Missing optional deps / env vars

| item | handling |
|---|---|
| `gradio` (collection-time import in `tests/examples/test_examples.py`) | `collect_ignore` via `tests/examples/conftest.py` — see §8. **Not** added to `requirements.txt`. |
| `AGENT_WORKSPACE` | CI sets it (§2). 4 tests in `test_regression_logging_refinement.py` depend on it. |

---

## 8. Non-obvious gotchas

1. **A collection-time `ImportError` survives `-m` filtering.** The gradio import in
   `tests/examples/test_examples.py` happens at *collection*, before any `-m` filter is
   applied, so a marker cannot fix it — and it aborts the *entire* run. The only correct
   remedy is `collect_ignore`.
2. **`tests/examples/` is `collect_ignore`d unless gradio is installed.**
   `tests/examples/conftest.py` sets
   `collect_ignore_glob = [] if importlib.util.find_spec('gradio') else ['*.py']`.
   This also fixes a clean dev machine (which lacks gradio). To run the examples
   deliberately: `pip install gradio && pytest tests/examples -p no:cacheprovider`.
3. **`AGENT_WORKSPACE` must be set or 4 tests hard-fail on Linux.** See §2.
4. **Mocking `subprocess` does not avoid the Docker gate** — it is in
   `CodeInterpreter.__init__`. See §4.
5. **Do not wipe `addopts` in CI** (`-o addopts=""`). That silently pulls in
   `live_api` / `stress` / `extra_*` — the documented trap. Only `-n` is overridden.
   **Exception:** the dedicated serial `tests-e2e-fullstack` job (§1.2) *does* use
   `-o addopts=""`, but only because it targets a **single in-scope file**
   (`test_streaming_fullstack_e2e.py`) — there is nothing to re-include, and it must
   drop `-n auto` (in-process frame capture doesn't work under xdist). Never apply
   `-o addopts=""` to a job that runs the whole suite.
6. **Capping `-n` fixes thread *exhaustion* at startup, not thread *leak* crashes.**
   The xdist-worker crash from leaked `AgentPool` background threads is addressed by the
   autouse `_stop_real_agent_pools` fixture in `conftest.py`, not by `-n`.
