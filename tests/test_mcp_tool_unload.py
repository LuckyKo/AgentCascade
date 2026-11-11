# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Scoped tests for post-compression unload of unused MCP (deferred) tools (v0.15.6).

Design under test (usage-tracking keep-set + a single queued marker notification):
  - tracking: the tool-dispatch path records every SUCCESSFUL MCP invocation into
    ``agent_instance._mcp_tools_used`` (engine/tool_execution.py).
  - hook: ``compression/handler.py:_unload_unused_mcp_tools`` keeps = ``_mcp_tools_used`` ∪
    names in the summary, unloads the rest of discovered MCP, announces ONE
    ``[SYSTEM] MCP_TOOLS_UNLOADED`` marker queued onto the next tool result (zero new
    conversation messages), and CLEARS ``_mcp_tools_used`` (compression = window boundary).
  - offer guard: ``engine/helpers.py:order_offered_tool_names`` skips ``_unloaded_mcp_tools``
    names, with ``__all_mcp_tools__`` sentinel precedence and core tools untouched.
  - reload: ``pool/session_io._rederive_discovered_mcp_tools`` re-derives discovery strictly
    from the ``tool_search`` envelope (role=function + name=tool_search), then applies the
    unload marker via an exact-prefix scan of ANY function-result content line (LWW).

