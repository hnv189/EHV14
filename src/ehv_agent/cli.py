from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from filelock import FileLock, Timeout

from . import __version__
from .config import ROLES, Config, discover_home, initialize
from .engine import Engine
from .events import Events


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="EHV Agent - serial Claude agents for your project")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("init", "Create agent folders and configuration"),
        ("start", "Start the live terminal dashboard"),
        ("enqueue", "Add a task, including while the app is running"),
        ("config", "Show or change configuration"),
        ("tasks", "List persisted pending, running, and finished tasks"),
        ("doctor", "Check local setup without making an API call"),
        ("models", "Scan Claude models and select the default or an agent's model"),
    ]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--home", type=Path, help="Agent data folder (default: nearest .ehv-agent)"
        )
        if name in {"init", "start"}:
            command.add_argument("--workspace", type=Path, help="Project folder")
            command.add_argument("--model", help="Default Claude API model ID")
        if name == "start":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument(
                "--headless", action="store_true", help="Watch queues with plain JSONL output"
            )
            mode.add_argument(
                "--once", action="store_true", help="Process one pending task without dashboard"
            )
            mode.add_argument(
                "--new-window",
                action="store_true",
                help="Open the dashboard in a new Windows Command Prompt",
            )
        if name == "enqueue":
            command.add_argument("prompt")
            command.add_argument("--agent", choices=list(ROLES), default="father")
            command.add_argument(
                "--at", help="Do not start before this ISO timestamp (include timezone)"
            )
        if name == "config":
            command.add_argument(
                "key",
                nargs="?",
                choices=[
                    "workspace",
                    "model",
                    "proxy_mode",
                    "proxy_url",
                    "poll_seconds",
                    "max_turns",
                    "max_tokens",
                    "auto_compact",
                    "compact_threshold",
                    "max_token_retries",
                    "max_recovery_tokens",
                    "command_timeout",
                    "max_rework",
                ],
            )
            command.add_argument("value", nargs="?")
        if name == "models":
            command.add_argument("--agent", choices=["default", "all", *ROLES], default="default")
            action = command.add_mutually_exclusive_group()
            action.add_argument("--select", action="store_true", help="Choose from a numbered list")
            action.add_argument(
                "--set", metavar="MODEL_ID", help="Scan and select this available model"
            )
            action.add_argument(
                "--inherit",
                action="store_true",
                help="Reset an individual agent to the default model",
            )
            action.add_argument(
                "--json", action="store_true", help="Print the available model catalogue as JSON"
            )
    return root


def print_events(events: Events) -> None:
    import queue

    while True:
        try:
            print(json.dumps(events.queue.get_nowait(), ensure_ascii=False), flush=True)
        except queue.Empty:
            return


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = (args.home or discover_home(Path.cwd())).expanduser().resolve()
    workspace = (getattr(args, "workspace", None) or Path.cwd()).expanduser().resolve()
    try:
        config = initialize(home, workspace)
        if args.command in {"init", "start"}:
            if args.workspace:
                config.workspace = str(workspace)
            if args.model:
                config.model = args.model
            config.save(home)
        if args.command == "init":
            print(f"Initialized {home}\nWorkspace: {config.workspace}\nModel: {config.model}")
        elif args.command == "config":
            if args.key and args.value is not None:
                value = args.value
                if args.key == "workspace":
                    value = str(Path(value).expanduser().resolve())
                elif args.key == "poll_seconds":
                    value = float(value)
                elif args.key == "auto_compact":
                    if value.lower() not in {"true", "false", "on", "off", "1", "0"}:
                        raise ValueError("auto_compact must be true or false")
                    value = value.lower() in {"true", "on", "1"}
                elif args.key in {
                    "max_turns",
                    "max_tokens",
                    "command_timeout",
                    "max_rework",
                    "compact_threshold",
                    "max_token_retries",
                    "max_recovery_tokens",
                }:
                    value = int(value)
                setattr(config, args.key, value)
                config.save(home)
            print((home / "config.json").read_text(encoding="utf-8"))
        elif args.command == "enqueue":
            extra = {}
            if args.at:
                from datetime import datetime

                parsed = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("--at needs an ISO timestamp with a timezone")
                extra["not_before"] = parsed.isoformat()
            task = Engine(home, Events(home)).enqueue(args.prompt, args.agent, **extra)
            print(f"Queued {task['id']} for {args.agent}")
        elif args.command == "tasks":
            for role in ROLES:
                for state in ("pending", "running", "results"):
                    for path in sorted((home / "agents" / role / state).glob("*.json")):
                        try:
                            task = json.loads(path.read_text(encoding="utf-8"))
                            print(
                                f"{path.stem}  {role:12} {task.get('status', state):12} {task.get('prompt', '')[:100]}"
                            )
                        except (OSError, ValueError):
                            print(f"{path.name}  INVALID JSON")
        elif args.command == "doctor":
            print(
                f"Python: {sys.version.split()[0]}\nWorkspace: {config.workspace}\nAgent home: {home}\nModel: {config.model}"
            )
            print(
                "ANTHROPIC_API_KEY: "
                + ("set (value hidden)" if os.environ.get("ANTHROPIC_API_KEY") else "missing")
            )
            print("Config valid. No API request made.")
        elif args.command == "start":
            return start(args, home)
        elif args.command == "models":
            from .models import cli_models

            return cli_models(args, home)
        return 0
    except Timeout:
        print(
            f"ehv-agent: Another instance is already using {home}. Quit the existing dashboard with /quit or Ctrl+Q before starting a new one. You can change models without a second dashboard: ehv-agent models --select",
            file=sys.stderr,
        )
        return 1
    except (ValueError, TypeError, OSError) as error:
        print(f"ehv-agent: {error}", file=sys.stderr)
        return 1


def start(args: argparse.Namespace, home: Path) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ehv-agent: ANTHROPIC_API_KEY is missing. Set it in your terminal, then run start again. Use doctor to check setup.",
            file=sys.stderr,
        )
        return 1
    if args.new_window:
        from .launcher import open_cmd_window

        open_cmd_window(home)
        print("Opened EHV Agent in a new Command Prompt window.")
        return 0
    events = Events(home)
    engine = Engine(home, events)
    with FileLock(str(home / "app.lock"), timeout=0):
        try:
            if args.once:
                engine.store.recover()
                task = engine.store.next_pending()
                if not task:
                    print("No due pending tasks.")
                    return 0
                import threading

                worker = threading.Thread(target=engine.run_task, args=(task, Config.load(home)))
                engine.thread = worker
                worker.start()
                while worker.is_alive():
                    print_events(events)
                    worker.join(timeout=0.1)
                print_events(events)
                return 0 if task.get("status") == "completed" else 1
            if args.headless:
                engine.start()
                while engine.thread.is_alive():
                    print_events(events)
                    time.sleep(0.1)
            else:
                from .terminal import terminal_session
                from .ui import AgentApp

                with terminal_session() as terminal:
                    AgentApp(home, engine, events, terminal=terminal).run(
                        size=terminal.size() if terminal is not None else None
                    )
        except KeyboardInterrupt:
            engine.shutdown()
        finally:
            engine.shutdown()
            if engine.thread:
                while engine.thread.is_alive():
                    engine.thread.join(timeout=0.1)
                    if args.headless or args.once:
                        print_events(events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
