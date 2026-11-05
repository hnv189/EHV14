import json
import threading
import time

import pytest
from test_engine import FakeRunner

from ehv_agent.config import atomic_json
from ehv_agent.engine import Engine
from ehv_agent.provider import ClaudeRunner
from ehv_agent.sessions import SessionStore, validate_session_title
from ehv_agent.store import TaskStore


def completed_task(home, prompt="Add CSV export", **extra):
    store = TaskStore(home)
    task = store.submit(prompt, **extra)
    store.claim(task)
    store.finish(task, "completed", "CSV export implemented and validated")
    return task


@pytest.mark.parametrize(
    "title",
    [
        None,
        "Untitled",
        "New session",
        "Task deadbeef",
        "## Add CSV export",
        "Add\nCSV export",
        "x" * 81,
    ],
)
def test_session_names_must_be_specific_plain_text(title):
    with pytest.raises(ValueError):
        validate_session_title(title)
    assert (
        validate_session_title("  CSV   Report Export and Validation  ")
        == "CSV Report Export and Validation"
    )


def test_history_includes_roots_across_workspaces_and_retains_father_titles(setup, tmp_path):
    _, home, _, _, _, _ = setup
    store = TaskStore(home)
    first = completed_task(home, workspace=str(tmp_path / "first"))
    store.submit("child", "coding", parent_id=first["id"])
    pending = store.submit(
        "Another request",
        workspace=str(tmp_path / "second"),
        session_title="An invented client title",
        title_source="father",
    )
    sessions = SessionStore(home)
    sessions.set_title(first["id"], "CSV Report Export and Validation", "father-model")
    reopened = SessionStore(home)
    recent = reopened.recent()
    assert {session.id for session in recent} == {first["id"], pending["id"]}
    named = reopened.get(first["id"])
    assert named.title == "CSV Report Export and Validation"
    assert named.title_model == "father-model"
    assert named.workspace == str(tmp_path / "first")
    assert reopened.get(pending["id"]).display_title == "Awaiting father title"
    assert len(reopened.recent(limit=1)) == 1
    with pytest.raises(ValueError):
        reopened.report("../escape")


def test_session_report_shows_saved_results_and_handles_malformed_history(setup):
    _, home, _, _, _, _ = setup
    task = completed_task(home)
    sessions = SessionStore(home)
    sessions.set_title(task["id"], "CSV Report Export and Validation", "father-model")
    run = sessions.run_path(task["id"])
    atomic_json(
        run / "handoffs.json",
        [
            {
                "role": "testing",
                "status": "passed",
                "summary": "Empty exports verified",
                "commands": [{"args": ["python", "-m", "pytest"], "exit_code": 0}],
            }
        ],
    )
    atomic_json(run / "outcome.json", {"changes": [{"path": "src/export.py", "action": "add"}]})
    report = sessions.report(task["id"])
    assert "CSV Report Export and Validation" in report and "father-model" in report
    assert "Empty exports verified" in report and "exit 0" in report
    assert "src/export.py" in report
    (home / "agents/father/results/broken.json").write_text("{")
    atomic_json(
        home / "agents/father/pending/invalid-fields.json",
        {"prompt": "Pending", "created_at": 5, "status": {}, "result": []},
    )
    atomic_json(
        run / "handoffs.json", [{"role": None, "summary": [], "commands": [{"args": None}]}]
    )
    assert len(sessions.recent()) == 2
    assert "## Request" in sessions.report(task["id"])


def test_name_is_persisted_while_specialists_are_still_running(setup):
    _, home, config, events, _, _ = setup
    started = threading.Event()
    release = threading.Event()

    class PausingRunner(FakeRunner):
        def run(self, role, mode, prompt, tools):
            if role == "requirement":
                started.set()
                release.wait(timeout=4)
            return super().run(role, mode, prompt, tools)

    engine = Engine(home, events, PausingRunner)
    task = engine.enqueue("Update the value")
    worker = threading.Thread(target=engine.run_task, args=(task, config))
    worker.start()
    try:
        assert started.wait(timeout=3)
        session = SessionStore(home).get(task["id"])
        assert session.status == "running"
        assert session.title == "Update Value and Validate Behavior"
        assert (
            json.loads((home / "runs" / task["id"] / "request.json").read_text())["title_source"]
            == "father"
        )
    finally:
        release.set()
        worker.join(timeout=5)
    assert task["status"] == "completed"


def test_idle_naming_runs_after_project_work_and_does_not_stage_or_rerun_legacy_session(setup):
    _, home, config, events, _, _ = setup
    old = completed_task(home, prompt="Review project structure")
    config.poll_seconds = 0.1
    config.save(home)
    calls = []

    class OrderedRunner(FakeRunner):
        def run(self, role, mode, prompt, tools):
            calls.append((role, mode))
            return super().run(role, mode, prompt, tools)

    engine = Engine(home, events, OrderedRunner)
    current = engine.enqueue("Update value")
    engine.start()
    try:
        deadline = time.monotonic() + 5
        while not SessionStore(home).get(old["id"]).title or engine.active:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert calls[-1] == ("father", "name")
        assert calls.index(("father", "review")) < calls.index(("father", "name"))
        assert SessionStore(home).get(current["id"]).title == "Update Value and Validate Behavior"
        assert not (home / "runs" / old["id"] / "project").exists()
        assert SessionStore(home).get(old["id"]).summary == "CSV export implemented and validated"
    finally:
        engine.shutdown()
        engine.thread.join(timeout=5)


def test_failed_legacy_naming_is_not_repeated_until_explicit_retry(setup):
    _, home, config, events, _, _ = setup
    old = completed_task(home)
    config.poll_seconds = 0.1
    config.save(home)
    attempts = []

    class FailingRunner(FakeRunner):
        def run(self, role, mode, prompt, tools):
            attempts.append(mode)
            raise ValueError("Temporary model issue")

    engine = Engine(home, events, FailingRunner)
    engine.start()
    try:
        deadline = time.monotonic() + 4
        while SessionStore(home).get(old["id"]).title_state != "error":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(0.3)
        assert attempts == ["name"]
        assert SessionStore(home).get(old["id"]).title == ""
        engine.request_session_name(old["id"])
        while len(attempts) != 2:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(0.2)
        assert attempts == ["name", "name"]
    finally:
        engine.shutdown()
        engine.thread.join(timeout=5)


def test_father_terminal_schema_requires_a_session_title():
    with pytest.raises(ValueError, match="title"):
        ClaudeRunner.validate_terminal(
            "review_result", {"decision": "approve", "summary": "Passed"}
        )
    with pytest.raises(ValueError, match="title"):
        ClaudeRunner.validate_terminal("name_session", {"session_title": "# Generic"})
