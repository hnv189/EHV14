from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .terminal import console_environment


def open_cmd_window(home: Path) -> None:
    """Open an interactive console without interpolating paths or secrets into shell code."""
    if sys.platform != "win32":
        raise ValueError("--new-window is supported on Windows; run start in your terminal")
    env = console_environment(dict(os.environ))
    env["EHV_AGENT_PYTHON"] = sys.executable
    env["EHV_AGENT_HOME"] = str(home.resolve())
    env["PYTHONUTF8"] = "1"
    # Values are expanded once inside double quotes. They never appear in the
    # shell command itself, and the API key stays in the inherited environment.
    subprocess.Popen(
        'cmd.exe /d /s /k "title EHV Agent & "%EHV_AGENT_PYTHON%" -m ehv_agent start --home "%EHV_AGENT_HOME%""',
        executable=os.environ.get("COMSPEC", "cmd.exe"),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        cwd=str(Path.cwd()),
        env=env,
    )
