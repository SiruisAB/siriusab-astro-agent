"""Lightweight, windowed launch geometry (no GUI framework imports)."""

import os


DEFAULT_WORK_AREA = (0, 0, 1366, 768)


def window_bounds(work_area):
    """Return centered (x, y, width, height) in desktop logical coordinates.

    Never impose a minimum larger than a small screen. Cap large-screen windows
    at a comfortable reading size, and leave visible desktop around all edges.
    The work area, unlike the full monitor rectangle, excludes the taskbar.
    """
    left, top, right, bottom = work_area
    available_width, available_height = right - left, bottom - top
    if available_width <= 0 or available_height <= 0:
        raise ValueError("Screen work area must have positive dimensions")
    width = min(1440, max(1, round(available_width * 0.88)))
    height = min(960, max(1, round(available_height * 0.86)))
    return (left + (available_width - width) // 2,
            top + (available_height - height) // 2, width, height)


def _windows_work_area():
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    user32.SystemParametersInfoW.restype = wintypes.BOOL

    # Edge's geometry switches use logical pixels. Query in that coordinate
    # space; changing only this thread avoids altering the server's DPI mode.
    set_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous = None
    if set_context is not None:
        set_context.argtypes = [wintypes.HANDLE]
        set_context.restype = wintypes.HANDLE
        previous = set_context(-1)  # DPI_AWARENESS_CONTEXT_UNAWARE
    try:
        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        monitor = user32.MonitorFromPoint(cursor, 2)  # MONITOR_DEFAULTTONEAREST
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            area = info.rcWork
        else:
            area = wintypes.RECT()
            if not user32.SystemParametersInfoW(48, 0, ctypes.byref(area), 0):
                raise OSError("Cannot query desktop work area")
        return area.left, area.top, area.right, area.bottom
    finally:
        if previous:
            set_context(previous)


def launch_bounds():
    """Prefer the cursor's monitor; use conservative bounds if detection fails."""
    if os.name == "nt":
        try:
            return window_bounds(_windows_work_area())
        except (OSError, AttributeError, ValueError):
            pass
    return window_bounds(DEFAULT_WORK_AREA)
