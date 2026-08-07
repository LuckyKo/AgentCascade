#!/usr/bin/env python
"""Test import sequence and logger availability."""
import sys
import os

sys.path.insert(0, '.')

print('Step 1: Importing log module...')
try:
    from agent_cascade import log
    print('SUCCESS: log module imported')
    print(f'   log.logger type: {type(log.logger)}')
    print(f'   log._initialized: {log._initialized}')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()

print('\nStep 2: Using logger before init_logging()...')
try:
    from agent_cascade.log import logger
    logger.info("Test message before init")
    print('SUCCESS: Logger works before init (uses basic logger)')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()

print('\nStep 3: Importing api_server...')
try:
    from agent_cascade import api_server
    print('SUCCESS: api_server imported')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()

print('\nStep 4: Checking _validate_disabled_tools function exists...')
try:
    from agent_cascade.api_server import _validate_disabled_tools
    print(f'SUCCESS: _validate_disabled_tools is callable: {callable(_validate_disabled_tools)}')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()

print('\nStep 5: Initializing logging...')
try:
    from agent_cascade.log import init_logging
    init_logging()
    print('SUCCESS: logging initialized')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()

print('\nStep 6: Using logger after init...')
try:
    logger.info("Test message after init")
    print('SUCCESS: Logger works after init')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()

print('\nAll tests completed.')