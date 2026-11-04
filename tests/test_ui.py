from textual.widgets import Input, Markdown, TabbedContent

from ehv_agent.engine import Engine
from ehv_agent.ui import AgentApp


async def test_live_dashboard_status_streaming_filter_and_configuration(setup, tmp_path):
    _, home, _, events, _, _ = setup
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause(0.15)
        events.status("coding", "working", "Editing src/login.py")
        events.emit("coding", "text", "Adding login validation…\n")
        events.emit("coding", "usage", "tokens", input_tokens=100, output_tokens=20)
        await pilot.pause(0.15)
        assert app.cards["coding"].state == "working"
        assert "login.py" in app.cards["coding"].action_text
        assert "validation" in app.cards["coding"].preview
        assert app.cards["coding"].output_tokens == 20
        app.command("/focus coding")
        assert app.focus_role == "coding"
        alternate = tmp_path / "alternate workspace"
        alternate.mkdir()
        app.command(f'/workspace "{alternate}"')
        assert app.config.workspace == str(alternate.resolve())
        app.command("/model custom-model testing")
        assert app.config.model_for("testing") == "custom-model"
        app.command("/pause")
        assert engine.paused.is_set()
        app.command("/resume")
        assert not engine.paused.is_set()
        await pilot.resize_terminal(140, 45)
        await pilot.pause()
        assert app.query_one("#team").styles.grid_size_columns == 2
        await pilot.resize_terminal(180, 45)
        await pilot.pause()
        assert app.query_one("#team").styles.grid_size_columns == 4
        app.action_request_quit()
        await pilot.pause(0.2)
    if engine.thread:
        engine.thread.join(timeout=3)
    assert not engine.thread.is_alive()


async def test_markdown_streams_without_filling_cards_and_input_stays_above_footer(setup):
    _, home, _, events, _, _ = setup
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(140, 38)) as pilot:
        await pilot.pause(0.2)
        report = "## What changed\n\n**Bold summary** with `inline_code`.\n\n- First item\n- Second item\n\n```python\nprint('hello')\n```\n"
        events.status("father", "working", "Reviewing changes")
        events.emit("father", "text", report[:40])
        events.emit("father", "text", report[40:])
        await pilot.pause(0.4)
        assert app.query_one("#response", Markdown)._markdown == report
        assert len(app.query_one("#response").query("MarkdownH2")) == 1
        events.emit("father", "usage", "tokens", input_tokens=380000, output_tokens=5298)
        events.status("father", "done", report)
        await pilot.pause(0.4)
        rendered = app.cards["father"].render().plain
        assert "**" not in rendered and "```" not in rendered
        assert len(rendered.splitlines()) == 3
        assert "Response" in rendered
        assert "385.3k" in str(app.cards["father"].border_subtitle)
        assert app.query_one("#response", Markdown)._markdown == report
        assert app.query_one("#panels", TabbedContent).active == "response-tab"
        app.command("/help")
        assert app.query_one("#panels", TabbedContent).active == "activity-tab"
        for width, height in [(100, 35), (80, 25), (180, 45)]:
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            prompt = app.query_one(Input)
            footer = app.query_one("Footer")
            assert prompt.region.bottom <= footer.region.y
            assert prompt.region.bottom <= height
            assert app.query_one("#panels").region.bottom <= prompt.region.y
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_viewport_correction_and_redraw_preserve_input_and_response(setup):
    from textual.events import Resize
    from textual.geometry import Size

    class FakeConsole:
        viewport = (120, 38)

        def size(self):
            return self.viewport

    _, home, _, events, _, _ = setup
    engine = Engine(home, events)
    console = FakeConsole()
    app = AgentApp(home, engine, events, terminal=console)
    async with app.run_test(size=(120, 38)) as pilot:
        await pilot.pause(0.2)
        app.query_one(Input).value = "Keep this draft"
        events.status("father", "done", "**Keep this response**")
        await pilot.pause(0.3)
        app.post_message(Resize(Size(240, 9000), Size(240, 9000)))
        await pilot.pause(0.3)
        assert app.size == Size(120, 38)
        console.viewport = (100, 35)
        await pilot.pause(0.4)
        assert app.size == Size(100, 35)
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app.query_one(Input).value == "Keep this draft"
        assert app.query_one("#response", Markdown)._markdown == "**Keep this response**"
        assert app.query_one("#panels", TabbedContent).active == "response-tab"
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)
