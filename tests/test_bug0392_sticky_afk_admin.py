"""BUG_0392 T8 — unit tests for the session-continuing AFK Admin rework.

Covers the settings/constant/prompt additions that are unit-provable without a live pool or LLM:
- T1: ``AFK_ADMIN_INSTANCE_NAME`` (the pinned fixed call_agent identity — N4).
- build-time #3: ``ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS`` (re-enforces read-only on the
  ``Admin`` call_agent child, since ``restricted_shell`` is system-agent-spawn-only).
- T6: ``AFK_ADMIN_PROMPT`` formats with its pinned placeholder set (guards a stray ``{}``) and
  now carries the prior-log recall step (lossless memory layer), the diff-against-prior-dispatches
  rule, and the call_agent tool-result + provenance-header contract (N9 / matrix #10).

The daemon dispatch/trigger path (pinned identity delivery, template-missing basic-nudge
fallback, and the dropped relaunch branch) is covered end-to-end at the observable-output layer in
``tests/test_bug0359_advisor_supersede_restart.py`` (Part B).
"""

from agent_cascade import constants, settings
from agent_cascade.prompts.dna import AFK_ADMIN_PROMPT


def test_afk_admin_instance_name_default():
    """T1: the fixed child identity defaults to 'AFK_Admin' (the call_agent instance_name the
    root must pass verbatim so the session continues across AFK windows)."""
    assert settings.AFK_ADMIN_INSTANCE_NAME == 'AFK_Admin'


def test_admin_class_read_only_disabled_tools_constant():
    """build-time #3: the fleet overseer must not be able to mutate the workspace, run code,
    or shell — enforced by injecting these into the Admin child's disabled-tool list."""
    dis = constants.ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS
    assert isinstance(dis, (frozenset, set))
    for tool in (
        'write_file', 'edit_file', 'delete_file', 'copy_file',
        'shell_cmd', 'code_interpreter',
    ):
        assert tool in dis, f'{tool} must be disabled for the read-only Admin child'


def test_afk_admin_prompt_formats_with_pinned_placeholders():
    """T6(a)/(c)/N9: the prompt must format cleanly with its pinned placeholder set (no stray
    brace → no KeyError) and carry the recall step, the call_agent tool-result framing, and the
    provenance header that survives the root's own compression."""
    text = AFK_ADMIN_PROMPT.format(
        root_name='root',
        log_path='logs/x.jsonl',
        last_step='committed the patch',
        telemetry_hint='system_info help="telemetry"',
        max_findings=5,
    )
    assert 'Recall first' in text            # lossless prior-log recall + diff-against-prior (T6a/c)
    assert 'call_agent tool result' in text  # result framing recast (matrix #10)
    assert '[AFK-Admin review' in text       # provenance header (N9)


# ── 🟡-1: discriminating guard for the dropped relaunch branch (build-time #1) ──────