Deferred membership is MCPManager.is_mcp_tool, so fake MCP names are registered in
``MCPManager._registered_tool_names`` (save/restore) to drive the real code path."""

import time
from types import SimpleNamespace

import pytest

from agent_cascade import settings
from agent_cascade.agent_instance import AgentInstance
from agent_cascade.compression.handler import (MCP_TOOLS_UNLOADED_MARKER,
                                                CompressionHandler)
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.engine.helpers import order_offered_tool_names
from agent_cascade.engine.tool_execution import ToolExecMixin
from agent_cascade.pool.session_io import _rederive_discovered_mcp_tools
from agent_cascade.tools.mcp_manager import MCPManager
from agent_cascade.tools.search_tools.tool_search import (
    DISCOVERED_KEY,
    TOOL_SEARCH_ENVELOPE_KEY,
)


class _registered_mcp:
    """Registers fake names as MCP tools, restoring on exit (process-global class state)."""

    def __init__(self, *names):
        self.names = names

    def __enter__(self):
        MCPManager._registered_tool_names.update(self.names)
        return self

    def __exit__(self, *exc):
        for name in self.names:
            MCPManager._registered_tool_names.discard(name)
        return False


class _FakeTool:
    def __init__(self, name, description='a tool'):
        self.name = name
        self.function = {
            'name': name,
            'description': description,
            'parameters': {'type': 'object', 'properties': {}},
        }


def _func_map(names):
    return {n: _FakeTool(n) for n in names}


def _make_instance(name='inst'):
    now = time.monotonic()
    return AgentInstance(
        instance_name=name, agent_class='coder', conversation=[],
        created_at=now, last_activity=now, latest_marker_index=0,
    )


def _loaded(names):
    """One tool_search result message (load envelope) in log order."""
    import json
    envelope = json.dumps({TOOL_SEARCH_ENVELOPE_KEY: {DISCOVERED_KEY: list(names)}})
    return {'role': 'function', 'name': TOOL_SEARCH_ENVELOPE_KEY,
            'content': 'unlocked\n' + envelope}


def _marker(names, tool_name='some_tool'):
    """A drained unload marker: the queued notification landed inside an ARBITRARY
    tool result's FUNCTION content (not a tool_search message), on its own line.
    The re-derive must catch it by exact prefix, name-agnostically."""
    return {'role': 'function', 'name': tool_name,
            'content': 'tool result body\n' + f'{MCP_TOOLS_UNLOADED_MARKER}: {', '.join(names)}'}


# ── Knob / inert-when-off ──────────────────────────────────────────────────────

def test_knob_defaults_off():
    assert settings.UNLOAD_UNUSED_TOOLS_POST_COMPRESSION is False


def test_offer_inert_without_unloaded_field():
    # An instance carrying no _unloaded_mcp_tools (e.g. the 33 pre-existing offer tests) is
    # byte-identical: the guard's getattr default makes unload a no-op.
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a'], instance_name='i')
    fmap = _func_map(['core_a', 'srv-a'])
    with _registered_mcp('srv-a'):
        offered = order_offered_tool_names(fmap, set(), instance=inst)
    assert offered == ['core_a', 'srv-a']


# ── Offer-path skip guard ──────────────────────────────────────────────────────

def test_unloaded_name_suppressed_from_offer():
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a', 'srv-b'],
                           _unloaded_mcp_tools=['srv-b'], instance_name='i')
    with _registered_mcp('srv-a', 'srv-b'):
        offered = order_offered_tool_names(_func_map(['core_a', 'srv-a', 'srv-b']), set(), instance=inst)
    assert offered == ['core_a', 'srv-a']


def test_sentinel_precedence_over_unloaded():
    # Non-vacuous: srv-a is unloaded, srv-b is discovered but NOT unloaded. With no sentinel,
    # srv-b is genuinely offered (proves the offer path CAN emit srv-b and that srv-a's absence
    # there is the unload guard, not an empty offer). With the sentinel, srv-b — which is NOT
    # unloaded — is blocked too, so the sentinel path (folded into disabled before the unloaded
    # skip) is what suppresses it, proving the two mechanisms do not conflate.
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a', 'srv-b'],
                           _unloaded_mcp_tools=['srv-a'], instance_name='i')
    fmap = _func_map(['core_a', 'srv-a', 'srv-b'])
    with _registered_mcp('srv-a', 'srv-b'):
        baseline = order_offered_tool_names(fmap, set(), instance=inst)
        blocked = order_offered_tool_names(fmap, {'__all_mcp_tools__'}, instance=inst)
    assert baseline == ['core_a', 'srv-b']   # srv-a suppressed by unload guard; srv-b offered
    assert blocked == ['core_a']             # sentinel suppresses the whole tail, incl. non-unloaded srv-b


def test_unload_only_touches_mcp_core_untouched():
    inst = SimpleNamespace(_discovered_mcp_tools=['srv-a', 'srv-b'],
                           _unloaded_mcp_tools=['srv-a', 'core_x'], instance_name='i')
    # core_x is NOT an MCP tool; listing it in _unloaded_mcp_tools must not suppress it from the
    # sorted core head (the guard only ever filters the discovered-tail loop).
    with _registered_mcp('srv-a', 'srv-b'):
        offered = order_offered_tool_names(_func_map(['core_x', 'srv-a', 'srv-b']), set(), instance=inst)
    assert 'core_x' in offered


# ── Re-derive: reload survival (make-or-break seam) ────────────────────────────

def test_rederive_marker_honored_no_resurrection():
    messages = [_loaded(['srv-a', 'srv-b']), _marker(['srv-b'])]
    out_unl = []
    discovered = _rederive_discovered_mcp_tools(messages, out_unl)
    assert discovered == ['srv-a']
    assert out_unl == ['srv-b']


def test_rederive_preserves_discovery_order():
    messages = [_loaded(['srv-zeta', 'srv-alpha']), _loaded(['srv-beta', 'srv-zeta'])]
    discovered = _rederive_discovered_mcp_tools(messages)
    assert discovered == ['srv-zeta', 'srv-alpha', 'srv-beta']


def test_rederive_marker_of_never_discovered_is_noop():
    # A marker naming a tool never discovered yields neither offer nor unloaded entry — but it is
    # recorded as unloaded (present-removed) without resurrecting it into the offered set.
    messages = [_loaded(['srv-a']), _marker(['srv-nope'])]
    out_unl = []
    discovered = _rederive_discovered_mcp_tools(messages, out_unl)
    assert discovered == ['srv-a']
    assert 'srv-nope' not in discovered
    assert out_unl == ['srv-nope']  # phantom name is recorded as unloaded (present-removed) without joining the offered set


def test_rederive_strict_envelope_filter_ignores_user_decoy():
    # The DISCOVERY envelope must NOT be honored from an arbitrary USER message — only
    # role=function + name=tool_search unlocks tools. (The MARKER scan IS name-agnostic; the
    # envelope filter is deliberately strict. This test pins the ENVELOPE filter.)
    import json
    decoy = json.dumps({TOOL_SEARCH_ENVELOPE_KEY: {DISCOVERED_KEY: ['srv-a']}})
    messages = [{'role': 'user', 'name': None, 'content': 'note\n' + decoy}]
    discovered = _rederive_discovered_mcp_tools(messages)
    assert discovered == []   # decoy must be ignored


def test_rederive_marker_then_reunlock_reoffers_last_writer_wins():
    # After a marker unloads srv-b, a LATER tool_search load envelope re-offers it.
    messages = [_loaded(['srv-a', 'srv-b']), _marker(['srv-b']), _loaded(['srv-b'])]
    out_unl = []
    discovered = _rederive_discovered_mcp_tools(messages, out_unl)
    assert discovered == ['srv-a', 'srv-b']
    assert out_unl == []


def test_rederive_colocated_marker_and_reunlock_envelope_offers():
    # MAJOR 1 regression. The handler queues an unload marker onto the NEXT tool result; when that
    # tool result IS a tool_search, the drained marker (queued at compression) and that call's
    # re-unlock envelope (produced after) co-locate in ONE function message. Chronology is
    # marker-then-envelope, so the envelope must win and srv-co stays OFFERED. Pre-fix (envelope-add
    # then marker-discard per message) the name was suppressed — the unsafe over-suppression.
    import json as _json
    envelope = _json.dumps({TOOL_SEARCH_ENVELOPE_KEY: {DISCOVERED_KEY: ['srv-co']}})
    colocated = {'role': 'function', 'name': TOOL_SEARCH_ENVELOPE_KEY,
                 'content': f'result body\n{MCP_TOOLS_UNLOADED_MARKER}: srv-co\n{envelope}'}
    out_unl = []
    discovered = _rederive_discovered_mcp_tools([colocated], out_unl)
    assert discovered == ['srv-co']
    assert out_unl == []
    # Control: the SAME message minus the re-unlock envelope leaves srv-co unloaded (so the test
    # above is not vacuously "everything offered").
    marker_only = {'role': 'function', 'name': TOOL_SEARCH_ENVELOPE_KEY,
                   'content': f'result body\n{MCP_TOOLS_UNLOADED_MARKER}: srv-co'}
    out_unl2 = []
    assert _rederive_discovered_mcp_tools([marker_only], out_unl2) == []
    assert out_unl2 == ['srv-co']


def test_marker_seq_discriminator_makes_identical_reunload_unique():
    # MINOR 3 regression. After an unload and an in-memory re-unlock (as tool_search pops the name),
    # unloading the IDENTICAL set must emit a DISTINCT marker line — otherwise the exact-text dedup
    # in _inject_compression_notification drops it and the names over-offer on reload.
    with _registered_mcp('srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-b']
        h = _handler()
        # Window 1: nothing used -> srv-b unloaded -> marker #1.
        inst._mcp_tools_used = set()
        h._unload_unused_mcp_tools(inst, inst.instance_name, 'unrelated')
        # Re-unlock srv-b in memory exactly as the tool_search POP does.
        inst._unloaded_mcp_tools.remove('srv-b')
        # Window 2: nothing used -> the IDENTICAL set {srv-b} unloaded again -> must be a NEW,
        # unique line (#2), not deduped against marker #1.
        h._unload_unused_mcp_tools(inst, inst.instance_name, 'unrelated')
        lines = list(inst._pending_notifications)
        assert len(lines) == 2
        assert lines[0] != lines[1]
        assert lines[1] == f'{MCP_TOOLS_UNLOADED_MARKER} #2: srv-b'
        # Each line parses independently (discriminator stripped) and unloads srv-b.
        for ln in lines:
            msgs = [_loaded(['srv-b']),
                    {'role': 'function', 'name': 'some_tool', 'content': 'body\n' + ln}]
            _u = []
            d = _rederive_discovered_mcp_tools(msgs, _u)
            assert d == [] and _u == ['srv-b']


def test_rederive_into_real_agentinstance_seeds_both_fields():
    # Mirrors the exact session_io.py reload assignment on a real slots=True instance.
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        messages = [_loaded(['srv-a', 'srv-b']), _marker(['srv-b'])]
        _unloaded_out = []
        inst._discovered_mcp_tools = _rederive_discovered_mcp_tools(messages, _unloaded_out)
        inst._unloaded_mcp_tools = _unloaded_out
        assert inst._discovered_mcp_tools == ['srv-a']
        assert inst._unloaded_mcp_tools == ['srv-b']


# ── Compression handler keep-set seam + queued marker notification ─────────────

def _handler():
    # No pool is touched by the unload seam anymore (it queues onto instance._pending_notifications).
    return object.__new__(CompressionHandler)


def _unload(instance, summary_text, used=None):
    """Run the real handler seam. Returns the queued notification count delta; asserts the
    window boundary (used) is cleared after computing."""
    if used is not None:
        instance._mcp_tools_used = set(used)
    h = _handler()
    h._unload_unused_mcp_tools(instance, instance.instance_name, summary_text)
    return instance


def test_handler_unloads_unused_keeps_used():
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        # srv-a was used this window; srv-b was not and is not named in the summary.
        _unload(inst, 'summary mentions unrelated work', used={'srv-a'})
        assert inst._unloaded_mcp_tools == ['srv-b']
        # Window boundary: the used set is cleared after computing.
        assert inst._mcp_tools_used == set()


def test_handler_queues_one_marker_zero_new_messages():
    # Contract: exactly ONE notification is queued and ZERO conversation messages are added.
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        before = len(inst.conversation)
        _unload(inst, 'unrelated', used={'srv-a'})
        pending = list(inst._pending_notifications)
        assert len(pending) == 1
        # First unload on a fresh instance → discriminator #1 (makes re-unloads unique — MINOR 3).
        assert pending[0] == f'{MCP_TOOLS_UNLOADED_MARKER} #1: srv-b'
        # Zero new conversation messages — the marker drains into a future tool result.
        assert len(inst.conversation) == before


def test_handler_keeps_tool_named_in_summary():
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        # Neither used, but srv-b is named verbatim in the summary -> kept; srv-a unloaded.
        _unload(inst, 'kept using srv-b in the compressed window', used=set())
        assert inst._unloaded_mcp_tools == ['srv-a']


def test_handler_summary_substring_is_not_a_keep():
    # Word-boundary keep-signal: a discovered name that is only a SUBSTRING of a longer
    # summary token must NOT be kept (discriminates the \b regex from a plain-substring swap,
    # which would keep 'log' because 'catalog' contains it).
    with _registered_mcp('log', 'srv-a'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['log', 'srv-a']
        _unload(inst, 'the catalog was compressed', used=set())
        assert inst._unloaded_mcp_tools == ['log', 'srv-a']


def test_handler_clears_used_even_when_all_kept():
    # Window boundary must close even when nothing is unloaded (fresh next window).
    with _registered_mcp('srv-a'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a']
        h = _handler()
        inst._mcp_tools_used = {'srv-a'}
        h._unload_unused_mcp_tools(inst, inst.instance_name, 'no mentions')
        assert inst._unloaded_mcp_tools == []
        assert inst._mcp_tools_used == set()   # cleared regardless of unload outcome
        assert len(inst._pending_notifications) == 0   # nothing announced


def test_handler_next_window_starts_fresh():
    # After the first hook clears _mcp_tools_used, a second compression (nothing used, not
    # named in summary) unloads the remaining discovered names too.
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        h = _handler()
        inst._mcp_tools_used = {'srv-a'}
        h._unload_unused_mcp_tools(inst, inst.instance_name, 'unrelated')   # unloads srv-b, clears used
        assert inst._unloaded_mcp_tools == ['srv-b']
        h._unload_unused_mcp_tools(inst, inst.instance_name, 'still unrelated')  # used empty -> also srv-a
        assert inst._unloaded_mcp_tools == ['srv-b', 'srv-a']


def test_handler_does_not_mutate_func_map_or_discovery_history():
    # Dispatch decoupling: unload suppresses only the offered set. Discovery history stays intact
    # (separate axis), so the name still resolves via func_map and re-unlock is just a pop.
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        _unload(inst, 'no mentions', used={'srv-a'})
        assert inst._discovered_mcp_tools == ['srv-a', 'srv-b']  # history intact
        fmap = _func_map(['srv-a', 'srv-b'])
        assert 'srv-b' in fmap  # still resolvable for dispatch
        inst._unloaded_mcp_tools.remove('srv-b')
        with _registered_mcp('srv-a', 'srv-b'):
            offered = order_offered_tool_names(fmap, set(), instance=inst)
        assert 'srv-b' in offered


# ── Knob gating through the real wrapper ───────────────────────────────────────

def test_maybe_wrapper_noop_when_knob_off(monkeypatch):
    # Off => zero work: no notification queued, _mcp_tools_used untouched (the dispatcher still
    # tracks cheaply, but consumption never happens).
    monkeypatch.setattr(settings, 'UNLOAD_UNUSED_TOOLS_POST_COMPRESSION', False)
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        inst._mcp_tools_used = {'srv-a'}
        h = _handler()
        h._maybe_unload_unused_mcp_tools(inst, inst.instance_name, 'unrelated')
        assert inst._unloaded_mcp_tools == []
        assert inst._mcp_tools_used == {'srv-a'}   # untouched when knob off
        assert len(inst._pending_notifications) == 0


def test_maybe_wrapper_runs_when_knob_on(monkeypatch):
    monkeypatch.setattr(settings, 'UNLOAD_UNUSED_TOOLS_POST_COMPRESSION', True)
    with _registered_mcp('srv-a', 'srv-b'):
        inst = _make_instance()
        inst._discovered_mcp_tools = ['srv-a', 'srv-b']
        inst._mcp_tools_used = {'srv-a'}
        h = _handler()
        h._maybe_unload_unused_mcp_tools(inst, inst.instance_name, 'unrelated')
        assert inst._unloaded_mcp_tools == ['srv-b']


# ── Usage-tracking guard: drives the REAL engine/tool_execution.py dispatch ─────
# The fragile seam is engine/tool_execution.py:282-283 inside ToolExecMixin
# _execute_detected_tools:
#     if _tool_success and MCPManager.is_mcp_tool(tool_name):
#         instance._mcp_tools_used.add(tool_name)
# These tests drive that REAL method — no predicate reimplementation. A Harness
# subclasses ExecutionEngine (which composes ToolExecMixin, so
# _execute_detected_tools IS the production guard body — pinned by the __func__
# identity check below) and stubs ONLY its collaborators. The real _detect_tool,
# the real _detect_tool_result_failure heuristic, the real _format_tool_dispatch_error
# (exception path) and the guard itself all run unoverridden.

class _FakeDispatcher:
    """Stands in for ToolDispatcher and records what was dispatched. ``result`` is
    either a string the tool returns or a zero-arg callable that raises, to drive
    BOTH failure shapes the guard sees (failure-string vs dispatcher-crash)."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute_tool(self, instance, tool_name, tool_args, llm_messages, **kwargs):
        self.calls.append(tool_name)
        if callable(self.result):
            return self.result()
        return self.result


