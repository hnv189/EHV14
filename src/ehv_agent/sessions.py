from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ROLES, atomic_json
from .events import timestamp


def validate_session_title(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Give the session a single-line, descriptive title")
    title = " ".join(value.split())
    if not 5 <= len(title) <= 80 or any(char in title for char in "`*#"):
        raise ValueError("Session title must be 5–80 characters of plain text, without Markdown")
    generic = {"session", "new session", "untitled", "task", "new task", "work session", "project work"}
    if title.casefold().strip(" .:-") in generic or re.fullmatch(r"(?:task|session)[\s:_-]*[0-9a-f]{4,}", title, re.I):
        raise ValueError("Name the actual work, not a generic session label or task ID")
    return title


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


@dataclass(frozen=True)
class Session:
    id: str
    title: str
    status: str
    prompt: str
    workspace: str
    role: str
    created_at: str
    finished_at: str
    summary: str
    title_error: str = ""
    title_state: str = ""
    title_model: str = ""

    @property
    def display_title(self) -> str:
        return self.title or "Awaiting father title"


class SessionStore:
    """A history view over existing root tasks; no duplicate task database."""

    def __init__(self, home: Path):
        self.home = home

    def run_path(self, session_id: str) -> Path:
        if not session_id or session_id in {".", ".."} or any(char in session_id for char in "/\\:\0"):
            raise ValueError("Invalid session ID")
        return self.home / "runs" / session_id

    def recent(self, limit: int | None = 100) -> list[Session]:
        records = {}
        for state in ("pending", "running", "results"):
            for role in ROLES:
                for path in (self.home / "agents" / role / state).glob("*.json"):
                    task = read_json(path)
                    if task.get("parent_id") or not isinstance(task.get("prompt"), str):
                        continue
                    session_id = path.stem
                    run = self.run_path(session_id)
                    request = read_json(run / "request.json")
                    metadata = read_json(run / "session.json")
                    title = ""
                    if metadata.get("title_source") == "father":
                        try:
                            title = validate_session_title(metadata.get("title"))
                        except ValueError:
                            pass
                    records[session_id] = Session(
                        id=session_id, title=title, status=task.get("status", state),
                        prompt=task["prompt"], workspace=task.get("workspace") or request.get("workspace") or "",
                        role=role, created_at=task.get("created_at") or request.get("created_at") or "",
                        finished_at=task.get("finished_at", ""), summary=task.get("result", ""),
                        title_error=metadata.get("title_error", ""), title_state=metadata.get("title_state", ""),
                        title_model=metadata.get("title_model", ""),
                    )
        ordered = sorted(records.values(), key=lambda item: (item.created_at, item.id), reverse=True)
        return ordered if limit is None else ordered[:limit]

    def get(self, session_id: str) -> Session:
        self.run_path(session_id)
        for session in self.recent(limit=None):
            if session.id == session_id:
                return session
        raise ValueError(f"Session not found: {session_id}")

    def set_title(self, session_id: str, title: str, model: str) -> str:
        title = validate_session_title(title)
        atomic_json(self.run_path(session_id) / "session.json", {
            "id": session_id, "title": title, "title_source": "father",
            "title_model": model, "titled_at": timestamp(), "title_state": "named",
        })
        return title

    def name_state(self, session_id: str, state: str, error: str = "") -> None:
        path = self.run_path(session_id) / "session.json"
        metadata = read_json(path)
        metadata.update(id=session_id, title_state=state, title_error=error)
        atomic_json(path, metadata)

    def report(self, session_id: str) -> str:
        session = self.get(session_id)
        run = self.run_path(session_id)
        parts = [f"# {session.display_title}", f"**Status:** {session.status}",
                 f"**Workspace:** {session.workspace or 'Not started'}",
                 f"**Created:** {local_time(session.created_at)}",
                 f"**Session ID:** `{session.id}`"]
        if session.finished_at:
            parts.append(f"**Finished:** {local_time(session.finished_at)}")
        if session.title:
            parts.append(f"**Named by:** Father · `{session.title_model}`")
        else:
            parts.append("The father has not named this session yet. Older sessions are named when the team is idle.")
        if session.title_error:
            parts.append(f"**Naming issue:** {session.title_error}")
        parts += ["## Request", session.prompt, "## Result", session.summary or "No final result yet."]
        try:
            handoffs = json.loads((run / "handoffs.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            handoffs = []
        for handoff in handoffs if isinstance(handoffs, list) else []:
            if not isinstance(handoff, dict):
                continue
            parts += [f"## {handoff.get('role', 'Agent').title()} · {handoff.get('status', '')}", handoff.get("summary", "")]
            for command in handoff.get("commands", []):
                if isinstance(command, dict):
                    parts.append(f"**Command:** `{' '.join(command.get('args', []))}` · exit {command.get('exit_code')}" + (" · timed out" if command.get("timed_out") else ""))
        outcome = read_json(run / "outcome.json")
        changes = outcome.get("changes") or read_json(run / "integration.json").get("changes", [])
        if changes:
            parts.append("## Files changed")
            parts += [f"- {change.get('action', 'change')}: `{change.get('path', '')}`" for change in changes if isinstance(change, dict)]
        parts += ["## Saved artifacts", f"`{run}`"]
        return "\n\n".join(parts)


def local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, AttributeError):
        return value or "—"
