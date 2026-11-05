import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from anthropic import Anthropic
from test_provider import sse_response
from textual.containers import VerticalScroll
from textual.widgets import Input, Markdown, Static, TabbedContent

from ehv_agent.chat import ChatTurn
from ehv_agent.config import Config
from ehv_agent.engine import Engine
from ehv_agent.provider import ClaudeRunner
from ehv_agent.session_browser import SessionDetails
from ehv_agent.sessions import SessionStore
from ehv_agent.store import TaskStore
from ehv_agent.ui import AgentApp
from ehv_agent.usage import UsageStore


class ChatRunner:
    prompts = []

    def __init__(self, config, events, cancel):
        self.events = events
        self.cancel = cancel

    def run(self, role, mode, prompt, tools):
        if mode == "name":
            return {"session_title": "Saved Conversation and Project Analysis"}
        assert role == "father" and mode == "plan"
        self.prompts.append(prompt)
        ask = prompt.split("Original user request:\n", 1)[1].split("\n", 1)[0]
        answer = f"Answer to **{ask}**.\n\n" + "\n\n".join(
            f"Evidence paragraph {number}." for number in range(20)
        )
        self.events.status("father", "working", "Plan · test model")
        self.events.emit("father", "text", answer[:40])
        self.events.emit("father", "text", answer[40:])
        store = UsageStore(tools.workspace.run)
        _, exchange = store.record(
            role="father",
            phase="plan",
            turn=1,
            model="claude-sonnet-5-5",
            response_id="test-response",
            usage={"input_tokens": 1000, "output_tokens": 2000},
            complete=True,
            overrides={},
        )
        self.events.emit(
            "father",
            "usage",
            "Ask–answer $0.022000 est.",
            session_usage=SessionStore(tools.home).usage(store.session_id()),
            exchange_usage=exchange,
        )
        return {
            "session_title": "Project Questions and Follow-up Analysis",
            "task_type": "question",
            "reason": "Can answer directly",
            "steps": [],
            "answer": answer,
        }

    def abort(self):
        self.cancel.set()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset_chat_runner():
    ChatRunner.prompts = []


