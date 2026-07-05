"""Path-based Explorer HWND lookup for Open in Folder focus."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import (
    _explorer_hwnds_for_folder,
    _folders_equivalent,
    _normalize_folder_path,
    _pick_topmost_hwnd,
    focus_explorer_window,
)


def test_normalize_custom_paths():
    if os.name != "nt":
        return
    assert _normalize_folder_path("D:\\VODs\\") == _normalize_folder_path("d:/vods")
    assert _folders_equivalent("D:\\VODs", "d:/vods/")
    with tempfile.TemporaryDirectory(prefix="vodrip_test_", dir=os.path.expanduser("~")) as td:
        norm = _normalize_folder_path(td)
        assert _folders_equivalent(td, norm)
        assert _folders_equivalent(td + "\\", norm)

def test_pick_topmost_empty():
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    assert _pick_topmost_hwnd(user32, ctypes, wintypes, []) == 0


def test_hwnd_lookup_matches_path_not_title():
    if os.name != "nt":
        return
    folder = os.path.abspath(os.path.expanduser("~\\Downloads"))
    hwnds = _explorer_hwnds_for_folder(folder)
    # ponytail: integration — requires an open Downloads window; skip if none
    if not hwnds:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    picked = _pick_topmost_hwnd(user32, ctypes, wintypes, hwnds)
    assert picked in hwnds


def test_focus_returns_false_for_missing_folder():
    if os.name != "nt":
        return
    bogus = os.path.abspath("C:\\__vodrip_no_such_folder_xyz__")
    assert focus_explorer_window(bogus) is False


if __name__ == "__main__":
    test_pick_topmost_empty()
    test_hwnd_lookup_matches_path_not_title()
    test_focus_returns_false_for_missing_folder()
    print("ok")
