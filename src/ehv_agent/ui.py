from __future__ import annotations

import json
import queue
import re
import shlex
from collections import deque
from pathlib import Path

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events as terminal_events
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, VerticalScroll
from textual.geometry import Size
from textual.widgets import Footer, Input, Markdown, RichLog, Static, TabbedContent, TabPane

from .config import ROLES, Config
from .engine import Engine
from .events import Events
from .terminal import WindowsConsole

COLORS = {
    "father": "#bda3ff",
    "requirement": "#55c9ff",
    "design": "#f3abdd",
    "coding": "#6ae1b4",
    "testing": "#ffc971",
}
ICONS = {
    "idle": "○",
    "queued": "◷",
    "working": "●",
    "waiting": "◌",
    "done": "✓",
    "error": "!",
    "cancelled": "×",
}
HELP = """Type a request and press Enter to queue a workflow.
/workspace PATH     Set the project folder for future workflows (quotes optional)
/model MODEL [ROLE] Set the default model or one agent's model
/models [ROLE]      Scan available Claude models and open model settings (F2)
/reload             Validate and reload config.json
/pause /resume      Pause or resume claiming new workflows; current work continues
/cancel             Cancel the active workflow; preserve staged files
/tasks              List pending and recent results
/retry TASK_ID      Queue a fresh attempt of a completed/failed parent workflow
/focus ROLE|all     Filter the live activity log
/redraw             Redraw the screen after a terminal resize (Ctrl+L)
/help               Show these commands
/quit               Cancel active work and exit
Drop {"prompt":"..."} JSON files into an agent's pending folder to schedule work.
Optional not_before: ISO timestamp with timezone. Ctrl+P pauses; Ctrl+X cancels.
"""


class AgentCard(Static):
    def __init__(self, role: str, model: str):
        super().__init__(id=f"agent-{role}", classes="agent-card")
        self.role = role
        self.model = model
        self.state = "idle"
        self.action_text = "Watching pending tasks"
        self.preview = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.styles.border = ("round", COLORS[role])

    def on_mount(self) -> None:
        self.refresh_content()

    def refresh_content(self) -> None:
        self.border_title = f" {self.role.upper()} "
        self.border_subtitle = f" {compact_number(self.input_tokens + self.output_tokens)} tokens "
        text = Text(no_wrap=True, overflow="ellipsis")
        color = "#ff777f" if self.state == "error" else COLORS[self.role]
        text.append(f"{ICONS.get(self.state, '○')} {self.state.upper()}", style=f"bold {color}")
        action = self.action_text
        if self.state in {"done", "error", "cancelled"}:
            action = {
                "done": "Completed · full report in Response",
                "error": "Needs attention · see Response",
                "cancelled": "Cancelled · staged work preserved",
            }[self.state]
        text.append("\n" + plain_summary(action), style="#d7dce6")
        preview = self.action_text if self.state in {"done", "error", "cancelled"} else self.preview
        text.append("\n" + (plain_summary(preview) if preview else self.model), style="#8995aa")
        self.update(text)

    def on_click(self) -> None:
        self.app.set_focus_role(self.role)


