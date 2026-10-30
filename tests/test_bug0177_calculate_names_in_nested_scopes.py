"""BUG_0177: `calculate` builtins must resolve inside lambdas / comprehensions, not just top level.

The incident (session `logs/researcher_strata-ram-investigator_20261030_004716.jsonl`): a
floor pass-rate table written as ``(lambda F: {...sum(1 for ...)...})(256)`` returned
``name 'sum' is not defined`` even though the tool advertises ``sum``/``min``/``max``/``len``
and they work at the top of an expression.

Root cause (`agent_cascade/tools/custom/calculation.py`, pre-fix line 79): the allowlist was
passed as the ``locals`` argument of ``eval(expr, {'__builtins__': {}}, allowed_names)``.
Top-level code resolves names with ``LOAD_NAME`` (locals, then globals) — so ``sum([1,2])``
worked. A name used inside any function scope (lambda body, comprehension) compiles to
``LOAD_GLOBAL``, which consults only the globals mapping and never sees the allowlist.

Fix: build the scope with the allowlist in **globals** (``{'__builtins__': {}}`` plus the
same allowlisted names). Identical name set, so the sandbox surface is unchanged —
``__builtins__`` stays empty and no non-allowlisted name becomes reachable, including from
inside a lambda.

Red/green: the nested-scope tests below fail against the pre-fix line with
``name '<builtin>' is not defined``; the security assertions pass either way (they are here
to stop the fix from ever being "solved" by exposing real builtins).
"""

import pytest

from agent_cascade.tools.custom.calculation import Calculate


@pytest.fixture(scope='module')
def calc():
    return Calculate()


def run(calc, expr):
    return calc.call({'expression': expr})


def assert_error(calc, expr):
    result = run(calc, expr)
    assert result.startswith('Error evaluating expression'), result
    return result


# ────────────────────────────────────────────────────────────────────────────
# The incident's three-row table — the exact red/green evidence
# ────────────────────────────────────────────────────────────────────────────


class TestIncidentTable:
    def test_top_level_sum(self, calc):
        assert run(calc, 'sum([1,2,3])') == '6'

    def test_top_level_generator_expression(self, calc):
        assert run(calc, 'sum(x for x in [1,2,3])') == '6'

    def test_sum_inside_lambda(self, calc):
        """The failing row: LOAD_GLOBAL never saw the locals-hosted allowlist."""
        assert run(calc, '(lambda: sum([1,2]))()') == '3'

    def test_floor_pass_rate_table_from_the_incident(self, calc):
        expr = ('(lambda F: sum(1 for n, a in [(128, 384), (256, 512), (192, 448)] '
                'if a >= F and n <= a - F))(256)')
        assert run(calc, expr) == '3'


# ────────────────────────────────────────────────────────────────────────────
# Whole allowlist, not a hand-picked sample
# ────────────────────────────────────────────────────────────────────────────


def test_every_allowlisted_name_resolves_inside_a_lambda(calc):
    """``(lambda: NAME)()`` is the minimal nested-scope reference for each name.

    It covers math callables and constants, the added builtins, and the aliases in one
    assertion set — a fix that only whitelisted ``sum`` would still fail here.
    """
    broken = {}
    for name in sorted(calc.allowed_names):
        result = run(calc, f'(lambda: {name})()')
        if result.startswith('Error evaluating expression'):
            broken[name] = result
    assert not broken, f'{len(broken)} allowlisted name(s) invisible in nested scope: {broken}'


def test_allowlisted_name_resolves_inside_a_comprehension_scope(calc):
    """Comprehensions are their own function scope in Python 3, same failure mode."""
    # `len` is resolved inside the generator frame (LOAD_GLOBAL), not at expression top level.
    assert run(calc, 'sum(len(s) for s in ["ab", "cde"])') == '5'
    assert run(calc, '[(lambda: max(1,2))() for _ in [0]]') == '[2]'


class TestNestedScopesThatNowWork:
    def test_lambda_can_call_supported_builtins(self, calc):
        assert run(calc, '(lambda x: abs(x) + len([x]))(-4)') == '5'
        assert run(calc, '(lambda a, b: divmod(a, b))(10, 3)') == '(3, 1)'
        assert run(calc, '(lambda: floor(3.7) + ceil(3.2))()') == '7'

    def test_lambda_closure_over_its_parameters_still_works(self, calc):
        assert run(calc, '(lambda x: sum([x, 2]))(5)') == '7'

    def test_dict_comprehension_inside_lambda(self, calc):
        expr = '(lambda F: {k: max(F, v) for k, v in [("a", 1), ("b", 300)]})(256)'
        assert run(calc, expr) == "{'a': 256, 'b': 300}"

    def test_math_constants_and_functions_inside_lambda(self, calc):
        # ln(e) == 1, sin(pi/2) == 1 — the two most common uses of the flattened math namespace.
        assert run(calc, '(lambda: ln(e) + sin(pi/2))()') == '2'

    def test_random_helpers_inside_lambda(self, calc):
        for _ in range(25):
            out = int(run(calc, '(lambda: randint(1, 5))()'))
            assert 1 <= out <= 5


# ────────────────────────────────────────────────────────────────────────────
# Sandbox surface must not grow — the fix moves names, it does not add any
# ────────────────────────────────────────────────────────────────────────────


class TestNestedScopeStaysSandboxed:
    @pytest.mark.parametrize(
        'expr',
        [
            "(lambda: __import__('os').system('echo pwned'))()",
            "(lambda: open('/etc/passwd').read())()",
            "(lambda: exec('x=1'))()",
            "(lambda: eval('1+1'))()",
            "(lambda: compile('1', '<s>', 'eval'))()",
            "(lambda: getattr(int, '__mro__'))()",
            '(lambda: globals())()',
            '(lambda: vars())()',
            '[__import__("os") for _ in [0]]',
        ],
    )
    def test_dangerous_names_are_undefined_in_nested_scopes(self, calc, expr):
        assert_error(calc, expr)

    def test_builtins_mapping_stays_empty_in_nested_scope(self, calc):
        """The allowlist lives in globals; the real builtins must not come with it."""
        assert run(calc, '(lambda: len(__builtins__))()') == '0'

    def test_math_module_object_is_still_not_in_scope(self, calc):
        """Names stay flattened: there is no `math` module (and no import path) anywhere."""
        assert_error(calc, '(lambda: math)()')
        assert_error(calc, 'math.sqrt(4)')

    def test_unknown_name_inside_lambda_still_errors(self, calc):
        assert_error(calc, '(lambda: definitely_not_allowlisted)()')
