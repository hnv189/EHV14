from __future__ import annotations

import ctypes
import os
import sys
from contextlib import contextmanager
from ctypes import wintypes


class Coord(ctypes.Structure):
    _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.SHORT),
        ("top", wintypes.SHORT),
        ("right", wintypes.SHORT),
        ("bottom", wintypes.SHORT),
    ]


class ScreenBufferInfo(ctypes.Structure):
    _fields_ = [
        ("size", Coord),
        ("cursor", Coord),
        ("attributes", wintypes.WORD),
        ("window", Rect),
        ("maximum_size", Coord),
    ]


class WindowsConsole:
    """Read the visible viewport, which can differ from the scrollback buffer."""

    PROCESSED_OUTPUT = 0x0001
    VIRTUAL_TERMINAL_PROCESSING = 0x0004
    DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.GetStdHandle.argtypes = [wintypes.DWORD]
        self.api.GetStdHandle.restype = wintypes.HANDLE
        self.api.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetConsoleMode.restype = wintypes.BOOL
        self.api.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.SetConsoleMode.restype = wintypes.BOOL
        self.api.GetConsoleScreenBufferInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ScreenBufferInfo),
        ]
        self.api.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
        self.handle = self.api.GetStdHandle(wintypes.DWORD(-11))

    def mode(self) -> int | None:
        mode = wintypes.DWORD()
        return mode.value if self.api.GetConsoleMode(self.handle, ctypes.byref(mode)) else None

    def set_mode(self, mode: int) -> bool:
        return bool(self.api.SetConsoleMode(self.handle, mode))

    def size(self) -> tuple[int, int] | None:
        info = ScreenBufferInfo()
        if not self.api.GetConsoleScreenBufferInfo(self.handle, ctypes.byref(info)):
            return None
        width = info.window.right - info.window.left + 1
        height = info.window.bottom - info.window.top + 1
        return (width, height) if width > 0 and height > 0 else None


def console_environment(environ: dict[str, str]) -> dict[str, str]:
    """Do not carry an old terminal's geometry into a different console."""
    result = dict(environ)
    result.pop("COLUMNS", None)
    result.pop("LINES", None)
    if result.get("TERM", "").lower() in {"", "dumb", "unknown"}:
        result["TERM"] = "xterm-256color"
    return result


@contextmanager
def terminal_session():
    if sys.platform != "win32":
        yield None
        return
    previous = {key: os.environ.get(key) for key in ("COLUMNS", "LINES", "TERM")}
    prepared = console_environment(dict(os.environ))
    for key in previous:
        if key in prepared:
            os.environ[key] = prepared[key]
        else:
            os.environ.pop(key, None)
    console = WindowsConsole()
    original_mode = console.mode()
    try:
        if original_mode is not None:
            # Avoid scrolling when the renderer writes the bottom-right cell.
            # Textual preserves this mode while enabling its own VT handling.
            requested = (
                original_mode
                | console.PROCESSED_OUTPUT
                | console.VIRTUAL_TERMINAL_PROCESSING
                | console.DISABLE_NEWLINE_AUTO_RETURN
            )
            if not console.set_mode(requested):
                requested &= ~console.DISABLE_NEWLINE_AUTO_RETURN
                if not console.set_mode(requested):
                    raise ValueError(
                        "This console cannot enable virtual-terminal output; use Windows Terminal or start --headless"
                    )
        yield console if original_mode is not None else None
    finally:
        if original_mode is not None:
            console.set_mode(original_mode)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
