from __future__ import annotations

import difflib
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .events import Events
from .sessions import SessionStore, read_json
from .workspace import Workspace, check_cancel, inside


def schema(
    name: str, description: str, properties: dict, required: list[str] | None = None
) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required if required is not None else list(properties),
            "additionalProperties": False,
        },
    }


STR = {"type": "string"}
READ_TOOLS = [
    schema(
        "read_session_history",
        "Read earlier asks and final answers in this conversation. Returns up to ten exchanges before the current assignment; queued future asks are excluded.",
        {"start_turn": {"type": "integer"}, "end_turn": {"type": "integer"}},
        [],
    ),
    schema(
        "list_files",
        "List staged project files. Paths are relative to the project root.",
        {"directory": STR},
        [],
    ),
    schema(
        "read_file",
        "Read a UTF-8 staged file by line range (up to 300 lines).",
        {"path": STR, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
        ["path"],
    ),
    schema(
        "search",
        "Find literal text in staged UTF-8 project files. Returns paths, line numbers, and excerpts.",
        {"text": STR, "directory": STR},
        ["text"],
    ),
    schema(
        "read_resource",
        "Read any of your own skill or knowledge files. Resource paths come from your context.",
        {"folder": {"enum": ["skills", "knowledge"]}, "path": STR},
    ),
    schema(
        "remember",
        "Save reusable knowledge for your role, excluding credentials. Use a relative .md path.",
        {"path": STR, "content": STR},
    ),
    schema(
        "get_changes",
        "Review staged changes versus the original project, including text diffs.",
        {},
    ),
]
WRITE_TOOLS = [
    schema(
        "write_file",
        "Create or replace a UTF-8 staged project file. The father later integrates approved changes.",
        {"path": STR, "content": STR},
    ),
    schema(
        "replace_text",
        "Replace an exact unique text fragment in a staged file. Fails if missing or ambiguous.",
        {"path": STR, "old": STR, "new": STR},
    ),
    schema("delete_file", "Delete one staged project file.", {"path": STR}),
    schema(
        "run_command",
        "Run a build/test/development command in the staged project, with live stdout and stderr. No shell syntax. args is an argv array. Use python for this app's interpreter; use npm/pnpm to install staged dependencies when needed. Commands can execute project code and are not an OS sandbox.",
        {
            "args": {"type": "array", "items": STR, "minItems": 1},
            "cwd": STR,
            "timeout": {"type": "integer"},
        },
        ["args"],
    ),
]
COMPLETE_TOOL = schema(
    "complete_task",
    "Finish this specialist task with a concrete handoff. Use blocked or failed when checks cannot run or requirements are not met. Do not claim unrun tests passed.",
    {
        "status": {"enum": ["passed", "blocked", "failed"]},
        "summary": STR,
    },
)
PLAN_TOOL = schema(
    "submit_plan",
    "Classify the request and choose the necessary work. For a simple question or explanation, return your final answer with steps=[]; no child agents will run. Otherwise assign only needed specialists, once each in requirement/design/coding/testing order. Implementation needs coding; validation needs testing. Inspect project evidence when relevant.",
    {
        "session_title": {
            "type": "string",
            "minLength": 5,
            "maxLength": 80,
            "description": "Give this session a specific, natural title for Recent Sessions, usually 4–8 words. Describe the actual goal, e.g. 'CSV Report Export and Validation'. No generic labels, IDs, copied prompt sentences, or Markdown.",
        },
        "task_type": {"enum": ["question", "analysis", "implementation", "validation"]},
        "reason": {
            "type": "string",
            "description": "Briefly explain why you can answer directly or why these specialists are needed.",
        },
        "answer": {
            "type": "string",
            "description": "The complete user-facing answer when steps is empty. Omit when delegating.",
        },
        "steps": {
            "type": "array",
            "minItems": 0,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "role": {"enum": ["requirement", "design", "coding", "testing"]},
                    "prompt": STR,
                },
                "required": ["role", "prompt"],
                "additionalProperties": False,
            },
        },
    },
    ["session_title", "task_type", "reason", "steps"],
)
REVIEW_TOOL = schema(
    "review_result",
    "Review every handoff and staged changes. Approve only when the request is fulfilled; project changes require passing independent testing. Read-only analysis does not need a testing pass. Request rework with concrete instructions for coding and testing, or reject incomplete work.",
    {
        "session_title": {
            "type": "string",
            "minLength": 5,
            "maxLength": 80,
            "description": "A concise, descriptive plain-text session title based on the actual work. Preserve the plan's title unless the scope changed. You, the father, own this name.",
        },
        "decision": {"enum": ["approve", "rework", "reject"]},
        "summary": STR,
    },
)
NAME_TOOL = schema(
    "name_session",
    "Name a past session from its saved request and results. Return a specific, natural title, usually 4–8 words, in the user's language. Use the actual goal, not a generic label, task ID, timestamp, raw prompt, or Markdown. Do not perform or rerun any project work.",
    {
        "session_title": {"type": "string", "minLength": 5, "maxLength": 80},
    },
)


