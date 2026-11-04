import json
import threading
import time

import pytest

from ehv_agent.config import Config
from ehv_agent.engine import Engine
from ehv_agent.workspace import check_cancel


class FakeRunner:
    """Deterministic provider double; executes actual file and process tools."""

    instances = []
    fail_tests = False
    rework = False
    delay = 0

    def __init__(self, config, events, cancel):
        self.events = events
        self.cancel = cancel
        self.config = config
        self.calls = []
        self.reviews = 0
        self.closed = False
        self.instances.append(self)

    def run(self, role, mode, prompt, tools):
        self.calls.append((role, mode))
        self.events.status(role, "working", mode)
        if self.delay:
            time.sleep(self.delay)
            check_cancel(self.cancel)
        if mode == "plan":
            assert "hello.py" in tools.execute("list_files", {})
            return {
                "session_title": "Update Value and Validate Behavior",
                "steps": [
                    {"role": name, "prompt": f"Assignment for {name}"}
                    for name in ["requirement", "design", "coding", "testing"]
                ],
            }
        if mode == "review":
            assert '"role": "testing"' in prompt
            tools.execute("get_changes", {})
            self.reviews += 1
            return {
                "session_title": "Update Value and Validate Behavior",
                "decision": "rework" if self.rework and self.reviews == 1 else "approve",
                "summary": "Reviewed implementation",
            }
        if mode == "name":
            assert {tool["name"] for tool in tools.definitions(mode)} == {"name_session"}
            return {"session_title": "Project Requirements and Implementation Review"}
        if role == "design":
            assert '"role": "requirement"' in prompt
        if role == "coding":
            tools.execute("write_file", {"path": "hello.py", "content": "VALUE = 2\n"})
        if role == "testing":
            result = tools.execute(
                "run_command",
                {
                    "args": [
                        "python",
                        "-c",
                        "exec(open('hello.py').read()); assert VALUE == 2; print('PASS')",
                    ]
                },
            )
            assert result["exit_code"] == 0
            if self.fail_tests:
                return {"status": "failed", "summary": "Intentional validation failure"}
        return {"status": "passed", "summary": f"{role} completed with evidence"}

    def abort(self):
        self.cancel.set()

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake():
    FakeRunner.instances = []
    FakeRunner.fail_tests = False
    FakeRunner.rework = False
    FakeRunner.delay = 0


def test_full_serial_workflow_has_persisted_handoffs_and_integration(setup):
    project, home, config, events, _, _ = setup
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Change VALUE to 2")
    engine.run_task(task, config)
    assert task["status"] == "completed"
    assert (project / "hello.py").read_text() == "VALUE = 2\n"
    runner = FakeRunner.instances[-1]
    assert runner.calls == [
        ("father", "plan"),
        ("requirement", "work"),
        ("design", "work"),
        ("coding", "work"),
        ("testing", "work"),
        ("father", "review"),
    ]
    assert runner.closed
    handoffs = json.loads((home / "runs" / task["id"] / "handoffs.json").read_text())
    assert [r["role"] for r in handoffs] == ["requirement", "design", "coding", "testing"]
    assert handoffs[-1]["commands"][0]["exit_code"] == 0
    assert "PASS" in handoffs[-1]["commands"][0]["output"]
    assert not list((home / "agents").glob("*/pending/*.json"))
    assert not list((home / "agents").glob("*/running/*.json"))


def test_failed_testing_cannot_integrate_even_if_father_approves(setup):
    project, home, config, events, _, _ = setup
    FakeRunner.fail_tests = True
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Change VALUE")
    engine.run_task(task, config)
    assert task["status"] == "failed"
    assert "did not pass" in task["result"]
    assert (project / "hello.py").read_text() == "VALUE = 1\n"
    assert (home / "runs" / task["id"] / "project/hello.py").read_text() == "VALUE = 2\n"


def test_father_rework_is_serial_and_bounded(setup):
    _, home, config, events, _, _ = setup
    FakeRunner.rework = True
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Change VALUE")
    engine.run_task(task, config)
    assert task["status"] == "completed"
    assert FakeRunner.instances[-1].calls[-4:] == [
        ("father", "review"),
        ("coding", "work"),
        ("testing", "work"),
        ("father", "review"),
    ]


def test_direct_coding_task_gets_testing_before_father_review(setup):
    project, home, config, events, _, _ = setup
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Change VALUE to 2", role="coding")
    engine.run_task(task, config)
    assert task["status"] == "completed"
    assert (project / "hello.py").read_text() == "VALUE = 2\n"
    assert FakeRunner.instances[-1].calls == [
        ("coding", "work"),
        ("testing", "work"),
        ("father", "review"),
    ]


def test_scheduler_pause_cancellation_and_config_reload(setup, tmp_path):
    project, home, config, events, _, _ = setup
    engine = Engine(home, events, FakeRunner)
    engine.paused.set()
    first = engine.enqueue("Change VALUE")
    second = engine.enqueue("Change VALUE again")
    alternate = tmp_path / "other"
    alternate.mkdir()
    (alternate / "hello.py").write_text("VALUE = 1\n")
    config.workspace = str(alternate)
    config.poll_seconds = 0.1
    config.save(home)
    engine.start()
    try:
        time.sleep(0.15)
        assert not FakeRunner.instances
        engine.paused.clear()
        engine.wake.set()
        deadline = time.monotonic() + 8
        while len(FakeRunner.instances) < 2 or engine.active:
            assert time.monotonic() < deadline, "scheduler did not complete two jobs"
            time.sleep(0.05)
        assert all(r.closed for r in FakeRunner.instances)
        assert (project / "hello.py").read_text() == "VALUE = 1\n"
        assert (alternate / "hello.py").read_text() == "VALUE = 2\n"
        assert Config.load(home).workspace == str(alternate.resolve())
        for task in (first, second):
            result = json.loads((home / "agents/father/results" / f"{task['id']}.json").read_text())
            assert result["status"] == "completed"
    finally:
        engine.shutdown()
        engine.thread.join(timeout=5)
    assert not engine.thread.is_alive()


def test_cancel_preserves_project_and_marks_workflow_cancelled(setup):
    project, home, config, events, _, _ = setup
    FakeRunner.delay = 0.2
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Change VALUE")
    worker = threading.Thread(target=engine.run_task, args=(task, config))
    worker.start()
    deadline = time.monotonic() + 4
    while not FakeRunner.instances:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    engine.cancel_active()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert task["status"] == "cancelled"
    assert (project / "hello.py").read_text() == "VALUE = 1\n"
