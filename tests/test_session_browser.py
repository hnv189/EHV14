from test_engine import FakeRunner
from textual.widgets import DataTable, Input, Markdown, TabbedContent

from ehv_agent.engine import Engine
from ehv_agent.session_browser import SessionBrowser, SessionDetails
from ehv_agent.sessions import SessionStore
from ehv_agent.store import TaskStore
from ehv_agent.ui import AgentApp


def saved_session(home):
    store = TaskStore(home)
    task = store.submit("Implement CSV reports")
    store.claim(task)
    store.finish(task, "completed", "**CSV export** implemented and tested.")
    SessionStore(home).set_title(task["id"], "CSV Report Export and Validation", "father-model")
    return task


async def test_sessions_tab_opens_persisted_named_session_without_rerunning_it(setup):
    _, home, _, events, _, _ = setup
    task = saved_session(home)
    engine = Engine(home, events)
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f3")
        await pilot.pause(0.2)
        assert app.query_one("#panels", TabbedContent).active == "sessions-tab"
        table = app.query_one("#sessions-table", DataTable)
        assert table.row_count == 1
        assert table.get_row(task["id"])[0].plain == "CSV Report Export and Validation"
        app.query_one(Input).value = "Preserve my next request"
        table.focus()
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, SessionDetails)
        report = app.screen.query_one("#session-report", Markdown)._markdown
        assert "CSV Report Export and Validation" in report
        assert "CSV export" in report and "father-model" in report
        assert not list((home / "agents/father/pending").glob("*.json"))
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(Input).value == "Preserve my next request"
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_recent_sessions_refreshes_titles_and_status_without_stealing_tab_focus(setup):
    _, home, _, events, _, _ = setup
    store = TaskStore(home)
    task = store.submit("A pending workflow")
    engine = Engine(home, events)
    engine.paused.set()
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(100, 35)) as pilot:
        app.command("/sessions")
        await pilot.pause(0.2)
        browser = app.query_one(SessionBrowser)
        table = app.query_one("#sessions-table", DataTable)
        assert table.get_row(task["id"])[0].plain == "Awaiting father title"
        store.claim(task)
        SessionStore(home).set_title(task["id"], "CSV Export Acceptance Criteria", "father-model")
        store.finish(task, "completed", "Verified CSV report requirements")
        events.status("father", "done", "Verified CSV report requirements")
        await browser.refresh_sessions()
        await pilot.pause(0.2)
        assert table.get_row(task["id"])[0].plain == "CSV Export Acceptance Criteria"
        assert table.get_row(task["id"])[1].plain == "COMPLETED"
        assert app.query_one("#panels", TabbedContent).active == "sessions-tab"
        await pilot.resize_terminal(80, 25)
        await pilot.pause()
        assert app.query_one(Input).region.bottom <= app.query_one("Footer").region.y
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_legacy_session_can_request_father_title_and_refresh_saved_details(setup):
    import asyncio

    _, home, config, events, _, _ = setup
    store = TaskStore(home)
    task = store.submit("Inspect project structure")
    store.claim(task)
    store.finish(task, "completed", "Reviewed project structure")
    engine = Engine(home, events, FakeRunner)
    engine.paused.set()
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(100, 35)) as pilot:
        app.command(f"/sessions {task['id']}")
        await pilot.pause(0.3)
        assert isinstance(app.screen, SessionDetails)
        details = app.screen
        assert "Awaiting father title" in details.query_one("#session-report", Markdown)._markdown
        details.ask_father_to_name()
        assert SessionStore(home).get(task["id"]).title_state == "queued"
        assert not list((home / "agents/father/pending").glob("*.json"))
        await asyncio.to_thread(engine._name_session, SessionStore(home).get(task["id"]), config)
        await details.refresh_report()
        assert (
            "Project Requirements and Implementation Review"
            in details.query_one("#session-report", Markdown)._markdown
        )
        details.action_close()
        await pilot.pause()
        await app.query_one(SessionBrowser).refresh_sessions()
        assert (
            app.query_one("#sessions-table", DataTable).get_row(task["id"])[0].plain
            == "Project Requirements and Implementation Review"
        )
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)
