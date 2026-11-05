from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .config import atomic_json
from .events import timestamp
from .pricing import estimate_cost, token_counts


def cost_label(cost: str | None, complete: bool = True) -> str:
    if cost is None:
        return "Unavailable"
    try:
        value = Decimal(cost)
        if not value.is_finite() or value < 0:
            return "Unavailable"
    except (InvalidOperation, TypeError, ValueError):
        return "Unavailable"
    amount = "<$0.000001" if 0 < value < Decimal(".000001") else f"${value:.6f}"
    return f"{amount}{'' if complete else '+'} est."


def summarize(messages: list[dict]) -> dict:
    known = [item for item in messages if item["cost_usd"] is not None]
    total = sum((Decimal(item["cost_usd"]) for item in known), Decimal(0))
    counts = [token_counts(item["usage"] or {}) for item in messages]
    return {
        "currency": "USD",
        "basis": "estimated_list_price",
        "message_count": len(messages),
        "unpriced_messages": len(messages) - len(known),
        "partial_messages": sum(not item["usage_complete"] for item in messages),
        "cost_usd": str(total) if known else None,
        "complete": len(known) == len(messages)
        and all(item["usage_complete"] for item in messages),
        "tokens": {key: sum(item[key] for item in counts) for key in counts[0]},
        "last_cost_usd": messages[-1]["cost_usd"],
        "last_usage_complete": messages[-1]["usage_complete"],
    }


def combine_usage(summaries: list[dict | None]) -> dict:
    """Add exchange totals once; missing usage stays unknown rather than free."""
    available = [item for item in summaries if isinstance(item, dict)]
    priced = [item for item in available if item.get("cost_usd") is not None]
    total = sum((Decimal(item["cost_usd"]) for item in priced), Decimal(0))
    keys = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
    result = {
        "currency": "USD",
        "basis": "estimated_list_price",
        "cost_usd": str(total) if priced or not summaries else None,
        "complete": len(available) == len(summaries)
        and all(item.get("complete") is True for item in available),
        "message_count": sum(item.get("message_count", 0) for item in available),
        "exchange_count": len(summaries),
        "unpriced_messages": sum(item.get("unpriced_messages", 0) for item in available),
        "untracked_exchanges": len(summaries) - len(available),
        "partial_messages": sum(item.get("partial_messages", 0) for item in available),
        "tokens": {
            key: sum(item.get("tokens", {}).get(key, 0) for item in available) for key in keys
        },
    }
    if available:
        result["last_cost_usd"] = available[-1].get("last_cost_usd")
        result["last_usage_complete"] = available[-1].get("last_usage_complete", False)
    roles = dict.fromkeys(role for item in available for role in item.get("by_role", {}))
    result["by_role"] = {
        role: combine_usage(
            [item["by_role"][role] for item in available if role in item.get("by_role", {})]
        )
        for role in roles
    }
    return result


class UsageStore:
    """Per-session accounting shared by all roles and rework/naming calls."""

    def __init__(self, run: Path):
        self.run = run
        self.path = run / "usage.json"

    def read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            raise ValueError("Invalid saved usage ledger")
        return value

    def session_id(self) -> str:
        try:
            request = json.loads((self.run / "request.json").read_text(encoding="utf-8"))
            value = request.get("session_id")
            return value if isinstance(value, str) and value else self.run.name
        except (OSError, ValueError, AttributeError):
            return self.run.name

    def summary(self) -> dict | None:
        try:
            return self.read().get("summary")
        except (ValueError, OSError):
            # Preserve workflow failure reporting when an existing ledger is corrupt.
            return None

    def record(
        self,
        *,
        role: str,
        phase: str,
        turn: int,
        model: str,
        response_id: str | None,
        usage: dict | None,
        complete: bool,
        overrides: dict,
    ) -> tuple[dict, dict]:
        ledger = self.read()
        messages = ledger.get("messages", [])
        record = {
            "id": uuid4().hex,
            "response_id": response_id,
            "session_id": self.session_id(),
            "task_id": self.run.name,
            "role": role,
            "phase": phase,
            "turn": turn,
            "model": model,
            "recorded_at": timestamp(),
            "usage": usage,
            "usage_complete": complete,
            **estimate_cost(model, usage, overrides),
        }
        messages.append(record)
        summary = summarize(messages)
        summary["by_role"] = {
            name: summarize([item for item in messages if item["role"] == name])
            for name in dict.fromkeys(item["role"] for item in messages)
        }
        atomic_json(self.path, {"summary": summary, "messages": messages})
        # Idle session naming may add spend after outcome.json was first written.
        outcome_path = self.run / "outcome.json"
        if outcome_path.exists():
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            outcome["usage"] = summary
            atomic_json(outcome_path, outcome)
        return record, summary
