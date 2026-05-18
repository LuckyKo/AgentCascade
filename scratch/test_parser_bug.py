import re

def parse_security_response(display_response: str):
    clean_text = display_response.strip()
    check_text = clean_text.upper()
    
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

    justification = ""
    if verdict_idx != -1:
        justification = clean_text[verdict_end_idx:].strip()
        justification = re.sub(r'^(\*\*|__)?[:\s-]*', '', justification).strip()
        justification = re.sub(r'^(Reason|Justification|Verdict|Tips)[:\s-]*', '', justification, flags=re.IGNORECASE).strip()
    
    if not is_yes and not is_no:
        if "SAFE" in check_text and "UNSAFE" not in check_text:
            is_yes = True
            justification = clean_text
        elif "UNSAFE" in check_text or "DANGEROUS" in check_text or "REJECT" in check_text:
            is_no = True
            justification = clean_text
            
    return is_yes, is_no, justification

input_text = """\n\n[YES] Reason: This is a compress_context tool call that summarizes 40% of conversation history to free up context space. It is a non-mutating, internal session management operation — no files are modified, no shell commands are executed, and no external resources are accessed. The summary content accurately reflects the feature implementation details described in prior turns (window resizing, desire display, pulldown menu, scheduler lock). The compression fraction (40%) is reasonable and preserves the essential technical information for future reference."""

y, n, just = parse_security_response(input_text)
print(f"Is YES: {y}")
print(f"Is NO: {n}")
print(f"Justification: {just}")
