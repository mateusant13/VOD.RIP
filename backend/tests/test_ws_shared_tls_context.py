"""The WebSocket sink must not build a TLS context per connection.

ssl.create_default_context() enumerates the ENTIRE Windows certificate store
while holding the GIL. On this box it stalled the event loop for seconds:

  rep fast   1.98s -> /api/health max  0.352s,  0 probes over 0.5 s
  rep slow  20.82s -> /api/health max  2.924s, 10 probes over 0.5 s

measured by probing /api/health every 20 ms while a preview was clicked in a
real browser, with py-spy catching the archive-scheduler thread inside this
sink (twitch_gql_service._gql_request -> read (ssl.py:1166)).

Same defect already fixed for services/http_opener.py and
services/twitch_gql_service.py - the guard existed, this call site did not.
"""
import ast
import inspect

from services.chat_sinks import _ws


def test_no_method_builds_a_tls_context():
    src = inspect.getsource(_ws.WSClient)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)
            assert name != "create_default_context"


def test_the_shared_context_exists_at_module_scope():
    ctx = getattr(_ws, "_TLS_CONTEXT", None)
    assert ctx is not None and isinstance(ctx, _ws.ssl.SSLContext)


def test_connect_uses_the_shared_context():
    src = inspect.getsource(_ws.WSClient.connect)
    assert "_TLS_CONTEXT.wrap_socket" in src


def test_the_client_still_builds():
    c = _ws.WSClient("wss://example.invalid/ws")
    assert c._tls is True
    c2 = _ws.WSClient("ws://example.invalid/ws")
    assert c2._tls is False
