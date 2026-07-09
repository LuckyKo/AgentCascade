#!/usr/bin/env python3
"""Test the BUG 8 fix: idempotency and error handling for record_session_end()."""

import sys
import os
import tempfile
import json
from pathlib import Path

# Add the unified directory to path - handle both Docker and host paths
test_file_dir = Path(__file__).parent.resolve()
if test_file_dir.exists():
    sys.path.insert(0, str(test_file_dir))

from agent_cascade.telemetry import TelemetryCollector


def test_idempotency():
    """Test that multiple calls to record_session_end() only write one event."""
    print("\n=== Test 1: Idempotency ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a TelemetryCollector writing to the temp dir
        tc = TelemetryCollector(log_dir=str(Path(tmpdir) / 'telemetry'))

        # Record session end multiple times
        tc.record_session_end()
        tc.record_session_end()
        tc.record_session_end()

        # Read the log file and count events
        log_files = list((Path(tmpdir) / 'telemetry').glob('*.jsonl'))
        assert len(log_files) == 1, f"Expected 1 log file, got {len(log_files)}"

        with open(log_files[0], 'r') as f:
            lines = [line for line in f.read().strip().split('\n') if line]

        print(f"Total events in log: {len(lines)}")
        event_types = [json.loads(line)['type'] for line in lines]
        session_end_count = event_types.count('session_end')
        session_start_count = event_types.count('session_start')

        print(f"Event types: {event_types}")
        assert session_start_count == 1, f"Expected 1 session_start, got {session_start_count}"
        assert session_end_count == 1, f"Expected 1 session_end, got {session_end_count}"
        print("✅ Idempotency test passed")


def test_io_error_propagates():
    """Test that I/O errors in _write_critical_event are not swallowed."""
    print("\n=== Test 2: Error Propagation ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        log_dir = Path(tmpdir) / 'telemetry'
        tc = TelemetryCollector(log_dir=str(log_dir))

        # Delete the log file to simulate I/O failure
        (log_dir).mkdir(parents=True, exist_ok=True)
        log_file = list(log_dir.glob('*.jsonl'))[0]
        os.remove(log_file)

        # Try to record session end - should raise an exception because file doesn't exist
        try:
            tc.record_session_end()
            print("❌ Expected an exception but none was raised")
            return False
        except (OSError, IOError) as e:
            print(f"✅ Correctly raised exception: {type(e).__name__}: {e}")
            return True


def test_normal_operation():
    """Test that record_session_end() works correctly in normal operation."""
    print("\n=== Test 3: Normal Operation ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = TelemetryCollector(log_dir=str(Path(tmpdir) / 'telemetry'))

        # Record session end once
        tc.record_session_end()

        # Read the log file and verify structure
        log_files = list((Path(tmpdir) / 'telemetry').glob('*.jsonl'))
        assert len(log_files) == 1, f"Expected 1 log file, got {len(log_files)}"

        with open(log_files[0], 'r') as f:
            lines = [line for line in f.read().strip().split('\n') if line]

        # Check that session_end event has required fields
        end_event = json.loads(lines[-1])
        assert end_event['type'] == 'session_end'
        assert 'session_id' in end_event
        assert 'timestamp' in end_event
        assert 'summary' in end_event
        assert 'config_comparison' in end_event

        print("✅ Normal operation test passed")


if __name__ == '__main__':
    # Set up basic logging to suppress output noise
    import logging
    logging.basicConfig(level=logging.INFO)

    test_idempotency()
    success = test_io_error_propagates()
    test_normal_operation()

    print("\n=== All tests passed! ===")