import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ehv_agent import terminal


def test_console_environment_discards_stale_geometry_and_preserves_credentials():
    original = {"COLUMNS": "240", "LINES": "120", "TERM": "dumb", "ANTHROPIC_API_KEY": "test-key"}
    clean = terminal.console_environment(original)
    assert clean == {"TERM": "xterm-256color", "ANTHROPIC_API_KEY": "test-key"}
    assert original["COLUMNS"] == "240"
    assert terminal.console_environment({"TERM": "xterm"})["TERM"] == "xterm"


def test_viewport_size_uses_visible_rectangle_not_buffer_dimensions():
    class FakeAPI:
        @staticmethod
        def GetConsoleScreenBufferInfo(handle, info):
            value = ctypes.cast(info, ctypes.POINTER(terminal.ScreenBufferInfo)).contents
            value.size = terminal.Coord(240, 9000)
            value.window = terminal.Rect(0, 100, 119, 129)
            return 1

    console = object.__new__(terminal.WindowsConsole)
    console.api = FakeAPI()
    console.handle = 1
    assert console.size() == (120, 30)


def test_console_mode_and_environment_restore_even_on_error(monkeypatch):
    calls = []

    class FakeConsole:
        PROCESSED_OUTPUT = 1
        VIRTUAL_TERMINAL_PROCESSING = 4
        DISABLE_NEWLINE_AUTO_RETURN = 8

        def mode(self):
            return 3

        def set_mode(self, value):
            calls.append(value)
            return True

    monkeypatch.setattr(terminal.sys, "platform", "win32")
    monkeypatch.setattr(terminal, "WindowsConsole", FakeConsole)
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setenv("LINES", "120")
    monkeypatch.setenv("TERM", "dumb")
    with pytest.raises(RuntimeError, match="simulated"):
        with terminal.terminal_session() as console:
            assert isinstance(console, FakeConsole)
            assert "COLUMNS" not in os.environ and "LINES" not in os.environ
            assert os.environ["TERM"] == "xterm-256color"
            assert calls == [15]
            raise RuntimeError("simulated")
    assert calls[-1] == 3
    assert os.environ["COLUMNS"] == "240" and os.environ["LINES"] == "120"
    assert os.environ["TERM"] == "dumb"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows console smoke check")
def test_real_windows_console_does_not_duplicate_prompt_or_footer(setup, monkeypatch):
    _, home, _, _, _, _ = setup
    output = home / "console-probe.json"
    env = os.environ.copy()
    env.update(COLUMNS="240", LINES="120", TERM="dumb", PYTHONUTF8="1")
    env.pop("ANTHROPIC_API_KEY", None)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("windows_console_probe.py")),
            str(home),
            str(output),
        ],
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startup,
    )
    try:
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert output.exists(), "Native console probe did not finish"
    result = json.loads(output.read_text(encoding="utf-8"))
    if result.get("unavailable"):
        pytest.skip("No native console is available in this environment")
    assert result["size"] == result["viewport"]
    assert result["size"] != [240, 120]
    assert result["prompt_count"] == 1, result
    assert result["footer_count"] == 1, result
    assert result["brand_count"] == 1, result
