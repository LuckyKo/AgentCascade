"""Profile the e2e streaming test with py-spy.

Starts the test, waits for active streaming (15s warmup), then profiles
the process for 45s and saves a flame graph SVG.
"""
import os
import subprocess
import sys
import time

os.environ["AGENT_CASCADE_STREAM_TIMING"] = "1"

TEST_CMD = [
    sys.executable, "-m", "pytest",
    "tests/test_streaming_fullstack_e2e.py",
    "-s", "-o", "addopts=", "--timeout=540", "-p", "no:xdist",
]

LOG_PATH = "logs/e2e_latency_profile.log"
PROFILE_SVG = "profile_streaming_e2e.svg"
WARMUP_SEC = 15   # wait for server to start + first turns to stream
PROFILE_SEC = 45  # profile duration (covers ~8-10 turns of streaming)

print(f"[profile] Starting e2e test...")
log_file = open(LOG_PATH, "w")
proc = subprocess.Popen(TEST_CMD, stdout=log_file, stderr=subprocess.STDOUT, cwd=os.path.dirname(os.path.abspath(__file__)) + "/..")

print(f"[profile] Test PID: {proc.pid}")
print(f"[profile] Waiting {WARMUP_SEC}s for streaming to start...")
time.sleep(WARMUP_SEC)

if proc.poll() is not None:
    print(f"[profile] ERROR: test exited early with code {proc.returncode}")
    log_file.close()
    sys.exit(1)

print(f"[profile] Profiling PID {proc.pid} for {PROFILE_SEC}s...")
py_result = subprocess.run(
    ["py-spy", "record", "-d", str(PROFILE_SEC), "-o", PROFILE_SVG, "--pid", str(proc.pid)],
    capture_output=True, text=True,
)
if py_result.returncode != 0:
    print(f"[profile] py-spy error: {py_result.stderr[-300:]}")
else:
    print(f"[profile] Flame graph saved to: {PROFILE_SVG}")

# Wait for test to finish
proc.wait()
log_file.close()
print(f"[profile] Test exited with code: {proc.returncode}")
print(f"[profile] Log: {LOG_PATH}")
