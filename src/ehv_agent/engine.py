from __future__ import annotations

import json
import os
import queue
import threading
from copy import deepcopy
from pathlib import Path

from .config import ROLES, Config, atomic_json
from .events import Events, timestamp
from .provider import ClaudeRunner
from .sessions import Session, SessionStore
from .store import TaskStore
from .tools import Toolset
from .workspace import Cancelled, Workspace, check_cancel


class Engine:
    """One scheduler thread; exactly one agent/model executes at a time."""

    def __init__(self, home: Path, events: Events, runner_factory=ClaudeRunner):
        self.home = home
        self.events = events
        self.store = TaskStore(home)
        self.sessions = SessionStore(home)
        self._name_requests: queue.Queue[str] = queue.Queue()
        self._naming_attempted: set[str] = set()
        self.runner_factory = runner_factory
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.wake = threading.Event()
        self.paused = threading.Event()
        self.thread: threading.Thread | None = None
        self.runner = None
        self.active: str | None = None
        self.last_error = ""

    def start(self) -> None:
        recovered = self.store.recover()
        if recovered:
            self.events.emit(
                "father",
                "info",
                f"Recovered {recovered} interrupted tasks; use /retry <parent-id> after review",
            )
        self.thread = threading.Thread(target=self._loop, name="ehv-serial-scheduler", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()
        self.cancel_active()
        self.wake.set()

    def cancel_active(self) -> None:
        self.cancel.set()
        if self.runner is not None:
            self.runner.abort()

    def enqueue(self, prompt: str, role: str = "father") -> dict:
        task = self.store.submit(prompt, role)
        self.events.emit(role, "queued", f"Queued {task['id']}: {prompt}")
        self.wake.set()
        return task

    def _loop(self) -> None:
        for role in ROLES:
            self.events.status(role, "idle", "Watching pending tasks")
        while not self.stop.is_set():
            interval = 1.0
            try:
                config = Config.load(self.home)
                interval = config.poll_seconds
                if not self.paused.is_set():
                    task = self.store.next_pending()
                    if task:
                        # Keep missing credentials from consuming pending tasks.
                        if (
                            not os.environ.get("ANTHROPIC_API_KEY")
                            and self.runner_factory is ClaudeRunner
                        ):
                            raise ValueError(
                                "Set ANTHROPIC_API_KEY before starting. Pending tasks are preserved."
                            )
                        self.last_error = ""
                        self.run_task(task, config)
                        continue
                    session = self._next_unnamed_session()
                    if session and (os.environ.get("ANTHROPIC_API_KEY") or self.runner_factory is not ClaudeRunner):
                        self._name_session(session, config)
                        continue
                self.last_error = ""
            except Exception as error:
                message = str(error)
                if message != self.last_error:
                    self.events.status("father", "error", message)
                    self.last_error = message
            self.wake.wait(interval)
            self.wake.clear()

    def _tools(self, role: str, workspace: Workspace, config: Config) -> Toolset:
        return Toolset(role, workspace, self.home, config, self.events, self.cancel)

    def request_session_name(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session.title:
            return
        if session.status in {"pending", "running"}:
            raise ValueError("The father names active sessions during planning or review")
        self.sessions.name_state(session_id, "queued")
        self._name_requests.put(session_id)
        self.events.emit("father", "session_naming", "Session title queued for the father", task_id=session_id)
        self.wake.set()

    def _next_unnamed_session(self) -> Session | None:
        while not self._name_requests.empty():
            session_id = self._name_requests.get_nowait()
            try:
                session = self.sessions.get(session_id)
                if not session.title and session.status not in {"pending", "running"}:
                    return session
            except ValueError:
                continue
        return next((session for session in self.sessions.recent()
                     if not session.title and session.status not in {"pending", "running"}
                     and session.id not in self._naming_attempted), None)

    def _name_session(self, session: Session, config: Config) -> None:
        """Idle-only father call; shares the serial scheduler with project work."""
        self._naming_attempted.add(session.id)
        self.cancel.clear()
        self.active = session.id
        self.sessions.name_state(session.id, "naming")
        self.events.status("father", "working", "Naming a recent session from its saved request and results")
        try:
            naming_config = deepcopy(config)
            naming_config.max_turns = min(config.max_turns, 3)
            naming_config.max_tokens = min(config.max_tokens, 512)
            self.runner = self.runner_factory(naming_config, self.events, self.cancel)
            # Naming exposes only a terminal title tool, never project tools.
            workspace = Workspace(Path(session.workspace or config.workspace), self.sessions.run_path(session.id), self.home, config.excludes, self.cancel, lambda _: None)
            prompt = "Give this saved session a concise, descriptive title for Recent Sessions. Call name_session directly. Do not rerun any work or add commentary.\n" + json.dumps({"request": session.prompt[:16000], "result": session.summary[:16000], "status": session.status, "workspace": session.workspace}, ensure_ascii=False)
            result = self.runner.run("father", "name", prompt, self._tools("father", workspace, naming_config))
            title = self.sessions.set_title(session.id, result["session_title"], config.model_for("father"))
            self.events.emit("father", "session_named", title, task_id=session.id, session_title=title)
        except Exception as error:
            self.sessions.name_state(session.id, "error", str(error))
            self.events.emit("father", "session_name_error", f"Could not name session {session.id}: {error}", task_id=session.id)
        finally:
            if self.runner:
                self.runner.close()
                self.runner = None
            self.active = None
            self.cancel.clear()
            self.events.status("father", "idle", "Watching pending tasks")

    def _save_session_title(self, task: dict, title: str, config: Config) -> None:
        title = self.sessions.set_title(task["id"], title, config.model_for("father"))
        task["session_title"] = title
        task["title_source"] = "father"
        atomic_json(self.sessions.run_path(task["id"]) / "request.json", task)
        self.events.emit("father", "session_named", title, task_id=task["id"], session_title=title)

    def _worker(self, task: dict, workspace: Workspace, config: Config, context: str) -> dict:
        self.store.claim(task)
        tools = self._tools(task["role"], workspace, config)
        try:
            result = self.runner.run(
                task["role"], "work", context + "\n\nYour assignment:\n" + task["prompt"], tools
            )
            result["commands"] = tools.commands
            self.store.finish(
                task,
                "completed" if result["status"] == "passed" else result["status"],
                result["summary"],
                outcome=result,
            )
            self.events.status(
                task["role"], "done" if result["status"] == "passed" else "error", result["summary"]
            )
            return {"role": task["role"], **result}
        except BaseException as error:
            self.store.finish(task, "cancelled" if self.cancel.is_set() else "failed", str(error))
            raise

    def run_task(self, task: dict, config: Config) -> None:
        self.cancel.clear()
        self.active = task["id"]
        self.store.claim(task)
        run = self.home / "runs" / task["id"]
        workspace = None
        children = []
        results: list[dict] = []
        try:
            for role in ROLES:
                self.events.status(role, "idle", "Waiting for assignment")
            if task.get("workspace"):
                config.workspace = str(Path(task["workspace"]).expanduser().resolve())
                config.validate()
            source = Path(config.workspace)
            task["workspace"] = str(source)
            run.mkdir(parents=True, exist_ok=True)
            atomic_json(run / "request.json", task)
            self.events.emit(
                "father",
                "job",
                f"Task {task['id']} · {source}",
                task_id=task["id"],
                workspace=str(source),
            )
            self.events.status("father", "working", "Preparing staged workspace")
            workspace = Workspace(
                source,
                run,
                self.home,
                config.excludes,
                self.cancel,
                lambda text: self.events.emit("father", "info", text),
            )
            workspace.prepare()
            self.runner = self.runner_factory(config, self.events, self.cancel)
            context = f"Original user request:\n{task['prompt']}\n\nProject root: {source}\nTools act on a staged copy.\n"
            context += "Inspect AGENTS.md and relevant project files using your tools.\n"
            if task["role"] == "father":
                plan = self.runner.run(
                    "father", "plan", context, self._tools("father", workspace, config)
                )
                atomic_json(run / "plan.json", plan)
                self._save_session_title(task, plan["session_title"], config)
                for step in plan["steps"]:
                    child = self.store.submit(step["prompt"], step["role"], parent_id=task["id"])
                    children.append(child)
                    self.events.status(step["role"], "queued", step["prompt"])
            else:
                # Standalone specialist tasks still receive father review/integration.
                children.append(
                    self.store.submit(task["prompt"], task["role"], parent_id=task["id"])
                )
            self.events.status(
                "father", "waiting", "Delegated; waiting for serial specialist results"
            )
            for child in children:
                check_cancel(self.cancel)
                result = self._worker(child, workspace, config, self._handoffs(context, results))
                results.append(result)
                atomic_json(run / "handoffs.json", results)
                if result["status"] != "passed":
                    # Failed coding/testing can be repaired by the father's rework pass.
                    if result["role"] in {"requirement", "design"}:
                        raise RuntimeError(
                            f"{result['role']} {result['status']}: {result['summary']}"
                        )
            # Direct coding tasks need independent testing before father review, too.
            if workspace.changes() and not any(r["role"] == "testing" for r in results):
                child = self.store.submit(
                    "Independently validate staged changes against the user request. Run appropriate checks and report evidence.",
                    "testing",
                    parent_id=task["id"],
                )
                children.append(child)
                results.append(
                    self._worker(child, workspace, config, self._handoffs(context, results))
                )
                atomic_json(run / "handoffs.json", results)
            review = None
            for attempt in range(config.max_rework + 1):
                check_cancel(self.cancel)
                review = self.runner.run(
                    "father",
                    "review",
                    self._handoffs(context, results)
                    + "\nReview the changes with get_changes and inspect files as needed.\n"
                    f"Rework attempts remaining: {config.max_rework - attempt}.",
                    self._tools("father", workspace, config),
                )
                atomic_json(run / "review.json", review)
                self._save_session_title(task, review["session_title"], config)
                if review["decision"] != "rework":
                    break
                if attempt == config.max_rework:
                    raise RuntimeError(
                        "Father requested more rework than max_rework allows. " + review["summary"]
                    )
                for role in ("coding", "testing"):
                    child = self.store.submit(review["summary"], role, parent_id=task["id"])
                    children.append(child)
                    results.append(
                        self._worker(child, workspace, config, self._handoffs(context, results))
                    )
                    atomic_json(run / "handoffs.json", results)
            latest = {result["role"]: result for result in results}
            if review["decision"] != "approve":
                raise RuntimeError("Father rejected integration: " + review["summary"])
            if any(result["status"] != "passed" for result in latest.values()):
                raise RuntimeError("Integration blocked: a specialist's latest result did not pass")
            self.events.status("father", "working", "Integrating approved changes into the project")
            changes = workspace.apply()
            summary = (
                review["summary"]
                + f"\n\nIntegrated {len(changes)} file change(s) into {source}.\nRun artifacts: {run}"
            )
            atomic_json(
                run / "outcome.json",
                {
                    "status": "completed",
                    "finished_at": timestamp(),
                    "summary": summary,
                    "changes": changes,
                },
            )
            self.store.finish(task, "completed", summary, run=str(run))
            self.events.status("father", "done", summary)
        except Exception as error:
            cancelled = self.cancel.is_set() or isinstance(error, Cancelled)
            status = "cancelled" if cancelled else "failed"
            summary = ("Cancelled. " if cancelled else "") + str(error)
            if run.exists():
                atomic_json(
                    run / "outcome.json",
                    {"status": status, "summary": summary, "finished_at": timestamp()},
                )
            self.store.finish(task, status, summary, run=str(run))
            self.events.status("father", "cancelled" if cancelled else "error", summary)
        finally:
            if not task.get("session_title"):
                self._naming_attempted.add(task["id"])
            for child in children:
                if (self.store.folder(child["role"], "pending") / f"{child['id']}.json").exists():
                    self.store.finish(
                        child, "cancelled", "Parent workflow ended before this task ran"
                    )
                    self.events.status(child["role"], "cancelled", "Parent workflow ended")
            if self.runner:
                self.runner.close()
                self.runner = None
            self.active = None
            self.cancel.clear()

    @staticmethod
    def _handoffs(context: str, results: list[dict]) -> str:
        # Preserve full reports on disk; bound prompt growth across repair passes.
        handoffs = [
            {
                "role": result["role"],
                "status": result["status"],
                "summary": result["summary"][:18000],
                "commands": [
                    {
                        "args": command["args"],
                        "exit_code": command["exit_code"],
                        "timed_out": command["timed_out"],
                        "output": command["output"][-5000:],
                    }
                    for command in result.get("commands", [])[-8:]
                ],
            }
            for result in results[-12:]
        ]
        return (
            context
            + "\nPrevious serial handoffs:\n"
            + json.dumps(handoffs, ensure_ascii=False, indent=2)
        )
