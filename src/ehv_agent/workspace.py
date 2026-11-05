from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock

from .config import atomic_json


class Cancelled(Exception):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("Cancelled by user; staged work has been preserved.")


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path must stay inside the configured folder")
    return path


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class Workspace:
    """Copy-on-run workspace, content manifest, and conflict-checked integration."""

    def __init__(
        self,
        source: Path,
        run: Path,
        home: Path,
        excludes: list[str],
        cancel: threading.Event,
        progress: Callable[[str], None],
    ):
        self.source = source.resolve()
        self.run = run
        self.root = run / "project"
        self.home = home.resolve()
        self.excludes = excludes
        self.cancel = cancel
        self.progress = progress
        self.baseline: dict[str, str] = {}

    def excluded(self, relative: str) -> bool:
        parts = Path(relative).parts
        return any(
            fnmatch.fnmatch(part, pattern)
            for part in parts
            for pattern in [*self.excludes, ".ehv-agent.apply.lock"]
        )

    def files(self, root: Path):
        exclude_home = not root.resolve().is_relative_to(self.home)
        for directory, dirs, files in os.walk(root, followlinks=False):
            check_cancel(self.cancel)
            parent = Path(directory)
            if root.resolve().is_relative_to(self.root.resolve()):
                for name in [*dirs, *files]:
                    if (parent / name).is_symlink() and not self.excluded(
                        str((parent / name).relative_to(root))
                    ):
                        raise ValueError(f"Staged symbolic link is not supported: {parent / name}")
            dirs[:] = sorted(
                d
                for d in dirs
                if not (parent / d).is_symlink()
                and not self.excluded(str((parent / d).relative_to(root)))
                and not (exclude_home and (parent / d).resolve().is_relative_to(self.home))
            )
            for name in sorted(files):
                path = parent / name
                relative = path.relative_to(root).as_posix()
                if (
                    not path.is_symlink()
                    and not self.excluded(relative)
                    and not (exclude_home and path.resolve().is_relative_to(self.home))
                ):
                    yield relative, path

    def prepare(self) -> None:
        if self.source.is_relative_to(self.home):
            raise ValueError("Workspace cannot be inside the agent data folder")
        self.root.mkdir(parents=True, exist_ok=False)
        for count, (relative, path) in enumerate(self.files(self.source), 1):
            check_cancel(self.cancel)
            target = inside(self.root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            before = digest(path)
            shutil.copy2(path, target)
            if before != digest(target) or before != digest(path):
                raise RuntimeError(f"Source changed while staging: {relative}; retry")
            self.baseline[relative] = before
            if count % 50 == 0:
                self.progress(f"Staging project: {count} files copied")
        atomic_json(self.run / "baseline.json", self.baseline)
        self.progress(f"Staged {len(self.baseline)} project files")

    def changes(self) -> list[dict]:
        current = {relative: digest(path) for relative, path in self.files(self.root)}
        return [
            {
                "path": name,
                "action": "delete"
                if name not in current
                else "add"
                if name not in self.baseline
                else "modify",
                "before": self.baseline.get(name),
                "after": current.get(name),
            }
            for name in sorted(set(self.baseline) | set(current))
            if self.baseline.get(name) != current.get(name)
        ]

    def project_path(self, relative: str) -> Path:
        path = inside(self.root, relative)
        if self.excluded(path.relative_to(self.root).as_posix()):
            raise ValueError("This path is excluded by workspace configuration")
        # Resolve must not change the path through a link created by a command.
        raw = self.root / relative
        if any(p.is_symlink() for p in [raw, *raw.parents] if p.is_relative_to(self.root)):
            raise ValueError("Symbolic links are not supported by project tools")
        return path

    def apply(self) -> list[dict]:
        check_cancel(self.cancel)
        changes = self.changes()
        # A workspace lock coordinates separate agent homes targeting the same project.
        with FileLock(str(self.source / ".ehv-agent.apply.lock"), timeout=10):
            for change in changes:
                destination = inside(self.source, change["path"])
                if destination.is_relative_to(self.home):
                    raise ValueError("Cannot integrate changes into agent state")
                raw = self.source / change["path"]
                if any(
                    p.is_symlink() for p in [raw, *raw.parents] if p.is_relative_to(self.source)
                ):
                    raise RuntimeError(f"Destination became a symbolic link: {change['path']}")
                actual = digest(destination) if destination.is_file() else None
                if actual != change["before"] or destination.is_dir():
                    raise RuntimeError(
                        f"Integration conflict: {change['path']} changed in the real project. Staged files are preserved."
                    )
            backups = self.run / "backups"
            for change in changes:
                if change["before"] is not None:
                    backup = inside(backups, change["path"])
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(inside(self.source, change["path"]), backup)
            atomic_json(
                self.run / "integration.json",
                {"state": "applying", "workspace": str(self.source), "changes": changes},
            )
            touched = []
            try:
                for change in changes:
                    check_cancel(self.cancel)
                    destination = inside(self.source, change["path"])
                    touched.append(change)
                    if change["action"] == "delete":
                        destination.unlink()
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        self.atomic_copy(self.project_path(change["path"]), destination)
                    self.progress(f"Integrated {change['action']}: {change['path']}")
            except BaseException:
                for change in reversed(touched):
                    destination = inside(self.source, change["path"])
                    if change["before"] is None:
                        destination.unlink(missing_ok=True)
                    else:
                        self.atomic_copy(inside(backups, change["path"]), destination)
                atomic_json(
                    self.run / "integration.json",
                    {"state": "rolled_back", "workspace": str(self.source), "changes": changes},
                )
                raise
            atomic_json(
                self.run / "integration.json",
                {"state": "applied", "workspace": str(self.source), "changes": changes},
            )
        return changes

    @staticmethod
    def atomic_copy(source: Path, destination: Path) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".ehv-", dir=destination.parent)
        os.close(fd)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
