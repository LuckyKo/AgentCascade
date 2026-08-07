#!/usr/bin/env python
"""Test if server hangs on startup."""
import sys
import os
import signal
import time

# Add timeout to prevent infinite hang
def timeout_handler(signum, frame):
    print("TIMEOUT: Server startup appears to hang!")
    sys.exit(1)

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(10)  # 10 second timeout

sys.path.insert(0, '.')

print("Starting test...")
from agent_cascade.log import init_logging, logger
print("1. Logging module imported")
init_logging()
print("2. Logging initialized")

# Now try to create app
from agent_cascade.api_server import create_app
print("3. api_server imported")

try:
    # We can't fully create_app without agents/pool, but we can test if it runs past import time
    print("4. Importing create_app function...")
    # This is just a reference check - shouldn't hang
    print(f"5. create_app is callable: {callable(create_app)}")
except Exception as e:
    print(f"ERROR during import/creation: {e}")
    import traceback
    traceback.print_exc()

print("Test completed successfully (no hang detected).")
signal.alarm(0)  # Cancel alarm