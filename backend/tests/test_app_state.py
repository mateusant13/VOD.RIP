"""Test the shared app state module (_app_state.py)."""

from services._app_state import (
    set_download_manager,
    get_download_manager,
    cancel_all_downloads,
)


def test_get_set_download_manager():
    """get_download_manager returns None before set, then the object after."""
    assert get_download_manager() is None, "should be None before initialisation"
    # Set to a sentinel object so we can confirm the reference is stored
    sentinel = object()
    set_download_manager(sentinel)  # type: ignore[arg-type]
    assert get_download_manager() is sentinel, "should return the object we set"
    # Clean up for subsequent tests
    set_download_manager(None)  # type: ignore[arg-type]


def test_cancel_all_downloads_safe():
    """cancel_all_downloads is a no-op (returns 0) when no manager is set."""
    set_download_manager(None)  # type: ignore[arg-type]
    result = cancel_all_downloads()
    assert result == 0, "should return 0 when no manager is registered"
