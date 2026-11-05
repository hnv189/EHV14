from __future__ import annotations

import asyncio
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
from textual.containers import Grid, Vertical, VerticalScroll
from textual.geometry import Size
from textual.widgets import Footer, Input, Markdown, RichLog, Static, TabbedContent, TabPane

from .chat import ChatTurn
from .config import ROLES, Config
from .engine import Engine
from .events import Events
from .model_picker import ModelPicker
from .session_browser import SessionBrowser, SessionDetails
from .terminal import WindowsConsole
from .usage import cost_label

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
/settings           Configure models and proxy connection settings (F2)
/reload             Validate and reload config.json
/pause /resume      Pause or resume claiming new workflows; current work continues
/cancel             Cancel the active workflow; preserve staged files
/tasks              List pending and recent results
/sessions [ID]      Open Recent Sessions or inspect a saved session (F3)
/newsession         Start a fresh conversation; keep previous sessions saved
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
        self.cost_usd = None
        self.cost_complete = False
        self.last_cost_usd = None
        self.last_usage_complete = False
        self.styles.border = ("round", COLORS[role])

    def on_mount(self) -> None:
        self.refresh_content()

    def refresh_content(self) -> None:
        self.border_title = f" {self.role.upper()} "
        self.border_subtitle = (
            f" {compact_number(self.input_tokens + self.output_tokens)} tokens · "
            + cost_label(self.cost_usd, self.cost_complete)
            + " "
        )
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
    #conversation { height: auto; }
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
        Binding("f2", "models", "Settings"),
        Binding("f3", "sessions", "Sessions"),
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
        self.session_titles: dict[str, str] = {}
        self.session_usage: dict = {}
        self.cost_session_id: str | None = None
        self.current_session_id = self.engine.sessions.current_id(self.config.workspace)
        self.chat_title = "New session"
        self.chat_widgets: dict[str, ChatTurn] = {}
        self.chat_live: dict[str, str] = {}
        self.chat_usage: dict[str, dict] = {}
        self.chat_dirty: set[str] = set()
        self.chat_rendered_id: str | None = None
        self.chat_refreshing = False
        self.chat_scroll_to_end = True
        if self.current_session_id:
            self.cost_session_id = self.current_session_id

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
                    yield Vertical(id="conversation")
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
            with TabPane("Recent Sessions", id="sessions-tab"):
                yield SessionBrowser(self.home)
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
        self.set_interval(1.5, self.refresh_chat)
        self.call_after_refresh(self.refresh_chat)
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
        active = (
            f" · {self.session_titles.get(self.engine.active, 'father is preparing a session')}"
            if self.engine.active
            else ""
        )
        # A queued timer can fire after the dashboard has unmounted on shutdown.
        for label in self.query("#workspace"):
            label.update(
                f"{self.config.workspace}\n{paused}{active} · Session "
                + cost_label(
                    self.session_usage.get("cost_usd"), self.session_usage.get("complete", False)
                )
                + f" · config {self.home / 'config.json'}"
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
            if (
                self.current_session_id is None
                and kind in {"queued", "job"}
                and isinstance(event.get("session_id"), str)
                and self.engine.sessions.conversation_path(event["session_id"]).exists()
            ):
                self.select_session(event["session_id"])
            self.track_chat_event(event)
            card = self.cards[role]
            dirty.add(role)
            if kind == "job" and self.current_session_id is None:
                self.session_usage = {}
                self.cost_session_id = event.get("task_id")
                self.response_dirty = True
                for other in self.cards.values():
                    other.input_tokens = other.output_tokens = 0
                    other.cost_usd = other.last_cost_usd = None
                    other.cost_complete = other.last_usage_complete = False
                dirty.update(self.cards)
            if kind == "session_named":
                self.session_titles[event["task_id"]] = event["session_title"]
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
                summary = event.get("session_usage")
                other_session_naming = event.get(
                    "phase"
                ) == "name" and self.cost_session_id not in (None, event.get("session_id"))
                selected_session = self.current_session_id is None or event.get("session_id") in (
                    None,
                    self.current_session_id,
                )
                if isinstance(summary, dict) and not other_session_naming and selected_session:
                    self.session_usage = summary
                    self.cost_session_id = event.get("session_id")
                    for name, totals in summary.get("by_role", {}).items():
                        if name not in self.cards:
                            continue
                        other = self.cards[name]
                        tokens = totals["tokens"]
                        other.input_tokens = sum(
                            value for key, value in tokens.items() if key != "output"
                        )
                        other.output_tokens = tokens["output"]
                        other.cost_usd = totals["cost_usd"]
                        other.cost_complete = totals["complete"]
                        other.last_cost_usd = totals["last_cost_usd"]
                        other.last_usage_complete = totals["last_usage_complete"]
                        dirty.add(name)
                    self.update_workspace()
                    self.response_dirty = True
                elif not isinstance(summary, dict):
                    card.input_tokens += event.get("input_tokens", 0) + event.get(
                        "cache_input_tokens", 0
                    )
                    card.output_tokens += event.get("output_tokens", 0)
                if self.pending_text[role]:
                    self.write_log(role, "text", self.pending_text[role])
                    self.pending_text[role] = ""
                self.write_log(role, "usage", text)
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
                        if (
                            role == "father"
                            and (
                                self.current_session_id is None
                                or event.get("session_id") in (None, self.current_session_id)
                            )
                            and self.focus_role == "all"
                            and self.query_one("#panels", TabbedContent).active != "sessions-tab"
                        ):
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

    def track_chat_event(self, event: dict) -> None:
        if not self.current_session_id or event.get("session_id") != self.current_session_id:
            return
        task_id = event.get("task_id")
        if not isinstance(task_id, str):
            return
        kind, text, role = event["kind"], event["text"], event["role"]
        if kind == "usage" and isinstance(event.get("exchange_usage"), dict):
            self.chat_usage[task_id] = event["exchange_usage"]
            self.chat_dirty.add(task_id)
        if event.get("phase") != "name" and role == "father":
            if kind == "text":
                self.chat_live[task_id] = self.chat_live.get(task_id, "") + text
                self.chat_dirty.add(task_id)
            elif kind == "status":
                if event.get("status") in {"done", "error", "cancelled"}:
                    self.chat_live[task_id] = text
                    self.chat_dirty.add(task_id)
                    self.call_after_refresh(self.refresh_chat)
                elif event.get("status") == "working" and text.startswith(("Plan ·", "Review ·")):
                    self.chat_live[task_id] = ""
        if kind in {"queued", "job", "session_named"}:
            self.call_after_refresh(self.refresh_chat)
        self.response_dirty = True

    def apply_session_usage(self, summary: dict) -> None:
        for role, card in self.cards.items():
            totals = summary.get("by_role", {}).get(role)
            if totals is None:
                card.input_tokens = card.output_tokens = 0
                card.cost_usd = "0"
                card.cost_complete = True
                card.last_cost_usd = None
                card.last_usage_complete = False
            else:
                tokens = totals["tokens"]
                card.input_tokens = sum(value for key, value in tokens.items() if key != "output")
                card.output_tokens = tokens["output"]
                card.cost_usd = totals["cost_usd"]
                card.cost_complete = totals["complete"]
                card.last_cost_usd = totals.get("last_cost_usd")
                card.last_usage_complete = totals.get("last_usage_complete", False)
            card.refresh_content()

    def set_response(self, role: str, text: str) -> None:
        self.responses[role] = text
        if self.focus_role in {"all", role}:
            self.response_role = role
            self.response_dirty = True

    async def render_response(self) -> None:
        if not self.response_dirty:
            return
        self.response_dirty = False
        show_chat = bool(self.current_session_id) and self.focus_role == "all"
        self.query_one("#conversation").display = (
            show_chat and self.chat_rendered_id == self.current_session_id
        )
        self.query_one("#response").display = not show_chat
        if show_chat:
            self.query_one("#response-role", Static).update(
                f"{self.chat_title} · Session total "
                + cost_label(
                    self.session_usage.get("cost_usd"), self.session_usage.get("complete", False)
                )
            )
            scroll = self.query_one("#response-scroll", VerticalScroll)
            follow = scroll.is_vertical_scroll_end or self.chat_scroll_to_end
            position = scroll.scroll_y
            for task_id in list(self.chat_dirty):
                widget = self.chat_widgets.get(task_id)
                if widget is not None:
                    if task_id in self.chat_live:
                        await widget.update_answer(self.chat_live[task_id])
                    if task_id in self.chat_usage:
                        widget.update_usage(self.chat_usage[task_id])
                    self.chat_dirty.discard(task_id)
            if follow:
                self.call_after_refresh(scroll.scroll_end, animate=False)
            else:
                scroll.scroll_to(y=position, animate=False)
            self.chat_scroll_to_end = False
            return
        card = self.cards[self.response_role]
        self.query_one("#response-role", Static).update(
            f"{self.response_role.upper()} · Message {cost_label(card.last_cost_usd, card.last_usage_complete)}"
            + " · Session "
            + cost_label(
                self.session_usage.get("cost_usd"), self.session_usage.get("complete", False)
            )
        )
        scroll = self.query_one("#response-scroll", VerticalScroll)
        follow = scroll.is_vertical_scroll_end
        await self.query_one("#response", Markdown).update(self.responses[self.response_role])
        if follow:
            scroll.scroll_end(animate=False)

    async def refresh_chat(self) -> None:
        session_id = self.current_session_id
        if not session_id or self.chat_refreshing or self.closing:
            return
        self.chat_refreshing = True
        try:
            title, turns, usage = await asyncio.to_thread(self.engine.sessions.view, session_id)
            if session_id != self.current_session_id or not self.is_mounted or self.closing:
                return
            scroll = self.query_one("#response-scroll", VerticalScroll)
            follow = scroll.is_vertical_scroll_end or self.chat_scroll_to_end
            position = scroll.scroll_y
            container = self.query_one("#conversation", Vertical)
            if self.chat_rendered_id != session_id:
                await container.remove_children()
                self.chat_widgets = {}
                self.chat_rendered_id = session_id
            if turns:
                await container.query(".chat-empty").remove()
            elif not list(container.query(".chat-empty")):
                await container.mount(
                    Static("New session. Type an ask below to begin.", classes="chat-empty")
                )
            for index, turn in enumerate(turns, 1):
                if session_id != self.current_session_id:
                    return
                widget = self.chat_widgets.get(turn.id)
                if widget is None:
                    widget = ChatTurn(turn, index)
                    await container.mount(widget)
                    self.chat_widgets[turn.id] = widget
                await widget.update_turn(turn, self.chat_live.get(turn.id, ""))
            self.chat_title = title
            self.session_usage = usage
            self.apply_session_usage(usage)
            self.cost_session_id = session_id
            self.response_dirty = True
            self.update_workspace()
            if follow:
                self.call_after_refresh(scroll.scroll_end, animate=False)
            else:
                scroll.scroll_to(y=position, animate=False)
        except (ValueError, OSError) as error:
            self.write_log("father", "error", f"Cannot load session: {error}")
        finally:
            self.chat_refreshing = False

    def select_session(self, session_id: str) -> None:
        self.engine.sessions.activate(session_id)
        self.current_session_id = session_id
        self.cost_session_id = session_id
        self.session_usage = self.engine.sessions.usage(session_id)
        self.apply_session_usage(self.session_usage)
        selected = self.engine.sessions.get(session_id)
        self.chat_title = selected.display_title if selected.turn_count else "New session"
        self.chat_scroll_to_end = True
        if self.focus_role != "all":
            self.set_focus_role("all")
        self.response_dirty = True
        self.query_one("#panels", TabbedContent).active = "response-tab"
        self.call_after_refresh(self.refresh_chat)

    def set_focus_role(self, role: str) -> None:
        if role not in {"all", *ROLES}:
            raise ValueError("Choose all, father, requirement, design, coding, or testing")
        self.focus_role = role
        self.response_dirty = True
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

    @on(Input.Submitted, "#prompt")
    def submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.closing:
            return
        event.input.value = ""
        try:
            if text.startswith("/"):
                self.command(text)
            else:
                task = self.engine.enqueue(text, session_id=self.current_session_id)
                if self.current_session_id != task["session_id"]:
                    self.select_session(task["session_id"])
                self.chat_scroll_to_end = True
                self.query_one("#panels", TabbedContent).active = "response-tab"
                self.call_after_refresh(self.refresh_chat)
        except (ValueError, OSError, TypeError) as error:
            self.write_log("father", "error", str(error))

    def command(self, text: str) -> None:
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if command not in {"/focus", "/redraw", "/models", "/settings", "/sessions", "/newsession"}:
            self.query_one("#panels", TabbedContent).active = "activity-tab"
        if command == "/newsession":
            if argument:
                raise ValueError("Usage: /newsession")
            self.select_session(self.engine.sessions.create(self.config.workspace))
            self.write_log(
                "father",
                "info",
                "New session started. Earlier conversations remain in Recent Sessions.",
            )
        elif command == "/workspace":
            path = argument.strip('"').strip("'")
            if not path:
                raise ValueError("Usage: /workspace PATH")
            config = Config.load(self.home)
            config.workspace = str(Path(path).expanduser().resolve())
            config.save(self.home)
            self.reload_config()
            self.select_session(self.engine.sessions.ensure_current(self.config.workspace))
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
        elif command == "/sessions":
            if argument:
                self.open_session(argument)
            else:
                self.action_sessions()
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
            if task.get("session_id"):
                self.select_session(task["session_id"])
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

    def reload_config(self, *, select_workspace_session: bool = True) -> None:
        previous_workspace = self.config.workspace
        self.config = Config.load(self.home)
        if select_workspace_session and self.config.workspace != previous_workspace:
            self.select_session(self.engine.sessions.ensure_current(self.config.workspace))
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
        from .models import TARGETS

        if target not in TARGETS:
            raise ValueError(
                "Usage: /models [default|all|father|requirement|design|coding|testing]"
            )
        if self.closing or isinstance(self.screen, ModelPicker):
            return
        self.push_screen(ModelPicker(self.home, target), self.model_selected)

    def action_sessions(self) -> None:
        self.query_one("#panels", TabbedContent).active = "sessions-tab"
        self.call_after_refresh(self.query_one("#sessions-table").focus)

    @on(SessionBrowser.OpenSession)
    def session_selected(self, event: SessionBrowser.OpenSession) -> None:
        self.open_session(event.session_id)

    def open_session(self, session_id: str) -> None:
        self.engine.sessions.get(session_id)
        self.push_screen(
            SessionDetails(self.home, session_id, self.engine.request_session_name),
            self.session_resumed,
        )

    def session_resumed(self, session_id: str | None) -> None:
        if session_id is None:
            return
        try:
            session = self.engine.sessions.get(session_id)
            config = Config.load(self.home)
            config.workspace = session.workspace or self.config.workspace
            config.save(self.home)
            self.reload_config(select_workspace_session=False)
            self.select_session(session_id)
        except (ValueError, TypeError, OSError) as error:
            self.write_log("father", "error", f"Cannot continue session: {error}")

    def model_selected(self, result: tuple[str, str] | None) -> None:
        if result is not None:
            self.reload_config()
            target, model = result
            self.write_log(
                "father", "info", f"Saved model for {target}: {model or 'inherit default'}"
            )

    @on(ModelPicker.SettingsSaved)
    def settings_saved(self) -> None:
        self.reload_config()

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
