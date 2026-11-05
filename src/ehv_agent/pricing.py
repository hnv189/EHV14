from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICING_DATE = "2026-11-05"
RATE_KEYS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
# USD per million tokens, standard first-party Messages API list prices.
# Cache read multipliers vary by model; never infer prices for unknown models.
BASE_RATES = {
    "claude-sonnet-5-5": ("2", "10", ".05"),
    "claude-sonnet-5": ("2", "10", ".1"),
    "claude-sonnet-4-6": ("3", "15", ".1"),
    "claude-sonnet-4-5": ("3", "15", ".1"),
    "claude-sonnet-4": ("3", "15", ".1"),
    "claude-opus-5-5": ("4", "20", ".05"),
    "claude-opus-5": ("5", "25", ".1"),
    "claude-opus-4-8": ("5", "25", ".1"),
    "claude-opus-4-7": ("5", "25", ".1"),
    "claude-opus-4-6": ("5", "25", ".1"),
    "claude-opus-4-5": ("5", "25", ".1"),
    "claude-opus-4-1": ("15", "75", ".1"),
    "claude-opus-4": ("15", "75", ".1"),
    "claude-haiku-5-5": (".1", ".5", ".1"),
    "claude-haiku-4-5": ("1", "5", ".1"),
    "claude-3-5-haiku": (".8", "4", ".1"),
    "claude-fable-5-1": ("10", "50", ".025"),
    "claude-fable-5": ("10", "50", ".1"),
    "claude-mythos-5-1": ("10", "50", ".025"),
    "claude-mythos-5": ("10", "50", ".1"),
}


def validate_model_pricing(value: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError("model_pricing must map model IDs to USD rates per million tokens")
    for model, rates in value.items():
        if (
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(rates, dict)
            or set(rates) != set(RATE_KEYS)
        ):
            raise ValueError(
                "Each model_pricing entry needs input, output, cache_write_5m, cache_write_1h, cache_read rates"
            )
        for rate in rates.values():
            try:
                number = Decimal(str(rate))
            except (InvalidOperation, ValueError):
                raise ValueError("Pricing rates must be finite nonnegative numbers") from None
            if not number.is_finite() or number < 0:
                raise ValueError("Pricing rates must be finite nonnegative numbers")


def token_counts(usage: dict) -> dict[str, int]:
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    creation = usage.get("cache_creation") or {}
    if not isinstance(creation, dict):
        creation = {}
    short = count(creation.get("ephemeral_5m_input_tokens"))
    long = count(creation.get("ephemeral_1h_input_tokens"))
    total = max(count(usage.get("cache_creation_input_tokens")), short + long)
    return {
        "input": count(usage.get("input_tokens")),
        "output": count(usage.get("output_tokens")),
        "cache_write_5m": short + max(0, total - short - long),
        "cache_write_1h": long,
        "cache_read": count(usage.get("cache_read_input_tokens")),
    }


def estimate_cost(model: str, usage: dict | None, overrides: dict) -> dict:
    result = {
        "cost_usd": None,
        "rates": None,
        "pricing_source": PRICING_SOURCE,
        "pricing_date": PRICING_DATE,
    }
    if usage is None:
        return {**result, "cost_reason": "No token usage returned"}
    counts = token_counts(usage)
    # Resolve only documented aliases and dated versions, never arbitrary prefixes.
    canonical = re.sub(r"-(?:latest|\d{8})$", "", model)
    custom = overrides.get(model, overrides.get(canonical))
    if custom is not None:
        rates = {key: Decimal(str(custom[key])) for key in RATE_KEYS}
        result["pricing_source"] = "config:model_pricing"
    elif canonical in BASE_RATES:
        base, output, read_multiplier = map(Decimal, BASE_RATES[canonical])
        if (
            canonical == "claude-haiku-5-5"
            and sum(counts[key] for key in RATE_KEYS if key != "output") > 100000
        ):
            base, output = Decimal(".5"), Decimal("2.5")
        rates = dict(
            zip(
                RATE_KEYS,
                (base, output, base * Decimal("1.25"), base * 2, base * read_multiplier),
                strict=True,
            )
        )
        # Older long-context beta requests need rates that are not in this table.
        if (
            canonical in {"claude-sonnet-4", "claude-sonnet-4-5"}
            and sum(counts[key] for key in RATE_KEYS if key != "output") > 200000
        ):
            return {**result, "cost_reason": "Configure long-context rates in model_pricing"}
    else:
        return {**result, "cost_reason": "Unknown model pricing; configure model_pricing"}
    if usage.get("service_tier") not in (None, "standard"):
        return {**result, "cost_reason": "Nonstandard service tier; billed cost unavailable"}
    server_tools = usage.get("server_tool_use") or {}
    if any(server_tools.values()):
        return {**result, "cost_reason": "Server tool charges are not priced"}
    multiplier = Decimal("1.1") if usage.get("inference_geo") == "us" else Decimal(1)
    cost = (
        sum((Decimal(counts[key]) * rates[key] for key in RATE_KEYS), Decimal(0))
        * multiplier
        / 1000000
    )
    return {
        **result,
        "cost_usd": str(cost),
        "rates": {key: str(value) for key, value in rates.items()},
        "cost_reason": "Estimated token list price",
    }
