"""Declarative workload model for the slot-pool stress harness (plan §2).

An `AgentSpec` describes one agent instance's scheduled action script. The
harness (`harness.py`) interprets the script against the REAL SlotPool /
ToolDispatcher; nothing here touches production objects, so it stays readable
and diffable against the plan's scenario table.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Action kinds ────────────────────────────────────────────────────────
LLM = 'llm'              # non-tool turn: chat completion, holds the slot
SPAWN = 'spawn'          # call_agent (child_depth derived from position)
SPAWN_ASYNC = 'spawn_async'
SPAWN_BURST = 'spawn_burst'
DISMISS = 'dismiss'
DISMISS_REPEAT = 'dismiss_repeat'   # same name again (the permit-leak bug)
DISMISS_GRANDPARENT = 'dismiss_grandparent'
TERMINATE = 'terminate'
STICKY_SYNC = 'sticky_sync'
SLEEP = 'sleep'          # relinquish slot, then reacquire (SLEEPING path)
REUSE_DISMISSED = 'reuse_dismissed'  # run as an agent whose name was dismissed
REENTRANT_SYNC = 'reentrant_sync'    # sync spawn from a sync child
TOOL = 'tool'            # generic non-special tool call


@dataclass
class Action:
    kind: str
    target: str = ''          # agent name
    sleep_ms: int = 0         # think-time while holding the slot
    tool_name: str = ''       # for TOOL
    args: Dict[str, Any] = field(default_factory=dict)
    label: str = ''


@dataclass
class AgentSpec:
    name: str
    agent_class: str
    script: List[Action]
    parent: Optional[str] = None
    is_child: bool = False
    children: List[str] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    agents: List[AgentSpec]
    seed: int
    expected_class: str = 'NONE'   # NONE | DEADLOCK | STARVATION | TIMEOUT | ZOMBIE
    notes: str = ''
    is_child: bool = False         # not a root agent; spawned by a parent
    budget_s: float = 60.0
    no_progress_s: float = 10.0

    def agent(self, name: str) -> AgentSpec:
        for a in self.agents:
            if a.name == name:
                return a
        raise KeyError(f'{name} not in scenario {self.name}')

    def roots(self) -> List[AgentSpec]:
        return [a for a in self.agents if a.parent is None]

    def by_name(self) -> Dict[str, AgentSpec]:
        return {a.name: a for a in self.agents}

    def repro_cmd(self, phase: str = 'post', root: str = '.') -> str:
        return (f'python -m tests.stress.run_stress --root {root} --phases {phase} '
                f'--only {self.name} --seeds {self.seed}')


# ── Script builders (mirror the plan's scenario table) ──────────────────

def _rand_sleep(rng: random.Random, lo: int = 1, hi: int = 25) -> int:
    return rng.randint(lo, hi)


def build_deep_chain(rng: random.Random,
                     root_cls: str,
                     leaf_cls: str,
                     depth: int,
                     chain: str = 'stress_a',
                     roots: int = 2) -> Scenario:
    """(b) deep_conc0 — linear grandparent chains A->B->C->D on conc=0 endpoints.

    `roots` independent chains run concurrently on the SAME sequential shared
    pool, so a caller's permit must be released (COLL-2) before its child
    acquires, and re-acquired after — while a rival chain is already queued.
    """
    agents: List[AgentSpec] = []
    for c in range(roots):
        levels = [f'{chain}{c}_c{i}' for i in range(depth)]
        for i, name in enumerate(levels):
            cls = root_cls if i == 0 else leaf_cls
            script: List[Action] = [Action(LLM, sleep_ms=_rand_sleep(rng))]
            if i + 1 < depth:
                script.append(Action(SPAWN, target=levels[i + 1], sleep_ms=_rand_sleep(rng, 1, 10)))
                script.append(Action(LLM, sleep_ms=_rand_sleep(rng)))
            else:
                script.append(Action(LLM, sleep_ms=_rand_sleep(rng, 1, 10)))
            agents.append(AgentSpec(name=name, agent_class=cls, script=script,
                                    parent=levels[i - 1] if i else None))
    return Scenario(name='deep_conc0', agents=agents, seed=rng.randint(0, 2**31 - 1),
                    notes=f'{roots} concurrent linear chains, depth={depth}')


def build_soak(rng: random.Random, n_agents: int, endpoint_id: str = 'stress_a') -> Scenario:
    """(a) soak — mixed actions, all sharing one conc=0 endpoint pool.

    Roots (`soak_r*`) and spawnable children (`soak_c*`) use DISJOINT names.
    Sharing a name between a live root thread and a synchronous spawn would make
    the harness cancel its own ticket — a workload bug, not a product finding.
    """
    n_children = max(2, n_agents)
    agents: List[AgentSpec] = []
    script_kinds = [LLM, SPAWN, TOOL, STICKY_SYNC, SLEEP]
    for i in range(n_agents):
        name = f'soak_r{i}'
        script: List[Action] = []
        spawns = 0
        for _ in range(rng.randint(2, 5)):
            kind = rng.choice(script_kinds)
            if kind == SPAWN and spawns < 2:
                script.append(Action(SPAWN, target=f'soak_c{rng.randrange(n_children)}',
                                     sleep_ms=_rand_sleep(rng, 1, 8)))
                spawns += 1
            elif kind == TOOL:
                script.append(Action(TOOL, tool_name='read_file', sleep_ms=_rand_sleep(rng, 1, 8)))
            elif kind == STICKY_SYNC:
                script.append(Action(STICKY_SYNC, sleep_ms=_rand_sleep(rng, 1, 8)))
            elif kind == SLEEP:
                script.append(Action(SLEEP, sleep_ms=_rand_sleep(rng, 1, 8)))
            else:
                script.append(Action(LLM, sleep_ms=_rand_sleep(rng, 1, 20)))
        if not script:
            script = [Action(LLM, sleep_ms=_rand_sleep(rng))]
        agents.append(AgentSpec(name=name, agent_class=endpoint_id, script=script))
    for c in range(n_children):
        agents.append(AgentSpec(
            name=f'soak_c{c}', agent_class='stress_b',
            script=[Action(LLM, sleep_ms=_rand_sleep(rng, 1, 15)),
                    Action(TOOL, tool_name='read_file', sleep_ms=_rand_sleep(rng, 1, 8))],
            is_child=True))
    return Scenario(name='soak', agents=agents, seed=rng.randint(0, 2**31 - 1),
                    notes=f'{n_agents} roots + {n_children} children on one conc=0 endpoint')


def build_dismiss_storm(rng: random.Random, n_pairs: int) -> Scenario:
    """(c) dismiss_storm — spawn then dismiss; repeat-name reuse.

    Expected to surface the KNOWN-OPEN bug: dismiss/terminate does not release
    the slot permit, so a reused name finds the pool leaked.
    """
    agents: List[AgentSpec] = []
    for i in range(n_pairs):
        parent = f'p{i}'
        child = f'c{i}'
        child_script = [Action(LLM, sleep_ms=_rand_sleep(rng, 5, 20))]
        parent_script = [
            Action(SPAWN, target=child, sleep_ms=_rand_sleep(rng, 1, 8)),
            Action(DISMISS_REPEAT, target=child, sleep_ms=_rand_sleep(rng, 1, 8)),
            Action(LLM, sleep_ms=_rand_sleep(rng, 1, 15)),
        ]
        if i % 3 == 0:
            parent_script.append(Action(SPAWN, target=child, sleep_ms=_rand_sleep(rng, 1, 8)))
            parent_script.append(Action(REUSE_DISMISSED, target=child, sleep_ms=_rand_sleep(rng, 1, 8)))
        agents.append(AgentSpec(name=parent, agent_class='stress_a', script=parent_script))
        agents.append(AgentSpec(name=child, agent_class='stress_b', script=child_script, parent=parent))
    return Scenario(name='dismiss_storm', agents=agents, seed=rng.randint(0, 2**31 - 1),
                    notes=f'{n_pairs} spawn/dismiss pairs; expected ZOMBIE (known bug)')


def build_async_fanout(rng: random.Random, n_children: int) -> Scenario:
    """(d) async_fanout — parent fires N async children concurrently.

    Exercises the register_async_call path, which imports run_child_core
    LOCALLY (pool/slots.py:88) — so the harness must wrap that too.
    """
    parent = 'fan_parent'
    script: List[Action] = []
    for i in range(n_children):
        script.append(Action(SPAWN_ASYNC, target=f'fan_{i}', sleep_ms=_rand_sleep(rng, 1, 8)))
    script.append(Action(LLM, sleep_ms=_rand_sleep(rng, 20, 60)))
    script.append(Action(TOOL, tool_name='read_file', sleep_ms=_rand_sleep(rng, 1, 10)))
    agents = [AgentSpec(name=parent, agent_class='stress_a', script=script)]
    for i in range(n_children):
        agents.append(AgentSpec(
            name=f'fan_{i}', agent_class='stress_b',
            script=[Action(LLM, sleep_ms=_rand_sleep(rng, 5, 30)),
                    Action(TOOL, tool_name='read_file', sleep_ms=_rand_sleep(rng, 1, 10))],
            parent=parent, is_child=True))
    return Scenario(name='async_fanout', agents=agents, seed=rng.randint(0, 2**31 - 1),
                    notes=f'{n_children} async children')


def build_sticky_churn(rng: random.Random, n_agents: int) -> Scenario:
    """(f) sticky_churn — many agents force sticky-slot sync onto one pool."""
    agents: List[AgentSpec] = []
    for i in range(n_agents):
        name = f'sticky_{i}'
        script = [Action(STICKY_SYNC, sleep_ms=_rand_sleep(rng, 1, 10))]
        if i + 1 < n_agents:
            script.append(Action(SPAWN, target=f'sticky_{i + 1}', sleep_ms=_rand_sleep(rng, 1, 6)))
        script.append(Action(LLM, sleep_ms=_rand_sleep(rng, 1, 15)))
        agents.append(AgentSpec(name=name, agent_class='stress_a', script=script))
    return Scenario(name='sticky_churn', agents=agents, seed=rng.randint(0, 2**31 - 1),
                    notes=f'{n_agents} agents forcing sticky sync')


def build_grandparent_dismiss(rng: random.Random, depth: int = 3) -> Scenario:
    """(e) grandparent_dismiss — root dismisses a mid-chain node.

    COLL-2 held a grandparent permit when the middle link was dismissed.
    """
    levels = [f'gp_c{i}' for i in range(depth)]
    root_script = [
        Action(SPAWN, target=levels[0], sleep_ms=_rand_sleep(rng, 1, 8)),
        Action(DISMISS_GRANDPARENT, target=levels[1] if depth > 1 else levels[0],
               sleep_ms=_rand_sleep(rng, 1, 8)),
        Action(LLM, sleep_ms=_rand_sleep(rng, 10, 30)),
    ]
    agents = [AgentSpec(name='gp_root', agent_class='stress_a', script=root_script)]
    for i, name in enumerate(levels):
        nxt = levels[i + 1] if i + 1 < depth else ''
        script = [Action(LLM, sleep_ms=_rand_sleep(rng, 1, 8))]
        if nxt:
            script.append(Action(SPAWN, target=nxt, sleep_ms=_rand_sleep(rng, 1, 8)))
        script.append(Action(LLM, sleep_ms=_rand_sleep(rng, 1, 10)))
        agents.append(AgentSpec(name=name, agent_class='stress_b', script=script,
                                parent=levels[i - 1] if i else 'gp_root'))
    return Scenario(name='grandparent_dismiss', agents=agents,
                    seed=rng.randint(0, 2**31 - 1), notes=f'chain depth={depth}')
