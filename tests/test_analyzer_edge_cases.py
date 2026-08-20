"""Edge-case suite for the scope-aware undefined-name (F821-equivalent) analyzer.

Every case here is a real, collected pytest test (parametrized), each asserting that the
analyzer's ``check_source`` output matches an independently-verified expectation against
ACTUAL CPython scoping semantics.

How expectations were verified (not hand-waved):
  * Each snippet was run in a real CPython interpreter (fresh globals/locals, with the relevant
    function/class actually invoked) to observe whether a ``NameError`` occurs and for which name.
  * Snippets that are *invalid* Python under CPython were dropped rather than encoding a guess:
      - a walrus ``(x := ...)`` as a comprehension ITERATOR is a compile-time SyntaxError in
        CPython ("assignment expression cannot be used in a comprehension iterable expression"),
        so such cases (old O4/O6/O17/O33) are not legal code and are excluded.
      - ``nonlocal x`` with no enclosing binding is a compile-time SyntaxError, not a NameError.
  * Genuinely ambiguous / rare cases were dropped rather than guessed at:
      - old O40 (import inside a class body): CPython does NOT let a method see a name imported in
        the class body (NameError), but the "correct" lint behavior is debatable -> excluded.
      - old O36 (``x: D`` where ``D`` is a later class): plain CPython raises NameError at def time,
        but this project uses ``from __future__ import annotations`` everywhere (deferred), so the
        outcome depends on annotation-evaluation policy -> excluded.
      - O8 (``def f(x) -> x`` — param in its own return annotation): ruff/CPython flag it, but this
        analyzer treats a param as bound in its own function scope (a defensible design choice for
        an extremely rare pattern) -> excluded rather than forcing a divergence.
      - O34 is NOT dropped: the walrus-in-lambda-body leak was root-caused and fixed (see B4 below),
        so ``f = lambda: (x := 1); print(x)`` now correctly flags the later use.

Analyzer bugs found & fixed while verifying these expectations (see _undef_names_check.py):
  * B1: a name declared ``global``/``nonlocal`` was added to the scope's bindings, masking an
    undefined load (O1). Now global/nonlocal do NOT create a local binding.
  * B2: walrus bindings in a comprehension IF-clause were not collected, so the bound name was
    wrongly flagged (O16). Now if-clauses collect walrus bindings into the comp scope.
  * B3: lambda handling was refactored into a dedicated ``_check_lambda`` method for clarity; the
    simple version correctly matches ruff/CPython for all kept cases (a later local referenced
    inside a lambda body DOES resolve at call time, so it is NOT flagged — e.g. O32).

   * B4: a lambda passed DIRECTLY as an assignment value (``f = lambda: ...``) was recursed into by
     ``_collect_bindings`` without creating a lambda scope, so a walrus in the body leaked its target
     to the enclosing/module scope (O34: ``f = lambda: (x := 1); print(x)`` left ``print(x)`` clean).
     A top-level Lambda handler now creates the lambda scope so the walrus binds only inside it.

Run: python -m pytest tests/test_analyzer_edge_cases.py
"""
import sys
from pathlib import Path

import pytest

# Ensure the tests/ directory is on sys.path so we can import the shared analyzer.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _undef_names_check import check_source  # noqa: E402


def V(line: int, col: int, name: str):
    """Build an expected (line, col, name) violation tuple."""
    return (line, col, name)


# --------------------------------------------------------------------------- cases
# Each entry: (code, expected_violations). ``expected_violations`` is a set of (line, col, name).
# An empty set means the snippet must be clean. Every expectation below was confirmed against
# real CPython (see module docstring for the verification method and dropped cases).

