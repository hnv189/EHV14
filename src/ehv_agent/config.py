from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .pricing import validate_model_pricing

ROLES = {
    "father": "Classify the request first. Answer simple questions and explanations yourself using project evidence when needed. Delegate only tasks that need specialist work, choosing the necessary roles in serial order. Review delegated results and integrate only complete, validated changes.",
    "requirement": "Analyze the request and existing code. Produce clear requirements, acceptance criteria, relevant file paths, constraints, and edge cases. Do not change project files.",
    "design": "Design the implementation to fit the existing architecture and UI conventions. Give the coding agent concrete file-level guidance and tradeoffs. Do not change project files.",
    "coding": "Implement the requested feature in the staged project. Read existing code and follow its conventions. Make focused changes and run relevant checks.",
    "testing": "Independently inspect the implementation against acceptance criteria. Add useful tests where appropriate, run relevant checks, fix focused defects, and explicitly report failures or checks you could not run.",
}
DEFAULT_EXCLUDES = [
    ".git",
    ".ehv-agent",
    ".conda-env",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.egg-info",
]


def replace_file(source: str | Path, destination: Path) -> None:
    # Windows readers and antivirus scanners can briefly deny replacement even
    # after a file has been closed. Keep atomic writes while bounding retries.
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if (
                os.name != "nt"
                or getattr(error, "winerror", None) not in {5, 32, 33}
                or attempt == 5
            ):
                raise
            time.sleep(0.01 * 2**attempt)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class Config:
    workspace: str
    model: str = "claude-sonnet-5-5"
    model_max_tokens: dict[str, int] = field(default_factory=dict)
    model_pricing: dict[str, dict[str, float | str]] = field(default_factory=dict)
    proxy_mode: str = "system"
    proxy_url: str = ""
    poll_seconds: float = 1.0
    max_turns: int = 40
    max_tokens: int = 8192
    auto_compact: bool = True
    compact_threshold: int = 60000
    max_token_retries: int = 2
    max_recovery_tokens: int = 32768
    model_context_tokens: dict[str, int] = field(default_factory=dict)
    command_timeout: int = 120
    max_rework: int = 1
    excludes: list[str] = field(default_factory=lambda: DEFAULT_EXCLUDES.copy())
    agents: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            role: {"model": "", "instructions": instructions}
            for role, instructions in ROLES.items()
        }
    )

    def validate(self) -> Config:
        workspace = Path(self.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {workspace}")
        self.workspace = str(workspace)
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a nonempty Claude model ID")
        validate_model_pricing(self.model_pricing)
        if not isinstance(self.auto_compact, bool):
            raise ValueError("auto_compact must be true or false")
        if self.proxy_mode not in ("system", "direct", "custom"):
            raise ValueError("proxy_mode must be system, direct, or custom")
        if not isinstance(self.proxy_url, str):
            raise ValueError("proxy_url must be a string")
        self.proxy_url = self.proxy_url.strip()
        if self.proxy_url:
            try:
                proxy = urlsplit(self.proxy_url)
                valid = (
                    proxy.scheme in {"http", "https"}
                    and bool(proxy.hostname)
                    and (proxy.port is None or 1 <= proxy.port <= 65535)
                    and proxy.path in {"", "/"}
                    and not proxy.query
                    and not proxy.fragment
                    and not any(
                        char.isspace() or ord(char) < 32 or ord(char) == 127
                        for char in self.proxy_url
                    )
                    and "\\" not in self.proxy_url
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(
                    "Proxy URL must be an HTTP/HTTPS address such as http://proxy.example.com:8080"
                )
        if self.proxy_mode == "custom" and not self.proxy_url:
            raise ValueError("Enter a proxy URL when using custom proxy mode")
        if not isinstance(self.model_max_tokens, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            for key, value in self.model_max_tokens.items()
        ):
            raise ValueError(
                "model_max_tokens must map model IDs to positive integer output limits"
            )
        if not isinstance(self.model_context_tokens, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            for key, value in self.model_context_tokens.items()
        ):
            raise ValueError(
                "model_context_tokens must map model IDs to positive integer input limits"
            )
        for name, low, high in [
            ("poll_seconds", 0.1, 60),
            ("max_turns", 1, 200),
            ("max_tokens", 256, 64000),
            ("compact_threshold", 1024, 1000000),
            ("max_token_retries", 0, 5),
            ("max_recovery_tokens", 256, 64000),
            ("command_timeout", 1, 3600),
            ("max_rework", 0, 5),
        ]:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not low <= value <= high
            ):
                raise ValueError(f"{name} must be between {low} and {high}")
            if name != "poll_seconds" and not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if not isinstance(self.excludes, list) or any(
            not isinstance(x, str) for x in self.excludes
        ):
            raise ValueError("excludes must be a list of file/directory patterns")
        if not isinstance(self.agents, dict) or set(self.agents) != set(ROLES):
            raise ValueError(f"agents must contain exactly: {', '.join(ROLES)}")
        for settings in self.agents.values():
            if not isinstance(settings, dict) or any(
                not isinstance(settings.get(k, ""), str) for k in ("model", "instructions")
            ):
                raise ValueError("Each agent needs string model and instructions fields")
        return self

    @classmethod
    def load(cls, home: Path) -> Config:
        return cls(**json.loads((home / "config.json").read_text(encoding="utf-8"))).validate()

    def save(self, home: Path) -> None:
        self.validate()
        atomic_json(home / "config.json", asdict(self))

    def model_for(self, role: str) -> str:
        return self.agents[role].get("model") or self.model

    def max_tokens_for(self, role: str) -> int:
        return min(
            self.max_tokens, self.model_max_tokens.get(self.model_for(role), self.max_tokens)
        )

    def recovery_tokens_for(self, role: str) -> int:
        return min(
            max(self.max_tokens, self.max_recovery_tokens),
            self.model_max_tokens.get(
                self.model_for(role), max(self.max_tokens, self.max_recovery_tokens)
            ),
        )

    def compact_threshold_for(self, role: str, output_tokens: int) -> int:
        context = self.model_context_tokens.get(self.model_for(role), 200000)
        return min(self.compact_threshold, max(1024, int(context * 0.75) - output_tokens))


def initialize(home: Path, workspace: Path) -> Config:
    home.mkdir(parents=True, exist_ok=True)
    config = (
        Config.load(home) if (home / "config.json").exists() else Config(str(workspace)).validate()
    )
    for role, instructions in ROLES.items():
        for folder in ("skills", "knowledge", "pending", "running", "results"):
            (home / "agents" / role / folder).mkdir(parents=True, exist_ok=True)
        guide = home / "agents" / role / "skills" / "ROLE.md"
        if not guide.exists():
            guide.write_text(f"# {role.title()}\n\n{instructions}\n", encoding="utf-8")
    (home / "runs").mkdir(exist_ok=True)
    if not (home / "config.json").exists():
        config.save(home)
    return config


def discover_home(start: Path) -> Path:
    for directory in [start, *start.parents]:
        candidate = directory / ".ehv-agent"
        if (candidate / "config.json").exists():
            return candidate
    return start / ".ehv-agent"
