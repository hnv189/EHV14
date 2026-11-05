from textual.widgets import Button, Input, Select, Static, Switch

from ehv_agent import model_picker
from ehv_agent.config import Config
from ehv_agent.engine import Engine
from ehv_agent.model_picker import ModelPicker
from ehv_agent.models import AvailableModel
from ehv_agent.ui import AgentApp

CATALOG = [
    AvailableModel("claude-large", "Large Claude", 32000, 200000),
    AvailableModel("claude-small", "Small Claude", 4096, 100000),
]


async def wait_for_scan(pilot, app):
    for _ in range(50):
        await pilot.pause(0.05)
        if isinstance(app.screen, ModelPicker) and not app.screen.scanning:
            return app.screen
    raise AssertionError("Model picker scan did not finish")


async def test_picker_scans_and_saves_role_model_preserving_other_settings(setup, monkeypatch):
    _, home, _, events, _, _ = setup
    monkeypatch.setattr(model_picker, "fetch_models", lambda **_: CATALOG)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.press("f2")
        screen = await wait_for_scan(pilot, app)
        assert screen.scan_ok
        assert "Found 2" in str(screen.query_one("#model-status", Static).render())
        screen.query_one("#model-target", Select).value = "coding"
        await pilot.pause()
        screen.query_one("#model-choice", Select).value = "claude-small"
        await pilot.pause()
        assert not screen.query_one("#model-save", Button).disabled
        assert "4,096" in str(screen.query_one("#model-details", Static).render())
        # Changing another setting while the scan is open must be preserved.
        changed = Config.load(home)
        changed.command_timeout = 55
        changed.save(home)
        screen.save_selection()
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ModelPicker)
        saved = Config.load(home)
        assert saved.model_for("coding") == "claude-small"
        assert saved.command_timeout == 55
        assert app.cards["coding"].model == "claude-small"
        assert saved.max_tokens_for("coding") == 4096
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_conversation_settings_save_without_model_scan_and_preserve_other_settings(
    setup, monkeypatch
):
    _, home, config, events, _, _ = setup

    def fail(**_):
        raise ValueError("Offline")

    monkeypatch.setattr(model_picker, "fetch_models", fail)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(100, 35)) as pilot:
        app.command("/settings")
        screen = await wait_for_scan(pilot, app)
        assert not screen.scan_ok
        screen.query_one("#auto-compact", Switch).value = False
        screen.query_one("#compact-threshold", Input).value = "45000"
        cap = screen.query_one("#recovery-tokens", Input)
        cap.value = "24000"
        config.command_timeout = 55
        config.save(home)
        cap.focus()
        await pilot.press("enter")
        await pilot.pause()
        saved = Config.load(home)
        assert not saved.auto_compact and saved.compact_threshold == 45000
        assert saved.max_recovery_tokens == 24000 and saved.command_timeout == 55
        assert app.config.compact_threshold == 45000
        assert not list((home / "agents").glob("*/pending/*.json"))
        before = (home / "config.json").read_bytes()
        screen.query_one("#compact-threshold", Input).value = "0"
        screen.save_conversation()
        await pilot.pause()
        assert "Check conversation settings" in str(
            screen.query_one("#conversation-status", Static).render()
        )
        assert (home / "config.json").read_bytes() == before
        screen.action_cancel()
        await pilot.pause()
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_model_picker_keyboard_selection_and_save_button_at_small_size(setup, monkeypatch):
    _, home, _, events, _, _ = setup
    monkeypatch.setattr(model_picker, "fetch_models", lambda **_: CATALOG)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(80, 25)) as pilot:
        await pilot.press("f2")
        screen = await wait_for_scan(pilot, app)
        screen.query_one("#model-choice", Select).focus()
        await pilot.press("enter", "down", "enter")
        await pilot.pause()
        selected = screen.query_one("#model-choice", Select).value
        assert selected in {item.id for item in CATALOG}
        save = screen.query_one("#model-save", Button)
        save.focus()
        save.scroll_visible(animate=False)
        await pilot.pause()
        assert save.region.bottom <= app.size.height
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ModelPicker)
        assert Config.load(home).model == selected
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_scan_error_refresh_inheritance_and_cancellation(setup, monkeypatch):
    _, home, config, events, _, _ = setup
    config.agents["father"]["model"] = "old-override"
    config.save(home)
    scans = 0

    def load(**_):
        nonlocal scans
        scans += 1
        if scans == 1:
            raise ValueError("Network unavailable; try Refresh")
        return CATALOG

    monkeypatch.setattr(model_picker, "fetch_models", load)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(100, 35)) as pilot:
        app.command("/models father")
        screen = await wait_for_scan(pilot, app)
        assert "Network unavailable" in str(screen.query_one("#model-status", Static).render())
        assert screen.query_one("#model-save", Button).disabled
        assert Config.load(home).agents["father"]["model"] == "old-override"
        screen.refresh_catalog()
        await pilot.pause(0.1)
        screen = await wait_for_scan(pilot, app)
        assert screen.scan_ok
        screen.query_one("#model-choice", Select).value = ""
        await pilot.pause()
        screen.save_selection()
        await pilot.pause()
        assert not Config.load(home).agents["father"]["model"]
        before = (home / "config.json").read_bytes()
        app.command("/model")
        screen = await wait_for_scan(pilot, app)
        screen.query_one("#model-choice", Select).value = "claude-large"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert (home / "config.json").read_bytes() == before
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_slow_model_scan_does_not_block_streaming_or_input(setup, monkeypatch):
    import threading

    release = threading.Event()

    def load(**_):
        release.wait(timeout=3)
        return CATALOG

    _, home, _, events, _, _ = setup
    monkeypatch.setattr(model_picker, "fetch_models", load)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(120, 45)) as pilot:
        app.query_one(Input).value = "Keep my draft"
        app.action_models()
        await pilot.pause(0.1)
        assert isinstance(app.screen, ModelPicker) and app.screen.scanning
        events.status("coding", "working", "Still editing during model scan")
        events.emit("coding", "text", "Live progress\n")
        await pilot.pause(0.2)
        assert app.cards["coding"].action_text == "Still editing during model scan"
        assert "Live progress" in app.cards["coding"].preview
        app.screen.action_cancel()
        release.set()
        await pilot.pause(0.2)
        assert app.query_one(Input).value == "Keep my draft"
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_proxy_settings_save_after_failed_scan_and_rescan_with_new_config(setup, monkeypatch):
    _, home, config, events, _, _ = setup
    seen = []

    def load(*, config):
        seen.append((config.proxy_mode, config.proxy_url))
        if config.proxy_mode != "custom":
            raise ValueError("Connection unavailable")
        return CATALOG

    monkeypatch.setattr(model_picker, "fetch_models", load)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(100, 35)) as pilot:
        app.command("/settings")
        screen = await wait_for_scan(pilot, app)
        assert not screen.scan_ok
        screen.query_one("#proxy-mode", Select).value = "custom"
        await pilot.pause()
        proxy = screen.query_one("#proxy-url", Input)
        assert not proxy.disabled and proxy.password
        proxy.value = "http://user:private-proxy-password@proxy.example.com:8080"
        # Save reads the latest config instead of reverting concurrent changes.
        config.command_timeout = 55
        config.save(home)
        proxy.focus()
        await pilot.press("enter")
        screen = await wait_for_scan(pilot, app)
        assert screen.scan_ok
        saved = Config.load(home)
        assert saved.proxy_mode == "custom" and saved.proxy_url == proxy.value
        assert saved.command_timeout == 55
        assert seen[-1] == (saved.proxy_mode, saved.proxy_url)
        assert app.config.proxy_mode == "custom"
        assert not list((home / "agents").glob("*/pending/*.json"))
        assert "private-proxy-password" not in (home / "events.jsonl").read_text()
        screen.action_cancel()
        await pilot.pause()
        app.action_models()
        screen = await wait_for_scan(pilot, app)
        assert screen.query_one("#proxy-mode", Select).value == "custom"
        assert screen.query_one("#proxy-url", Input).value == saved.proxy_url
        before = (home / "config.json").read_bytes()
        screen.query_one("#proxy-url", Input).value = "http://host:bad"
        screen.save_proxy()
        await pilot.pause()
        assert "Proxy URL" in str(screen.query_one("#proxy-status", Static).render())
        assert (home / "config.json").read_bytes() == before
        screen.query_one("#proxy-mode", Select).value = "direct"
        await pilot.pause()
        assert screen.query_one("#proxy-url", Input).disabled
        screen.save_proxy()
        await wait_for_scan(pilot, app)
        assert Config.load(home).proxy_mode == "direct"
        assert Config.load(home).proxy_url == saved.proxy_url
        screen.action_cancel()
        await pilot.pause()
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)
