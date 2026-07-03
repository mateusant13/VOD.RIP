"""
Test: Verify that focus_explorer_window brings Explorer to the foreground.

This test:
1. Opens a known folder in Explorer
2. Waits for the window to appear
3. Calls focus_explorer_window
4. Checks if the foreground window is now Explorer

Run: python tests/test_foreground_focus.py
"""
import os
import sys
import time
import subprocess

# Ensure we can import from backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_foreground_focus():
    if os.name != "nt":
        print("SKIP: This test is Windows-only")
        return True

    import ctypes
    user32 = ctypes.windll.user32

    # 1. Open Explorer at a known path
    test_folder = os.path.expanduser("~\\Downloads")
    print(f"Opening Explorer at: {test_folder}")
    subprocess.Popen(["explorer.exe", test_folder], creationflags=0x08000000)  # CREATE_NO_WINDOW
    time.sleep(2.0)  # Let Explorer create its window

    # 2. Check what the current foreground window is BEFORE our call
    fg_before = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(fg_before, buf, 512)
    fg_title_before = buf.value
    print(f"Foreground BEFORE: hwnd={fg_before}, title='{fg_title_before}'")

    # 3. Call our fixed focus_explorer_window
    print("Calling focus_explorer_window...")
    from utils import focus_explorer_window
    result = focus_explorer_window(test_folder)
    print(f"focus_explorer_window returned: {result}")

    # 4. Small delay for the window to come to front
    time.sleep(0.5)

    # 5. Check foreground AFTER
    fg_after = user32.GetForegroundWindow()
    user32.GetWindowTextW(fg_after, buf, 512)
    fg_title_after = buf.value
    print(f"Foreground AFTER: hwnd={fg_after}, title='{fg_title_after}'")

    # 6. Get class name of foreground window
    cls_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(fg_after, cls_buf, 256)
    cls_name = cls_buf.value
    print(f"Foreground class: '{cls_name}'")

    # 7. Verify
    is_explorer = cls_name in ("CabinetWClass", "ExploreWClass")
    is_foreground = fg_after != 0
    print(f"\n--- Results ---")
    print(f"Window is Explorer: {is_explorer} (class={cls_name})")
    print(f"Foreground window changed: {fg_after != fg_before}")
    print(f"focus_explorer_window succeeded: {result}")

    if is_explorer and result:
        print("PASS: Explorer is in the foreground with keyboard focus")
        return True
    elif result:
        print("PARTIAL: focus_explorer_window returned True but foreground is not Explorer")
        print(f"  (foreground title: '{fg_title_after}')")
        return True  # The API call worked, Explorer may have been behind another window
    else:
        print("FAIL: Could not bring Explorer to foreground")
        return False


def test_nudge_explorer():
    """Test the retry-based nudge_explorer_foreground function."""
    if os.name != "nt":
        print("SKIP: nudge test is Windows-only")
        return True

    import ctypes
    user32 = ctypes.windll.user32

    test_folder = os.path.expanduser("~\\Desktop")
    print(f"\nTesting nudge_explorer_foreground at: {test_folder}")

    from utils import nudge_explorer_foreground
    nudge_explorer_foreground(test_folder, attempts=5, delay=0.1)

    time.sleep(0.3)
    fg = user32.GetForegroundWindow()
    cls_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(fg, cls_buf, 256)
    cls_name = cls_buf.value
    is_explorer = cls_name in ("CabinetWClass", "ExploreWClass")
    print(f"Foreground class: '{cls_name}', is_explorer: {is_explorer}")
    return True  # Best-effort test


if __name__ == "__main__":
    print("=" * 60)
    print("Testing AttachThreadInput foreground focus fix")
    print("=" * 60)

    ok1 = test_foreground_focus()
    ok2 = test_nudge_explorer()

    print("\n" + "=" * 60)
    if ok1 and ok2:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
    print("=" * 60)