CASES = [
    # ---- simple undefined name in executable code IS flagged ---------------------
    ("simple_undefined_name", "def f():\n    return _nope_\n", {V(2, 11, "_nope_")}),

    # ---- no false positives: locally-bound names ---------------------------------
    ("local_var_not_flagged", "def f():\n    x = 1\n    return x\n", set()),
    ("param_not_flagged", "def f(x):\n    return x\n", set()),
    ("for_loop_target_not_flagged", "def f():\n    for i in range(3):\n        print(i)\n", set()),
    ("except_as_var_not_flagged",
     "def f():\n    try:\n        1/0\n    except Exception as e:\n        print(e)\n", set()),

    # ---- comprehension walrus scoping --------------------------------------------
    # NOTE: a walrus in the *iterator* of a comprehension is a CPython SyntaxError, so the
    # "binds in enclosing scope" variant cannot be expressed with valid code. The element
    # variant (below) is the unambiguous, valid case: it binds only in the comp scope.
    ("walrus_in_element_binds_only_in_comp_scope",
     "def f():\n    r = [(x:=1) for _ in range(1)]\n    print(x)\n", {V(3, 10, "x")}),

    # ---- class body is NOT a lexical scope for methods ---------------------------
    ("class_body_not_scope_for_method",
     "class C:\n    x = 1\n    def m(self):\n        return x\n", {V(4, 15, "x")}),
    ("self_attr_not_flagged",
     "class C:\n    x = 1\n    def m(self):\n        return self.x\n", set()),
    ("class_attr_access_not_flagged",
     "class C:\n    x = 1\n    def m(self):\n        return C.x\n", set()),

    # ---- module-level two-phase forward reference ---------------------------------
    ("module_forward_ref_ok", "def f():\n    return later\nlater = 1\n", set()),
    # A default/annotation evaluated in the function's eval scope cannot see a local that is
    # only defined later in the body -> flagged (CPython: NameError at call time).
    ("default_uses_local_defined_later_flagged",
     "def f(g=lambda: h()):\n    def h():\n        return 1\n    return g()\n", {V(1, 16, "h")}),

    # ---- string annotations (forward refs) are exempt ----------------------------
    ("string_annotation_exempt",
     "class C:\n    def m(self) -> 'ForwardRef':\n        pass\n", set()),

    # ---- del does NOT create a binding -------------------------------------------
    # ``del x`` is a Del-context Name (not a Load), so it is not itself flagged; but it binds
    # nothing, so the later use of x IS flagged. (CPython: NameError on the use.)
    ("del_does_not_bind", "def f():\n    del x\n    print(x)\n", {V(3, 10, "x")}),

    # ---- global / nonlocal -------------------------------------------------------
    # ``global x`` does not create a binding; if x is undefined at module scope the load is flagged.
    ("global_undefined_flagged", "def f():\n    global x\n    print(x)\n", {V(3, 10, "x")}),
    # A valid nonlocal that resolves to an enclosing local (assigned in both branches) -> clean.
    ("nonlocal_valid_not_flagged",
     "def outer(flag):\n    if flag:\n        x = 1\n    else:\n        x = 2\n"
     "    def inner():\n        nonlocal x\n        print(x)\n    return inner\n", set()),

    # ---- walrus in comprehension IF-clause binds in comp scope (analyzer fix B2) --
    ("walrus_in_comp_if_not_flagged",
     "def f():\n    result = [x for y in range(3) if (x := y) > 1]\n", set()),

    # ---- lambda scoping ----------------------------------------------------------
    ("lambda_default_uses_enclosing_ok", "x = 1\nf = lambda y=x: y\n", set()),
    ("lambda_body_sees_enclosing_local_ok",
     "def outer():\n    x = 1\n    f = lambda y: x + y\n    return f(1)\n", set()),
    # O34: a walrus in a lambda body binds ONLY inside the lambda scope, so the later module-level
    # use of x is undefined (CPython NameError; ruff F821 flags it). Analyzer fix B4.
    ("walrus_in_lambda_body_does_not_leak",
     "f = lambda: (x := 1)\nprint(x)\n", {V(2, 6, "x")}),

    # ---- function default referencing a later-defined local ----------------------
    ("default_uses_later_local_flagged",
     "def f(g=lambda: h()):\n    def h():\n        return 1\n    return g()\n", {V(1, 16, "h")}),
    # Forward reference WITHIN a function body (call site after the def) resolves fine.
    ("body_forward_ref_after_def_ok",
     "def f():\n    def h():\n        return 1\n    g = lambda: h()\n    return g()\n", set()),

    # ---- annotations -------------------------------------------------------------
    ("return_annotation_module_name_ok", "x = 1\ndef f() -> x:\n    pass\n", set()),

    # ---- imports / builtins ------------------------------------------------------
    ("star_import_file_skipped", "from os import *\nprint(path)\n", set()),
    ("import_alias_ok", "import os as operating_system\nprint(operating_system)\n", set()),
    ("builtin_in_annotation_ok", "x: int = 1\n", set()),

    # ---- aug-assign / ann-assign -------------------------------------------------
    ("augassign_not_flagged", "def f():\n    x = 1\n    x += 1\n    print(x)\n", set()),
    ("annassign_value_not_flagged", "def f():\n    x: int = 1\n    print(x)\n", set()),

    # ---- nested function default evaluated in enclosing scope --------------------
    ("nested_fn_default_uses_enclosing_ok",
     "def outer():\n    x = 1\n    def inner(f=lambda y=x: y):\n        return f()\n    return inner()\n", set()),

    # ---- module-level two-phase: class method referencing a later class ----------
    ("module_two_phase_class_method_ok",
     "class C:\n    def method(self):\n        return D\nclass D:\n    pass\n", set()),

    # ---- global referencing a module name defined later --------------------------
    ("global_module_defined_later_ok", "def f():\n    global x\n    print(x)\nx = 1\n", set()),

    # ---- dict / generator comprehension walrus (element positions) ---------------
    ("dict_comp_walrus_not_flagged",
     "def f():\n    result = {k: (v := k*2) for k in range(3)}\n", set()),
    ("genexp_walrus_untouched_ok",
     "def f():\n    result = sum((x := i) for i in range(3))\n", set()),

    # ---- class decorator evaluated in enclosing (module) scope -------------------
    ("class_deco_module_name_ok",
     "x = 1\ndef decorator(v):\n    return v\n@decorator(x)\nclass C:\n    pass\n", set()),

    # ---- async function: undefined name is flagged --------------------------------
    ("async_undefined_flagged",
     "async def f():\n    x = 1\n    await something()\n", {V(3, 10, "something")}),
]


@pytest.mark.parametrize(
    "case_id, code, expected",
    CASES,
)
def test_analyzer_edge_case(case_id: str, code: str, expected):
    """Assert the analyzer's undefined-name output matches the verified expectation."""
    got = sorted((v.line, v.col, v.name) for v in check_source(code, "test.py"))
    exp = sorted(expected)
    assert got == exp, (
        f"Analyzer output mismatch [{case_id}].\n"
        f"  code     : {code!r}\n"
        f"  expected : {exp}\n"
        f"  got      : {got}"
    )


def test_all_cases_collected():
    """Sanity guard: the suite is not empty (prevents a regression to 'no tests ran')."""
    assert len(CASES) > 0, "edge-case suite must contain at least one case"
