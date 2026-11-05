from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from .config import ROLES, Config, atomic_json
from .events import timestamp
from .pricing import token_counts
from .usage import UsageStore, combine_usage, cost_label


def validate_session_title(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Give the session a single-line, descriptive title")
    title = " ".join(value.split())
    if not 5 <= len(title) <= 80 or any(char in title for char in "`*#"):
        raise ValueError("Session title must be 5–80 characters of plain text, without Markdown")
    generic = {
        "session",
        "new session",
        "untitled",
        "task",
        "new task",
        "work session",
        "project work",
    }
    if title.casefold().strip(" .:-") in generic or re.fullmatch(
        r"(?:task|session)[\s:_-]*[0-9a-f]{4,}", title, re.I
    ):
        raise ValueError("Name the actual work, not a generic session label or task ID")
    return title


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def text_value(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


def time_order(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


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
    cost_usd: str | None = None
    cost_complete: bool = False
    message_count: int = 0
    conversation_id: str = ""
    turn_count: int = 1
    task_ids: tuple[str, ...] = ()

    @property
    def display_title(self) -> str:
        return self.title or ("New session" if self.turn_count == 0 else "Awaiting father title")


class SessionStore:
    """Persistent conversations group ordered root workflows without duplicating results."""

    def __init__(self, home: Path):
        self.home = home

    def run_path(self, session_id: str) -> Path:
        if (
            not session_id
            or session_id in {".", ".."}
            or any(char in session_id for char in "/\\:\0")
        ):
            raise ValueError("Invalid session ID")
        return self.home / "runs" / session_id

    def conversation_path(self, session_id: str) -> Path:
        self.run_path(session_id)  # shared ID/path validation
        return self.home / "sessions" / f"{session_id}.json"

    def current_id(self, workspace: str | None = None) -> str | None:
        value = read_json(self.home / "active-session.json").get("session_id")
        if not isinstance(value, str):
            return None
        try:
            metadata = read_json(self.conversation_path(value))
        except ValueError:
            return None
        if not metadata or (workspace and metadata.get("workspace") != workspace):
            return None
        return value

    def create(self, workspace: str) -> str:
        with FileLock(str(self.home / "sessions.lock")):
            session_id = uuid4().hex[:16]
            atomic_json(
                self.conversation_path(session_id),
                {
                    "id": session_id,
                    "workspace": str(Path(workspace).resolve()),
                    "created_at": timestamp(),
                },
            )
            atomic_json(self.home / "active-session.json", {"session_id": session_id})
        return session_id

    def ensure_current(self, workspace: str) -> str:
        # Resolve concurrent CLI submissions under one lock.
        with FileLock(str(self.home / "sessions.lock")):
            current = self.current_id(workspace)
            if current:
                return current
            session_id = uuid4().hex[:16]
            atomic_json(
                self.conversation_path(session_id),
                {
                    "id": session_id,
                    "workspace": workspace,
                    "created_at": timestamp(),
                },
            )
            atomic_json(self.home / "active-session.json", {"session_id": session_id})
            return session_id

    def activate(self, session_id: str) -> str:
        with FileLock(str(self.home / "sessions.lock")):
            path = self.conversation_path(session_id)
            if not path.exists():
                # A legacy one-request session becomes a resumable conversation.
                original = next(
                    (item for item in self.workflows(None) if item.id == session_id), None
                )
                if original is None:
                    raise ValueError(f"Session not found: {session_id}")
                atomic_json(
                    path,
                    {
                        "id": session_id,
                        "workspace": original.workspace or Config.load(self.home).workspace,
                        "created_at": original.created_at,
                    },
                )
            atomic_json(self.home / "active-session.json", {"session_id": session_id})
        return session_id

    def workflows(self, limit: int | None = 100) -> list[Session]:
        records = {}
        for state in ("pending", "running", "results"):
            for role in ROLES:
                for path in (self.home / "agents" / role / state).glob("*.json"):
                    task = read_json(path)
                    if task.get("parent_id") or not isinstance(task.get("prompt"), str):
                        continue
                    session_id = path.stem
                    try:
                        run = self.run_path(session_id)
                    except ValueError:
                        continue
                    request = read_json(run / "request.json")
                    metadata = read_json(run / "session.json")
                    usage = read_json(run / "usage.json").get("summary", {})
                    if not isinstance(usage, dict):
                        usage = {}
                    title = ""
                    if metadata.get("title_source") == "father":
                        try:
                            title = validate_session_title(metadata.get("title"))
                        except ValueError:
                            pass
                    records[session_id] = Session(
                        id=session_id,
                        title=title,
                        status=text_value(task.get("status"), state),
                        prompt=task["prompt"],
                        workspace=text_value(task.get("workspace"))
                        or text_value(request.get("workspace")),
                        role=role,
                        created_at=text_value(task.get("created_at"))
                        or text_value(request.get("created_at")),
                        finished_at=text_value(task.get("finished_at")),
                        summary=text_value(task.get("result")),
                        title_error=text_value(metadata.get("title_error")),
                        title_state=text_value(metadata.get("title_state")),
                        title_model=text_value(metadata.get("title_model")),
                        cost_usd=usage.get("cost_usd")
                        if isinstance(usage.get("cost_usd"), str)
                        else None,
                        cost_complete=usage.get("complete") is True,
                        message_count=usage.get("message_count", 0)
                        if isinstance(usage.get("message_count", 0), int)
                        else 0,
                        conversation_id=text_value(task.get("session_id"))
                        or text_value(request.get("session_id")),
                        task_ids=(session_id,),
                    )
        ordered = sorted(
            records.values(), key=lambda item: (time_order(item.created_at), item.id), reverse=True
        )
        return ordered if limit is None else ordered[:limit]

    def _aggregate(self, session_id: str, metadata: dict, turns: list[Session]) -> Session:
        usage = combine_usage([UsageStore(self.run_path(turn.id)).summary() for turn in turns])
        named = next((turn for turn in turns if turn.title), None)
        state = (
            "running"
            if any(turn.status == "running" for turn in turns)
            else (
                "pending"
                if any(turn.status == "pending" for turn in turns)
                else (turns[-1].status if turns else "idle")
            )
        )
        first, last = (turns[0], turns[-1]) if turns else (None, None)
        return Session(
            id=session_id,
            title=named.title if named else "",
            status=state,
            prompt=first.prompt if first else "",
            workspace=text_value(metadata.get("workspace")),
            role="father",
            created_at=text_value(metadata.get("created_at")),
            finished_at=last.finished_at if last else "",
            summary=last.summary if last else "",
            title_model=named.title_model if named else "",
            title_state=first.title_state if first else "",
            title_error=first.title_error if first else "",
            cost_usd=usage["cost_usd"],
            cost_complete=usage["complete"],
            message_count=usage["message_count"],
            conversation_id=session_id,
            turn_count=len(turns),
            task_ids=tuple(turn.id for turn in turns),
        )

    def recent(self, limit: int | None = 100) -> list[Session]:
        workflows = self.workflows(None)
        metadata = {path.stem: read_json(path) for path in (self.home / "sessions").glob("*.json")}
        grouped: dict[str, list[Session]] = {key: [] for key, value in metadata.items() if value}
        records = []
        for turn in workflows:
            group = turn.conversation_id or turn.id
            if group in grouped:
                grouped[group].append(turn)
            else:
                records.append(turn)
        for session_id, turns in grouped.items():
            turns.sort(key=lambda item: (time_order(item.created_at), item.id))
            records.append(self._aggregate(session_id, metadata[session_id], turns))
        records.sort(key=lambda item: (time_order(item.created_at), item.id), reverse=True)
        return records if limit is None else records[:limit]

    def turns(self, session_id: str) -> list[Session]:
        if self.conversation_path(session_id).exists():
            turns = [
                turn
                for turn in self.workflows(None)
                if turn.conversation_id == session_id
                or (not turn.conversation_id and turn.id == session_id)
            ]
            return sorted(turns, key=lambda item: (time_order(item.created_at), item.id))
        turn = next((item for item in self.workflows(None) if item.id == session_id), None)
        if turn is None:
            raise ValueError(f"Session not found: {session_id}")
        return [turn]

    def usage(self, session_id: str) -> dict:
        return combine_usage(
            [UsageStore(self.run_path(turn.id)).summary() for turn in self.turns(session_id)]
        )

    def view(self, session_id: str) -> tuple[str, list[Session], dict]:
        turns = self.turns(session_id)
        title = next((turn.title for turn in turns if turn.title), "New session")
        usage = combine_usage([UsageStore(self.run_path(turn.id)).summary() for turn in turns])
        return title, turns, usage

    def prior_turns(self, session_id: str, task_id: str) -> list[Session]:
        previous = []
        for turn in self.turns(session_id):
            if turn.id == task_id:
                break
            if turn.status not in {"pending", "running"}:
                previous.append(turn)
        else:
            return []  # no permission to include future turns for an unknown task
        return previous

    def context(self, session_id: str, task_id: str) -> str:
        previous = self.prior_turns(session_id, task_id)
        if not previous:
            return ""
        entries = [
            {"ask": turn.prompt[:4000], "answer": turn.summary[:8000], "status": turn.status}
            for turn in previous[-12:]
        ]
        # Keep this pinned context bounded; older exchanges remain retrievable by tool.
        while len(json.dumps(entries, ensure_ascii=False)) > 50000 and len(entries) > 1:
            entries.pop(0)
        return (
            "\nPrevious exchanges in this session (context, not a request to rerun completed work). "
            "The latest user request controls this assignment. Use read_session_history for earlier details.\n"
            + json.dumps(entries, ensure_ascii=False)
        )

    def get(self, session_id: str) -> Session:
        self.run_path(session_id)
        metadata = read_json(self.conversation_path(session_id))
        if metadata:
            return self._aggregate(session_id, metadata, self.turns(session_id))
        for workflow in self.workflows(None):
            if workflow.id == session_id:
                return workflow
        raise ValueError(f"Session not found: {session_id}")

    def set_title(self, session_id: str, title: str, model: str) -> str:
        title = validate_session_title(title)
        atomic_json(
            self.run_path(session_id) / "session.json",
            {
                "id": session_id,
                "title": title,
                "title_source": "father",
                "title_model": model,
                "titled_at": timestamp(),
                "title_state": "named",
            },
        )
        return title

    def name_state(self, session_id: str, state: str, error: str = "") -> None:
        path = self.run_path(session_id) / "session.json"
        metadata = read_json(path)
        metadata.update(id=session_id, title_state=state, title_error=error)
        atomic_json(path, metadata)

    def report(self, session_id: str) -> str:
        session = self.get(session_id)
        if self.conversation_path(session_id).exists():
            return self.conversation_report(session)
        run = self.run_path(session_id)
        parts = [
            f"# {session.display_title}",
            f"**Status:** {session.status}",
            f"**Workspace:** {session.workspace or 'Not started'}",
            f"**Created:** {local_time(session.created_at)}",
            f"**Session ID:** `{session.id}`",
        ]
        if session.finished_at:
            parts.append(f"**Finished:** {local_time(session.finished_at)}")
        if session.title:
            parts.append(f"**Named by:** Father · `{session.title_model}`")
        else:
            parts.append(
                "The father has not named this session yet. Older sessions are named when the team is idle."
            )
        if session.title_error:
            parts.append(f"**Naming issue:** {session.title_error}")
        parts.append(
            f"**Estimated cost (USD):** {cost_label(session.cost_usd, session.cost_complete)}"
            + f" · {session.message_count} API message(s)"
        )
        if session.message_count and not session.cost_complete:
            parts.append(
                "Cost is incomplete: some messages have unknown pricing, missing usage, or an interrupted stream. Any shown amount totals the priced usage observed so far."
            )
        parts += [
            "## Request",
            session.prompt,
            "## Result",
            session.summary or "No final result yet.",
        ]
        usage = read_json(run / "usage.json")
        messages = usage.get("messages", [])
        if isinstance(messages, list) and messages:
            parts += [
                "## Message costs",
                "Estimates use token usage and saved model rates, before account discounts or credits. Each row is one API response or failed/interrupted attempt, including tool turns and session naming.",
            ]
            rows = [
                "| # | Agent | Phase | Model | Input | Cache write | Cache read | Output | Estimated USD |",
                "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
            ]
            for index, message in enumerate(messages, 1):
                if not isinstance(message, dict):
                    continue
                raw = message.get("usage") or {}
                if not isinstance(raw, dict):
                    raw = {}
                counts = token_counts(raw)
                model = text_value(message.get("model")).replace("|", "\\|")
                rows.append(
                    f"| {index} | {message.get('role', '')} | {message.get('phase', '')} | {model} "
                    f"| {counts['input']} | {counts['cache_write_5m'] + counts['cache_write_1h']} "
                    f"| {counts['cache_read']} | {counts['output']} "
                    f"| {cost_label(message.get('cost_usd'), message.get('usage_complete') is True)} |"
                )
            parts.append("\n".join(rows))
        try:
            handoffs = json.loads((run / "handoffs.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            handoffs = []
        for handoff in handoffs if isinstance(handoffs, list) else []:
            if not isinstance(handoff, dict):
                continue
            parts += [
                f"## {text_value(handoff.get('role'), 'Agent').title()} · {text_value(handoff.get('status'))}",
                text_value(handoff.get("summary")),
            ]
            commands = handoff.get("commands", [])
            for command in commands if isinstance(commands, list) else []:
                if isinstance(command, dict):
                    args = command.get("args", [])
                    args = [str(arg) for arg in args] if isinstance(args, list) else []
                    parts.append(
                        f"**Command:** `{' '.join(args)}` · exit {command.get('exit_code')}"
                        + (" · timed out" if command.get("timed_out") else "")
                    )
        outcome = read_json(run / "outcome.json")
        changes = outcome.get("changes") or read_json(run / "integration.json").get("changes", [])
        if isinstance(changes, list) and changes:
            parts.append("## Files changed")
            parts += [
                f"- {change.get('action', 'change')}: `{change.get('path', '')}`"
                for change in changes
                if isinstance(change, dict)
            ]
        parts += ["## Saved artifacts", f"`{run}`"]
        return "\n\n".join(parts)

    def conversation_report(self, session: Session) -> str:
        parts = [
            f"# {session.display_title}",
            f"**Workspace:** {session.workspace}",
            f"**Session total (USD):** {cost_label(session.cost_usd, session.cost_complete)} · {session.turn_count} ask–answer exchange(s)",
        ]
        for index, turn in enumerate(self.turns(session.id), 1):
            parts.extend(
                [
                    f"## You · {index}",
                    turn.prompt,
                    f"## Father · {index}",
                    turn.summary or f"{turn.status.title()}…",
                    f"**Ask–answer cost:** {cost_label(turn.cost_usd, turn.cost_complete)} · {turn.status} · Task `{turn.id}`",
                ]
            )
        return "\n\n".join(parts)


def local_time(value: str) -> str:
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M %z")
        )
    except (ValueError, AttributeError):
        return value or "—"