class _Harness(ExecutionEngine):
    """Bare ExecutionEngine that exists only to run one tool dispatch end-to-end.
    __init__ deliberately does NOT call super().__init__(); it wires only the
    collaborators _execute_detected_tools touches between method entry and the
    guard (and its finally/epilogue). Every overridden method is a collaborator we
    do not want to execute — none of them is the guard region."""

    def __init__(self, dispatcher_result):
        self.pool = SimpleNamespace(
            is_paused=lambda: False,
            wait_if_paused=lambda timeout=1.0: None,
            get_instance=lambda name: None,   # nothing is_terminated to propagate
            settings=SimpleNamespace(cache_threshold_chars=100000),
            telemetry=None,                   # real _telemetry() -> None -> skipped
        )
        self.compression_handler = None       # falsy -> finally skips _postprocess_tool_result
        self.tool_dispatcher = _FakeDispatcher(dispatcher_result)

    def _resolve_tool_policy(self, instance):
        return (None, set(), {})

    def _scan_tool_batch_indices(self, turn_output, inst_name):
        return ([], [], [])

    def _is_terminal_stop(self, inst_name):
        return False

    def _is_suspended_by_compression(self, inst_name):
        return False

    def _is_stopped(self, inst_name):
        return False

    def _auto_deny_reason(self, template, disabled_tools, function_map, tool_name):
        return None  # no deny -> real dispatch path proceeds to execute_tool

    def _cache_tool_output(self, instance_name, tool_name, output, threshold=1000):
        return None  # cache side effect not relevant to the guard

    def _append_and_log(self, instance, message):
        return None  # log/conv side effect not relevant to the guard

    def _run_deferred_state_restore_safety_nets(self, instance, inst_name, **kwargs):
        return None  # epilogue net, unrelated to tracking

    def _proactive_compression_check(self, *args, **kwargs):
        return None  # epilogue check, unrelated to tracking


