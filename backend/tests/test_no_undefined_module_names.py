"""Fail if any backend module loads a name it never binds in scope.

WHY THIS EXISTS
---------------
A bare `WINDOW_HLS_MARKER` in routers/preview.py raised NameError on EVERY
/api/preview/hls/<sid>/resource request: sessions were created fine and then every
segment answered 502, so the player spun forever and finally errored (live report
2026-10-11 04:02 local, errors.jsonl http_502 x8). The same shape hid in
services/preview/warm.py three times - the preflight import sat inside a branch that
returns before it matters, so the preflight warm never ran, and the full-mux warm
raised NameError, caught it, and marked the video warm-dead for 6 hours.

None of that is visible to the test suite: the module imports fine, the endpoint
route exists, and the calls are wrapped in `except Exception`. So this test walks the
STACK of scopes (module -> function -> nested function/class) and reports loads that
resolve nowhere, which is exactly what a NameError is.

Annotation semantics are honoured: `from __future__ import annotations` defers every
annotation, and local variable annotations are never evaluated at runtime (PEP 526),
so neither counts as a load.
"""
from __future__ import annotations

import ast
import builtins
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
import ast, builtins, pathlib, sys

LAZY_ANNOTATIONS = False

BASE = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__builtins__",
    "__class__", "__debug__", "WindowsError", "__loader__", "__dict__",
}


def _stores_in_scope(body):
    """Names bound in this scope only: nested def/class bodies are NOT descended."""
    found = set()
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)          # bound here, body is its own scope
            stack.extend(getattr(node, "decorator_list", []))
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                found.add((a.asname or a.name).split(".")[0])
            continue
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            found.update(node.names)
            continue
        if isinstance(node, ast.ExceptHandler) and node.name:
            found.add(node.name)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.add(node.id)
        if isinstance(node, (ast.Lambda,)):
            for a in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
                found.add(a.arg)
            if node.args.vararg: found.add(node.args.vararg.arg)
            if node.args.kwarg: found.add(node.args.kwarg.arg)
        stack.extend(ast.iter_child_nodes(node))
    return found


class Checker(ast.NodeVisitor):
    def __init__(self, path, lazy_annotations=False):
        self.path = path
        self.lazy = lazy_annotations
        self.stack = [set(BASE)]
        self.hits = []

    def _args(self, node):
        out = set()
        for a in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            out.add(a.arg)
        if node.args.vararg: out.add(node.args.vararg.arg)
        if node.args.kwarg: out.add(node.args.kwarg.arg)
        return out

    def _visit_scope(self, node, body, extra):
        self.stack[-1].add(getattr(node, "name", None) or "")
        names = _stores_in_scope(body) | extra
        self.stack.append(names)
        try:
            for child in body:
                self.visit(child)
            for d in getattr(node, "decorator_list", []):
                self.visit(d)
        finally:
            self.stack.pop()

    def visit_FunctionDef(self, node):
        extra = self._args(node)
        if node.returns and not self.lazy: self.visit(node.returns)
        self._visit_scope(node, node.body, extra)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._visit_scope(node, node.body, set())

    def visit_Lambda(self, node):
        self.stack.append(self._args(node))
        try:
            self.visit(node.body)
        finally:
            self.stack.pop()

    def visit_AnnAssign(self, node):
        # Local annotations are never evaluated at runtime (PEP 526); module and
        # class level ones are, unless the module defers them with
        # `from __future__ import annotations`.
        inside_function = len(self.stack) > 2
        if not (self.lazy or inside_function) and node.annotation is not None:
            self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_arg(self, node):
        if node.annotation is not None and not self.lazy:
            self.visit(node.annotation)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id not in self.stack[-1]:
            for scope in reversed(self.stack[:-1]):
                if node.id in scope:
                    return
            self.hits.append((node.lineno, node.id))
        self.generic_visit(node)


def check(path):
    src = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    if "import *" in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    c = Checker(path, lazy_annotations='from __future__ import annotations' in src)
    c.stack.append(_stores_in_scope(tree.body))
    for stmt in tree.body:
        c.visit(stmt)
    return c.hits


def _main():
        targets = sys.argv[1:] or [str(p) for p in sorted(pathlib.Path("backend").rglob("*.py"))
                              if "tests" not in p.parts]
        total = 0
        for t in targets:
            for lineno, name in check(t):
                total += 1
                print(f"{t}: line {lineno}: undefined name {name}")
        print(f"--- {total} undefined-name load(s) across {len(targets)} module(s) ---")

def test_no_backend_module_loads_an_undefined_name():
    """The tree as it stands: every load resolves to a scope or a builtin."""
    modules = [p for p in sorted(BACKEND.rglob("*.py")) if "tests" not in p.parts]
    assert len(modules) > 100, f"only {len(modules)} modules found - wrong root?"
    hits = []
    for path in modules:
        hits.extend((str(path.relative_to(BACKEND)), line, name) for line, name in check(str(path)))
    assert not hits, "undefined name load(s): " + "; ".join(f"{f}:{l} {n}" for f, l, n in hits[:10])


def test_the_checker_flags_an_undefined_name(tmp_path):
    """Falsifiability: a module that loads a name it never binds must be caught."""
    mod = tmp_path / "bad_module.py"
    mod.write_text("def f():\n    return window_hls_marker + 1\n", encoding="utf-8")
    hits = check(str(mod))
    assert [n for _, n in hits] == ["window_hls_marker"], hits


def test_the_checker_accepts_closures_and_deferred_annotations(tmp_path):
    """The false-positive guards: closure reads and lazy annotations are legal."""
    mod = tmp_path / "ok_module.py"
    mod.write_text(
        "from __future__ import annotations\n"
        "\n"
        "def outer():\n"
        "    value: Optional[Path] = None\n"
        "    def inner():\n"
        "        return value\n"
        "    return inner\n"
        "\n"
        "def locals_only():\n"
        "    x: Undefined = 1\n"
        "    return x\n",
        encoding="utf-8",
    )
    assert check(str(mod)) == []
