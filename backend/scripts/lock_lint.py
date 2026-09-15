"""Oracle lint: download-manager lock discipline (07-C5 bug class).

Forbids, structurally:
  N1 nested acquire  - `with self._lock:` inside another `with self._lock:`
                       (the 44009cf self-deadlock mechanism: the worker's Paused
                       finally re-acquired the non-reentrant lock it already held).
  N2 call-in-holder  - a call to a lock-taking helper (_notify_sse, get,
                       get_all, pause, cancel, _has_active_runtime,
                       discard_from_queue, remove_history, _purge_download_runtime)
                       while `_lock` is held (re-entry via a helper = the same
                       deadlock class).
  N3 io-in-holder    - persistence/IO calls (_db.*, os.*, open(), time.sleep,
                       subprocess) while `_lock` is held (manager-wide stall
                       behind disk latency = the deferred JSON-under-lock issue).
                       Reported only (soft): escalating it to hard is the
                       pre-agreed trigger for the full state/persist lock split.

N4 shadowed-handler - an `except` arm for a subclass-or-equal of an EARLIER
                       arm in the same Try (unreachable arm = dead weight;
                       live-exception handling silently bypasses it). Hard.

Exit 0 = no hard violations (N1/N2/N4); exit 1 = hard violations printed.
Runnable on any revision:  python lock_lint.py <path-to-download_manager.py>

N2's shape is a *named-sinks allowlist*, not a call graph: pure AST cannot
resolve which called method actually takes ``_lock`` (that requires an
interprocedural analysis the stdlib does not give us). The allowlist is the
documented invariant — it is (a subset of) the set of DownloadManager methods
that acquire ``self._lock`` and are reachable as re-entry sinks — so adding a
new lock-taking method is a deliberate, lint-visible act. The structural
backstop behind N2 is the single non-reentrant lock: any re-entry self-deadlocks
loudly, and the LockHoldWitness deathlock pins
(test_download_pause_deadlock.py / test_download_cancel_deadlock.py) keep it
observable. Two-hop chains (a holder calling a closure that calls a helper) are
invisible to N2 by design; that is the recorded escalation trigger to the
single-writer actor model.
"""
import ast
import sys

LOCK_ATTR = "_lock"
# Invariant: allowlist of TOP-LEVEL DownloadManager methods that acquire
# self._lock and are reachable as re-entry sinks while the non-reentrant lock
# is held. It is NOT exhaustive of every method containing `with self._lock:`:
# pure AST cannot build a call graph, and the worker closures nested inside
# _spawn_worker (e.g. _download_worker_body, the progress/poller closures)
# lexically nest `with self._lock` bodies inside _spawn_worker's brief hold —
# adding THEIR (nested-def) names would false-positive. Add any new top-level
# lock-taking helper callable from a holder to this set (N2 is a named-sinks
# denylist, documented above). Verified: adding the four below produced zero
# new hard hits at tip.
HELPERS_THAT_TAKE_LOCK = {
    "_notify_sse", "get", "get_all", "get_active_and_history", "pause",
    "cancel", "resume", "_has_active_runtime", "discard_from_queue",
    "remove_history", "_purge_download_runtime", "register_sse",
    "unregister_sse", "_submit", "set_max_workers",
    "_force_stop_download", "start_download", "cancel_all",
    "get_resumable_entry",
}
IO_ATTRS = {"_db"}
IO_FUNCS = {"open", "sleep", "run", "Popen", "check_output"}
IO_MODULES = {"os", "subprocess", "shutil", "time"}


class Visitor(ast.NodeVisitor):
    def __init__(self):
        self.violations = []
        self.depth = 0

    def _is_hold(self, node) -> bool:
        return any(
            isinstance(i.context_expr, ast.Attribute)
            and i.context_expr.attr == LOCK_ATTR
            and isinstance(i.context_expr.value, ast.Name)
            and i.context_expr.value.id == "self"
            for i in node.items
        )

    def visit_With(self, node):
        if self._is_hold(node):
            if self.depth >= 1:
                self.violations.append(("N1 nested acquire", node.lineno))
            self.depth += 1
            for child in node.body:
                self.visit(child)
            self.depth -= 1
        else:
            self.generic_visit(node)

    def visit_Call(self, node):
        if self.depth >= 1:
            f = node.func
            name = None
            if isinstance(f, ast.Attribute):
                name = f.attr
                if isinstance(f.value, ast.Attribute) and f.value.attr in IO_ATTRS:
                    self.violations.append(("N3 IO in holder (_db)", node.lineno))
                elif isinstance(f.value, ast.Name) and f.value.id in IO_MODULES:
                    self.violations.append((f"N3 IO in holder ({f.value.id})", node.lineno))
            elif isinstance(f, ast.Name):
                name = f.id
            receiver_is_self = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            )
            if name in HELPERS_THAT_TAKE_LOCK and receiver_is_self:
                self.violations.append((f"N2 lock-taking call in holder: {name}", node.lineno))
            elif name in IO_FUNCS and isinstance(f, ast.Name):
                self.violations.append((f"N3 IO in holder ({name})", node.lineno))
        self.generic_visit(node)


def _covers(broad: str, narrow: str) -> bool:
    """True when an except arm typing `broad` makes a later arm `narrow` shadowed.

    Degrades to exact-name equality when the names are not importable in this
    process (they usually are not — exception classes live in project modules).
    """
    if broad == narrow:
        return True
    try:
        b, n = eval(broad), eval(narrow)
    except Exception:
        return False
    return (
        isinstance(n, type)
        and isinstance(b, type)
        and issubclass(n, b)
    )


class ShadowedHandler(ast.NodeVisitor):
    """N4: an `except` arm for a subclass-or-equal of an EARLIER arm in the
    same Try is unreachable for try-body exceptions (the 44009cf deferred
    dead `:716` second `except Exception`)."""

    def __init__(self):
        self.violations = []

    def visit_Try(self, node):
        seen = []
        for h in node.handlers:
            if h.type is None:
                tname = "BaseException"
            elif isinstance(h.type, ast.Name):
                tname = h.type.id
            else:
                tname = ast.unparse(h.type)
            for prev in seen:
                if _covers(prev, tname):
                    self.violations.append(
                        ("N4 shadowed handler (%s after %s)" % (tname, prev), h.lineno)
                    )
                    break
            seen.append(tname)
        self.generic_visit(node)


def main(path: str) -> int:
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    v = Visitor()
    v.visit(tree)
    s = ShadowedHandler()
    s.visit(tree)
    v.violations += s.violations
    for kind, line in sorted(v.violations, key=lambda x: x[1]):
        print(f"{path}:{line}: {kind}")
    hard = len([x for x in v.violations if x[0].startswith(("N1", "N2", "N4"))])
    soft = len(v.violations) - hard
    print(f"hard violations (N1+N2+N4): {hard}; soft (N3): {soft}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))