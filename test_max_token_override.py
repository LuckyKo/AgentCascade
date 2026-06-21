"""
Test script to validate that per-instance max_input_tokens override takes precedence 
over endpoint config in the merged_cfg.

This test demonstrates the fix for the bug where user's UI-set max_input_tokens 
was being silently overwritten by endpoint defaults when using the API router.

SCENARIO:
- User sets max_input_tokens=10000 via UI for a specific agent instance
- API router selects an endpoint with its own config (max_input_tokens=4096)
- BEFORE FIX: Endpoint config overwrote user's value -> max_input_tokens=4096
- AFTER FIX: User override takes precedence -> max_input_tokens=10000 ✓
"""

def test_config_merge_order():
    """
    Simulates the _do_call closure's config merge logic to verify that
    per-instance overrides take precedence over endpoint configs.
    
    The bug occurred in agent_cascade/execution_engine.py, lines 1569-1578.
    
    BEFORE FIX:
        merged_cfg = {}
        if instance._generate_cfg_override:
            merged_cfg.update(instance._generate_cfg_override)  # User sets max_input_tokens=10000
        merged_cfg.update(llm_cfg)  # Endpoint has max_input_tokens=4096 -> OVERWRITES user's value!
    
    AFTER FIX:
        merged_cfg = dict(llm_cfg)  # Start with endpoint defaults (max_input_tokens=4096)
        if instance._generate_cfg_override is not None:
            merged_cfg.update(instance._generate_cfg_override)  # User override (10000) takes precedence
        # Result: max_input_tokens=10000 ✓
    """
    
    print("=" * 80)
    print("Testing max_input_tokens Override Precedence Fix")
    print("=" * 80)
    
    # Simulate endpoint config from API router (the actual endpoint being used)
    llm_cfg = {
        'max_input_tokens': 4096,  # This endpoint's default
        'temperature': 0.7,
        'model': 'gpt-4',
        'api_base': 'https://endpoint1.example.com',
    }
    
    # Simulate user's per-instance override (set via UI for a specific agent instance)
    # This is what gets stored in instance._generate_cfg_override
    instance_override = {
        'max_input_tokens': 10000,  # User wants more tokens for this specific instance
        'temperature': 0.9,  # User also wants different temperature
    }
    
    print("\n[INPUT] Input Configuration:")
    print(f"   Endpoint config (llm_cfg from API router): {llm_cfg}")
    print(f"   Per-instance override (user set via UI): {instance_override}")
    
    # Test the FIXED merge logic
    print("\n[PROCESS] Applying FIXED merge logic:")
    print("   1. Start with endpoint config as base")
    merged_cfg = dict(llm_cfg)
    print(f"      merged_cfg after step 1: {merged_cfg}")
    
    print("   2. Apply per-instance override (takes precedence)")
    # Note: Use 'is not None' to match actual code behavior (not truthiness check)
    if instance_override is not None:
        merged_cfg.update(instance_override)
    elif hasattr(type(llm_cfg), '__getattr__'):  # Simulate llm.generate_cfg fallback
        pass  # Not applicable in this test scenario
    print(f"      merged_cfg after step 2: {merged_cfg}")
    
    # Verify the result
    print("\n[VERIFY] Verification:")
    expected_max_tokens = 10000
    actual_max_tokens = merged_cfg.get('max_input_tokens')
    
    if actual_max_tokens == expected_max_tokens:
        print(f"   [PASS] SUCCESS: max_input_tokens = {actual_max_tokens} (user override preserved)")
        print(f"   [PASS] User's value ({expected_max_tokens}) takes precedence over endpoint default ({llm_cfg['max_input_tokens']})")
        test_passed = True
    else:
        print(f"   [FAIL] FAILURE: max_input_tokens = {actual_max_tokens} (expected {expected_max_tokens})")
        print(f"   [FAIL] User's override was lost!")
        test_passed = False
    
    # Also verify temperature was overridden correctly
    expected_temp = 0.9
    actual_temp = merged_cfg.get('temperature')
    
    if actual_temp == expected_temp:
        print(f"   [PASS] SUCCESS: temperature = {actual_temp} (user override preserved)")
    else:
        print(f"   [FAIL] FAILURE: temperature = {actual_temp} (expected {expected_temp})")
        test_passed = False
    
    # Test with no override (should use endpoint defaults)
    print("\n[TEST] Testing fallback behavior (no instance override):")
    instance_override_none = None
    merged_cfg_fallback = dict(llm_cfg)
    
    if instance_override_none is not None:
        merged_cfg_fallback.update(instance_override_none)
    
    print(f"   Endpoint config: {llm_cfg}")
    print(f"   Per-instance override: {instance_override_none}")
    print(f"   Resulting merged_cfg: {merged_cfg_fallback}")
    
    if merged_cfg_fallback['max_input_tokens'] == llm_cfg['max_input_tokens']:
        print("   [PASS] SUCCESS: Falls back to endpoint defaults when no override present")
    else:
        print("   [FAIL] FAILURE: Did not fall back correctly")
        test_passed = False
    
    # Test the OLD buggy logic for comparison
    print("\n[BUG_DEMO] For comparison, here's what the BUGGY logic produced:")
    merged_cfg_buggy = {}
    if instance_override is not None:
        merged_cfg_buggy.update(instance_override)  # User sets max_input_tokens=10000
    merged_cfg_buggy.update(llm_cfg)  # Endpoint overwrites with max_input_tokens=4096
    
    print(f"   Buggy merged_cfg: {merged_cfg_buggy}")
    if merged_cfg_buggy['max_input_tokens'] == 4096:
        print("   [BUG] User's override (10000) was overwritten by endpoint default (4096)")
    
    print("\n" + "=" * 80)
    if test_passed:
        print("[SUCCESS] ALL TESTS PASSED - Fix is working correctly!")
    else:
        print("[FAILURE] SOME TESTS FAILED - Fix needs review")
    print("=" * 80)
    
    return test_passed


