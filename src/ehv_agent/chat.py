from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Markdown, Static

from .sessions import Session
from .usage import cost_label


class ChatTurn(Vertical):
    """One persistent ask/answer; live updates never rebuild earlier exchanges."""

    DEFAULT_CSS = """
    ChatTurn { height: auto; margin-bottom: 1; padding: 1;
               border-bottom: solid #28354b; }
    .chat-speaker { height: 1; text-style: bold; color: #6ae1b4; }
    .chat-question { height: auto; background: #141d2e; padding: 0 1; margin-bottom: 1; }
    .chat-answer { height: auto; background: #0b101b; }
    .chat-cost { height: auto; color: #8995aa; margin-bottom: 1; }
    """

    def __init__(self, turn: Session, number: int):
        super().__init__(id=f"chat-turn-{turn.id}")
        self.turn = turn
        self.number = number
        self.answer = turn.summary or f"{turn.status.title()}…"
        self.usage = {"cost_usd": turn.cost_usd, "complete": turn.cost_complete}

    def compose(self) -> ComposeResult:
        yield Static(f"YOU · {self.number}", classes="chat-speaker")
        yield Markdown(self.turn.prompt, classes="chat-question", open_links=False)
        yield Static(self.cost_text(), classes="chat-cost", markup=False)
        yield Markdown(self.answer, classes="chat-answer", open_links=False)

    def cost_text(self) -> str:
        return f"FATHER · {self.turn.status.upper()} · Ask–answer {cost_label(self.usage.get('cost_usd'), self.usage.get('complete', False))}"

    async def update_answer(self, text: str) -> None:
        if text != self.answer:
            self.answer = text
            await self.query_one(".chat-answer", Markdown).update(text)

    def update_usage(self, usage: dict) -> None:
        self.usage = usage
        self.query_one(".chat-cost", Static).update(self.cost_text())

    async def update_turn(self, turn: Session, live: str = "") -> None:
        self.turn = turn
        self.update_usage({"cost_usd": turn.cost_usd, "complete": turn.cost_complete})
        await self.update_answer(turn.summary or live or f"{turn.status.title()}…")
