#!/usr/bin/env python3
"""pyflakes-style undefined-name scanner for the preview package.

Scans each .py file for names that are used but never defined or imported.
Treats lazy in-function imports as FINE (import inside the calling function
body before use). Python builtins and typing names are also fine.

Usage:
    python scripts/audit_undefined_names.py [file-or-dir ...]
"""

import ast
import glob
import os
import sys


BUILTINS = set(dir(__builtins__)) if hasattr(__builtins__, '__dict__') else set()

SAFE_NAMES = BUILTINS | {
    'TYPE_CHECKING', 'Any', 'Dict', 'List', 'Optional', 'Set', 'Tuple',
    'Callable', 'Literal', 'Iterable', 'Iterator', 'Generator',
    'TypeVar', 'Union', 'Protocol', 'runtime_checkable',
    '_', 'self', 'cls', 'super', 'True', 'False', 'None',
    '__name__', '__file__', '__doc__', '__version__',
}


def _defined_names_visit(body, defined=None):
    """Return {name} for names defined in *body* statements."""
    if defined is None:
        defined = set()
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            defined.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
                elif isinstance(t, (ast.List, ast.Tuple)):
                    for el in t.elts:
                        if isinstance(el, ast.Name):
                            defined.add(el.id)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                defined.add(stmt.target.id)
        elif isinstance(stmt, ast.Import):
            for a in stmt.names:
                defined.add(a.asname or a.name.split('.')[0])
        elif isinstance(stmt, ast.ImportFrom):
            for a in stmt.names:
                defined.add(a.asname or a.name)
        elif isinstance(stmt, ast.For):
            if isinstance(stmt.target, ast.Name):
                defined.add(stmt.target.id)
        elif isinstance(stmt, ast.With):
            for item in stmt.items:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    defined.add(item.optional_vars.id)
        elif isinstance(stmt, ast.Try):
            for h in stmt.handlers:
                if h.name:
                    defined.add(h.name)
    return defined


def _module_defined(tree):
    """Names defined at module level."""
    return _defined_names_visit(tree.body)


