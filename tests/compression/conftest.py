"""Shared fixtures for the compression test suite.

BUG_0309 added a post-L2-compaction reviewer gate inside ``_consolidate_markers``
that is ON by default. The legacy consolidation suites
(``test_memory_consolidation.py``, ``test_l2_multiday_timestamps.py``,
``test_last_task_in_marker.py``) call ``_consolidate_markers`` directly with a ``MagicMock``
pool and were written before the gate existed. Left at the default they would boot the real
``invoke_compaction_reviewer`` plumbing, fail open via an empty-summary error, and emit noise —
and if a mock pool ever became permissive they would hit the live endpoint.

The autouse fixture below pins the gate OFF for the whole directory so those suites stay
byte-identical to pre-BUG_0309 behavior and never touch the reviewer path. The gate's own tests
(``test_compaction_reviewer_gate.py``) opt back in explicitly per test via
``patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True)``; a nested patch
always wins over this fixture, so the gate tests are unaffected.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _default_disable_compaction_reviewer():
    """Keep the BUG_0309 compaction-reviewer gate OFF for all compression tests by default.

    The gate is imported into ``agent_cascade.compression.core`` at module load, so patching the
    core module attribute is the seam ``_validate_and_reconsolidate`` reads.
    """
    with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', False):
        yield