def test_afk_admin_dispatcher_handles_pending_text_without_relaunch(monkeypatch):
    """🟡-1 — the dropped relaunch_idle_agent branch handled the BUG_0359 'root idle + pending
    text + no runner' window. Under v2 the dispatcher drives the turn itself (enqueue ->
    _start_generation + run_agent_thread), so that window never occurs and relaunch is never
    called.

    This is NON-loop-driven on purpose. A loop-driven test is non-discriminating: the decision
    gate (afk_should_nudge) returns False whenever pool.has_messages is True, so the dispatcher
    would never run with pending text — exactly why the prior 'has_messages hard-wired False'
    guard would pass even if the dead branch were reintroduced. We instead capture the nested
    _afk_admin_nudge closure (via a threading.Thread recorder) and invoke it DIRECTLY with
    has_messages=True, no runner, and the Admin template unavailable — the exact None->basic-nudge
    path where the dead branch lived. If reintroduced, it would relaunch and return WITHOUT
    enqueuing, so BOTH asserts fail."""
    import threading as _threading
    import tempfile
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from agent_cascade import api_server
    from agent_cascade.api_server import AFK_DEFAULT_MESSAGE
    from agent_cascade.agent_instance import AgentInstance
    import agent_cascade.utils.wakeup_helpers as wh
    from fastapi.testclient import TestClient

    now = time.time()
    inst = AgentInstance(instance_name='w1', agent_class='coder', conversation=[],
                         created_at=now, last_activity=now, latest_marker_index=0)

    class _Om:
        afk_enabled = True
        afk_message = ''
        afk_mode = 'admin'
        base_dir = Path(tempfile.gettempdir())

    class _Pool:
        def __init__(self):
            self.stopped = False
            self.instances = {'w1': inst}
            self.operation_manager = _Om()
            self._enqueue_calls = []
            self._has_messages = False
            self._afk_last_nudge_ts = 0.0
            self._afk_admin_in_flight = False
            self.get_template = lambda name: (name == 'Admin')

        def get_instance(self, name):
            return inst

        def has_pending(self, name):
            return False

        def has_messages(self, name):
            return self._has_messages

        def enqueue_message(self, name, content):
            self._enqueue_calls.append((name, content))

        def set_auto_security(self, value):
            self._auto_security = bool(value)

    pool = _Pool()

    # Phase 1 — capture the dispatcher closure. Wrap api_server.threading so only the admin daemon
    # thread is recorded (and suppressed); every other thread still starts for real so the AFK loop
    # runs and reaches the spawn site.
    captured = {}

    class _Recorder:
        def __init__(self, target=None, args=(), name=None, **kw):
            if getattr(target, '__name__', '') == '_afk_admin_nudge':
                captured['fn'] = target
                self._t = None
            else:
                self._t = _threading.Thread(target=target, args=args, name=name, **kw)

        def start(self):
            if self._t is not None:
                self._t.start()

        def join(self, *a, **k):
            if self._t is not None:
                self._t.join(*a, **k)

        def is_alive(self):
            return self._t.is_alive() if self._t is not None else False

        def __getattr__(self, n):
            return getattr(self._t, n) if self._t is not None else False

    fake_threading = SimpleNamespace()
    for attr in dir(_threading):
        if not attr.startswith('__'):
            setattr(fake_threading, attr, getattr(_threading, attr))
    fake_threading.Thread = _Recorder

    monkeypatch.setattr(api_server, 'AFK_NUDGE_TICK_SECS', 0.01)
    monkeypatch.setattr(api_server, 'AFK_NUDGE_COOLDOWN', 5.0)
    monkeypatch.setattr(api_server, 'threading', fake_threading)
    app = api_server.create_app(agents=[], agent_pool=pool, config={'session_name': 'w1'})
    with TestClient(app):
        end = time.monotonic() + 1.5
        while time.monotonic() < end and 'fn' not in captured:
            time.sleep(0.01)
    assert 'fn' in captured, 'AFK admin daemon thread never spawned — capture harness broken'
    dispatcher = captured['fn']

    # Phase 2 — invoke the dispatcher DIRECTLY in the BUG_0359 window: pending text (has_messages
    # True), no runner (_is_generating False), template unavailable (trigger_text -> None -> basic
    # nudge). This is where the dead branch lived.
    pool._has_messages = True
    pool.get_template = lambda name: None
    pool._enqueue_calls = []
    relaunch = []
    monkeypatch.setattr(api_server, '_is_generating', lambda session: False)
    monkeypatch.setattr(api_server, '_start_generation', lambda session: 'gen')
    monkeypatch.setattr(wh, 'relaunch_idle_agent',
                        lambda p, n: (relaunch.append((p, n)), False)[1])
    # Swap threading to a fully-silent recorder so the dispatcher's run_agent_thread Thread is
    # created but never started — the drive itself is irrelevant to this guard; what must be shown
    # is that enqueue happened and relaunch_idle_agent was NOT called.
    class _Silent:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def join(self, *a, **k):
            pass

        def is_alive(self):
            return False

    silent_threading = SimpleNamespace()
    for attr in dir(_threading):
        if not attr.startswith('__'):
            setattr(silent_threading, attr, getattr(_threading, attr))
    silent_threading.Thread = _Silent
    monkeypatch.setattr(api_server, 'threading', silent_threading)

    dispatcher('w1', None)

    assert relaunch == [], (
        'relaunch_idle_agent must NOT be invoked under v2 — if it fired, the dead BUG_0359 '
        'branch was reintroduced and returned before enqueue')
    assert pool._enqueue_calls == [('w1', AFK_DEFAULT_MESSAGE)], (
        'pending-text/no-runner must be resolved by the enqueue-and-drive path (basic nudge when '
        f'the template is unavailable) — got {pool._enqueue_calls!r}')


