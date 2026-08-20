"""Scope-aware, dependency-free "undefined name" analyzer (ruff F821 equivalent).

Pure stdlib (``ast`` only) — no third-party linter required. This is the single source of
truth for the permanent lint gate that catches the class of refactor bug where a name is used
in a module but never defined/imported there (a function body moved to a sibling sub-module
without carrying its imports).

Used by:
  * ``tests/test_no_undefined_names.py``   (pytest gate)
  * ``scripts/check_undefined_names.py``    (standalone CLI)

Algorithm — two clean phases per file, mirroring CPython scoping so there are NO false positives:

  Phase 1 (build scope tree + collect bindings): recursively walk the AST building a tree of
      ``_Scope`` nodes (each linked to its parent and children). For each scope record every name
      it BINDS. A name is local to a function if it is bound ANYWHERE in that function body
      (assignment, for/with/except target, lambda param, nested def/class name, walrus ``:=``, del,
      aug-assign, ``global``/``nonlocal`` decl, local import). Comprehension targets bind in the
      comprehension's own implicit scope and do NOT leak to the enclosing function.

  Phase 2 (check loads): walk the same tree; for every Name node in Load context resolve it against
      the innermost scope that binds it (walking the parent chain outward), else builtins. A load is
      "undefined" only if no reachable scope binds it and it is not a builtin.

  Annotations: string annotations (forward refs) are NOT flagged — under PEP 563 every annotation
      is a string, and even when evaluated at runtime an unresolvable annotation raises NameError
      only inside ``typing.get_type_hints()`` (never in executable code). Flagging them would create
      false positives for legitimate forward refs (e.g. ``'AgentPool'``). Non-string annotations and
      all executable expressions MUST resolve.

  Star imports (``from x import *``) make a file un-analyzable for F821; such files are skipped
  rather than risk false positives.
"""

from __future__ import annotations

import ast
import builtins
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set


# Names that are never "undefined" — the module-level namespace every Python module starts with.
_BUILTIN_NAMES: Set[str] = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__path__",
}


@dataclass
class Violation:
    """One undefined-name occurrence."""
    file: str
    line: int
    col: int
    name: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}  undefined name {self.name!r}"


@dataclass
class _Scope:
    """A lexical scope. ``kind`` is one of 'module', 'function', 'class', 'comprehension'."""
    kind: str
    parent: Optional["_Scope"] = None
    bindings: Set[str] = field(default_factory=set)   # names bound in THIS scope (phase 1)

    def resolve(self, name: str) -> bool:
        """True if `name` is bound in this scope or any enclosing scope (or a builtin)."""
        cur: Optional["_Scope"] = self
        while cur is not None:
            if name in cur.bindings:
                return True
            cur = cur.parent
        return name in _BUILTIN_NAMES


def _target_names(node: ast.AST) -> List[str]:
    """All Name ids bound by an assignment target (handles tuples, star, nested subscripts)."""
    out: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            out.append(sub.id)
    return out


def _has_star_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return True
    return False


