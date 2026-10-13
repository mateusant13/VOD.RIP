"""An async route must never call blocking sqlite directly on the event loop.

Measured 2026-10-13 with py-spy against a live hung API process. The stack was
unambiguous and it is the whole of "o backend sempre cai" + "previews carregam
pra sempre":

    Thread MainThread:
        execute (services/archive_db.py:1742)
        clear_user_focus (services/archive_db.py:3504)
        archive_focus_stamp (routers/archive.py:1280)

while every background thread (boot-maintenance, archive-watchdog,
archive-scheduler, app-activity-hb, entity-watcher, app-activity_0) waited on the
same archive_db.execute line for the write lock. One blocking call on the loop
froze /api/health for the full sqlite busy_timeout, so the supervisor killed an
API that was merely waiting on its own database. archive_focus_stamp fires when
the user focuses a video - i.e. on the preview click path.

This is a LAW, not a snapshot: it fails on the next async route that does it.
A route that offloads via run_in_executor/asyncio.to_thread passes; a nested sync
helper (which is offloadable) does not count.
"""
import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

BLOCKING = ("archive_db.", "cookie_store.")


def _direct_blocking_calls(tree) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    async_stack: list[str] = []
    sync_depth = 0

    class V(ast.NodeVisitor):
        def visit_AsyncFunctionDef(self, node):
            nonlocal sync_depth
            async_stack.append(node.name)
            saved, sync_depth = sync_depth, 0
            self.generic_visit(node)
            sync_depth = saved
            async_stack.pop()

        def visit_FunctionDef(self, node):
            nonlocal sync_depth
            if async_stack:
                sync_depth += 1
                self.generic_visit(node)
                sync_depth -= 1
            else:
                self.generic_visit(node)

        def visit_Lambda(self, node):
            nonlocal sync_depth
            if async_stack:
                sync_depth += 1
                self.generic_visit(node)
                sync_depth -= 1
            else:
                self.generic_visit(node)

        def visit_Call(self, node):
            if async_stack and sync_depth == 0:
                f = node.func
                name = ""
                if isinstance(f, ast.Attribute):
                    base = f.value.id if isinstance(f.value, ast.Name) else ""
                    name = f"{base}.{f.attr}"
                if name.startswith(BLOCKING):
                    hits.append((node.lineno, async_stack[-1], name))
            self.generic_visit(node)

    V().visit(tree)
    return hits


def test_the_focus_route_does_not_block_the_event_loop():
    """RED before the fix: archive_focus_stamp called clear_user_focus directly."""
    src = (BACKEND / "routers" / "archive.py").read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    bad = [h for h in _direct_blocking_calls(tree) if h[1] == "archive_focus_stamp"]
    assert not bad, (
        "archive_focus_stamp calls blocking sqlite on the event loop again: "
        f"{bad} - this freezes /api/health and the supervisor kills the API"
    )


def test_it_still_does_the_work_it_used_to():
    """The control: the fix must not silently drop the focus stamping."""
    src = (BACKEND / "routers" / "archive.py").read_text(encoding="utf-8", errors="replace")
    body = src[src.index("async def archive_focus_stamp"):]
    body = body[: body.find("\nasync def ", 1) if body.find("\nasync def ", 1) != -1 else len(body)]
    for call in ("clear_user_focus", "set_user_focus", "set_job_priority"):
        assert call in body, f"the fix dropped {call}"
    assert "run_in_executor" in body


def test_the_scanner_can_actually_fail():
    """A law that cannot fail is not evidence - prove the detector fires."""
    bad_src = (
        "import archive_db\n"
        "async def route():\n"
        "    archive_db.clear_user_focus()\n"
    )
    hits = _direct_blocking_calls(ast.parse(bad_src))
    assert len(hits) == 1 and hits[0][2] == "archive_db.clear_user_focus"
    good_src = (
        "import archive_db, asyncio\n"
        "async def route():\n"
        "    loop = asyncio.get_running_loop()\n"
        "    await loop.run_in_executor(None, archive_db.clear_user_focus)\n"
    )
    assert _direct_blocking_calls(ast.parse(good_src)) == []
