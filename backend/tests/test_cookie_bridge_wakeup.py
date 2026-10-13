"""Red arms for the cookie-bridge wake-up trade-off (measured 2026-10-13).

TRADE-OFF under test: routers/cookie_bridge.py used to wake the cookie
extension's service worker by opening an off-screen, minimized browser tab,
because "the content script needs the renderer". Measured on the live box:

  * _wake_extension_browser() looked the browser up by PLATFORM name
    ("twitch"/"youtube"/"kick"); _find_browser only resolves BROWSER names
    ("chrome"/"msedge"/"brave") and shutil.which finds nothing for the
    platform names. An instrumented in-process run of the real function
    recorded subprocess.Popen calls == [] (65 chrome processes before, 63
    after — none new), and tmp/vodrip-devall-api.log held 0 wake lines in
    31,721 lines. The window never existed: production cost 0 spawns / 0 MB.
  * Had it run, the cost was measured on a 100%-attributable temporary
    profile with the same argv: 10 processes / 544 MB at t+5s,
    41 processes / 1,962 MB at t+20s, 38 processes / 1,722 MB at t+45s.
    Against the user's RUNNING browser the spawn created 8 processes
    parented to the browser pid — the tab lands in the user's existing
    window, so it is neither off-screen nor invisible.
  * The bridge does not need a renderer: the extension's service worker
    collects cookies itself (chrome.cookies.getAll) and pushes on
    cookies.onChanged, tabs.onUpdated, a 10-minute alarm and a 30-s poll of
    /api/extension/status (the poll is visible every 30s in the API log,
    with the bridge paired and no rousing browser anywhere). The manifest's
    only injected page scripts are the Twitch clip editor, which does not
    even match the platform root URLs the dead code opened.
  * The documented upgrade paths are unavailable on this box: 9222 is not a
    usable CDP endpoint (404 on /json/version and /json/list; the WS
    handshake is rejected 403 by the OMP browser-relay sharing the port),
    and a CDP-capable fresh-profile Chrome carries neither the paired
    cookie extension nor the cookies.

The first four tests below FAIL on the pre-change tree and pass now; they are
the tripwires that keep the mechanism from coming back. Reproduce with:

    cd backend && python -m pytest tests/test_cookie_bridge_wakeup.py -v
"""
import ast
import inspect
import subprocess
from pathlib import Path

import pytest

ROUTER_SRC = (
    Path(__file__).resolve().parents[1] / "routers" / "cookie_bridge.py"
).read_text(encoding="utf-8")

PLATFORM_NAMES = {"twitch", "youtube", "kick"}
WAKE_URLS = ("https://www.twitch.tv/", "https://www.youtube.com/", "https://kick.com/")