class Toolset:
    def __init__(
        self,
        role: str,
        workspace: Workspace,
        home: Path,
        config: Config,
        events: Events,
        cancel: threading.Event,
    ):
        self.role = role
        self.workspace = workspace
        self.home = home
        self.config = config
        self.events = events
        self.cancel = cancel
        self.commands: list[dict] = []

    def definitions(self, mode: str) -> list[dict]:
        if mode == "name":
            return [NAME_TOOL]
        if mode == "plan":
            return [*READ_TOOLS, PLAN_TOOL]
        if mode == "review":
            return [*READ_TOOLS, REVIEW_TOOL]
        return [
            *READ_TOOLS,
            *(WRITE_TOOLS if self.role in {"coding", "testing"} else []),
            COMPLETE_TOOL,
        ]

    def resources(self) -> str:
        result = []
        root = self.home / "agents" / self.role
        for folder in ("skills", "knowledge"):
            for path in sorted((root / folder).rglob("*")):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to((root / folder).resolve())
                ):
                    result.append(f"{folder}/{path.relative_to(root / folder).as_posix()}")
        # The default role guide is small and always included. Other resources load on demand.
        guide = root / "skills" / "ROLE.md"
        return (
            "\n".join(result[:300]) + "\n\nROLE.md:\n" + guide.read_text(encoding="utf-8")[:12000]
        )

    def execute(self, name: str, arguments: dict):
        check_cancel(self.cancel)
        detail = json.dumps(arguments, ensure_ascii=False)
        if name in {"write_file", "remember", "replace_text"}:
            detail = str(arguments.get("path", ""))
        self.events.emit(self.role, "tool", f"{name}: {detail[:500]}")
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            raise ValueError(f"Unsupported tool: {name}")
        return handler(**arguments)

    def tool_read_session_history(self, start_turn: int = 1, end_turn: int | None = None) -> str:
        if start_turn < 1:
            raise ValueError("start_turn must be >= 1")
        request = read_json(self.workspace.run / "request.json")
        if not request.get("session_id"):
            return "No earlier conversation history"
        turns = SessionStore(self.home).prior_turns(request["session_id"], self.workspace.run.name)
        end = min(end_turn or start_turn + 9, start_turn + 9)
        return json.dumps(
            [
                {
                    "turn": index + 1,
                    "ask": turn.prompt,
                    "answer": turn.summary,
                    "status": turn.status,
                }
                for index, turn in enumerate(turns)
                if start_turn - 1 <= index < end
            ],
            ensure_ascii=False,
        )[:50000]

    def tool_list_files(self, directory: str = ".") -> str:
        parent = self.workspace.project_path(directory)
        names = [
            str(path.relative_to(self.workspace.root).as_posix())
            for _, path in self.workspace.files(parent)
        ]
        return "\n".join(names[:2000]) + (
            "\n[Truncated to 2000 files]" if len(names) > 2000 else ""
        )

    def tool_read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        if start_line < 1:
            raise ValueError("start_line must be >= 1")
        end_line = min(end_line or start_line + 299, start_line + 299)
        lines = self.workspace.project_path(path).read_text(encoding="utf-8").splitlines()
        return "\n".join(
            f"{i + 1}: {line[:2000]}"
            for i, line in enumerate(lines)
            if start_line - 1 <= i < end_line
        )[:40000]

    def tool_search(self, text: str, directory: str = ".") -> str:
        if not text:
            raise ValueError("Search text must not be empty")
        matches = []
        for _, path in self.workspace.files(self.workspace.project_path(directory)):
            check_cancel(self.cancel)
            if path.stat().st_size > 1024 * 1024:
                continue
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if text in line:
                        matches.append(
                            f"{path.relative_to(self.workspace.root).as_posix()}:{number}: {line[:400]}"
                        )
                        if len(matches) >= 100:
                            return "\n".join(matches) + "\n[Truncated to 100 matches]"
            except UnicodeError:
                continue
        return "\n".join(matches) or "No matches"

    def tool_read_resource(self, folder: str, path: str) -> str:
        if folder not in {"skills", "knowledge"}:
            raise ValueError("Resource folder must be skills or knowledge")
        return inside(self.home / "agents" / self.role / folder, path).read_text(encoding="utf-8")[
            :40000
        ]

    def tool_remember(self, path: str, content: str) -> str:
        if not path.endswith(".md"):
            raise ValueError("Knowledge files must use .md")
        destination = inside(self.home / "agents" / self.role / "knowledge", path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return "Knowledge saved"

    def tool_write_file(self, path: str, content: str) -> str:
        destination = self.workspace.project_path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return f"Staged {path} ({len(content)} characters)"

    def tool_replace_text(self, path: str, old: str, new: str) -> str:
        destination = self.workspace.project_path(path)
        content = destination.read_text(encoding="utf-8")
        if not old or content.count(old) != 1:
            raise ValueError("old must match exactly once; read the file and retry")
        destination.write_text(content.replace(old, new, 1), encoding="utf-8")
        return f"Updated {path}"

    def tool_delete_file(self, path: str) -> str:
        self.workspace.project_path(path).unlink()
        return f"Deleted staged file {path}"

    def tool_get_changes(self) -> str:
        changes = self.workspace.changes()
        output = []
        for change in changes[:100]:
            relative = change["path"]
            output.append(f"{change['action']}: {relative}")
            try:
                # Source may have changed; the final integration will detect this.
                old = (
                    inside(self.workspace.source, relative)
                    .read_text(encoding="utf-8")
                    .splitlines(True)
                    if change["before"]
                    else []
                )
                new = (
                    self.workspace.project_path(relative)
                    .read_text(encoding="utf-8")
                    .splitlines(True)
                    if change["after"]
                    else []
                )
                output.extend(
                    difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}")
                )
            except UnicodeError:
                output.append("[Binary file changed]")
            if sum(map(len, output)) > 40000:
                output.append("[Diff truncated; inspect specific files]")
                break
        return "\n".join(output) or "No project changes"

    def tool_run_command(self, args: list[str], cwd: str = ".", timeout: int | None = None) -> dict:
        if not args or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise ValueError("args must be a nonempty array of strings")
        allowed = {
            "python",
            "python3",
            "pytest",
            "node",
            "npm",
            "npx",
            "pnpm",
            "yarn",
            "uv",
            "ruff",
            "mypy",
            "cargo",
            "go",
            "dotnet",
            "make",
            "cmake",
        }
        if args[0] not in allowed:
            raise ValueError(f"Allowed executables: {', '.join(sorted(allowed))}")
        import sys

        executable = sys.executable if args[0] in {"python", "python3"} else shutil.which(args[0])
        if not executable:
            raise ValueError(f"Executable is not installed: {args[0]}")
        argv = [executable, *args[1:]]
        if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            # npm and friends ship cmd shims. Resolve the JS entrypoint and invoke
            # Node directly so metacharacters never pass through cmd.exe.
            shim = Path(executable)
            entries = {
                "npm": "node_modules/npm/bin/npm-cli.js",
                "npx": "node_modules/npm/bin/npx-cli.js",
                "pnpm": "node_modules/pnpm/bin/pnpm.cjs",
                "yarn": "node_modules/yarn/bin/yarn.js",
            }
            entry = shim.parent / entries.get(args[0], "__unsupported__")
            node = shutil.which("node")
            if not node or not entry.is_file():
                raise ValueError(
                    f"Cannot resolve {args[0]} cmd shim to a JS executable; install it with npm globally or use node with the JS entrypoint"
                )
            argv = [node, str(entry), *args[1:]]
        duration = min(timeout or self.config.command_timeout, self.config.command_timeout)
        if duration < 1:
            raise ValueError("timeout must be positive")
        # Do not intentionally hand the API credential to subprocesses.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
        }
        env["PYTHONUNBUFFERED"] = "1"
        env["NO_COLOR"] = "1"
        self.events.status(self.role, "working", f"Running: {' '.join(args)}")
        process = subprocess.Popen(
            argv,
            cwd=self.workspace.project_path(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=256)

        def read_output():
            try:
                while block := process.stdout.read1(1024):
                    while True:
                        try:
                            chunks.put(block, timeout=0.1)
                            break
                        except queue.Full:
                            if process.poll() is not None:
                                return
            finally:
                try:
                    chunks.put(None, timeout=0.2)
                except queue.Full:
                    pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        output = ""
        deadline = time.monotonic() + duration
        timed_out = False
        try:
            while True:
                check_cancel(self.cancel)
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                try:
                    block = chunks.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if block is None:
                    break
                text = block.decode("utf-8", errors="replace")
                output = (output + text)[-30000:]
                self.events.emit(self.role, "output", text)
        finally:
            # Also clean up descendant processes on timeout and cancellation.
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            reader.join(timeout=1)
            process.stdout.close()
        result = {
            "args": args,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output": output,
        }
        self.commands.append(result)
        self.events.emit(
            self.role,
            "command_done",
            f"Exit {process.returncode}" + (" (timed out)" if timed_out else ""),
        )
        return result
