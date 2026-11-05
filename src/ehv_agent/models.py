from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import anthropic

from .config import ROLES, Config
from .network import create_claude_client

TARGETS = ["default", "all", *ROLES]


@dataclass(frozen=True)
class AvailableModel:
    id: str
    name: str
    max_tokens: int | None = None
    max_input_tokens: int | None = None
    lifecycle: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_models(client=None, *, config: Config | None = None) -> list[AvailableModel]:
    """Fetch the full account-visible catalogue with the SDK's automatic pagination."""
    owned = client is None
    if owned and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("Set ANTHROPIC_API_KEY in this terminal to scan available Claude models.")
    try:
        if owned:
            client = create_claude_client(config, timeout=20.0, max_retries=1)
        models = {}
        for model in client.models.list(limit=100):
            if not isinstance(model.id, str) or not model.id:
                continue
            lifecycle = getattr(model, "lifecycle", "") or ""
            if lifecycle == "retired":
                continue
            models[model.id] = AvailableModel(
                id=model.id,
                name=model.display_name or model.id,
                max_tokens=getattr(model, "max_tokens", None),
                max_input_tokens=getattr(model, "max_input_tokens", None),
                lifecycle=lifecycle,
            )
        if not models:
            raise ValueError(
                "Claude returned no available models for this API key. Check account access and try Refresh."
            )
        return list(models.values())
    except anthropic.AuthenticationError:
        raise ValueError(
            "Claude rejected ANTHROPIC_API_KEY. Check the key in this terminal, then scan again."
        ) from None
    except anthropic.PermissionDeniedError:
        raise ValueError("This Claude API key does not have permission to list models.") from None
    except anthropic.RateLimitError:
        raise ValueError(
            "Claude rate limited the model scan. Wait briefly, then try Refresh."
        ) from None
    except anthropic.APITimeoutError:
        raise ValueError(
            "The Claude model scan timed out. Check your connection and try Refresh."
        ) from None
    except anthropic.APIConnectionError:
        raise ValueError(
            "Could not connect to Claude to list models. Check your connection and try Refresh."
        ) from None
    except anthropic.APIStatusError as error:
        raise ValueError(
            f"Claude model scan failed (HTTP {error.status_code}). Try again or check account access."
        ) from None
    except anthropic.AnthropicError:
        raise ValueError(
            "The Claude SDK could not complete the model scan. Check your API setup."
        ) from None
    finally:
        if owned and client is not None:
            client.close()


def select_model(
    home: Path, target: str, model_id: str, models: list[AvailableModel] | None = None
) -> Config:
    if target not in TARGETS:
        raise ValueError(f"Choose a target from: {', '.join(TARGETS)}")
    if not model_id and target not in ROLES:
        raise ValueError("Only an individual agent can inherit the default model")
    selected = next((item for item in models or [] if item.id == model_id), None)
    if model_id and models is not None and selected is None:
        raise ValueError("Choose a model returned by the latest scan")
    # Read the latest settings at save time, preserving edits made while scanning.
    config = Config.load(home)
    if target in {"default", "all"}:
        config.model = model_id
        if target == "all":
            for settings in config.agents.values():
                settings["model"] = ""
    else:
        config.agents[target]["model"] = model_id
    if selected and selected.max_tokens is not None and selected.max_tokens > 0:
        config.model_max_tokens[selected.id] = selected.max_tokens
    if selected and selected.max_input_tokens is not None and selected.max_input_tokens > 0:
        config.model_context_tokens[selected.id] = selected.max_input_tokens
    config.save(home)
    return config


def cli_models(args, home: Path) -> int:
    """Use the model catalogue without taking the running dashboard's app lock."""
    import json

    target = args.agent
    if args.inherit:
        select_model(home, target, "")
        print(f"{target}: now inherits the default model. Applies to the next workflow.")
        return 0
    config = Config.load(home)
    models = fetch_models(config=config)
    if args.json:
        print(json.dumps([item.to_dict() for item in models], indent=2))
        return 0
    current = config.model if target in {"default", "all"} else config.model_for(target)
    print(f"Available Claude models for {target} (current: {current}):")
    for index, model in enumerate(models, 1):
        marker = " *" if model.id == current else ""
        state = f" [{model.lifecycle}]" if model.lifecycle else ""
        print(f"{index:2}. {model.name}  {model.id}{state}{marker}")
    if not args.select and not args.set:
        print("Use models --select to choose interactively, or --set MODEL_ID [--agent ROLE].")
        return 0
    chosen = args.set
    if args.select:
        print(
            "Enter a number or model ID; Enter cancels."
            + (" Enter 0 to inherit the default." if target in ROLES else "")
        )
        while True:
            try:
                choice = input("Model: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nSelection cancelled; settings unchanged.")
                return 0
            if not choice:
                print("Selection cancelled; settings unchanged.")
                return 0
            if choice == "0" and target in ROLES:
                chosen = ""
                break
            if choice.isdigit() and 1 <= int(choice) <= len(models):
                chosen = models[int(choice) - 1].id
                break
            if choice in {item.id for item in models}:
                chosen = choice
                break
            print("Choose a number from the list or a listed model ID.")
    select_model(home, target, chosen, models)
    print(f"Saved {target}: {chosen or 'inherit default'}. Applies to the next workflow.")
    return 0
