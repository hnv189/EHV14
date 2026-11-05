from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Markdown, Static

from .sessions import Session, SessionStore, local_time
from .usage import cost_label


class SessionBrowser(Vertical):
    DEFAULT_CSS = """
    SessionBrowser { height: 1fr; }
    #sessions-hint { height: 1; color: #8995aa; }
    #sessions-table { height: 1fr; min-height: 3; background: #0b101b; border: round #28354b; }
    """

    class OpenSession(Message):
        def __init__(self, session_id: str):
            super().__init__()
            self.session_id = session_id

    def __init__(self, home: Path):
        super().__init__(id="recent-sessions")
        self.store = SessionStore(home)
        self.sessions: list[Session] = []
        self.refreshing = False

    def compose(self) -> ComposeResult:
        yield Static("Loading recent sessions…", id="sessions-hint", markup=False)
        yield DataTable(id="sessions-table", cursor_type="row", zebra_stripes=True, fixed_columns=1)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for label, key, width in [
            ("Session", "title", 42),
            ("Status", "status", 12),
            ("Est. USD", "cost", 23),
            ("Exchanges", "exchanges", 10),
            ("Created (local)", "created", 22),
            ("Workspace", "workspace", 40),
        ]:
            table.add_column(label, key=key, width=width)
        self.call_after_refresh(self.refresh_sessions)
        self.set_interval(1.5, self.refresh_sessions)

    async def refresh_sessions(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        try:
            sessions = await asyncio.to_thread(self.store.recent)
            if not self.is_mounted or sessions == self.sessions:
                if self.is_mounted and not sessions:
                    self.query_one("#sessions-hint", Static).update(
                        "No sessions yet. Submit a request to begin."
                    )
                return
            table = self.query_one(DataTable)
            row = table.cursor_row
            selected = self.sessions[row].id if 0 <= row < len(self.sessions) else None
            self.sessions = sessions
            table.clear()
            for session in sessions:
                color = {
                    "completed": "#6ae1b4",
                    "running": "#55c9ff",
                    "failed": "#ff777f",
                    "cancelled": "#ffc971",
                    "interrupted": "#ffc971",
                }.get(session.status, "#8995aa")
                table.add_row(
                    Text(session.display_title, no_wrap=True, overflow="ellipsis"),
                    Text(session.status.upper(), style=color),
                    cost_label(session.cost_usd, session.cost_complete),
                    str(session.turn_count),
                    local_time(session.created_at),
                    Text(session.workspace or "Not started", no_wrap=True, overflow="ellipsis"),
                    key=session.id,
                )
            ids = [session.id for session in sessions]
            if ids:
                table.move_cursor(
                    row=ids.index(selected) if selected in ids else min(row, len(ids) - 1),
                    animate=False,
                    scroll=False,
                )
            self.query_one("#sessions-hint", Static).update(
                f"{len(sessions)} recent sessions · Enter or click to open · titles by father"
            )
        finally:
            self.refreshing = False

    @on(DataTable.RowSelected, "#sessions-table")
    def open_selected(self, event: DataTable.RowSelected) -> None:
        self.post_message(self.OpenSession(str(event.row_key.value)))


class SessionDetails(ModalScreen[str | None]):
    DEFAULT_CSS = """
    SessionDetails { align: center middle; background: #000000 60%; }
    #session-dialog { width: 94%; max-width: 125; height: 90%; padding: 0 1;
                      border: round #bda3ff; background: #0b101b; }
    #session-report-scroll { height: 1fr; }
    #session-report { background: #0b101b; }
    #session-note { height: auto; min-height: 1; color: #8995aa; }
    #session-buttons { height: 3; align-horizontal: right; }
    #session-buttons Button { margin-left: 1; min-width: 12; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, home: Path, session_id: str, request_name):
        super().__init__()
        self.store = SessionStore(home)
        self.session_id = session_id
        self.request_name = request_name
        self.last_report = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="session-dialog"):
            with VerticalScroll(id="session-report-scroll"):
                yield Markdown("Loading saved session…", id="session-report", open_links=False)
            yield Static(
                "Scroll through saved exchanges. Continue session to send another ask.",
                id="session-note",
                markup=False,
            )
            with Horizontal(id="session-buttons"):
                yield Button("Ask father to name", id="session-name", disabled=True)
                yield Button("Continue session", id="session-continue", variant="success")
                yield Button("Close", id="session-close", variant="primary")

    def on_mount(self) -> None:
        self.call_after_refresh(self.refresh_report)
        self.set_interval(1.5, self.refresh_report)

    async def refresh_report(self) -> None:
        try:
            session = await asyncio.to_thread(self.store.get, self.session_id)
            report = await asyncio.to_thread(self.store.report, self.session_id)
        except (ValueError, OSError) as error:
            self.query_one("#session-note", Static).update(str(error))
            return
        if not self.is_mounted:
            return
        self.query_one("#session-name", Button).disabled = (
            bool(session.title)
            or session.turn_count == 0
            or session.status in {"pending", "running"}
            or session.title_state in {"queued", "naming"}
        )
        if report != self.last_report:
            self.last_report = report
            scroll = self.query_one("#session-report-scroll", VerticalScroll)
            position = scroll.scroll_y
            await self.query_one("#session-report", Markdown).update(report)
            scroll.scroll_to(y=position, animate=False)

    @on(Button.Pressed, "#session-name")
    def ask_father_to_name(self) -> None:
        try:
            self.request_name(self.session_id)
            self.query_one("#session-name", Button).disabled = True
            self.query_one("#session-note", Static).update(
                "Naming queued for the father. It runs when the scheduler is resumed and the team is idle."
            )
        except (ValueError, OSError) as error:
            self.query_one("#session-note", Static).update(str(error))

    @on(Button.Pressed, "#session-close")
    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#session-continue")
    def continue_session(self) -> None:
        self.dismiss(self.session_id)