# Verbatim claim strings from the removed mechanism. An honest comment never
# has to reproduce them; a reintroduced mechanism almost certainly does.
FORBIDDEN_CLAIMS = (
    "off-screen and started minimized",
    "the content script needs",
    "content script needs it",
    "background tab on",
    "to activate extension SW",
    "SW_SHOWMINNOACTIVE",
    "--window-position",
    "--new-tab",
    *WAKE_URLS,
)


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _platform_loop_vars(tree: ast.AST) -> set:
    """Names iterated over a dict/set literal that carries a platform key."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            if keys & PLATFORM_NAMES:
                out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Set):
            vals = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
            if vals & PLATFORM_NAMES:
                out.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return out


def _platform_loop_key_vars(tree: ast.AST) -> set:
    """Loop variables that hold a platform KEY: the first element of a
    for-loop target over a platform mapping, including its .items() form.
    """
    platform_maps = _platform_loop_vars(tree)
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        map_name = None
        if isinstance(it, ast.Name):
            map_name = it.id
        elif isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr == "items":
            base = it.func.value
            if isinstance(base, ast.Name):
                map_name = base.id
        elif isinstance(it, ast.Attribute) and isinstance(it.value, ast.Name):
            map_name = it.value.id
        if map_name not in platform_maps:
            continue
        tgt = node.target
        if isinstance(tgt, ast.Tuple) and tgt.elts and isinstance(tgt.elts[0], ast.Name):
            out.add(tgt.elts[0].id)
        elif isinstance(tgt, ast.Name):
            out.add(tgt.id)
    return out


# The removed mechanism argv signature, independent of whether the URL is a
# literal or a loop variable.
SPAWN_FLAGS = ("--new-tab", "--window-position")


def test_lookup_is_never_done_by_platform_name():
    """RED ARM (fails pre-change at old line 647): _find_browser() must never
    receive a platform name — it resolves browser names only, so the old
    wake-up loop could not find any browser and never opened anything."""
    tree = ast.parse(ROUTER_SRC)
    offenders = []
    # Scope the walk per function: the old wake-up loop and the extension
    # manager loop both use the variable name "name", and only the wake-up
    # one iterates a platform mapping.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        key_vars = _platform_loop_key_vars(fn)
        if not key_vars:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and _call_name(node) == "_find_browser":
                if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in key_vars:
                    offenders.append(node.lineno)
    assert offenders == [], (
        f"_find_browser() receives a PLATFORM name at line(s) {offenders}; "
        "it resolves chrome/msedge/brave only, so the lookup always fails"
    )


def test_no_browser_spawn_with_a_platform_url():
    """RED ARM (fails pre-change at old lines 660-668): the module must not
    spawn a browser carrying a platform URL — that is the window/tab the
    bridge must never open to rouse the extension."""
    tree = ast.parse(ROUTER_SRC)
    key_vars = _platform_loop_key_vars(tree)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "Popen":
            for arg in node.args:
                if not isinstance(arg, ast.List):
                    continue
                for elt in arg.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and (
                        elt.value in WAKE_URLS
                        or elt.value.startswith(SPAWN_FLAGS)
                        or elt.value == SPAWN_FLAGS[0]
                    ):
                        offenders.append((node.lineno, elt.value))
                    # the URL may arrive as the platform loop variable itself
                    if isinstance(elt, ast.Name) and elt.id in key_vars:
                        offenders.append((node.lineno, elt.id))
    assert offenders == [], (
        f"subprocess.Popen carries a platform URL at {offenders}; the bridge "
        "must not open any browser window/tab"
    )


def test_no_wake_up_claim_survives_in_the_source():
    """RED ARM (fails pre-change): none of the removed mechanism's claim
    strings may survive anywhere in the module, comments included — a doc
    that promises a window the code no longer opens is the same defect as a
    knob that does nothing."""
    found = [c for c in FORBIDDEN_CLAIMS if c in ROUTER_SRC]
    assert found == [], f"stale wake-up claim(s) still in cookie_bridge.py: {found}"


def test_enable_route_does_not_spawn_anything():
    """RED ARM (fails pre-change at old line 935): the enable route's own
    source must contain no Thread/Popen — the extension rouses itself."""
    from routers import cookie_bridge as cb

    src = inspect.getsource(cb.session_cookies_enable)
    assert "Thread(" not in src, "enable route still spawns a thread"
    assert "Popen(" not in src, "enable route still spawns a process"


def test_platform_names_do_not_resolve_to_a_browser():
    """Tripwire on the name-domain confusion itself: a platform name must
    never resolve to a browser path, or a future wake-up would silently
    start opening windows again."""
    from routers import cookie_bridge as cb

    for name in sorted(PLATFORM_NAMES):
        assert cb._find_browser(name) is None, (
            f"_find_browser({name!r}) resolved — platform names must stay "
            "unresolvable (they were the dead wake-up's lookup keys)"
        )


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_enable_route_launches_no_browser(client, monkeypatch):
    """Behavioural GUARD (passes before and after — the old code was already
    a no-op, which is the point): toggling the bridge on must not launch any
    process, recorded at the real subprocess.Popen the router shares."""
    launched = []
    real_popen = subprocess.Popen

    def spy(argv, *a, **k):
        launched.append(list(argv))
        return real_popen.__new__(real_popen)

    monkeypatch.setattr(subprocess, "Popen", spy)
    resp = await client.post("/api/session/cookies/enable")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}
    assert launched == [], f"enable route launched: {launched}"