def _drive_dispatch(instance, tool_name, dispatcher_result):
    """Run the REAL _execute_detected_tools for a single tool call on a real
    AgentInstance. Returns (used_any_tool, dispatcher_calls, response_len).

    The returned witnesses (used_any_tool True, one dispatcher call, one FUNCTION
    result appended to response) prove the dispatch actually reached the guard
    region — so a result that leaves _mcp_tools_used empty reflects the GUARD's
    decision (failure or non-MCP) and not a silently skipped dispatch."""
    harness = _Harness(dispatcher_result)
    # Pin that we drive the production guard body from engine/tool_execution.py, not a copy.
    assert harness._execute_detected_tools.__func__ is ToolExecMixin._execute_detected_tools
    # A dict message with a function_call -> the REAL _detect_tool returns (True, tool_name, ...).
    turn_output = [{'role': 'assistant', 'content': '',
                    'function_call': {'name': tool_name, 'arguments': '{}'},
                    'extra': {'function_id': 'fc-1'}}]
    response = []
    used_any = harness._execute_detected_tools(
        instance, instance.instance_name, turn_output, list(turn_output), [], response)
    return used_any, harness.tool_dispatcher.calls, len(response)


def test_real_dispatch_tracks_mcp_only_on_success():
    # Success adds the MCP name; a FAILED result on the SAME instance adds nothing.
    with _registered_mcp('srv-a'):
        inst = _make_instance()

        used, calls, resp = _drive_dispatch(inst, 'srv-a', 'all good output')
        assert used is True and calls == ['srv-a'] and resp == 1   # witness: dispatched + succeeded
        assert inst._mcp_tools_used == {'srv-a'}                   # success tracked

        # A failure-detectable string (real _detect_tool_result_failure flags it) must not track.
        used, calls, resp = _drive_dispatch(inst, 'srv-a', 'Error: simulated tool failure')
        assert used is True and calls == ['srv-a'] and resp == 1   # witness: dispatched again
        assert inst._mcp_tools_used == {'srv-a'}                   # failure added nothing


