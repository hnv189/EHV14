import threading

import pytest

from ehv_agent.config import initialize
from ehv_agent.events import Events
from ehv_agent.workspace import Workspace


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
    home = project / ".ehv-agent"
    config = initialize(home, project)
    cancel = threading.Event()
    events = Events(home)
    workspace = Workspace(
        project, home / "runs" / "test", home, config.excludes, cancel, lambda _: None
    )
    return project, home, config, events, cancel, workspace
