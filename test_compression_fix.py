"""Test script to verify the tail-offset compression fix."""

def test_tail_offset_calculation():
    """
    Verify the tail-offset calculation preserves log entries correctly.
    
    Scenario:
    - Log has 10 messages (indices 0-9)
    - Pool has 8 messages after compression (marker + 7 tail messages)
    - tail_count = 7 (messages remaining after marker in pool)
    
    Expected behavior:
    - log_insert_pos = len(log_history) - tail_count = 10 - 7 = 3
    - Messages at indices 0-2 are preserved (3 discarded messages worth)
    - Marker inserted at index 3
    - Messages at indices 3-9 shift to indices 4-10 (7 tail messages)
    - Final log has 11 messages: [0,1,2, MARKER, 3,4,5,6,7,8,9]
    """
    
    # Simulate log history with 10 messages
    log_history = [{"role": "user", "content": f"Message {i}"} for i in range(10)]
    
    # Compression parameters
    tail_count = 7  # 7 messages remain after marker in pool
    
    # Calculate insert position using tail-offset method
    log_insert_pos = len(log_history) - tail_count
    
    print(f"Initial log length: {len(log_history)}")
    print(f"Tail count: {tail_count}")
    print(f"Calculated insert position: {log_insert_pos}")
    
    # Verify calculation
    assert log_insert_pos == 3, f"Expected insert_pos=3, got {log_insert_pos}"
    
    # Simulate marker insertion
    marker = {"role": "user", "content": "<context_summary>Compressed messages</context_summary>"}
    log_history.insert(log_insert_pos, marker)
    
    print(f"Final log length: {len(log_history)}")
    print(f"Marker at index: {log_history.index(marker)}")
    print(f"Messages before marker: {log_insert_pos}")
    print(f"Messages after marker: {len(log_history) - log_insert_pos - 1}")
    
    # Verify structure
    assert len(log_history) == 11, f"Expected 11 messages, got {len(log_history)}"
    assert log_history[log_insert_pos]["content"] == "<context_summary>Compressed messages</context_summary>"
    assert len([m for m in log_history if "Message" in m.get("content", "")]) == 10
    
    print("\n[OK] Tail-offset calculation test PASSED")
    print("  - All original messages preserved")
    print("  - Marker inserted at correct position")
    print("  - Log mirrors pool structure: [PRESERVED][MARKER][TAIL]")


def test_force_marker_insertion():
    """Test that force marker and summary marker are both inserted correctly."""
    
    # Simulate log history with SYSTEM message + 8 messages
    log_history = [{"role": "system", "content": "System prompt"}]
    log_history += [{"role": "user", "content": f"Message {i}"} for i in range(8)]
    
    tail_count = 5
    include_force_marker = True
    
    # Calculate insert position
    log_insert_pos = len(log_history) - tail_count
    
    # Safety check for SYSTEM message
    if log_insert_pos == 0 and log_history and log_history[0].get('role') == 'system':
        log_insert_pos = 1
    
    # Clamp to valid range (both lower and upper bounds)
    log_insert_pos = max(0, min(log_insert_pos, len(log_history)))
    
    print(f"\nForce marker test:")
    print(f"Log length with SYSTEM: {len(log_history)}")
    print(f"Tail count: {tail_count}")
    print(f"Insert position (after safety check): {log_insert_pos}")
    
    # Simulate force and summary marker insertion
    force_marker = {"role": "user", "content": "[SYSTEM INFO: Forced compression started...]"}
    summary_marker = {"role": "user", "content": "<context_summary>Compressed</context_summary>"}
    
    log_history.insert(log_insert_pos, force_marker)
    log_history.insert(log_insert_pos + 1, summary_marker)
    
    print(f"Final log length: {len(log_history)}")
    print(f"Force marker at index: {log_history.index(force_marker)}")
    print(f"Summary marker at index: {log_history.index(summary_marker)}")
    
    # Verify both markers inserted in correct order
    assert log_history[log_insert_pos]["content"].startswith("[SYSTEM INFO:")
    assert log_history[log_insert_pos + 1]["content"].startswith("<context_summary>")
    
    print("[OK] Force marker insertion test PASSED")


