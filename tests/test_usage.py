import json
import os
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from anthropic import Anthropic
from anthropic.types import Usage
from test_engine import FakeRunner
from test_provider import sse_response

from ehv_agent.config import Config, atomic_json
from ehv_agent.engine import Engine
from ehv_agent.pricing import estimate_cost, token_counts
from ehv_agent.provider import ClaudeRunner
from ehv_agent.sessions import SessionStore
from ehv_agent.tools import Toolset
from ehv_agent.usage import UsageStore, cost_label
from ehv_agent.workspace import Cancelled


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        ("claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 2000}, ".022"),
        ("claude-sonnet-5-5-20261001", {"input_tokens": 1000}, ".002"),
        ("claude-opus-5-5", {"cache_read_input_tokens": 1000000}, ".2"),
        ("claude-fable-5-1", {"cache_read_input_tokens": 1000000}, ".25"),
        ("claude-haiku-5-5", {"input_tokens": 100000}, ".01"),
        ("claude-haiku-5-5", {"input_tokens": 1, "cache_read_input_tokens": 100000}, ".0050005"),
        ("claude-sonnet-5-5", {"input_tokens": 1000, "inference_geo": "us"}, ".0022"),
    ],
)
def test_documented_prices_aliases_context_threshold_and_geography(model, usage, expected):
    assert Decimal(estimate_cost(model, usage, {})["cost_usd"]) == Decimal(expected)


def test_cache_categories_are_not_double_counted():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 5000,
        "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 3000},
        "cache_read_input_tokens": 10000,
    }
    result = estimate_cost("claude-sonnet-5-5", usage, {})
    # .002 input + .005 output + .005 five-minute writes + .012 hourly writes + .001 reads.
    assert Decimal(result["cost_usd"]) == Decimal(".025")
    assert sum(token_counts(usage).values()) == 16500
    assert Decimal(
        estimate_cost("claude-sonnet-5-5", {"cache_creation_input_tokens": 1000}, {})["cost_usd"]
    ) == Decimal(".0025")


def test_unknown_models_and_unsupported_charges_are_not_reported_as_free():
    for model, usage in [
        ("claude-sonnet-5-50", {"input_tokens": 100}),
        ("claude-sonnet-5-5", None),
        ("claude-sonnet-5-5", {"service_tier": "priority", "input_tokens": 100}),
        ("claude-sonnet-5-5", {"server_tool_use": {"web_search_requests": 1}}),
        ("claude-sonnet-4-5", {"input_tokens": 200001}),
    ]:
        result = estimate_cost(model, usage, {})
        assert result["cost_usd"] is None
        assert cost_label(result["cost_usd"]) == "Unavailable"


def test_custom_rates_roundtrip_and_invalid_rates_do_not_modify_config(setup):
    _, home, config, _, _, _ = setup
    rates = {
        "input": "1.5",
        "output": "7.5",
        "cache_write_5m": "2",
        "cache_write_1h": "3",
        "cache_read": ".1",
    }
    config.model_pricing = {"private-model": rates}
    config.save(home)
    loaded = Config.load(home)
    result = estimate_cost(
        "private-model", {"input_tokens": 1000, "output_tokens": 2000}, loaded.model_pricing
    )
    assert Decimal(result["cost_usd"]) == Decimal(".0165")
    assert result["pricing_source"] == "config:model_pricing"
    before = (home / "config.json").read_bytes()
    for invalid in ["NaN", "Infinity", "-1", True, "not a number"]:
        config.model_pricing["private-model"]["input"] = invalid
        with pytest.raises(ValueError, match="Pricing rates"):
            config.save(home)
        assert (home / "config.json").read_bytes() == before


def record(
    store, role="father", phase="plan", model="claude-sonnet-5-5", usage=None, complete=True
):
    return store.record(
        role=role,
        phase=phase,
        turn=1,
        model=model,
        response_id="msg-test",
        usage={"input_tokens": 1000, "output_tokens": 2000} if usage is None else usage,
        complete=complete,
        overrides={},
    )


