import subprocess
import sys

import pytest

from ehv_agent import cli, launcher
from ehv_agent.config import initialize


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console launcher")
def test_new_window_inherits_credentials_without_putting_them_in_shell_text(setup, monkeypatch):
    _, home, _, _, _, _ = setup
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setenv("LINES", "120")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    assert cli.main(["start", "--new-window", "--home", str(home)]) == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "test-secret" not in args[0]
    assert "--new-window" not in args[0]  # The child cannot recursively launch itself.
    assert "mode con" not in args[0]
    assert kwargs["creationflags"] == subprocess.CREATE_NEW_CONSOLE
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == "test-secret"
    assert kwargs["env"]["EHV_AGENT_HOME"] == str(home.resolve())
    assert kwargs["env"]["EHV_AGENT_PYTHON"] == sys.executable
    assert "COLUMNS" not in kwargs["env"] and "LINES" not in kwargs["env"]
    assert kwargs["env"]["TERM"] == "xterm-256color"


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows shell quoting")
def test_cmd_command_handles_spaces_ampersands_and_percent_in_paths(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "team & (demo) 100% %EHV_NOT_DEFINED%"
    initialize(home, project)
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
        launcher.open_cmd_window(home)
    args, kwargs = calls[0]
    # Exercise the exact launch quoting through a hidden cmd process, running
    # doctor instead of the dashboard so this test never opens a window/API call.
    command = (
        args[0].replace(" /k ", " /c ").replace(" -m ehv_agent start ", " -m ehv_agent doctor ")
    )
    completed = subprocess.run(
        command,
        executable=kwargs["executable"],
        cwd=kwargs["cwd"],
        env=kwargs["env"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert str(home.resolve()) in completed.stdout
    assert "Config valid" in completed.stdout


def test_non_windows_new_window_reports_clear_error(setup, monkeypatch):
    _, home, _, _, _, _ = setup
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    with pytest.raises(ValueError, match="Windows"):
        launcher.open_cmd_window(home)


def test_new_window_cannot_be_combined_with_headless():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["start", "--new-window", "--headless"])
