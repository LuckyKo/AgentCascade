---
name: case-fix-original-vs-upper-copy-parity
description: Fixing an ALL-CAPS / wrong-case bug by slicing output from an original-case copy instead of an upper-cased one, while preserving the case-insensitive matching and every behavior the old upper-cased code provided.
source: auto-generated
version: "1.0.0"
triggers:
  - "ALL CAPS"
  - "all caps"
  - "wrong case"
  - "original case"
  - "upper()"
  - ".upper()"
  - "case-insensitive matching"
  - "justification casing"
  - "reason text"
generated_by: coder
generated_from_task: "todo.md:155 — security approval/rejection reason text sometimes arrives in ALL CAPS; _parse_verdict Fallback-2 sliced justification from an .upper()'d line copy. Fix: keep a parallel original-case copy for slicing, upper-cased copy only for the [YES]/[NO] membership test."
---

## Goal
Fix a "text arrives in ALL CAPS / wrong case" bug where output is sliced from an `.upper()`'d string — by keeping a **parallel original-case copy** for the output and using the upper-cased copy **only** for case-insensitive matching — WITHOUT regressing behaviors the old single-copy code provided *by accident*.

## Why it bites
The obvious minimal fix is "slice from the original instead of the `.upper()`'d copy." But the old `.upper()` often **incidentally** supplied several behaviors your new parallel copy does not reproduce. In the security-verdict case (reason text sometimes ALL CAPS), three separate latent regressions lived inside one 3-line change:
1. **Case-sensitive token removal that only worked because input was pre-uppercased.** Old: `lc.replace('[YES]','')` on an already-`.upper()`'d string → a lowercase `[no]` was stripped "for free." New (slice from original): a literal `.replace('[yes]','')` no longer matches → the token **leaks** into the user-facing reason.
2. **Removing multiple tokens.** Old used two `.replace()` calls (one per token). A "simplification" to one `re.sub(r'\[YES\]|\[NO\]', '', s, count=1)` removes only the FIRST — on a line containing both, the other leaks.
3. **A load-bearing `.strip()` feeding an anchored regex.** The prefix-strip regex is anchored (`^(Reason|...)`). Removing `[NO]` from `'[NO] Reason: x'` leaves a leading space; drop the `.strip()` and the anchored prefix no longer matches, so `'Reason:'` leaks into the output.

## Procedure
### Step 1 — Inventory what the old upper-cased copy did, not just its intent
List every operation applied to the old string: the case change, each token removal (how many, case-sensitivity), any `.strip()`, and which downstream consumers rely on anchored/pattern matching. Ask: *which of these depended on the input already being upper-cased?*

### Step 2 — Decide correct semantics for each behavior explicitly
For each operation: must it now be case-insensitive (usually YES, since you slice from original)? Remove every occurrence or just one? Is a `.strip()` required before an anchored regex? Write these down so they're intentional, not inherited.

### Step 3 — Reproduce each behavior in the new code
Mirror the old operation count and semantics: e.g. two sequential `re.sub(..., count=1, flags=re.IGNORECASE)` calls (one per token) instead of one combined sub; keep `.strip()` where an anchored regex follows. Keep the diff minimal: two parallel local vars + a comment saying which copy is for matching vs output.

### Step 4 — Write a test for EACH latent behavior, not just the headline ALL-CAPS bug
- Headline: mixed-case reason must come back in original case (this is the load-bearing regression guard).
- Lowercase-token: `[no] some reason` → `'some reason'` (token must NOT leak; proves case-insensitive removal survived).
- Both-tokens line: `[YES] a [NO] b` → neither token leaks.
- Prefix-strip: `[NO] Reason: x` → `'x'` (locks the `.strip()`/anchored-regex interaction).

### Step 5 — Prove revert-proof (red on revert, green after)
Run the new tests against a TEMP-REVERT of the fix to confirm they fail with the ALL-CAPS signature, then restore and confirm green. See [[regression-test-revert-proof]]. The latent-behavior tests will often fail even on the "headline-fixed" version if you skipped Step 3 — that's the point.

## Tips
- **The old code's correctness was partly accidental.** `.upper()` + case-sensitive `.replace` only *looked* like case-insensitive matching because input was pre-normalized. Splitting the copies exposes the accident — don't assume they're equivalent.
- **A reviewer catches parity gaps your tests miss** (e.g. the both-tokens count=1 issue). Run an independent review on any such fix; make "removes N occurrences?" and "case-sensitive or not?" explicit review questions.
- **Anchored regexes are whitespace-fragile.** Any `.strip()` you remove is suspect if a `^`-anchored pattern follows it.
- Match the token test against the upper-cased copy (so `[yes]`/`[No]` still match) but slice output from the original — that's the whole fix in one sentence.