def test_real_dispatch_does_not_track_on_dispatcher_exception():
    # The guard must also skip when execute_tool RAISES: the crash is laundered into an
    # 'Error: ...' string by the real _format_tool_dispatch_error and flagged as failure,
    # so _mcp_tools_used stays empty despite a real, reached dispatch.
    with _registered_mcp('srv-a'):
        inst = _make_instance()

        def _boom():
            raise RuntimeError('simulated dispatcher crash')

        used, calls, resp = _drive_dispatch(inst, 'srv-a', _boom)
        assert used is True and calls == ['srv-a'] and resp == 1   # witness: dispatched + reached guard
        assert inst._mcp_tools_used == set()                        # crash never tracked


def test_real_dispatch_never_tracks_non_mcp_tools():
    # A non-MCP core tool that SUCCEEDS must not enter the unload universe. core_x is not
    # registered as MCP, so the real MCPManager.is_mcp_tool returns False at the guard.
    inst = _make_instance()
    used, calls, resp = _drive_dispatch(inst, 'core_x', 'core ok')
    assert used is True and calls == ['core_x'] and resp == 1      # witness: dispatched + succeeded
    assert inst._mcp_tools_used == set()                            # non-MCP never tracked


def test_real_dispatch_tracks_per_instance_independent():
    # Two real slots=True AgentInstances keep their own keep-set; a successful dispatch
    # on one never leaks into the other (each drives the real guard independently).
    with _registered_mcp('srv-a', 'srv-b'):
        parent = _make_instance(name='parent')
        child = _make_instance(name='child')
        _drive_dispatch(parent, 'srv-a', 'ok')
        _drive_dispatch(child, 'srv-b', 'ok')
        assert parent._mcp_tools_used == {'srv-a'}
        assert child._mcp_tools_used == {'srv-b'}