def test_session_ledger_totals_all_roles_rework_and_naming_and_keeps_precision(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Update value")
    engine.run_task(task, config)
    run = home / "runs" / task["id"]
    store = UsageStore(run)
    record(store)
    record(store, role="coding", phase="work")
    record(store, role="testing", phase="work")
    record(store, role="coding", phase="work")
    record(store, phase="review")
    record(store, phase="name")
    ledger = store.read()
    summary = ledger["summary"]
    assert summary["message_count"] == 6 and summary["complete"]
    assert Decimal(summary["cost_usd"]) == Decimal(".132")
    assert Decimal(summary["by_role"]["father"]["cost_usd"]) == Decimal(".066")
    assert Decimal(summary["by_role"]["coding"]["cost_usd"]) == Decimal(".044")
    assert len({message["id"] for message in ledger["messages"]}) == 6
    assert ledger["messages"][0]["rates"]["input"] == "2"
    assert json.loads((run / "outcome.json").read_text())["usage"] == summary
    reopened = SessionStore(home).get(task["id"])
    assert reopened.cost_usd == summary["cost_usd"]
    assert reopened.message_count == 6
    report = SessionStore(home).report(task["id"])
    assert "$0.132000 est." in report and "## Message costs" in report
    assert "| 6 | father | name |" in report
    # A separate session cannot inherit the previous workflow's charges.
    other = UsageStore(home / "runs" / "another-session")
    assert other.summary() is None
    assert Decimal(record(other)[1]["cost_usd"]) == Decimal(".022")


def test_unknown_and_partial_messages_make_total_incomplete(setup):
    _, home, _, _, _, _ = setup
    store = UsageStore(home / "runs" / "partial")
    _, summary = record(store, model="unknown-model")
    assert summary["cost_usd"] is None and not summary["complete"]
    _, summary = record(store, complete=False)
    assert summary["unpriced_messages"] == 1 and summary["partial_messages"] == 1
    assert Decimal(summary["cost_usd"]) == Decimal(".022")
    assert cost_label(summary["cost_usd"], summary["complete"]) == "$0.022000+ est."


def test_real_sdk_records_each_tool_turn_once_and_session_sum(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return sse_response("read_file", {"path": "hello.py"})
        return sse_response("complete_task", {"status": "passed", "summary": "Read the file"})

    client = Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    runner = ClaudeRunner(config, events, cancel, client=client)
    try:
        runner.run(
            "requirement",
            "work",
            "Inspect",
            Toolset("requirement", workspace, home, config, events, cancel),
        )
    finally:
        runner.close()
    ledger = UsageStore(workspace.run).read()
    assert len(ledger["messages"]) == 2
    # Each SSE response has input=10, cumulative output=20 (not initial 1 + final 20).
    assert ledger["summary"]["tokens"]["output"] == 40
    assert Decimal(ledger["summary"]["cost_usd"]) == Decimal(".00044")
    usage_events = [event for event in events.queue.queue if event["kind"] == "usage"]
    assert len(usage_events) == 2
    assert usage_events[-1]["session_usage"] == ledger["summary"]


@pytest.mark.parametrize("failure", ["cancel", "disconnect"])
def test_interrupted_stream_saves_observed_usage_and_does_not_double_count(setup, failure):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def __iter__(self):
            yield SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(
                    id="partial-response",
                    model="claude-sonnet-5-5",
                    usage=Usage(input_tokens=1000, output_tokens=1),
                ),
            )
            if failure == "cancel":
                cancel.set()
            yield SimpleNamespace(
                type="message_delta", usage=Usage(input_tokens=1000, output_tokens=12)
            )
            raise OSError("Disconnected")

    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_: Stream()))
    runner = ClaudeRunner(config, events, cancel, client=client)
    with pytest.raises(Cancelled if failure == "cancel" else OSError):
        runner.run(
            "requirement",
            "work",
            "Inspect",
            Toolset("requirement", workspace, home, config, events, cancel),
        )
    ledger = UsageStore(workspace.run).read()
    assert len(ledger["messages"]) == 1
    assert ledger["messages"][0]["usage"]["output_tokens"] == 12
    assert not ledger["summary"]["complete"]
    assert Decimal(ledger["summary"]["cost_usd"]) == Decimal(".00212")


def test_api_failure_without_usage_is_unknown_instead_of_zero(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()

    def fail(**_):
        raise OSError("Connection failed")

    runner = ClaudeRunner(
        config, events, cancel, client=SimpleNamespace(messages=SimpleNamespace(stream=fail))
    )
    with pytest.raises(OSError):
        runner.run(
            "father", "plan", "Question", Toolset("father", workspace, home, config, events, cancel)
        )
    summary = UsageStore(workspace.run).summary()
    assert summary["cost_usd"] is None and summary["unpriced_messages"] == 1


@pytest.mark.parametrize("mode", ["direct", "delegated", "failed"])
def test_engine_preserves_session_accounting_in_outcomes(setup, mode):
    _, home, config, events, _, _ = setup

    class AccountingRunner(FakeRunner):
        plan_roles = () if mode == "direct" else ("coding", "testing")
        task_type = "question" if mode == "direct" else "implementation"
        answer = "VALUE is 1." if mode == "direct" else ""
        fail_tests = mode == "failed"

        def run(self, role, phase, prompt, tools):
            record(UsageStore(tools.workspace.run), role=role, phase=phase)
            return super().run(role, phase, prompt, tools)

    engine = Engine(home, events, AccountingRunner)
    task = engine.enqueue("Inspect or update VALUE")
    engine.run_task(task, config)
    outcome = json.loads((home / "runs" / task["id"] / "outcome.json").read_text())
    assert outcome["status"] == ("failed" if mode == "failed" else "completed")
    assert outcome["usage"]["message_count"] == (1 if mode == "direct" else 4)
    assert (
        Decimal(outcome["usage"]["cost_usd"]) == Decimal(".022") * outcome["usage"]["message_count"]
    )


def test_legacy_and_malformed_cost_history_do_not_show_a_zero_bill(setup):
    _, home, config, events, _, _ = setup
    engine = Engine(home, events, FakeRunner)
    task = engine.enqueue("Update value")
    engine.run_task(task, config)
    assert SessionStore(home).get(task["id"]).cost_usd is None
    assert "Unavailable" in SessionStore(home).report(task["id"])
    atomic_json(
        home / "runs" / task["id"] / "usage.json", {"summary": {"cost_usd": "NaN"}, "messages": []}
    )
    assert "Unavailable" in SessionStore(home).report(task["id"])


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing behavior")
def test_transient_windows_sharing_violation_does_not_lose_or_duplicate_usage(setup, monkeypatch):
    from ehv_agent import config as config_module

    _, home, _, _, _, _ = setup
    store = UsageStore(home / "runs" / "sharing")
    record(store)
    original = config_module.os.replace
    attempts = 0

    def transient(source, destination):
        nonlocal attempts
        if destination == store.path:
            attempts += 1
            if attempts <= 2:
                error = PermissionError("Temporary sharing violation")
                error.winerror = 32
                raise error
        return original(source, destination)

    monkeypatch.setattr(config_module.os, "replace", transient)
    record(store)
    assert attempts == 3
    assert store.summary()["message_count"] == 2
    assert Decimal(store.summary()["cost_usd"]) == Decimal(".044")
