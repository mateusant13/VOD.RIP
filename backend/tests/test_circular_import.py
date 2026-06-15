"""Verify the circular import fix: shutdown_util no longer imports from main."""

import sys
import types


def test_shutdown_util_no_main_import():
    """shutdown_util must not import main (would create circular import cycle)."""
    # Simulate a clean import of shutdown_util before main is loaded
    # by removing any cached references
    for mod in list(sys.modules.keys()):
        if "shutdown_util" in mod or "_app_state" in mod:
            del sys.modules[mod]

    # We only care that the module-level import doesn't try to touch main
    import services.shutdown_util  # noqa: F811

    # The module should be importable without main being in sys.modules
    assert "main" not in sys.modules, (
        "importing shutdown_util pulled in main — circular import still exists!"
    )

    # Verify the function signature — should call cancel_all_downloads helper
    import inspect

    src = inspect.getsource(services.shutdown_util.shutdown_downloads_and_children)
    assert "cancel_all_downloads" in src, (
        "shutdown_downloads_and_children should use cancel_all_downloads, "
        "not import from main"
    )
    assert "from main import" not in src, (
        "shutdown_downloads_and_children still has a from main import!"
    )
