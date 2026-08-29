"""Tests for the Calculate tool (agent_cascade/tools/custom/calculation.py).

Covers:
- New numeric builtins (int, float, bool, len, sum, divmod, trunc) and the
  bare-name floor/ceil aliases.
- Pre-existing behavior (math.*, ^ -> **, round/min/max/pow, randint range).
- Safety: dangerous builtins (__import__, open, exec, eval, compile, getattr,
  ...) are NOT in allowed_names and expressions using them FAIL with an Error
  string rather than executing.

The tool evaluates via a restricted ``eval`` with ``__builtins__={}`` and an
explicit allowlist, so anything not in the allowlist is simply undefined.
"""

import math

import pytest

from agent_cascade.tools.custom.calculation import Calculate


@pytest.fixture(scope="module")
def calc():
    return Calculate()


def run(calc, expr):
    """Call the tool and return its string result."""
    return calc.call({"expression": expr})


# ── New numeric builtins ────────────────────────────────────────────────────


def test_int_coercion(calc):
    assert run(calc, "int(3.9)") == "3"


def test_float_from_string_literal(calc):
    # A string literal is a safe eval target; float("2.5") must work.
    assert run(calc, 'float("2.5")') == "2.5"


def test_bool_coercion(calc):
    # NOTE: bool is a subclass of int, so the tool's numeric result formatter
    # renders it as an int ("0"/"1"), not "False"/"True". This matches the
    # existing (int, float) formatting branch — we assert that behavior.
    assert run(calc, "bool(0)") == "0"
    assert run(calc, "bool(1)") == "1"


def test_len_list(calc):
    assert run(calc, "len([1, 2, 3])") == "3"


def test_sum_list(calc):
    assert run(calc, "sum([1, 2, 3])") == "6"


def test_divmod(calc):
    # divmod returns (quotient, remainder); the tool stringifies tuples.
    assert run(calc, "divmod(10, 3)") == "(3, 1)"


def test_trunc_negative(calc):
    # trunc rounds toward zero (distinct from floor for negatives).
    assert run(calc, "trunc(-3.9)") == "-3"


# ── Bare-name floor/ceil aliases ────────────────────────────────────────────


def test_floor_bare_name(calc):
    assert run(calc, "floor(3.7)") == "3"


def test_ceil_bare_name(calc):
    assert run(calc, "ceil(3.2)") == "4"


def test_math_namespace_is_flattened(calc):
    # The math namespace is flattened into allowed_names (no `math` module
    # object in scope), so functions are reachable by bare name only.
    assert "math" not in calc.allowed_names
    assert run(calc, "floor(3.7)") == "3"


# ── Pre-existing behavior regression ────────────────────────────────────────


def test_trig_pi_over_2(calc):
    assert run(calc, "sin(pi/2)") == "1"


def test_caret_becomes_pow(calc):
    assert run(calc, "2^10") == "1024"


def test_round_two_decimals(calc):
    assert run(calc, "round(2.567, 2)") == "2.57"


def test_min_max_pow(calc):
    assert run(calc, "min(3, 1, 2)") == "1"
    assert run(calc, "max(3, 1, 2)") == "3"
    assert run(calc, "pow(2, 8)") == "256"


def test_abs_ln(calc):
    assert run(calc, "abs(-5)") == "5"
    # ln(e) == 1
    assert run(calc, "ln(e)") == "1"


def test_randint_range_sanity(calc):
    for _ in range(50):
        out = int(run(calc, "randint(1, 5)"))
        assert 1 <= out <= 5


# ── Safety: dangerous builtins are blocked ──────────────────────────────────

DANGEROUS_BUILTINS = [
    "__import__", "open", "exec", "eval", "compile",
    "getattr", "setattr", "delattr", "globals", "locals",
    "vars", "input", "breakpoint", "__builtins__",
]


@pytest.mark.parametrize("name", DANGEROUS_BUILTINS)
def test_dangerous_builtin_not_in_allowlist(calc, name):
    assert name not in calc.allowed_names


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "exec('x=1')",
        "eval('1+1')",
        "compile('1','<s>','eval')",
        "getattr(int, '__mro__')",
        "globals()",
    ],
)
def test_dangerous_expression_fails_with_error(calc, expr):
    result = run(calc, expr)
    # Must not execute; must return the tool's Error string.
    assert result.startswith("Error evaluating expression")


def test_getattr_builtin_unavailable(calc):
    # getattr is a dangerous builtin; it must not resolve in the eval scope.
    result = run(calc, "getattr(1, 'real')")
    assert result.startswith("Error evaluating expression")