# ── 🟡-2: behavioral read-only enforcement (acpt #6) ──────────────────────────────

def test_admin_child_gets_read_only_disabled_tools_on_call_agent_path(monkeypatch):
    """🟡-2 — the read-only enforcement must ACTUALLY disable the write tools on an Admin child
    driven through the call_agent path, not merely exist as a constant. Drives the real
    SystemAgentMixin._initialize_child_run seam (where the injection lives) with a real
    AgentInstance, stubbing only collaborators. propagate_settings reproduces the real production
    state: it writes an EMPTY disabled_tools override — exactly what suppresses the resolver's
    read-only baseline, which is why the explicit Admin merge is load-bearing. Asserts (a) the
    instance override carries the registered write tools and (b) the use-time resolver
    (_resolve_tool_policy) denies them. Negative control: a non-Admin class through the same seam
    does NOT gain them — proving the gate is class-conditional, not always-on."""
    import time

    from agent_cascade.agent import Agent
    from agent_cascade.agent_instance import AgentInstance
    from agent_cascade.constants import (ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS,
                                         RUNTIME_REGISTERED_TOOLS)
    from agent_cascade.engine.system_agent import SystemAgentMixin
    from agent_cascade.engine.tool_execution import ToolExecMixin
    from agent_cascade.tools.base import TOOL_REGISTRY
    from types import SimpleNamespace

    known = set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS
    write_tools = set(ADMIN_CLASS_READ_ONLY_DISABLED_TOOLS) & known
    assert write_tools, 'fixture broken: no registered write tools to assert denial on'

    class _Tpl(Agent):
        def __init__(self):
            self.name = 'Admin'
            self.agent_type = 'Admin'
            self.llm = SimpleNamespace(generate_cfg={})
            self.function_map = {}

        def _run(self, *a, **k):
            yield []

    template = _Tpl()

    class _Pool:
        def get_template(self, agent_class):
            return template

    engine_view = SimpleNamespace(pool=_Pool())  # _resolve_tool_policy reads only .pool

    class _Lifecycle:
        def build_task_message(self, args, caller):
            return 'task'

        def initialize_conversation(self, *a, **k):
            return []

        def propagate_settings(self, inst, caller, cls, call_agent_args=None):
            # Real production behavior: propagate_settings always writes a disabled_tools into the
            # override (here empty) — the suppression the Admin merge exists to defeat.
            inst._generate_cfg_override = {'disabled_tools': []}

    def _drive(agent_class):
        mgr = object.__new__(SystemAgentMixin)
        mgr.lifecycle = _Lifecycle()
        mgr._boot_system_agent = lambda *a, **k: None
        now = time.time()
        inst = AgentInstance(instance_name='AFK_Admin', agent_class=agent_class, conversation=[],
                             created_at=now, last_activity=now, latest_marker_index=0)
        mgr._initialize_child_run(
            inst, 'AFK_Admin', {}, 'root', 'sys', False, False, agent_class=agent_class)
        return inst

    admin_inst = _drive('Admin')
    eff = set(admin_inst._generate_cfg_override.get('disabled_tools') or [])
    assert write_tools <= eff, (
        f'Admin override missing write tools: {sorted(write_tools - eff)}')

    _, disabled, _ = ToolExecMixin._resolve_tool_policy(engine_view, admin_inst)
    assert write_tools <= disabled, (
        f'use-time resolver did not deny write tools for Admin: {sorted(write_tools - disabled)}')

    coder_inst = _drive('coder')
    eff2 = set(coder_inst._generate_cfg_override.get('disabled_tools') or [])
    assert not (write_tools & eff2), (
        f'non-Admin class must not inherit the Admin read-only set: {sorted(write_tools & eff2)}')