class AgentApp(App):
    TITLE = "EHV Agent"
    CSS = """
    Screen { background: #0b101b; color: #d7dce6; }
    #brand { height: 1; padding: 0 2; text-style: bold; color: #bda3ff; }
    #workspace { height: 2; padding: 0 2; color: #8995aa; }
    #dashboard { height: auto; max-height: 22; padding: 0 1; }
    .agent-card { height: 6; padding: 0 1; margin: 0 1; overflow-y: hidden; }
    #agent-father { height: 5; }
    #team { grid-size: 2 2; grid-gutter: 0; height: 12; }
    #activity-label { height: 1; padding: 0 1; color: #8995aa; }
    #panels { height: 1fr; min-height: 7; margin: 0 2; }
    TabPane { padding: 0; }
    #activity { height: 1fr; background: #0b101b; border: round #28354b; padding: 0 1; }
    #response-scroll { height: 1fr; background: #0b101b; border: round #28354b; padding: 0 1; }
    #response { background: #0b101b; padding: 0 1; margin: 0; }
    #response-role { height: 1; color: #8995aa; padding: 0 1; }
    #prompt { height: 3; margin: 0 2; border: round #6ae1b4; }
    Footer { background: #141d2e; }
    """
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Quit"),
        Binding("ctrl+c", "request_quit", "Quit", show=False),
        Binding("ctrl+x", "cancel_job", "Cancel"),
        Binding("ctrl+p", "toggle_pause", "Pause/resume"),
        Binding("ctrl+l", "redraw", "Redraw"),
        Binding("f1", "help", "Help"),
        Binding("f2", "models", "Models"),
    ]

    def __init__(
        self, home: Path, engine: Engine, events: Events, terminal: WindowsConsole | None = None
    ):
        super().__init__()
        self.home = home
        self.engine = engine
        self.events = events
        self.terminal = terminal
        self.config = Config.load(home)
        self.cards = {role: AgentCard(role, self.config.model_for(role)) for role in ROLES}
        self.history: deque[tuple[str, str, str]] = deque(maxlen=3000)
        self.pending_text = {role: "" for role in ROLES}
        self.pending_output = {role: "" for role in ROLES}
        self.responses = {role: "" for role in ROLES}
        self.response_role = "father"
        self.response_dirty = False
        self.focus_role = "all"
        self.closing = False

    def compose(self) -> ComposeResult:
        yield Static("EHV AGENT    /    SERIAL MULTI-AGENT WORKBENCH", id="brand")
        yield Static(id="workspace", markup=False)
        with VerticalScroll(id="dashboard"):
            yield self.cards["father"]
            with Grid(id="team"):
                for role in ("requirement", "design", "coding", "testing"):
                    yield self.cards[role]
        with TabbedContent(initial="response-tab", id="panels"):
            with TabPane("Response", id="response-tab"):
                yield Static("FATHER · live response", id="response-role", markup=False)
                with VerticalScroll(id="response-scroll"):
                    yield Markdown(
                        "Type a request below to begin. Agent responses will appear here with formatted text, lists, and code.",
                        id="response",
                        open_links=False,
                    )
            with TabPane("Activity", id="activity-tab"):
                yield Static("ACTIVITY · all agents", id="activity-label", markup=False)
                yield RichLog(
                    id="activity", wrap=True, markup=False, highlight=False, max_lines=3000
                )
        yield Input(placeholder="Describe a task…  /help for commands", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.update_workspace()
        self.write_log("father", "info", f"Agent folders: {self.home / 'agents'}")
        self.write_log(
            "father", "info", "One role at a time · staged edits · father review · /help"
        )
        self.query_one(Input).focus()
        self.set_interval(0.06, self.drain_events)
        self.set_interval(0.2, self.render_response)
        self.set_interval(1, self.update_workspace)
        if self.terminal is not None:
            self.set_interval(0.25, self.sync_terminal_size)
            self.sync_terminal_size()
        self.engine.start()

    def on_resize(self, event) -> None:
        if not self.is_mounted:
            return
        if self.terminal is not None:
            viewport = self.terminal.size()
            if viewport is not None and event.size != Size(*viewport):
                self.post_message(terminal_events.Resize(Size(*viewport), Size(*viewport)))
                return
        team = self.query_one("#team", Grid)
        columns = 4 if event.size.width >= 160 else 2
        team.styles.grid_size_columns = columns
        team.styles.grid_size_rows = 1 if columns == 4 else 2
        team.styles.height = 6 if columns == 4 else 12
        self.query_one("#dashboard").styles.max_height = max(5, event.size.height - 15)
        self.refresh(layout=True)

    def sync_terminal_size(self) -> None:
        if self.terminal is None:
            return
        viewport = self.terminal.size()
        if viewport is not None and self.size != Size(*viewport):
            self.post_message(terminal_events.Resize(Size(*viewport), Size(*viewport)))

    def action_redraw(self) -> None:
        self.sync_terminal_size()
        self.refresh(layout=True)

    def update_workspace(self) -> None:
        paused = "PAUSED" if self.engine.paused.is_set() else "WATCHING"
        active = f" · active {self.engine.active}" if self.engine.active else ""
        self.query_one("#workspace", Static).update(
            f"{self.config.workspace}\n{paused}{active} · config {self.home / 'config.json'}"
        )
        if self.closing and (not self.engine.thread or not self.engine.thread.is_alive()):
            self.exit()

    def write_log(self, role: str, kind: str, text: str, remember: bool = True) -> None:
        if remember:
            self.history.append((role, kind, text))
        if self.focus_role not in {"all", role}:
            return
        if kind in {"text", "report"}:
            self.query_one("#activity", RichLog).write(
                Group(
                    Text(role.upper(), style=f"bold {COLORS.get(role, 'white')}"),
                    RichMarkdown(text, code_theme="monokai"),
                    Text(""),
                )
            )
            return
        line = Text()
        line.append(f"{role:11} ", style=COLORS.get(role, "white"))
        line.append(text, style="#ff777f" if kind in {"error", "tool_error"} else "#d7dce6")
        self.query_one("#activity", RichLog).write(line)

    def drain_events(self) -> None:
        dirty = set()
        for _ in range(300):
            try:
                event = self.events.queue.get_nowait()
            except queue.Empty:
                break
            role, kind, text = event["role"], event["kind"], event["text"]
            if role not in self.cards:
                continue
            card = self.cards[role]
            dirty.add(role)
            if kind == "text":
                self.pending_text[role] += text
                card.preview = (card.preview + text)[-1000:]
                self.set_response(role, self.pending_text[role])
            elif kind == "output":
                card.preview = (card.preview + text)[-1000:]
                self.pending_output[role] += text
                while "\n" in self.pending_output[role]:
                    line, self.pending_output[role] = self.pending_output[role].split("\n", 1)
                    self.write_log(role, kind, line)
                if len(self.pending_output[role]) > 2000:
                    self.write_log(role, kind, self.pending_output[role])
                    self.pending_output[role] = ""
            elif kind == "usage":
                card.input_tokens += event.get("input_tokens", 0)
                card.output_tokens += event.get("output_tokens", 0)
            else:
                if self.pending_text[role]:
                    self.write_log(role, "text", self.pending_text[role])
                    self.pending_text[role] = ""
                if self.pending_output[role]:
                    self.write_log(role, "output", self.pending_output[role])
                    self.pending_output[role] = ""
                if kind == "status":
                    card.state = event["status"]
                    card.action_text = text
                    if card.state in {"working", "queued"}:
                        card.preview = ""
                    if card.state in {"done", "error", "cancelled"}:
                        self.set_response(role, text)
                        if role == "father" and self.focus_role == "all":
                            self.query_one("#panels", TabbedContent).active = "response-tab"
                elif kind == "tool":
                    card.action_text = text
                self.write_log(
                    role,
                    "report"
                    if kind == "status" and event.get("status") in {"done", "error", "cancelled"}
                    else kind,
                    text,
                )
        for role in dirty:
            self.cards[role].refresh_content()

    def set_response(self, role: str, text: str) -> None:
        self.responses[role] = text
        if self.focus_role in {"all", role}:
            self.response_role = role
            self.response_dirty = True

    async def render_response(self) -> None:
        if not self.response_dirty:
            return
        self.response_dirty = False
        self.query_one("#response-role", Static).update(
            f"{self.response_role.upper()} · full response · click an agent to focus"
        )
        scroll = self.query_one("#response-scroll", VerticalScroll)
        follow = scroll.is_vertical_scroll_end
        await self.query_one("#response", Markdown).update(self.responses[self.response_role])
        if follow:
            scroll.scroll_end(animate=False)

    def set_focus_role(self, role: str) -> None:
        if role not in {"all", *ROLES}:
            raise ValueError("Choose all, father, requirement, design, coding, or testing")
        self.focus_role = role
        selected = role if role != "all" else self.response_role
        if self.responses.get(selected):
            self.response_role = selected
            self.response_dirty = True
        self.query_one("#activity-label", Static).update(
            f"ACTIVITY · {role} · click an agent to filter, /focus all to reset"
        )
        self.query_one("#activity", RichLog).clear()
        for saved_role, kind, text in self.history:
            self.write_log(saved_role, kind, text, remember=False)

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.closing:
            return
        event.input.value = ""
        try:
            if text.startswith("/"):
                self.command(text)
            else:
                self.engine.enqueue(text)
        except (ValueError, OSError, TypeError) as error:
            self.write_log("father", "error", str(error))

    def command(self, text: str) -> None:
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if command not in {"/focus", "/redraw", "/models", "/settings"}:
            self.query_one("#panels", TabbedContent).active = "activity-tab"
        if command == "/workspace":
            path = argument.strip('"').strip("'")
            if not path:
                raise ValueError("Usage: /workspace PATH")
            config = Config.load(self.home)
            config.workspace = str(Path(path).expanduser().resolve())
            config.save(self.home)
            self.reload_config()
            self.write_log(
                "father",
                "info",
                "Workspace updated for future workflows. Active workflow keeps its original folder.",
            )
        elif command == "/model":
            parts = shlex.split(argument)
            if not parts:
                self.action_models()
                return
            if not 1 <= len(parts) <= 2:
                raise ValueError("Usage: /model MODEL [ROLE]")
            config = Config.load(self.home)
            if len(parts) == 1:
                config.model = parts[0]
            else:
                if parts[1] not in ROLES:
                    raise ValueError("Unknown agent role")
                config.agents[parts[1]]["model"] = parts[0]
            config.save(self.home)
            self.reload_config()
        elif command in {"/models", "/settings"}:
            self.action_models(argument or "default")
        elif command == "/reload":
            self.reload_config()
        elif command == "/pause":
            self.engine.paused.set()
            self.write_log(
                "father", "info", "Paused claiming new workflows; active workflow continues"
            )
        elif command == "/resume":
            self.engine.paused.clear()
            self.engine.wake.set()
            self.write_log("father", "info", "Scheduler resumed")
        elif command == "/cancel":
            self.action_cancel_job()
        elif command == "/retry":
            task = self.engine.store.retry(argument)
            self.engine.wake.set()
            self.write_log("father", "info", f"Retry queued: {task['id']}")
        elif command == "/focus":
            self.set_focus_role(argument or "all")
        elif command == "/tasks":
            for role in ROLES:
                for state in ("pending", "running", "results"):
                    paths = sorted(
                        self.engine.store.folder(role, state).glob("*.json"),
                        key=lambda p: p.stat().st_mtime,
                    )[-8:]
                    for path in paths:
                        try:
                            task = json.loads(path.read_text(encoding="utf-8"))
                            self.write_log(
                                role,
                                "info",
                                f"{path.stem} [{task.get('status', state)}] {task.get('prompt', task.get('error', ''))[:140]}",
                            )
                        except (ValueError, OSError):
                            pass
        elif command == "/redraw":
            self.action_redraw()
        elif command in {"/quit", "/exit"}:
            self.action_request_quit()
        elif command == "/help":
            self.action_help()
        else:
            raise ValueError("Unknown command; type /help")

    def reload_config(self) -> None:
        self.config = Config.load(self.home)
        for role, card in self.cards.items():
            card.model = self.config.model_for(role)
            card.refresh_content()
        self.engine.wake.set()
        self.update_workspace()
        self.write_log("father", "info", "Configuration loaded; changes apply to the next workflow")

    def action_help(self) -> None:
        self.query_one("#panels", TabbedContent).active = "activity-tab"
        self.write_log("father", "info", HELP)

    def action_models(self, target: str = "default") -> None:
        from .model_picker import ModelPicker
        from .models import TARGETS

        if target not in TARGETS:
            raise ValueError(
                "Usage: /models [default|all|father|requirement|design|coding|testing]"
            )
        if self.closing or isinstance(self.screen, ModelPicker):
            return
        self.push_screen(ModelPicker(self.home, target), self.model_selected)

    def model_selected(self, result: tuple[str, str] | None) -> None:
        if result is not None:
            self.reload_config()
            target, model = result
            self.write_log(
                "father", "info", f"Saved model for {target}: {model or 'inherit default'}"
            )

    def action_toggle_pause(self) -> None:
        self.command("/resume" if self.engine.paused.is_set() else "/pause")

    def action_cancel_job(self) -> None:
        if self.engine.active:
            self.engine.cancel_active()
            self.write_log(
                "father", "info", "Cancellation requested; waiting for active operation to stop"
            )
        else:
            self.write_log("father", "info", "No active workflow")

    def action_request_quit(self) -> None:
        if not self.closing:
            self.closing = True
            self.query_one(Input).disabled = True
            self.write_log("father", "info", "Stopping scheduler and active work…")
            self.engine.shutdown()
            self.update_workspace()

    def on_unmount(self) -> None:
        self.engine.shutdown()


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def plain_summary(value: str) -> str:
    """Keep verbose Markdown reports out of the compact status cards."""
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[`*#]", "", value)
    return " ".join(value.split())[:500]