# ── Re-unlock POP driven by the REAL ToolSearch ────────────────────────────────

def test_reunlock_pop_driven_by_real_tool_search():
    # The re-unlock POP path in tool_search must run through the REAL ToolSearch, not a mirror.
    # A tool previously unloaded post-compression, then re-discovered by a fresh search, must be
    # popped from _unloaded_mcp_tools (or the offer guard would suppress it forever).
    import json as _json
    from agent_cascade.tools.search_tools.tool_search import ToolSearch

    class _FakeTool2:
        def __init__(self, name):
            self.name = name
            self.function = {'name': name, 'description': 'read a file from disk',
                             'parameters': {'type': 'object', 'properties': {}}}

    class _FakeTemplate:
        def __init__(self, function_map):
            self.function_map = function_map

    class _FakePool:
        def __init__(self, instance, template):
            self._instance = instance
            self._template = template

        def get_instance(self, name):
            return self._instance if name == self._instance.instance_name else None

        def get_template(self, agent_class):
            return self._template

    class _FakeAgentObj:
        def __init__(self, pool):
            self.pool = pool

    mcp_name = 'srv-pop-reunlock'
    with _registered_mcp(mcp_name):
        inst = _make_instance()
        inst._discovered_mcp_tools = []  # re-searched fresh; ranker skips already-discovered names
        inst._unloaded_mcp_tools = [mcp_name]  # suppressed by the offer guard
        # Suppressed before the search.
        assert order_offered_tool_names(_func_map([mcp_name]), set(), instance=inst) == []

        template = _FakeTemplate({mcp_name: _FakeTool2(mcp_name)})
        agent_obj = _FakeAgentObj(_FakePool(inst, template))
        result = ToolSearch().call(
            {'query': 'read file from disk', 'limit': 1},
            agent_obj=agent_obj, agent_instance_name=inst.instance_name,
        )
        # The search re-discovered it (envelope lists it) and the POP removed it from the tail.
        assert _json.loads(result.splitlines()[-1])[TOOL_SEARCH_ENVELOPE_KEY][DISCOVERED_KEY] == [mcp_name]
        assert mcp_name not in inst._unloaded_mcp_tools
        assert mcp_name in inst._discovered_mcp_tools
        # And it is offered again.
        assert order_offered_tool_names(_func_map([mcp_name]), set(), instance=inst) == [mcp_name]


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