class _UndefinedNameChecker(ast.NodeVisitor):
    """Single-pass scope-aware checker.

    For each function/class we (a) collect ALL bindings in its body first — implementing Python's
    "a name assigned anywhere in the function is local to the whole function" rule — then (b) check
    every Load against that complete binding set plus enclosing scopes. This ordering is what makes
    forward references within a body (e.g. a helper defined below its call site) resolve correctly,
    and it is exactly how CPython/pyflakes decide localness.
    """

    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.violations: List[Violation] = []
        self._module_scope = _Scope(kind="module")

    # ------------------------------------------------------------------ entry
    def analyze(self, tree: ast.AST) -> List[Violation]:
        self.visit(tree)
        return self.violations

    # ------------------------------------------------------------------ module
    def visit_Module(self, node: ast.Module) -> None:
        # Two-phase at module level (same as functions): collect ALL module-level bindings first
        # so that forward references within the module resolve (e.g. a class method using a
        # module constant defined later in the file — legal because by call-time the module is done).
        for stmt in node.body:
            self._stmt_collect(stmt, self._module_scope)
        for stmt in node.body:
            self._stmt(stmt, self._module_scope)

    def _stmt(self, stmt: ast.stmt, scope: _Scope) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.bindings.add(stmt.name)
            self._function(stmt, scope)
            return
        if isinstance(stmt, ast.ClassDef):
            scope.bindings.add(stmt.name)
            self._class(stmt, scope)
            return
        # Simple statement evaluated in `scope`.
        self._collect_bindings(stmt, scope)
        self._check_loads(stmt, scope)

    def _function(self, fn: ast.FunctionDef, enclosing: _Scope) -> None:
        func_scope = _Scope(kind="function", parent=enclosing)
        # Parameters bind in the function scope.
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            func_scope.bindings.add(a.arg)
        if fn.args.vararg is not None:
            func_scope.bindings.add(fn.args.vararg.arg)
        if fn.args.kwarg is not None:
            func_scope.bindings.add(fn.args.kwarg.arg)

        # Decorators + defaults are evaluated in the ENCLOSING scope.
        for deco in fn.decorator_list:
            self._collect_bindings(deco, enclosing)
            self._check_loads(deco, enclosing)
        for d in fn.args.defaults + fn.args.kw_defaults:
            if d is not None:
                self._collect_bindings(d, enclosing)
                self._check_loads(d, enclosing)

        # PHASE A: collect ALL bindings in the body first (forward refs within the body resolve).
        for stmt in fn.body:
            self._stmt_collect(stmt, func_scope)

        # Annotations (string forward refs skipped by _maybe_check_annotation).
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            if a.annotation is not None:
                self._maybe_check_annotation(a.annotation, func_scope)
        if fn.args.vararg is not None and fn.args.vararg.annotation is not None:
            self._maybe_check_annotation(fn.args.vararg.annotation, func_scope)
        if fn.args.kwarg is not None and fn.args.kwarg.annotation is not None:
            self._maybe_check_annotation(fn.args.kwarg.annotation, func_scope)
        if fn.returns is not None:
            self._maybe_check_annotation(fn.returns, func_scope)

        # PHASE B: now that all bindings are known, check every load in the body.
        for stmt in fn.body:
            self._stmt(stmt, func_scope)

    def _class(self, cls: ast.ClassDef, enclosing: _Scope) -> None:
        class_scope = _Scope(kind="class", parent=enclosing)
        # Bases / metaclass are expressions in the ENCLOSING scope.
        for base in cls.bases:
            self._collect_bindings(base, enclosing)
            self._check_loads(base, enclosing)
        for kw in cls.keywords:
            if kw.value is not None:
                self._collect_bindings(kw.value, enclosing)
                self._check_loads(kw.value, enclosing)
        # Class decorators are evaluated in the ENCLOSING scope.
        for deco in cls.decorator_list:
            self._collect_bindings(deco, enclosing)
            self._check_loads(deco, enclosing)
        # Class body: collect all class-level bindings first (for forward refs within class body).
        for stmt in cls.body:
            self._stmt_collect(stmt, class_scope)
        # Now check loads. Methods' BODIES resolve through the class's enclosing scope (Python rule:
        # class body is not a lexical scope for methods), but method DECORATORS and class-body-level
        # expressions are evaluated in class_scope (e.g. @stopped.setter references the getter).
        for stmt in cls.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Method: decorators checked in class_scope; body resolved through enclosing.
                self._function_with_class_deco(stmt, class_scope, enclosing)
            elif isinstance(stmt, ast.ClassDef):
                self._class(stmt, class_scope)
            else:
                self._stmt(stmt, class_scope)

    def _function_with_class_deco(self, fn: ast.FunctionDef, class_scope: _Scope, enclosing: _Scope) -> None:
        """Handle a method inside a class: decorators in class_scope, body through enclosing."""
        func_scope = _Scope(kind="function", parent=enclosing)
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            func_scope.bindings.add(a.arg)
        if fn.args.vararg is not None:
            func_scope.bindings.add(fn.args.vararg.arg)
        if fn.args.kwarg is not None:
            func_scope.bindings.add(fn.args.kwarg.arg)
        # Decorators evaluated in the CLASS scope (where sibling properties/attrs are visible).
        for deco in fn.decorator_list:
            self._collect_bindings(deco, class_scope)
            self._check_loads(deco, class_scope)
        # Defaults evaluated in enclosing scope.
        for d in fn.args.defaults + fn.args.kw_defaults:
            if d is not None:
                self._collect_bindings(d, enclosing)
                self._check_loads(d, enclosing)
        # PHASE A: collect all body bindings.
        for stmt in fn.body:
            self._stmt_collect(stmt, func_scope)
        # Annotations.
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            if a.annotation is not None:
                self._maybe_check_annotation(a.annotation, func_scope)
        if fn.args.vararg is not None and fn.args.vararg.annotation is not None:
            self._maybe_check_annotation(fn.args.vararg.annotation, func_scope)
        if fn.args.kwarg is not None and fn.args.kwarg.annotation is not None:
            self._maybe_check_annotation(fn.args.kwarg.annotation, func_scope)
        if fn.returns is not None:
            self._maybe_check_annotation(fn.returns, func_scope)
        # PHASE B: check body loads (resolved through enclosing, NOT class_scope).
        for stmt in fn.body:
            self._stmt(stmt, func_scope)

    # ------------------------------------------------------------------ binding collection
    def _stmt_collect(self, stmt: ast.stmt, scope: _Scope) -> None:
        """Collect bindings for a statement (no load checking).

        Nested functions/classes are their own scopes: record their NAME in `scope`, then recurse
        into their body to collect the inner bindings (so e.g. a module-level helper defined below
        its call site is still known when the enclosing function's loads are checked later).
        """
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.bindings.add(stmt.name)
            self._function_collect(stmt, scope)
            return
        if isinstance(stmt, ast.ClassDef):
            scope.bindings.add(stmt.name)
            self._class_collect(stmt, scope)
            return
        self._collect_bindings(stmt, scope)

    def _function_collect(self, fn: ast.FunctionDef, enclosing: _Scope) -> None:
        func_scope = _Scope(kind="function", parent=enclosing)
        for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            func_scope.bindings.add(a.arg)
        if fn.args.vararg is not None:
            func_scope.bindings.add(fn.args.vararg.arg)
        if fn.args.kwarg is not None:
            func_scope.bindings.add(fn.args.kwarg.arg)
        for stmt in fn.body:
            self._stmt_collect(stmt, func_scope)

    def _class_collect(self, cls: ast.ClassDef, enclosing: _Scope) -> None:
        class_scope = _Scope(kind="class", parent=enclosing)
        for stmt in cls.body:
            self._stmt_collect(stmt, class_scope)

    def _collect_bindings(self, node: ast.AST, scope: _Scope) -> None:
        """Record every name bound by `node` into `scope`, recursing through ALL non-scope nodes.

        Handles BOTH top-level binding statements (Import/Assign/etc. passed directly) and their
        sub-nodes, so it works whether called on a statement or on an expression tree.
        """
        # --- top-level: node itself is a binding statement ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                scope.bindings.add(alias.asname or alias.name.split(".")[0])
            return
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    scope.bindings.add(alias.asname or alias.name)
            return
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                for t in _target_names(tgt):
                    scope.bindings.add(t)
            self._collect_bindings(node.value, scope)
            return
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self._collect_bindings(node.value, scope)
            if node.target is not None:
                for t in _target_names(node.target):
                    scope.bindings.add(t)
            return
        if isinstance(node, ast.AugAssign):
            # target name is local (read+write); record it and collect the value's bindings.
            if isinstance(node.target, ast.Name):
                scope.bindings.add(node.target.id)
            self._collect_bindings(node.value, scope)
            return
        if isinstance(node, ast.NamedExpr):
            # Walrus := binds its target in the CURRENT (lexical) scope it appears in, then
            # evaluates its value. (A walrus inside a lambda/comprehension is reached with that
            # scope as `scope`, so this is correct; it must NOT leak to an outer scope.)
            if isinstance(node.target, ast.Name):
                scope.bindings.add(node.target.id)
            self._collect_bindings(node.value, scope)
            return
        if isinstance(node, ast.Lambda):
            # A lambda passed DIRECTLY as a node (e.g. the value of an Assign: `f = lambda: ...`)
            # is a scope boundary. Its body must bind into a NEW lambda scope, not `scope` —
            # otherwise a walrus in the body leaks to the enclosing scope
            # (`f = lambda: (x := 1); print(x)` would wrongly treat x as module-bound).
            lam_scope = self._new_lambda_scope(node, scope)
            # Defaults are evaluated in the ENCLOSING scope (bind there); body binds in lam_scope.
            for d in node.args.defaults + node.args.kw_defaults:
                if d is not None:
                    self._collect_bindings(d, scope)
            self._collect_bindings(node.body, lam_scope)
            return
        # NOTE: ``global``/``nonlocal`` declarations do NOT create a local binding — they only
        # redirect where the name resolves. If the referenced name is not defined in the target
        # scope, the load is genuinely undefined (CPython raises NameError). We therefore add
        # nothing to `scope.bindings` here; loads are resolved via the normal parent chain and
        # flagged if no reachable scope binds the name. (A ``nonlocal`` with no enclosing binding
        # is a compile-time SyntaxError, so it never reaches this check.)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # Comprehension targets bind in their OWN implicit scope, NOT the enclosing one.
            comp_scope = _Scope(kind="comprehension", parent=scope)
            for gen in node.generators:
                for t in _target_names(gen.target):
                    comp_scope.bindings.add(t)
                # Recurse into the iter and ifs to collect any nested bindings (e.g. nested defs).
                self._collect_bindings(gen.iter, comp_scope)
                for cond in gen.ifs:
                    # A walrus in an if-clause binds in the comprehension's own scope; collect it
                    # so the bound name is not later flagged as undefined when used in the element.
                    self._collect_walrus(cond, comp_scope)
                    self._collect_bindings(cond, comp_scope)
            return
        # --- recurse through children (compound statements, expressions, etc.) ---
        for child in ast.iter_child_nodes(node):
            # scope boundaries: record the defining name, do not descend into the body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.bindings.add(child.name)
                continue
            if isinstance(child, ast.ClassDef):
                scope.bindings.add(child.name)
                continue
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                comp_scope = _Scope(kind="comprehension", parent=scope)
                for gen in child.generators:
                    for t in _target_names(gen.target):
                        comp_scope.bindings.add(t)
                continue
            if isinstance(child, ast.Lambda):
                lam_scope = self._new_lambda_scope(child, scope)
                # Lambda defaults are evaluated in the ENCLOSING scope (bind there), while the body
                # binds only inside the lambda's own scope. A walrus in the body therefore must NOT
                # leak to `scope` (e.g. `f = lambda: (x := 1); print(x)` -> x is undefined outside).
                for d in child.args.defaults + child.args.kw_defaults:
                    if d is not None:
                        self._collect_bindings(d, scope)
                self._collect_bindings(child.body, lam_scope)
                continue
            # bindings in the current scope (when they appear as children)
            if isinstance(child, ast.Import):
                for alias in child.names:
                    scope.bindings.add(alias.asname or alias.name.split(".")[0])
                continue
            if isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name != "*":
                        scope.bindings.add(alias.asname or alias.name)
                continue
            if isinstance(child, ast.ExceptHandler):
                if child.name is not None:
                    scope.bindings.add(child.name)
                self._collect_bindings(child, scope)   # recurse into handler body
                continue
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, ast.Store):
                    scope.bindings.add(child.id)
                # Del context does NOT create a binding — it only removes. If the name was
                # never assigned, `del x` is a NameError (caught by ruff F821 too).
                continue
            # everything else: recurse
            self._collect_bindings(child, scope)

    @staticmethod
    def _new_lambda_scope(lam: ast.Lambda, parent: _Scope) -> _Scope:
        """Build a fresh lambda scope (kind="function") with all params/vararg/kwarg bound.

        Shared by the binding phase (``_collect_bindings``), the check phase (``_check_lambda``),
        and the direct-node lambda handler so the param-binding logic lives in one place. Defaults
        are intentionally NOT handled here — they evaluate in the enclosing scope, which each caller
        recurses into with its own phase-specific routine.
        """
        lam_scope = _Scope(kind="function", parent=parent)
        for a in lam.args.posonlyargs + lam.args.args + lam.args.kwonlyargs:
            lam_scope.bindings.add(a.arg)
        if lam.args.vararg is not None:
            lam_scope.bindings.add(lam.args.vararg.arg)
        if lam.args.kwarg is not None:
            lam_scope.bindings.add(lam.args.kwarg.arg)
        return lam_scope

    # ------------------------------------------------------------------ load checking
    def _check_lambda(self, lam: ast.Lambda, enclosing: _Scope) -> None:
        """Check a lambda's loads, honoring its own scope.

        The lambda body resolves against the LAMBDA's scope (parent = `enclosing`), so it sees the
        lambda's params plus names bound in `enclosing` and outward. This matches CPython/ruff: a
        name that is local to `enclosing`'s function BODY (e.g. ``def f(): g=lambda: h(); def h()``)
        still resolves at runtime because by the time the lambda is *called* the body has run, so it
        is NOT flagged. Defaults are evaluated in `enclosing`. A walrus in the body binds only in the
        lambda scope (does not leak to `enclosing`), so ``f = lambda: (x := 1); print(x)`` IS flagged.
        """
        lam_scope = self._new_lambda_scope(lam, enclosing)
        for d in lam.args.defaults + lam.args.kw_defaults:
            if d is not None:
                self._check_loads(d, enclosing)   # defaults evaluated in the enclosing scope
        self._check_loads(lam.body, lam_scope)

    def _check_loads(self, node: ast.AST, scope: _Scope) -> None:
        """Check every Load-context Name in `node` resolves within `scope` (and outward)."""
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                self._resolve_name(node, scope)
            return
        # If the node ITSELF is a comprehension/lambda, handle it as a scope boundary.
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            self._check_comp_loads(node, scope)
            return
        if isinstance(node, ast.Lambda):
            self._check_lambda(node, scope)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(child, scope)
                continue
            if isinstance(child, ast.ClassDef):
                self._class(child, scope)
                continue
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                self._check_comp_loads(child, scope)
                continue
            if isinstance(child, ast.Lambda):
                self._check_lambda(child, scope)
                continue
            # Recurse into all other nodes (If/For/Try/With/calls/etc.).
            self._check_loads(child, scope)

    def _check_comp_loads(self, comp_node: ast.AST, enclosing: _Scope) -> None:
        """Check loads in a comprehension, honoring its implicit py3 scope."""
        comp_scope = _Scope(kind="comprehension", parent=enclosing)
        generators = comp_node.generators
        if generators:
            # First iter is evaluated in the ENCLOSING scope. Collect walrus bindings first
            # so they don't get flagged as undefined (walrus := binds in the enclosing scope).
            self._collect_walrus(generators[0].iter, enclosing)
            self._check_loads(generators[0].iter, enclosing)
            for t in _target_names(generators[0].target):
                comp_scope.bindings.add(t)
            # If-clauses are evaluated in the COMPREHENSION scope; a walrus there binds x in that
            # scope, so collect it before checking the element (e.g. [x for y in r if (x:=y)>1]).
            for cond in generators[0].ifs:
                self._collect_walrus(cond, comp_scope)
        for gen in generators[1:]:
            # Subsequent iterators are in the comprehension scope.
            self._collect_walrus(gen.iter, comp_scope)
            self._check_loads(gen.iter, comp_scope)
            for t in _target_names(gen.target):
                comp_scope.bindings.add(t)
            for cond in gen.ifs:
                self._collect_walrus(cond, comp_scope)
                self._check_loads(cond, comp_scope)
        if isinstance(comp_node, ast.DictComp):
            self._check_loads(comp_node.key, comp_scope)
            self._check_loads(comp_node.value, comp_scope)
        else:  # ListComp / SetComp / GeneratorExp
            self._check_loads(comp_node.elt, comp_scope)

    def _collect_walrus(self, node: ast.AST, scope: _Scope) -> None:
        """Record walrus (:=) target names into `scope` so they aren't flagged as undefined."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.NamedExpr):
                # NamedExpr.target is always a Name in Store context.
                if isinstance(sub.target, ast.Name):
                    scope.bindings.add(sub.target.id)

    def _resolve_name(self, name_node: ast.Name, scope: _Scope) -> None:
        if not scope.resolve(name_node.id):
            self.violations.append(
                Violation(self.relpath, name_node.lineno, name_node.col_offset, name_node.id))

    def _maybe_check_annotation(self, ann: ast.AST, scope: _Scope) -> None:
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            return  # string forward ref -> OK (never a runtime undefined-name in executable code)
        self._check_loads(ann, scope)


def check_source(source: str, relpath: str = "<source>") -> List[Violation]:
    """Analyze one source string; return undefined-name violations (empty list == clean)."""
    tree = ast.parse(source)
    if _has_star_import(tree):
        return []
    checker = _UndefinedNameChecker(relpath)
    return checker.analyze(tree)


def check_file(path: Path, root: Optional[Path] = None) -> List[Violation]:
    """Analyze a single .py file. Syntax errors surface as a violation so the gate fails loudly."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        return [Violation(str(path), 0, 0, f"<unreadable: {exc}>")]
    rel = str(path.relative_to(root)) if root else str(path)
    try:
        return check_source(source, relpath=rel)
    except SyntaxError as exc:
        return [Violation(rel, exc.lineno or 0, exc.offset or 0, f"<syntax error: {exc.msg}>")]


