"""Raises the current process's OS scheduling priority a notch, so the
audio callback thread is less likely to get starved of CPU by some other
app at the exact moment it needs to send/receive a frame — a common cause
of audio dropouts that has nothing to do with the network at all."""

import ctypes
import logging
import sys

log = logging.getLogger("priority")

ABOVE_NORMAL_PRIORITY_CLASS = 0x8000


def raise_process_priority():
    if sys.platform != "win32":
        return
    try:
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32
        # GetCurrentProcess() returns a pointer-sized pseudo-handle;
        # ctypes' default 32-bit int restype/argtypes truncate it on
        # 64-bit Windows, so SetPriorityClass then sees garbage and fails
        # with "the handle is invalid" — declare the real signatures.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS):
            raise ctypes.WinError()
    except OSError:
        log.warning("не успях да вдигна приоритета на процеса", exc_info=True)


if __name__ == "__main__":
    raise_process_priority()  # must not raise even off-Windows/without privileges
    if sys.platform == "win32":
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        assert kernel32.GetPriorityClass(kernel32.GetCurrentProcess()) == ABOVE_NORMAL_PRIORITY_CLASS
    print("priority.py: ok")