def audit_file(filepath, verbose=False):
    """Return list of (filepath, lineno, name) for undefined names."""
    try:
        with open(filepath, encoding='utf-8') as f:
            source = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return [(filepath, 0, f"Can't read: {e}")]

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError as e:
        return [(filepath, e.lineno or 0, f"Syntax error: {e}")]

    module_defs = _module_defined(tree)
    errors = []

    class FuncChecker(ast.NodeVisitor):
        """Walk the tree, entering function/class bodies, checking names."""
        def __init__(self):
            # Stack of local-variable sets (one per function scope)
            self.scope_stack = []
            # Stack collecting lazy imports per scope
            self.lazy_imports_stack = []

        def visit_Module(self, node):
            self.scope_stack.append(set())
            self._check_body(node.body)
            self.scope_stack.pop()

        def _check_name(self, node):
            """Report undefined Name if not in any accessible scope."""
            name = node.id
            if name in SAFE_NAMES:
                return
            if name in module_defs:
                return
            for scope in reversed(self.scope_stack):
                if name in scope:
                    return
            errors.append((filepath, node.lineno, name))

        def _check_body(self, body, extra_defs=None):
            """Walk a list of statements sequentially, tracking defs.
            Uses self.scope_stack[-1] for the current scope."""
            if not self.scope_stack:
                # Shouldn't happen — bodies always inside a scope
                return
            scope = self.scope_stack[-1]
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope.add(stmt.name)
                    self.visit(stmt)
                    continue
                elif isinstance(stmt, ast.ClassDef):
                    scope.add(stmt.name)
                    self._check_class_body(stmt)
                    continue
                elif isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            scope.add(t.id)
                        elif isinstance(t, (ast.List, ast.Tuple)):
                            for el in t.elts:
                                if isinstance(el, ast.Name):
                                    scope.add(el.id)
                    if stmt.value is not None:
                        for n in ast.walk(stmt.value):
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                                self._check_name(n)
                    continue
                elif isinstance(stmt, ast.AnnAssign):
                    if isinstance(stmt.target, ast.Name):
                        scope.add(stmt.target.id)
                    if stmt.value is not None:
                        for n in ast.walk(stmt.value):
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                                self._check_name(n)
                    continue
                elif isinstance(stmt, ast.AugAssign):
                    if isinstance(stmt.target, ast.Name):
                        scope.add(stmt.target.id)
                    for n in ast.walk(stmt.value):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)
                    continue
                elif isinstance(stmt, ast.For):
                    if isinstance(stmt.target, ast.Name):
                        scope.add(stmt.target.id)
                    for n in ast.walk(stmt.iter):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)
                    self._check_body(stmt.body)
                    self._check_body(stmt.orelse)
                    continue
                elif isinstance(stmt, ast.With):
                    for item in stmt.items:
                        if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                            scope.add(item.optional_vars.id)
                        for n in ast.walk(item.context_expr):
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                                self._check_name(n)
                    self._check_body(stmt.body)
                    continue
                elif isinstance(stmt, ast.Import):
                    for a in stmt.names:
                        scope.add(a.asname or a.name.split('.')[0])
                    continue
                elif isinstance(stmt, ast.ImportFrom):
                    for a in stmt.names:
                        scope.add(a.asname or a.name)
                    continue
                elif isinstance(stmt, ast.Try):
                    self._check_body(stmt.body)
                    self._check_body(stmt.orelse)
                    self._check_body(stmt.finalbody)
                    for h in stmt.handlers:
                        if h.name:
                            scope.add(h.name)
                            if self.scope_stack:
                                self.scope_stack[-1].add(h.name)
                        if h.type:
                            for n in ast.walk(h.type):
                                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                                    self._check_name(n)
                        if h.body:
                            if h.name:
                                # Already added to scope; recursive call shares the same stack
                                pass
                            self._check_body(h.body)
                    continue
                elif isinstance(stmt, ast.Global):
                    for name in stmt.names:
                        scope.add(name)
                    continue
                elif isinstance(stmt, ast.Raise):
                    if stmt.exc:
                        for n in ast.walk(stmt.exc):
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                                self._check_name(n)
                    continue
                elif isinstance(stmt, ast.Delete):
                    for n in ast.walk(stmt):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)
                    continue
                elif isinstance(stmt, ast.Assert):
                    for n in ast.walk(stmt):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)
                    continue
                elif isinstance(stmt, ast.If):
                    for n in ast.walk(stmt.test):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)
                    self._check_body(stmt.body)
                    self._check_body(stmt.orelse)
                    continue
                elif isinstance(stmt, ast.While):
                    for n in ast.walk(stmt.test):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)
                    self._check_body(stmt.body)
                    self._check_body(stmt.orelse)
                    continue

                # Fallback: walk for Name nodes
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                        self._check_name(n)

        def _check_class_body(self, node):
            """Check a class body. Methods get proper scope; class-level
            code is checked against the enclosing function scope."""
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.visit(item)
                else:
                    for n in ast.walk(item):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            self._check_name(n)

        def visit_FunctionDef(self, node):
            # Push scope for this function
            self.scope_stack.append(set())
            params = set()
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                params.add(arg.arg)
            if node.args.vararg:
                params.add(node.args.vararg.arg)
            if node.args.kwarg:
                params.add(node.args.kwarg.arg)
            self.scope_stack[-1].update(params)
            self._check_body(node.body)
            self.scope_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    checker = FuncChecker()
    checker.visit(tree)
    return errors


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scan Python files for undefined names")
    parser.add_argument("paths", nargs="*", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    paths = args.paths
    if not paths:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = [
            os.path.join(base, "backend", "services", "preview"),
            os.path.join(base, "backend", "services", "youtube_innertube.py"),
            os.path.join(base, "backend", "services", "preview_service.py"),
        ]

    files = []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "**", "*.py"), recursive=True)))

    all_errors = []
    for f in files:
        errors = audit_file(f, verbose=args.verbose)
        all_errors.extend(errors)

    if all_errors:
        # Collapse duplicates
        unique = sorted(set((os.path.relpath(fp), ln, name) for fp, ln, name in all_errors))
        print(f"\n{'='*60}")
        print(f"UNDEFINED NAMES FOUND: {len(unique)}")
        print(f"{'='*60}")
        for fp, ln, name in unique:
            print(f"  {fp}:{ln}  {name}")
        sys.exit(1)
    else:
        print(f"\n{'='*60}")
        print("NO UNDEFINED NAMES FOUND — all clean!")
        print(f"{'='*60}")
        sys.exit(0)


if __name__ == "__main__":
    main()