def find_python_files(root: Path) -> List[Path]:
    """All .py files under `root`, recursively, sorted for deterministic output."""
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def check_package(root: Path) -> List[Violation]:
    """Run the analyzer over every .py file under `root`. Returns all violations."""
    violations: List[Violation] = []
    for path in find_python_files(root):
        violations.extend(check_file(path, root=root))
    return violations


def default_package_root() -> Path:
    """Locate the agent_cascade package relative to this file (tests/_undef_names_check.py)."""
    here = Path(__file__).resolve()
    # tests/ -> project root -> agent_cascade/
    return here.parent.parent / "agent_cascade"


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Exit 0 if clean, 1 on violations, 2 on usage/IO problems."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect undefined names (ruff F821 equivalent) in agent_cascade.")
    parser.add_argument("path", nargs="?", default=None,
                        help="Directory to scan (default: the agent_cascade package).")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve() if args.path else default_package_root()
    if not root.is_dir():
        print(f"error: directory not found: {root}", file=sys.stderr)
        return 2

    violations = check_package(root)
    files_scanned = len(find_python_files(root))

    if not violations:
        print(f"OK: no undefined names in {files_scanned} files under {root}")
        return 0

    by_file: dict = {}
    for v in violations:
        by_file.setdefault(v.file, []).append(v)
    print(f"FAIL: {len(violations)} undefined name(s) across {len(by_file)} file(s):\n")
    for fname in sorted(by_file):
        print(f"  {fname}")
        for v in by_file[fname]:
            print(f"    line {v.line}:{v.col}  {v.name!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
