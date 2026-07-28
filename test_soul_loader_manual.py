#!/usr/bin/env python3
"""Manual tests of _preprocess_soul_content to verify behavior."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.soul_loader import _preprocess_soul_content, load_soul, build_system_prompt

# Test 1: trailing whitespace
content1 = "name: Tester  \ntagline: A test agent  \nidentity:\n  role: Tester  \n"
print("=== Test 1: trailing whitespace ===")
result1 = _preprocess_soul_content(content1)
print(result1)
config1 = load_soul(content1)  # This will fail because it expects a file, so let's just check preprocessing
print("Preprocessed:", repr(result1))

# Test 2: continuation lines
content2 = (
    "name: Tester\n"
    "rules:\n"
    "  - First rule\n"
    "      continued here\n"
)
print("\n=== Test 2: continuation lines ===")
result2 = _preprocess_soul_content(content2)
print(result2)

# Test 3: irregular nested indent
content3 = (
    "name: Tester\n"
    "rules:\n"
    "  - Rule one\n"
    "    - Sub one\n"
    "        - Deep one\n"
)
print("\n=== Test 3: irregular nested indent ===")
result3 = _preprocess_soul_content(content3)
print(result3)

# Test 4: colons in list items
content4 = (
    "name: Tester\n"
    "rules:\n"
    "  - Use colons: always\n"
)
print("\n=== Test 4: colons in list items ===")
result4 = _preprocess_soul_content(content4)
print(result4)

# Test 5: YAML list only
content5 = "- one\n- two\n- three\n"
print("\n=== Test 5: YAML list only ===")
result5 = _preprocess_soul_content(content5)
print(result5)

# Test 6: plain string
content6 = "just a plain string\n"
print("\n=== Test 6: plain string ===")
result6 = _preprocess_soul_content(content6)
print(result6)