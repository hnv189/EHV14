import threading
import time

import pytest

from ehv_agent.tools import Toolset
from ehv_agent.workspace import Cancelled


def make_tools(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    return Toolset("testing", workspace, home, config, events, cancel)


def test_commands_stream_output_and_do_not_inherit_api_key(setup, monkeypatch):
    tools = make_tools(setup)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-print-this")
    result = tools.tool_run_command(
        ["python", "-c", "import os; print('hello'); print('ANTHROPIC_API_KEY' in os.environ)"]
    )
    assert result["exit_code"] == 0
    assert "hello" in result["output"]
    assert "False" in result["output"]
    assert "never-print-this" not in result["output"]
    assert any(e["kind"] == "output" for e in list(tools.events.queue.queue))


def test_commands_cancel_and_timeout(setup):
    tools = make_tools(setup)
    start = time.monotonic()
    result = tools.tool_run_command(["python", "-c", "import time; time.sleep(30)"], timeout=1)
    assert result["timed_out"]
    assert time.monotonic() - start < 6
    timer = threading.Timer(0.2, tools.cancel.set)
    timer.start()
    try:
        with pytest.raises(Cancelled):
            tools.tool_run_command(["python", "-c", "import time; time.sleep(30)"])
    finally:
        timer.join()


def test_resource_isolation_and_role_tools(setup):
    tools = make_tools(setup)
    tools.tool_remember("lessons.md", "Use project conventions")
    assert "project conventions" in tools.tool_read_resource("knowledge", "lessons.md")
    with pytest.raises(ValueError):
        tools.tool_read_resource("knowledge", "../../coding/skills/ROLE.md")
    with pytest.raises(ValueError):
        tools.tool_run_command(["powershell", "-Command", "echo bad"])
    tools.role = "design"
    names = {t["name"] for t in tools.definitions("work")}
    assert "write_file" not in names and "run_command" not in names
    assert "complete_task" in names


def test_tools_search_and_unique_replacements(setup):
    tools = make_tools(setup)
    assert "hello.py:1:" in tools.tool_search("VALUE")
    assert "1: VALUE" in tools.tool_read_file("hello.py")
    with pytest.raises(ValueError, match="exactly once"):
        tools.tool_replace_text("hello.py", "missing", "oops")
    with pytest.raises(ValueError):
        tools.tool_run_command(["python", "-c", "print('x')"], cwd="../../outside")
