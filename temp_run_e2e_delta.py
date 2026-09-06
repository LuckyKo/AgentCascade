"""Bulletproof delta-mode e2e runner: set the env var IN-PROCESS, confirm both flags,
then exec pytest programmatically so the flag is guaranteed present in the test process.
Usage:  python temp_run_e2e_delta.py
"""
import os, sys

os.environ["AGENT_CASCADE_STREAM_DELTA"] = "1"

# Confirm the backend sees it before pytest imports anything.
from agent_cascade.api_integration_pkg import state_builder as sb
print(f"[runner] state_builder.STREAM_DELTA_ENABLED={sb.STREAM_DELTA_ENABLED} TAIL_COMMITTED={sb.TAIL_COMMITTED}")
print(f"[runner] os.environ AGENT_CASCADE_STREAM_DELTA={os.environ.get('AGENT_CASCADE_STREAM_DELTA')}")

import pytest
rc = pytest.main([
    "tests/test_streaming_fullstack_e2e.py", "-s", "--timeout=540",
    "-o", "addopts=",
])
sys.exit(rc)
