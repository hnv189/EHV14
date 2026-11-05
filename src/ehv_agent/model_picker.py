from __future__ import annotations

import asyncio
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from .config import ROLES, Config
from .models import AvailableModel, fetch_models, select_model
from .pricing import estimate_cost


class ModelPicker(ModalScreen[tuple[str, str] | None]):
    """Persistent connection settings and a live account model catalogue."""

    class SettingsSaved(Message):
        """Notify the dashboard without exposing connection secrets."""

    DEFAULT_CSS = """
    ModelPicker { align: center middle; background: #000000 60%; }
    #model-dialog { width: 90%; max-width: 100; height: auto; max-height: 90%;
                    padding: 1 2; border: round #bda3ff; background: #141d2e; }
    #model-title { height: 1; text-style: bold; color: #bda3ff; margin-bottom: 1; }
    #model-status { height: auto; min-height: 2; margin-bottom: 1; }
    #model-current, #model-details, #model-note { height: auto; margin-bottom: 1; color: #aab5c7; }
    #model-dialog Label { height: 1; }
    #model-dialog Select { margin-bottom: 1; }
    #proxy-status { height: auto; margin-bottom: 1; color: #aab5c7; }
    #proxy-url { margin-bottom: 1; }
    #proxy-save { margin-bottom: 1; }
    #conversation-status { height: auto; margin-bottom: 1; color: #aab5c7; }
    #conversation-save { margin-bottom: 1; }
    #model-buttons { height: 3; align-horizontal: right; }
    #model-buttons Button { min-width: 12; margin-left: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, home: Path, target: str = "default", loader=None):
        super().__init__()
        self.home = home
        self.target = target
        self.loader = loader
        self.proxy_config = Config.load(home)
        self.model_pricing = self.proxy_config.model_pricing
        self.catalog: list[AvailableModel] = []
        self.scanning = False
        self.scan_ok = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="model-dialog"):
            yield Static("CLAUDE SETTINGS", id="model-title")
            yield Label("Connection")
            yield Select(
                [
                    ("Environment defaults", "system"),
                    ("Direct connection (ignore environment proxies)", "direct"),
                    ("Custom HTTP/HTTPS proxy", "custom"),
                ],
                value=self.proxy_config.proxy_mode,
                allow_blank=False,
                id="proxy-mode",
            )
            yield Label("Proxy URL")
            yield Input(
                value=self.proxy_config.proxy_url,
                placeholder="http://proxy.example.com:8080",
                password=True,
                disabled=self.proxy_config.proxy_mode != "custom",
                id="proxy-url",
            )
            yield Static(
                "Environment defaults use HTTP_PROXY, HTTPS_PROXY, ALL_PROXY and NO_PROXY. "
                "Save connection changes before refreshing models.",
                id="proxy-status",
                markup=False,
            )
            yield Button("Save proxy", id="proxy-save")
            yield Label("Automatically compact long conversations")
            yield Switch(value=self.proxy_config.auto_compact, id="auto-compact")
            yield Label("Compact near this many input tokens (estimated)")
            yield Input(
                value=str(self.proxy_config.compact_threshold),
                type="integer",
                id="compact-threshold",
            )
            yield Label("Maximum output tokens during automatic recovery")
            yield Input(
                value=str(self.proxy_config.max_recovery_tokens),
                type="integer",
                id="recovery-tokens",
            )
            yield Static(
                "Compaction retains the original task and recent tool exchanges. "
                "Truncated replies retry within the recovery limit and model limits.",
                id="conversation-status",
                markup=False,
            )
            yield Button("Save conversation", id="conversation-save")
            yield Static(
                "Scanning models available to your API key…", id="model-status", markup=False
            )
            yield Label("Apply to")
            yield Select(
                [
                    ("Default model · agents without an override", "default"),
                    ("All agents · replace overrides with this default", "all"),
                    *((role.title(), role) for role in ROLES),
                ],
                value=self.target,
                allow_blank=False,
                id="model-target",
            )
            yield Static(id="model-current", markup=False)
            yield Label("Available model")
            yield Select([], prompt="Waiting for model scan…", id="model-choice", disabled=True)
            yield Static(id="model-details", markup=False)
            yield Static(
                "Model and connection changes apply to the next workflow. The current workflow keeps its settings.",
                id="model-note",
                markup=False,
            )
            with Horizontal(id="model-buttons"):
                yield Button("Refresh", id="model-refresh")
                yield Button("Save", variant="success", id="model-save", disabled=True)
                yield Button("Cancel", id="model-cancel")

    def on_mount(self) -> None:
        self.populate_choices()
        self.scan_models()

    def selected_target(self) -> str:
        return str(self.query_one("#model-target", Select).value)

    def populate_choices(self) -> None:
        try:
            config = Config.load(self.home)
        except (ValueError, TypeError, OSError) as error:
            self.query_one("#model-status", Static).update(f"Check config.json: {error}")
            self.query_one("#model-save", Button).disabled = True
            return
        target = self.selected_target()
        self.model_pricing = config.model_pricing
        current = config.model if target in {"default", "all"} else config.model_for(target)
        inheritance = (
            " (inherits default)"
            if target in ROLES and not config.agents[target].get("model")
            else ""
        )
        self.query_one("#model-current", Static).update(f"Current: {current}{inheritance}")
        options = []
        if target in ROLES:
            options.append((f"Use default ({config.model})", ""))
        options.extend(
            (
                f"{model.name} · {model.id}"
                + (" · deprecated" if model.lifecycle == "deprecated" else ""),
                model.id,
            )
            for model in self.catalog
        )
        choice = self.query_one("#model-choice", Select)
        choice.set_options(options)
        configured = config.agents[target].get("model", "") if target in ROLES else config.model
        choice.value = configured if configured in {value for _, value in options} else Select.NULL
        choice.disabled = self.scanning or not self.scan_ok
        self.update_choice_details()

    def update_choice_details(self) -> None:
        value = self.query_one("#model-choice", Select).value
        available = {model.id: model for model in self.catalog}
        inherit = value == "" and self.selected_target() in ROLES
        valid = isinstance(value, str) and (value in available or inherit)
        self.query_one("#model-save", Button).disabled = (
            self.scanning or not self.scan_ok or not valid
        )
        details = "Choose a model from the scanned list."
        if inherit:
            details = "This agent will follow the default model, including future default changes."
        elif value in available:
            selected = available[value]
            parts = [selected.id]
            if selected.max_input_tokens:
                parts.append(f"Context: {selected.max_input_tokens:,} tokens")
            if selected.max_tokens:
                parts.append(f"Max output: {selected.max_tokens:,} tokens")
            price = estimate_cost(selected.id, {}, self.model_pricing)
            if price["rates"] is not None:
                rates = price["rates"]
                parts.append(f"Est. USD/MTok: input ${rates['input']}, output ${rates['output']}")
            else:
                parts.append("Cost estimate unavailable; add this model's rates in config.json")
            details = " · ".join(parts)
        self.query_one("#model-details", Static).update(details)

    @work(exclusive=True, group="model-scan", exit_on_error=False)
    async def scan_models(self) -> None:
        self.scanning = True
        self.scan_ok = False
        self.query_one("#model-refresh", Button).disabled = True
        self.query_one("#model-choice", Select).disabled = True
        self.query_one("#model-save", Button).disabled = True
        self.query_one("#model-status", Static).update(
            "Scanning models available to your Claude API key…"
        )
        try:
            if self.loader is not None:
                catalog = await asyncio.to_thread(self.loader)
            else:
                catalog = await asyncio.to_thread(fetch_models, config=Config.load(self.home))
            if not catalog:
                raise ValueError(
                    "No available models were returned. Check account access, then try Refresh."
                )
            self.catalog = catalog
            self.scan_ok = True
            self.query_one("#model-status", Static).update(
                f"Found {len(self.catalog)} available models. Choose a target and model, then Save."
            )
        except (ValueError, OSError) as error:
            self.catalog = []
            self.query_one("#model-status", Static).update(str(error))
        except Exception:
            self.catalog = []
            self.query_one("#model-status", Static).update(
                "Model scan failed. Check your API setup, then try Refresh."
            )
        finally:
            self.scanning = False
            if self.is_mounted:
                self.query_one("#model-refresh", Button).disabled = False
                self.populate_choices()

    @on(Select.Changed, "#model-target")
    def target_changed(self) -> None:
        if self.is_mounted:
            self.populate_choices()

    @on(Select.Changed, "#model-choice")
    def model_changed(self) -> None:
        if self.is_mounted:
            self.update_choice_details()

    @on(Button.Pressed, "#model-refresh")
    def refresh_catalog(self) -> None:
        self.scan_models()

    @on(Select.Changed, "#proxy-mode")
    def proxy_mode_changed(self) -> None:
        if self.is_mounted:
            self.query_one("#proxy-url", Input).disabled = (
                self.query_one("#proxy-mode", Select).value != "custom"
            )

    @on(Button.Pressed, "#proxy-save")
    def save_proxy(self) -> None:
        try:
            config = Config.load(self.home)
            config.proxy_mode = str(self.query_one("#proxy-mode", Select).value)
            if config.proxy_mode == "custom":
                config.proxy_url = self.query_one("#proxy-url", Input).value
            config.save(self.home)
        except (ValueError, TypeError, OSError) as error:
            self.query_one("#proxy-status", Static).update(str(error))
            return
        self.query_one("#proxy-status", Static).update(
            "Proxy settings saved. Model scans use them now; workflows use them when they start."
        )
        self.post_message(self.SettingsSaved())
        self.scan_models()

    @on(Button.Pressed, "#conversation-save")
    def save_conversation(self) -> None:
        try:
            config = Config.load(self.home)
            config.auto_compact = self.query_one("#auto-compact", Switch).value
            config.compact_threshold = int(self.query_one("#compact-threshold", Input).value)
            config.max_recovery_tokens = int(self.query_one("#recovery-tokens", Input).value)
            config.save(self.home)
        except (ValueError, TypeError, OSError) as error:
            self.query_one("#conversation-status", Static).update(
                f"Check conversation settings: {error}"
            )
            return
        self.query_one("#conversation-status", Static).update(
            "Conversation settings saved for the next workflow."
        )
        self.post_message(self.SettingsSaved())

    @on(Input.Submitted, "#compact-threshold, #recovery-tokens")
    def conversation_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.save_conversation()

    @on(Input.Submitted, "#proxy-url")
    def proxy_url_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.save_proxy()

    @on(Button.Pressed, "#model-save")
    def save_selection(self) -> None:
        value = self.query_one("#model-choice", Select).value
        if not isinstance(value, str) or self.scanning or not self.scan_ok:
            return
        target = self.selected_target()
        try:
            select_model(self.home, target, value, self.catalog)
        except (ValueError, OSError) as error:
            self.query_one("#model-status", Static).update(str(error))
            return
        self.dismiss((target, value))

    @on(Button.Pressed, "#model-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
