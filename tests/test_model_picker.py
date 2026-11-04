from textual.widgets import Button, Input, Select, Static

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
    monkeypatch.setattr(model_picker, "fetch_models", lambda: CATALOG)
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


async def test_model_picker_keyboard_selection_and_save_button_at_small_size(setup, monkeypatch):
    _, home, _, events, _, _ = setup
    monkeypatch.setattr(model_picker, "fetch_models", lambda: CATALOG)
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

    def load():
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

    def load():
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
