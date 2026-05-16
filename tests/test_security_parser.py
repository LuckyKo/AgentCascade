import re

def parse_security_response(display_response: str):
    """
    Improved parsing logic.
    """
    clean_text = display_response.strip()
    check_text = clean_text.upper()
    
    # regex to find [YES] or [NO], potentially bolded or with leading markers
    # Matches: [YES], **[YES]**, Verdict: [YES], etc.
    yes_pattern = r'\[YES\]'
    no_pattern = r'\[NO\]'
    
    yes_matches = list(re.finditer(yes_pattern, check_text))
    no_matches = list(re.finditer(no_pattern, check_text))
    
    is_yes = len(yes_matches) > 0
    is_no = len(no_matches) > 0
    
    verdict_idx = -1
    verdict_end_idx = -1
    
    if is_yes or is_no:
        last_yes = yes_matches[-1].start() if yes_matches else -1
        last_no = no_matches[-1].start() if no_matches else -1
        
        if last_yes > last_no:
            is_yes, is_no = True, False
            verdict_idx = last_yes
            verdict_end_idx = yes_matches[-1].end()
        else:
            is_yes, is_no = False, True
            verdict_idx = last_no
            verdict_end_idx = no_matches[-1].end()
            
    # Fallback 1: Check for "Verdict: YES" or "Verdict: NO" without brackets
    if not is_yes and not is_no:
        v_yes = re.search(r'VERDICT:\s*YES', check_text)
        v_no = re.search(r'VERDICT:\s*NO', check_text)
        if v_yes and (not v_no or v_yes.start() > v_no.start()):
            is_yes = True
            verdict_idx = v_yes.start()
            verdict_end_idx = v_yes.end()
        elif v_no:
            is_no = True
            verdict_idx = v_no.start()
            verdict_end_idx = v_no.end()

    # 3. Extract justification: everything AFTER the final verdict token
    justification = ""
    if verdict_idx != -1:
        justification = clean_text[verdict_end_idx:].strip()
        # Clean up leftover markdown bolding or prefixes
        justification = re.sub(r'^(\*\*|__)?[:\s-]*', '', justification).strip()
        # Strip leading "Reason:" or similar
        justification = re.sub(r'^(Reason|Justification|Verdict|Tips)[:\s-]*', '', justification, flags=re.IGNORECASE).strip()
    
    # Fallback 2: if still no verdict found, look for safe/unsafe keywords
    if not is_yes and not is_no:
        if "SAFE" in check_text and "UNSAFE" not in check_text:
            is_yes = True
            justification = clean_text
        elif "UNSAFE" in check_text or "DANGEROUS" in check_text or "REJECT" in check_text:
            is_no = True
            justification = clean_text
            
    return is_yes, is_no, justification

def test_parser():
    test_cases = [
        {
            "name": "Standard YES",
            "input": "[YES] Reason: The command is safe.",
            "expected": (True, False, "The command is safe.")
        },
        {
            "name": "Standard NO",
            "input": "[NO] Reason: Dangerous command.",
            "expected": (False, True, "Dangerous command.")
        },
        {
            "name": "User Log Case",
            "input": "\n\n[NO] Reason: Before approving any shell command execution, I need to inspect the actual content of temp_test_coder.py to ensure it doesn't contain malicious or destructive operations. Let me check the file first.",
            "expected": (False, True, "Before approving any shell command execution, I need to inspect the actual content of temp_test_coder.py to ensure it doesn't contain malicious or destructive operations. Let me check the file first.")
        },
        {
            "name": "Lowercase brackets",
            "input": "[yes] it is fine",
            "expected": (True, False, "it is fine")
        },
        {
            "name": "No brackets YES (Plausible failure)",
            "input": "Verdict: YES\nReason: Safe",
            "expected": (True, False, "Safe") # This will likely fail currently
        },
        {
            "name": "SAFE keyword fallback",
            "input": "This command is safe to run.",
            "expected": (True, False, "This command is safe to run.")
        },
        {
            "name": "Markdown bolding",
            "input": "**[YES]** Reason: approved",
            "expected": (True, False, "approved")
        },
        {
            "name": "No Reason prefix",
            "input": "[YES] approved immediately",
            "expected": (True, False, "approved immediately")
        },
        {
            "name": "Ambiguous investigation",
            "input": "I need to check the file `temp.py` before I can decide.",
            "expected": (False, False, "")
        },
        {
            "name": "Mixed verdicts (Last one wins)",
            "input": "[YES] wait actually [NO] reason: I saw a virus",
            "expected": (False, True, "I saw a virus")
        }
    ]
    
    print(f"{'Test Case':<30} | {'Status':<10} | {'Result'}")
    print("-" * 80)
    
    for case in test_cases:
        y, n, just = parse_security_response(case["input"])
        actual = (y, n, just)
        passed = actual == case["expected"]
        status = "PASSED" if passed else "FAILED"
        
        print(f"{case['name']:<30} | {status:<10} | {actual}")
        if not passed:
            print(f"   Expected: {case['expected']}")

if __name__ == "__main__":
    test_parser()
