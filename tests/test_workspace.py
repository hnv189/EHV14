import json
import os

import pytest

from ehv_agent.tools import Toolset
from ehv_agent.workspace import Cancelled, inside


def test_stage_excludes_state_secrets_dependencies_and_integrates_files(setup):
    project, home, config, events, cancel, workspace = setup
    (project / ".env").write_text("SECRET=secret")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "huge.js").write_text("ignored")
    workspace.prepare()
    assert set(workspace.baseline) == {"hello.py"}
    tools = Toolset("coding", workspace, home, config, events, cancel)
    tools.tool_replace_text("hello.py", "1", "2")
    tools.tool_write_file("src/new.py", "print('new')\n")
    assert (project / "hello.py").read_text() == "VALUE = 1\n"
    assert len(workspace.changes()) == 2
    assert "hello.py" in tools.tool_get_changes()
    changes = workspace.apply()
    assert {c["action"] for c in changes} == {"modify", "add"}
    assert (project / "hello.py").read_text() == "VALUE = 2\n"
    assert (project / "src/new.py").exists()
    assert (workspace.run / "backups/hello.py").read_text() == "VALUE = 1\n"
    assert json.loads((workspace.run / "integration.json").read_text())["state"] == "applied"


def test_conflict_blocks_entire_integration(setup):
    project, _, _, _, _, workspace = setup
    workspace.prepare()
    (workspace.root / "hello.py").write_text("staged")
    (workspace.root / "new.py").write_text("new")
    (project / "hello.py").write_text("user edit")
    with pytest.raises(RuntimeError, match="conflict"):
        workspace.apply()
    assert not (project / "new.py").exists()
    assert (project / "hello.py").read_text() == "user edit"


def test_cancel_and_failure_roll_back_all_touched_files(setup, monkeypatch):
    project, _, _, _, cancel, workspace = setup
    workspace.prepare()
    (workspace.root / "hello.py").write_text("staged")
    (workspace.root / "new.py").write_text("new")
    original = workspace.atomic_copy
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated failure")
        original(source, destination)

    monkeypatch.setattr(workspace, "atomic_copy", fail_second)
    with pytest.raises(OSError, match="simulated"):
        workspace.apply()
    assert (project / "hello.py").read_text() == "VALUE = 1\n"
    assert not (project / "new.py").exists()
    assert json.loads((workspace.run / "integration.json").read_text())["state"] == "rolled_back"
    cancel.set()
    with pytest.raises(Cancelled):
        workspace.apply()


def test_deletion_and_path_boundaries(setup):
    project, home, config, events, cancel, workspace = setup
    workspace.prepare()
    tools = Toolset("coding", workspace, home, config, events, cancel)
    with pytest.raises(ValueError, match="inside"):
        tools.tool_write_file("../escape.txt", "bad")
    with pytest.raises(ValueError, match="excluded"):
        tools.tool_write_file(".env", "bad")
    with pytest.raises(ValueError, match="inside"):
        inside(workspace.root, str(project / "hello.py"))
    tools.tool_delete_file("hello.py")
    assert workspace.changes()[0]["action"] == "delete"
    workspace.apply()
    assert not (project / "hello.py").exists()


def test_stage_rejects_links_created_by_commands(setup):
    _, _, _, _, _, workspace = setup
    workspace.prepare()
    try:
        os.symlink(workspace.root / "hello.py", workspace.root / "link.py")
    except OSError:
        pytest.skip("Creating symlinks needs Windows developer mode")
    with pytest.raises(ValueError, match="symbolic link"):
        workspace.changes()
