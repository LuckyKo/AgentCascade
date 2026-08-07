#!/usr/bin/env python
"""Test import sequence and logging initialization."""
import sys
import os
import traceback

# Add to path
sys.path.insert(0, '.')

print('Step 1: Importing agent_cascade.api_server...')
try:
    from agent_cascade import api_server
    print('SUCCESS: api_server imported')
except Exception as e:
    print(f'FAILED: {e}')
    traceback.print_exc()

print('\nStep 2: Importing log module...')
try:
    from agent_cascade import log
    print('SUCCESS: log module imported')
except Exception as e:
    print(f'FAILED: {e}')
    traceback.print_exc()

print('\nStep 3: Initializing logging...')
try:
    log.init_logging()
    print('SUCCESS: logging initialized')
except Exception as e:
    print(f'FAILED: {e}')
    traceback.print_exc()

print('\nStep 4: Checking _validate_disabled_tools import time...')
# This function is at module level and might have dependencies
try:
    from agent_cascade.api_server import _validate_disabled_tools
    print('SUCCESS: _validate_disabled_tools can be imported')
except Exception as e:
    print(f'FAILED: {e}')
    traceback.print_exc()

print('\nAll tests completed.')