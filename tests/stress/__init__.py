"""Randomized slot-pool liveness stress harness (plan: slot-stress-test_PLAN.md).

Test-only package. No production code is imported for side effects beyond the
real SlotPool / EndpointScheduler / ToolDispatcher / AgentPool objects the
harness drives. Importable both from pytest and standalone
(`python -m tests.stress.run_stress`).

NOTE: this package intentionally does NOT import tests.slot_test_helpers at
module level — that file does not exist in the pre-COLL-2 baseline worktree
(`d78f2347~1` == fa77772b) which phase 2 runs against.
"""
