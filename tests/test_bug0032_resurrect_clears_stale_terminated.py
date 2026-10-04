"""BUG_0032 regression: resurrecting a session from a log must clear a stale
`pool.terminated_instances` entry for that name, otherwise the restored agent is
aborted before its first turn ("instance X terminated before execution - skipping",
engine/core.py).

Root cause (see .bug_tracker/BUG_0032_stale_terminated_set_blocks_log_restore.md):
  - `is_instance_terminated(name)` is `in_set or inst_flag` (pool/slots.py). The
    restored instance's own flag is False, but the name-scoped SET survives
    `load_session_from_log` (which only swaps the instance object).
  - Sub-agent names never register a thread in `pool._instance_threads`, so the
    ONLY live-instance discard (dismiss_instance, guarded on a registered thread)
    never runs for them -> their set entry is immortal.

The fix lives in the orchestration layer: `LifecycleManager.find_or_create_instance`
clears the name under `_pool_lock` on the log_file branch. THIS TEST targets that
exact path (not just `load_session_from_log`, which would pass both pre- and
post-fix). No LLM calls required.
"""

import json
from datetime import datetime

from agent_cascade.agent_pool import AgentPool
from agent_cascade.lifecycle_manager import AgentLifecycleManager

DUMMY_LLM_CFG = {
    'model': 'qwen/qwen3-4b',
    'model_server': 'http://127.0.0.1:1234/v1',
    'api_key': 'EMPTY',
    'model_type': 'qwenvl_oai',
}


def _write_minimal_log(path, instance_name):
    """Write a minimal valid JSONL session log (metadata header + a couple of msgs)."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({
            'agent_class': 'coder',
            'instance_name': instance_name,
            'start_timestamp': datetime.now().isoformat(),
        }) + '\n')
        f.write(json.dumps({'role': 'user', 'content': 'hello'}) + '\n')
        f.write(json.dumps({'role': 'assistant', 'content': 'hi there'}) + '\n')


class TestResurrectClearsStaleTerminated:

    def test_find_or_create_clears_stale_terminated_set_entry(self, tmp_path):
        """Seed a stale set entry, resurrect via find_or_create_instance(log_file=...),
        assert the name is cleared and no longer reports terminated."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        name = f"resurrect_{datetime.now().strftime('%H%M%S%f')}"

        # The poison: a stale termination marker for this name (as left behind by a
        # prior terminate_instance() on a sub-agent with no registered thread).
        pool.terminated_instances.add(name)
        assert name in pool.terminated_instances, 'precondition: name is pre-terminated'
        assert pool.is_instance_terminated(name), \
            'precondition: predicate reports terminated (in_set half)'

        log_path = str(tmp_path / f"{name}.jsonl")
        _write_minimal_log(log_path, name)

        inst, is_reuse, session_was_loaded = manager.find_or_create_instance(
            agent_class='coder', instance_name=name, caller=None,
            nest_depth=0, force_fresh=False, log_file=log_path)

        # The fix: the stale marker must be gone after a successful resurrect.
        assert name not in pool.terminated_instances, \
            'BUG_0032: stale terminated_instances entry survived the log restore'
        assert pool.is_instance_terminated(name) is False, \
            'BUG_0032: predicate still reports the restored instance as terminated'

        # The restored instance's own flag must be clean (proves the SET was the trigger).
        assert inst.is_terminated is False, \
            'restored instance unexpectedly has its own is_terminated flag set'
        assert session_was_loaded is True, 'session should report as loaded from log'

    def test_fresh_create_without_log_does_not_touch_set(self, tmp_path):
        """Guard: the discard must only run on the log_file branch. A fresh create with
        no log_file and no pre-existing set entry must leave the set untouched."""
        pool = AgentPool(DUMMY_LLM_CFG)
        manager = AgentLifecycleManager(pool)
        name = f"fresh_{datetime.now().strftime('%H%M%S%f')}"

        assert name not in pool.terminated_instances, 'precondition: clean slate'

        inst, is_reuse, session_was_loaded = manager.find_or_create_instance(
            agent_class='coder', instance_name=name, caller=None,
            nest_depth=0, force_fresh=False, log_file=None)

        assert session_was_loaded is False
        assert name not in pool.terminated_instances, \
            'fresh create (no log_file) must not add to terminated_instances'