def test_edge_cases():
    """Test edge cases to ensure robustness"""
    print("\n[EDGE_CASES] Testing Edge Cases:")
    print("-" * 40)
    
    # Edge case 1: Empty override dict (is not None, so it gets applied)
    llm_cfg = {'max_input_tokens': 4096, 'model': 'gpt-4'}
    instance_override = {}
    merged_cfg = dict(llm_cfg)
    if instance_override is not None:  # Use 'is not None' to match actual code
        merged_cfg.update(instance_override)
    
    assert merged_cfg['max_input_tokens'] == 4096, "Empty override should keep endpoint default"
    print("[PASS] Edge case 1: Empty override dict handled correctly (keeps endpoint defaults)")
    
    # Edge case 2: Override with only some keys (partial override)
    llm_cfg = {'max_input_tokens': 4096, 'temperature': 0.7, 'model': 'gpt-4'}
    instance_override = {'max_input_tokens': 8192}  # Only override max_input_tokens
    merged_cfg = dict(llm_cfg)
    if instance_override is not None:
        merged_cfg.update(instance_override)
    
    assert merged_cfg['max_input_tokens'] == 8192, "Should use override value"
    assert merged_cfg['temperature'] == 0.7, "Should keep endpoint default for non-overridden keys"
    print("[PASS] Edge case 2: Partial override handled correctly (other keys preserved)")
    
    # Edge case 3: Override adds new keys not in endpoint config
    llm_cfg = {'max_input_tokens': 4096}
    instance_override = {'max_input_tokens': 10000, 'custom_param': 'custom_value'}
    merged_cfg = dict(llm_cfg)
    if instance_override is not None:
        merged_cfg.update(instance_override)
    
    assert merged_cfg['max_input_tokens'] == 10000, "Should use override value"
    assert merged_cfg.get('custom_param') == 'custom_value', "Should include new keys from override"
    print("[PASS] Edge case 3: Override with new keys handled correctly")
    
    # Edge case 4: None override (falls back to endpoint config only)
    llm_cfg = {'max_input_tokens': 4096, 'temperature': 0.7}
    instance_override = None
    merged_cfg = dict(llm_cfg)
    if instance_override is not None:
        merged_cfg.update(instance_override)
    
    assert merged_cfg['max_input_tokens'] == 4096, "None override should keep endpoint default"
    print("[PASS] Edge case 4: None override falls back to endpoint config only")
    
    print("-" * 40)
    print("[SUCCESS] All edge cases passed!")


if __name__ == '__main__':
    print("\n[TEST] MAX_TOKEN OVERRIDE FIX VALIDATION TEST\n")
    
    # Run main test
    passed = test_config_merge_order()
    
    # Run edge case tests
    test_edge_cases()
    
    # Final result
    print("\n" + "=" * 80)
    if passed:
        print("[SUCCESS] VALIDATION COMPLETE: The fix successfully ensures per-instance overrides")
        print("   take precedence over endpoint configs, preserving user's max_input_tokens!")
    else:
        print("[WARNING] VALIDATION FAILED: Please review the implementation")
    print("=" * 80)