def test_followups_share_session_context_and_sum_all_exchange_costs(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    first = engine.enqueue("Remember the alpha decision")
    engine.run_task(first, config)
    second = engine.enqueue("Explain that decision")
    future = engine.enqueue("Future request must not enter earlier context")
    engine.run_task(second, Config.load(home))
    assert first["session_id"] == second["session_id"] == future["session_id"]
    assert "Remember the alpha decision" in ChatRunner.prompts[-1]
    assert "Answer to **Remember the alpha decision**" in ChatRunner.prompts[-1]
    assert "Future request must not enter earlier context" not in ChatRunner.prompts[-1]
    session_id = first["session_id"]
    # An unprocessed ask keeps the running total partial, without counting it as free.
    usage = engine.sessions.usage(session_id)
    assert Decimal(usage["cost_usd"]) == Decimal(".044") and not usage["complete"]
    engine.run_task(future, Config.load(home))
    reopened = SessionStore(home)
    session = reopened.get(session_id)
    assert session.turn_count == 3 and session.message_count == 3
    assert Decimal(session.cost_usd) == Decimal(".066") and session.cost_complete
    assert len(reopened.recent()) == 1
    assert [turn.id for turn in reopened.turns(session_id)] == [
        first["id"],
        second["id"],
        future["id"],
    ]
    assert reopened.current_id() == session_id
    report = reopened.report(session_id)
    assert "You · 1" in report and "You · 3" in report
    assert "Session total (USD):** $0.066000 est." in report
    assert report.count("**Ask–answer cost:** $0.022000 est.") == 3
    ledger = UsageStore(home / "runs" / first["id"]).read()
    assert ledger["messages"][0]["session_id"] == session_id
    assert ledger["messages"][0]["task_id"] == first["id"]


def test_real_sdk_events_report_exchange_cost_and_cumulative_conversation_cost(setup):
    _, home, config, events, _, _ = setup
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 2:
            assert "SDK answer 1" in body["messages"][0]["content"]
        return sse_response(
            "submit_plan",
            {
                "session_title": "Persistent Conversation and Cost Tracking",
                "task_type": "question",
                "reason": "Can answer directly",
                "steps": [],
                "answer": f"SDK answer {len(bodies)}",
            },
        )

    def factory(config, events, cancel):
        client = Anthropic(
            api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
        )
        return ClaudeRunner(config, events, cancel, client=client)

    engine = Engine(home, events, factory)
    first = engine.enqueue("First SDK ask")
    engine.run_task(first, config)
    second = engine.enqueue("Second SDK ask")
    engine.run_task(second, Config.load(home))
    usage_events = [event for event in events.queue.queue if event["kind"] == "usage"]
    assert len(usage_events) == 2
    assert usage_events[1]["task_id"] == second["id"]
    assert usage_events[1]["session_id"] == first["session_id"]
    assert Decimal(usage_events[1]["exchange_usage"]["cost_usd"]) == Decimal(".00022")
    assert Decimal(usage_events[1]["session_usage"]["cost_usd"]) == Decimal(".00044")
    assert Decimal(engine.sessions.get(first["session_id"]).cost_usd) == Decimal(".00044")


def test_new_session_has_no_previous_context_and_keeps_old_history(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    first = engine.enqueue("Remember secret project choice alpha")
    engine.run_task(first, config)
    fresh = engine.sessions.create(config.workspace)
    assert engine.sessions.get(fresh).cost_usd == "0"
    second = engine.enqueue("Start independent work")
    engine.run_task(second, Config.load(home))
    assert second["session_id"] == fresh != first["session_id"]
    assert "secret project choice alpha" not in ChatRunner.prompts[-1]
    assert len(engine.sessions.recent()) == 2
    assert Decimal(engine.sessions.get(fresh).cost_usd) == Decimal(".022")
    assert Decimal(engine.sessions.get(first["session_id"]).cost_usd) == Decimal(".022")


def test_legacy_session_can_be_continued_without_losing_its_first_exchange(setup):
    project, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    old = TaskStore(home).submit("Original legacy question", workspace=str(project))
    engine.run_task(old, config)
    engine.sessions.activate(old["id"])
    followup = engine.enqueue("Follow up on legacy answer")
    engine.run_task(followup, Config.load(home))
    assert followup["session_id"] == old["id"]
    assert len(engine.sessions.recent()) == 1
    assert engine.sessions.get(old["id"]).turn_count == 2
    assert Decimal(engine.sessions.get(old["id"]).cost_usd) == Decimal(".044")
    assert "Original legacy question" in ChatRunner.prompts[-1]


def test_retry_and_background_costs_stay_attached_to_original_session(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    old = engine.enqueue("First conversation")
    engine.run_task(old, config)
    fresh = engine.sessions.create(config.workspace)
    retry = engine.store.retry(old["id"])
    engine.run_task(retry, Config.load(home))
    assert retry["session_id"] == old["session_id"]
    assert engine.sessions.current_id() == fresh
    assert engine.sessions.get(fresh).cost_usd == "0"
    assert Decimal(engine.sessions.get(old["session_id"]).cost_usd) == Decimal(".044")
    scoped = [event for event in events.queue.queue if event.get("task_id") == retry["id"]]
    assert scoped and all(event["session_id"] == old["session_id"] for event in scoped)


def test_history_tool_excludes_future_and_pending_requests(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    first = engine.enqueue("Completed earlier ask")
    engine.run_task(first, config)
    pending = engine.enqueue("Scheduled older ask, not yet executed")
    current = engine.enqueue("Current ask")
    future = engine.enqueue("Future ask")
    engine.run_task(current, Config.load(home))
    assert "Scheduled older ask" not in ChatRunner.prompts[-1]
    from ehv_agent.workspace import Workspace

    workspace = Workspace(
        home.parent,
        home / "runs" / current["id"],
        home,
        config.excludes,
        engine.cancel,
        lambda _: None,
    )
    result = engine._tools("father", workspace, config).tool_read_session_history()
    assert "Completed earlier ask" in result
    assert pending["prompt"] not in result and future["prompt"] not in result


async def test_chat_appends_exchanges_preserves_scroll_and_restarts_in_same_session(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    engine.paused.set()
    first = engine.enqueue("First visible ask")
    engine.run_task(first, config)
    second = engine.enqueue("Second visible ask")
    engine.run_task(second, Config.load(home))
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.refresh_chat()
        await pilot.pause(0.3)
        assert len(app.query(ChatTurn)) == 2
        first_widget = app.chat_widgets[first["id"]]
        assert "First visible ask" in first_widget.query_one(".chat-question", Markdown)._markdown
        assert (
            "Answer to **First visible ask**"
            in first_widget.query_one(".chat-answer", Markdown)._markdown
        )
        assert "$0.022000 est." in str(first_widget.query_one(".chat-cost", Static).render())
        assert "$0.044000 est." in str(app.query_one("#response-role", Static).render())
        scroll = app.query_one("#response-scroll", VerticalScroll)
        scroll.scroll_home(animate=False)
        await pilot.pause(0.2)
        position = scroll.scroll_y
        assert scroll.max_scroll_y > 0
        task = engine.enqueue("Third visible ask", session_id=app.current_session_id)
        await asyncio.to_thread(engine.run_task, task, Config.load(home))
        await app.refresh_chat()
        await pilot.pause(0.4)
        assert len(app.query(ChatTurn)) == 3
        assert app.chat_widgets[first["id"]] is first_widget
        assert scroll.scroll_y == position
        assert "$0.066000 est." in str(app.query_one("#response-role", Static).render())
        old_session = app.current_session_id
        app.command("/newsession")
        await app.refresh_chat()
        await pilot.pause(0.3)
        assert app.current_session_id != old_session
        assert len(app.query(ChatTurn)) == 0
        assert "$0.000000 est." in str(app.query_one("#response-role", Static).render())
        assert engine.sessions.get(old_session).turn_count == 3
        app.open_session(old_session)
        await pilot.pause(0.2)
        assert isinstance(app.screen, SessionDetails)
        app.screen.continue_session()
        await app.refresh_chat()
        await pilot.pause(0.3)
        assert len(app.query(ChatTurn)) == 3
        assert app.query_one("#panels", TabbedContent).active == "response-tab"
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)
    reopened = AgentApp(home, Engine(home, events), events)
    assert reopened.current_session_id == old_session


async def test_new_session_during_work_isolated_from_old_events_and_input_appends(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    engine.paused.set()
    old = engine.enqueue("Old queued ask")
    app = AgentApp(home, engine, events)
    async with app.run_test(size=(120, 40)) as pilot:
        app.command("/newsession")
        fresh = app.current_session_id
        await app.refresh_chat()
        await asyncio.to_thread(engine.run_task, old, config)
        await pilot.pause(0.3)
        assert app.current_session_id == fresh and app.cost_session_id == fresh
        assert app.session_usage["cost_usd"] == "0"
        assert len(app.query(ChatTurn)) == 0
        app.query_one("#prompt", Input).value = "New conversation ask"
        await pilot.press("enter")
        await app.refresh_chat()
        await pilot.pause(0.3)
        turns = engine.sessions.turns(fresh)
        assert len(turns) == 1 and turns[0].prompt == "New conversation ask"
        assert len(app.query(ChatTurn)) == 1
        assert (
            "Old queued ask"
            not in app.query_one(ChatTurn).query_one(".chat-question", Markdown)._markdown
        )
        assert not engine.cancel.is_set()
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)


async def test_externally_queued_asks_open_a_persistent_conversation_in_idle_dashboard(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, ChatRunner)
    engine.paused.set()
    app = AgentApp(home, engine, events)
    assert app.current_session_id is None
    async with app.run_test(size=(100, 35)) as pilot:
        first = engine.enqueue("Ask queued from another terminal")
        await pilot.pause(0.3)
        assert app.current_session_id == first["session_id"]
        await asyncio.to_thread(engine.run_task, first, config)
        second = engine.enqueue("Second external ask")
        await asyncio.to_thread(engine.run_task, second, Config.load(home))
        await app.refresh_chat()
        await pilot.pause(0.3)
        assert app.current_session_id == second["session_id"] == first["session_id"]
        assert len(app.query(ChatTurn)) == 2
        assert "$0.044000 est." in str(app.query_one("#response-role", Static).render())
        app.action_request_quit()
        await pilot.pause(0.2)
    engine.thread.join(timeout=3)
