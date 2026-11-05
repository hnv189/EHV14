from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Events:
    """A thread-safe event stream shared by the engine, JSONL audit log, and terminal."""

    def __init__(self, home: Path):
        self.home = home
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=10000)
        self.lock = threading.Lock()

    def emit(self, role: str, kind: str, text: str, **extra: object) -> None:
        event = {"time": timestamp(), "role": role, "kind": kind, "text": text, **extra}
        with self.lock:
            with (self.home / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # Disk remains authoritative; the UI may discard old token deltas under load.
            pass

    def status(self, role: str, status: str, text: str) -> None:
        self.emit(role, "status", text, status=status)

    def scoped(self, **context: object) -> ScopedEvents:
        return ScopedEvents(self, context)


class ScopedEvents:
    """Attach immutable workflow identity without affecting concurrent UI events."""

    def __init__(self, events: Events, context: dict):
        self.events = events
        self.context = context

    def __getattr__(self, name):
        return getattr(self.events, name)

    def emit(self, role: str, kind: str, text: str, **extra: object) -> None:
        self.events.emit(role, kind, text, **{**self.context, **extra})

    def status(self, role: str, status: str, text: str) -> None:
        self.emit(role, "status", text, status=status)
