from __future__ import annotations

import json
import uuid
from pathlib import Path

from .config import ROLES, atomic_json
from .events import timestamp


class TaskStore:
    def __init__(self, home: Path):
        self.home = home

    def folder(self, role: str, state: str) -> Path:
        if role not in ROLES or state not in {"pending", "running", "results"}:
            raise ValueError("Unknown agent or task state")
        return self.home / "agents" / role / state

    def submit(self, prompt: str, role: str = "father", **extra: object) -> dict:
        if not prompt.strip():
            raise ValueError("Task prompt must not be empty")
        task = {
            **extra,
            "id": uuid.uuid4().hex[:12],
            "role": role,
            "prompt": prompt,
            "created_at": timestamp(),
            "status": "pending",
        }
        atomic_json(self.folder(role, "pending") / f"{task['id']}.json", task)
        return task

    def read(self, path: Path) -> dict:
        from datetime import datetime, timezone

        task = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("prompt"), str)
            or not task["prompt"].strip()
        ):
            raise ValueError("Task JSON must contain a nonempty prompt string")
        # The filename is authoritative: do not permit JSON to escape task folders.
        task["id"] = path.stem
        task["role"] = path.parent.parent.name
        task.setdefault(
            "created_at", datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        )
        if not isinstance(task["created_at"], str):
            raise ValueError("created_at must be an ISO timestamp string")
        created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        task["created_at"] = created.astimezone(timezone.utc).isoformat()
        if "not_before" in task:
            if not isinstance(task["not_before"], str):
                raise ValueError("not_before must be an ISO timestamp string")
            value = datetime.fromisoformat(task["not_before"].replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("not_before must include a timezone")
        return task

    def next_pending(self) -> dict | None:
        from datetime import datetime, timezone

        candidates = []
        for role in ROLES:
            for path in self.folder(role, "pending").glob("*.json"):
                try:
                    task = self.read(path)
                    if task.get("parent_id"):
                        continue
                    if task.get("not_before") and datetime.fromisoformat(
                        task["not_before"].replace("Z", "+00:00")
                    ) > datetime.now(timezone.utc):
                        continue
                    candidates.append(task)
                except (ValueError, TypeError, AttributeError) as error:
                    atomic_json(
                        self.folder(role, "results") / path.name,
                        {"id": path.stem, "role": role, "status": "invalid", "error": str(error)},
                    )
                    path.unlink(missing_ok=True)
        return min(candidates, key=lambda t: (t["created_at"], t["id"])) if candidates else None

    def claim(self, task: dict) -> None:
        pending = self.folder(task["role"], "pending") / f"{task['id']}.json"
        running = self.folder(task["role"], "running") / pending.name
        pending.replace(running)
        task.update(status="running", started_at=timestamp())
        atomic_json(running, task)

    def finish(self, task: dict, status: str, result: str, **extra: object) -> None:
        task.update(status=status, result=result, finished_at=timestamp(), **extra)
        atomic_json(self.folder(task["role"], "results") / f"{task['id']}.json", task)
        (self.folder(task["role"], "running") / f"{task['id']}.json").unlink(missing_ok=True)
        (self.folder(task["role"], "pending") / f"{task['id']}.json").unlink(missing_ok=True)
        (self.folder(task["role"], "results") / f"{task['id']}.md").write_text(
            result, encoding="utf-8"
        )

    def recover(self) -> int:
        count = 0
        for role in ROLES:
            paths = list(self.folder(role, "running").glob("*.json"))
            paths += [p for p in self.folder(role, "pending").glob("*.json") if self._is_child(p)]
            for path in paths:
                try:
                    task = self.read(path)
                except (ValueError, TypeError, AttributeError) as error:
                    atomic_json(
                        self.folder(role, "results") / path.name,
                        {"id": path.stem, "role": role, "status": "invalid", "error": str(error)},
                    )
                    path.unlink(missing_ok=True)
                    continue
                self.finish(
                    task,
                    "interrupted",
                    "App stopped during this workflow. Review staged files and retry the parent task explicitly.",
                )
                count += 1
        return count

    @staticmethod
    def _is_child(path: Path) -> bool:
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get("parent_id"))
        except (ValueError, AttributeError):
            return False

    def retry(self, task_id: str) -> dict:
        if not task_id.isalnum():
            raise ValueError("Invalid task ID")
        for role in ROLES:
            path = self.folder(role, "results") / f"{task_id}.json"
            if path.exists():
                old = self.read(path)
                if old.get("parent_id"):
                    raise ValueError(f"Retry the parent workflow: {old['parent_id']}")
                return self.submit(
                    old["prompt"], role, retry_of=task_id, workspace=old.get("workspace")
                )
        raise ValueError(f"No completed task with ID {task_id}")
