import re

def parse_security_response(clean_text: str):
    # 2. Simplified Verdict Extraction: Check ONLY the last non-empty line
    lines = [l.strip() for l in clean_text.split('\n') if l.strip()]
    last_line = lines[-1] if lines else ""
    
    # Remove markdown bolding if present (e.g. **[YES]**)
    last_line_clean = re.sub(r'(\*\*|__)', '', last_line).strip()
    last_line_upper = last_line_clean.upper()
    
    is_yes = last_line_upper.startswith('[YES]')
    is_no = last_line_upper.startswith('[NO]')
    
    justification = ""
    if is_yes:
        justification = last_line_clean[5:].strip()
    elif is_no:
        justification = last_line_clean[4:].strip()
        
    if is_yes or is_no:
        # Strip "Reason:", "Justification:", etc.
        justification = re.sub(r'^(Reason|Justification|Verdict|Tips)[:\s-]*', '', justification, flags=re.IGNORECASE).strip()
    
    # Fallback: if no [YES]/[NO] on last line, check if the entire response is JUST the verdict
    if not is_yes and not is_no and len(lines) == 1:
        if last_line_upper == 'YES' or last_line_upper == 'SAFE':
            is_yes = True
            justification = last_line
        elif last_line_upper == 'NO' or last_line_upper == 'UNSAFE':
            is_no = True
            justification = last_line
            
    return is_yes, is_no, justification

test_cases = [
    "\n\n[YES] Reason: All good",
    "I checked it.\n[YES] Reason: Safe",
    "**[YES]** Reason: bolded",
    "YES",
    "[NO] Reason: bad",
    "Thinking...\n[YES] it is fine"
]

for tc in test_cases:
    y, n, just = parse_security_response(tc)
    print(f"Input: {repr(tc)}")
    print(f"  YES: {y}, NO: {n}, Just: {just}")
