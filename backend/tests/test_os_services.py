import sys

from services.os_services import _wsl_windows_path, sanitize_filename_component


def test_wsl_windows_path():
    assert _wsl_windows_path("/mnt/c/Users/foo/bar.mp4") == "C:\\Users\\foo\\bar.mp4"
    assert _wsl_windows_path("/home/user/x") is None


def test_sanitize_forbidden_chars_platform_aware():
    if sys.platform == "win32":
        assert sanitize_filename_component('bad<>name') == "bad__name"


def test_virtual_display_adapter_is_filtered():
    from services.os_services import _is_virtual_display_adapter

    assert _is_virtual_display_adapter("Virtual Display Driver")
    assert _is_virtual_display_adapter("Microsoft Basic Display Adapter")
    assert not _is_virtual_display_adapter("NVIDIA GeForce RTX 5080")
    assert not _is_virtual_display_adapter("Intel(R) UHD Graphics 770")


def test_list_gpu_names_puts_nvidia_first_and_drops_virtual(monkeypatch):
    from services import os_services as oss

    monkeypatch.setattr(oss, "is_windows", lambda: True)
    monkeypatch.setattr(oss, "is_macos", lambda: False)
    monkeypatch.setattr(oss, "_gpu_names_windows", lambda: [
        "Virtual Display Driver",
        "NVIDIA GeForce RTX 5080",
        "Intel(R) UHD Graphics 770",
    ])
    monkeypatch.setattr(oss, "_gpu_names_nvidia_smi", lambda: ["NVIDIA GeForce RTX 5080"])
    names = oss.list_gpu_names()
    assert names[0] == "NVIDIA GeForce RTX 5080"
    assert "Virtual Display Driver" not in names
    assert "Intel(R) UHD Graphics 770" in names
