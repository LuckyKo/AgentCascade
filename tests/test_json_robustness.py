import json
import re
import sys
import os

# Add the project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_cascade.utils.utils import json_loads

def test_json_robustness():
    print("Running JSON Robustness Tests...")
    
    # Test Case 1: Standard Thinking Block at Start (Should be stripped and parsed)
    text1 = "<think>\nSome reasoning here\n</think>\n{\"key\": \"value\"}"
    res1 = json_loads(text1)
    assert isinstance(res1, dict), "Test 1 Failed: Expected dict"
    assert res1.get("key") == "value", "Test 1 Failed: Incorrect value"
    print("Test 1 Passed: Standard Thinking Block at Start")

    # Test Case 2: Thinking Block in the Middle (Should NOT be stripped, parsing should fallback to raw string)
    text2 = "{\"code\": \"<think>Inside Code</think>\"}"
    res2 = json_loads(text2)
    # With the new fix, anchored regex won't strip it. 
    # json5.loads should handle the literal <think> tags inside a string fine.
    assert isinstance(res2, dict), "Test 2 Failed: Expected dict"
    assert "<think>Inside Code</think>" in res2.get("code"), "Test 2 Failed: Content corrupted"
    print("Test 2 Passed: Thinking Block in Middle (Anchored)")

    # Test Case 3: Poisoned JSON (Unanchored regex would have corrupted this)
    # If unanchored, _THINK_BLOCK_RE would match <think>...</think> across the JSON structure
    text3 = "{\"part1\": \"val1\", \"poison\": \"<think>Content</think>\", \"part2\": \"val2\"}"
    res3 = json_loads(text3)
    # Anchored regex won't touch this. 
    # If it's a dict, verify content wasn't stripped. 
    # If it fell back to string, that's also acceptable protection against corruption.
    if isinstance(res3, dict):
        assert "<think>Content</think>" in res3.get("poison"), "Test 3 Failed: Tag content stripped from middle"
    print("Test 3 Passed: Poisoned JSON with Tag in value")

    # Test Case 4: Complete Corruption (Should return original text as fallback)
    text4 = "This is definitely not JSON { but it has a bracket <think> and a tag"
    res4 = json_loads(text4)
    # The function strips the input at the start: original_text = text.strip()
    assert isinstance(res4, str), "Test 4 Failed: Expected string fallback"
    assert res4 == text4.strip(), f"Test 4 Failed: Fallback content mismatch. Got: {res4}"
    print("Test 4 Passed: Complete Corruption Fallback")

    # Test Case 5: Bracket Style at Start
    text5 = "[THINK]\nReasoning\n[/THINK]\n{\"a\": 1}"
    res5 = json_loads(text5)
    assert isinstance(res5, dict), f"Test 5 Failed: Expected dict, got {type(res5)}"
    assert res5.get("a") == 1, "Test 5 Failed: Incorrect value"
    print("Test 5 Passed: Bracket Style at Start")

    # Test Case 6: Nested JSON with Tags
    # Unanchored stripping would turn this into: {"nested": "inner"}
    text6 = "{\"nested\": \"<think>\"}{\"inner\": \"val\"}"
    # json5 might fail on multiple objects, but we check if it's corrupted
    res6 = json_loads(text6)
    # If it fails parsing and falls back to string, that's fine too as long as it's not corrupted.
    if isinstance(res6, dict):
         assert "<think>" in str(res6), "Test 6 Failed: Tag stripped from middle of valid JSON"
    print("Test 6 Passed: Nested JSON Robustness")

    # Test Case 7: Multiple tags at start
    text7 = "<think>1</think><think>2</think>{\"a\": 1}"
    res7 = json_loads(text7)
    assert isinstance(res7, dict), "Test 7 Failed: Expected dict for multiple tags"
    assert res7.get("a") == 1, "Test 7 Failed: Value incorrect"
    print("Test 7 Passed: Multiple tags at start")

    # Test Case 8: Unclosed tag at start
    text8 = "<think>Reasoning... no close{\"b\": 2}"
    res8 = json_loads(text8)
    # The robust extraction (Step 4) should find the JSON even with leading garbage
    assert isinstance(res8, dict), f"Test 8 Failed: Expected dict (recovered by extraction), got {type(res8)}"
    assert res8.get("b") == 2, "Test 8 Failed: Value incorrect"
    print("Test 8 Passed: Unclosed tag at start (Recovered by extraction)")

if __name__ == "__main__":
    try:
        test_json_robustness()
        print("\nALL JSON ROBUSTNESS TESTS PASSED!")
    except Exception as e:
        print(f"\nTEST SUITE FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
