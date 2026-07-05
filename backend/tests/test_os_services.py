import sys

from services.os_services import _wsl_windows_path, sanitize_filename_component


def test_wsl_windows_path():
    assert _wsl_windows_path("/mnt/c/Users/foo/bar.mp4") == "C:\\Users\\foo\\bar.mp4"
    assert _wsl_windows_path("/home/user/x") is None


def test_sanitize_forbidden_chars_platform_aware():
    if sys.platform == "win32":
        assert sanitize_filename_component('bad<>name') == "bad__name"