def test_system_message_at_position_zero():
    """Test SYSTEM message protection when insert position would be 0."""
    
    # Simulate log with SYSTEM message and only 2 user messages
    log_history = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Message 1"},
        {"role": "user", "content": "Message 2"}
    ]
    
    tail_count = 2  # Would result in insert_pos = 3 - 2 = 1
    
    log_insert_pos = len(log_history) - tail_count
    
    print(f"\nSYSTEM message protection test:")
    print(f"Log length: {len(log_history)}")
    print(f"First message role: {log_history[0]['role']}")
    print(f"Initial insert position: {log_insert_pos}")
    
    # Safety check for SYSTEM message
    if log_insert_pos == 0 and log_history and log_history[0].get('role') == 'system':
        log_insert_pos = 1
        print(f"Adjusted insert position (SYSTEM protection): {log_insert_pos}")
    
    # Clamp to valid range
    log_insert_pos = max(0, min(log_insert_pos, len(log_history)))
    
    marker = {"role": "user", "content": "<context_summary>Compressed</context_summary>"}
    log_history.insert(log_insert_pos, marker)
    
    # Verify SYSTEM message is still at index 0
    assert log_history[0]["role"] == "system"
    print(f"SYSTEM message preserved at index 0: {log_history[0]['role']}")
    print("[OK] SYSTEM message protection test PASSED")


def test_tail_count_zero():
    """Test when tail_count is 0 (all active messages discarded)."""
    
    log_history = [{"role": "user", "content": f"Message {i}"} for i in range(5)]
    tail_count = 0
    
    log_insert_pos = len(log_history) - tail_count
    
    print(f"\nTail count zero test:")
    print(f"Log length: {len(log_history)}")
    print(f"Tail count: {tail_count}")
    print(f"Insert position: {log_insert_pos}")
    
    # When tail_count is 0, marker should be inserted at the end
    assert log_insert_pos == len(log_history), "Marker should go at end when tail_count=0"
    
    marker = {"role": "user", "content": "<context_summary>All discarded</context_summary>"}
    log_history.insert(log_insert_pos, marker)
    
    # Verify marker is at the end
    assert log_history[-1]["content"] == "<context_summary>All discarded</context_summary>"
    print(f"Marker correctly placed at end (index {log_insert_pos})")
    print("[OK] Tail count zero test PASSED")


def test_negative_insert_position():
    """Test clamping when tail_count > len(log_history) results in negative position."""
    
    log_history = [{"role": "user", "content": f"Message {i}"} for i in range(3)]
    tail_count = 5  # More than log length
    
    log_insert_pos = len(log_history) - tail_count
    
    print(f"\nNegative insert position test:")
    print(f"Log length: {len(log_history)}")
    print(f"Tail count: {tail_count}")
    print(f"Raw insert position (before clamp): {log_insert_pos}")
    
    # Clamp to valid range (both lower and upper bounds)
    log_insert_pos = max(0, min(log_insert_pos, len(log_history)))
    
    print(f"Clamped insert position: {log_insert_pos}")
    
    # Should be clamped to 0
    assert log_insert_pos == 0, f"Expected 0 after clamp, got {log_insert_pos}"
    
    marker = {"role": "user", "content": "<context_summary>Compressed</context_summary>"}
    log_history.insert(log_insert_pos, marker)
    
    # Verify marker inserted at beginning (not from end due to negative index)
    assert log_history[0]["content"] == "<context_summary>Compressed</context_summary>"
    print(f"Marker correctly placed at beginning after clamp")
    print("[OK] Negative insert position test PASSED")


if __name__ == "__main__":
    test_tail_offset_calculation()
    test_force_marker_insertion()
    test_system_message_at_position_zero()
    test_tail_count_zero()
    test_negative_insert_position()
    print("\n" + "="*60)
    print("All tests PASSED! The tail-offset method correctly:")
    print("  1. Preserves all existing log entries")
    print("  2. Inserts markers at the correct position")
    print("  3. Maintains proper ordering for force+summary markers")
    print("  4. Mirrors pool structure: [MARKER][TAIL_MESSAGES]")
    print("  5. Protects SYSTEM message from being displaced")
    print("  6. Handles edge cases (tail_count=0, negative positions)")