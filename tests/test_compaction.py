import json
from decimal import Decimal

import httpx
import pytest
from anthropic import Anthropic

from ehv_agent.config import Config
from ehv_agent.conversation import split_history
from ehv_agent.provider import ClaudeRunner
from ehv_agent.tools import Toolset
from ehv_agent.usage import UsageStore
from ehv_agent.workspace import Cancelled


def response(content, stop="tool_use", *, input_tokens=10):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_compaction_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        }
    ]
    for index, block in enumerate(content):
        if block["type"] == "text":
            initial = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            initial = {
                "type": "tool_use",
                "id": block.get("id", "tool_test"),
                "name": block["name"],
                "input": {},
            }
            delta = {
                "type": "input_json_delta",
                "partial_json": block.get("partial_json", json.dumps(block.get("input", {}))),
            }
        events += [
            {"type": "content_block_start", "index": index, "content_block": initial},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 20},
        },
        {"type": "message_stop"},
    ]
    data = "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


def tool(name, args, stop="tool_use", **kwargs):
    return response([{"type": "tool_use", "name": name, "input": args}], stop, **kwargs)


def done():
    return tool("complete_task", {"status": "passed", "summary": "Finished with evidence"})


def run(
    setup,
    handler,
    role="requirement",
    prompt="Inspect files and preserve the original requirements.",
):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    client = Anthropic(
        api_key="test-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    runner = ClaudeRunner(config, events, cancel, client=client)
    try:
        return runner.run(
            role, "work", prompt, Toolset(role, workspace, home, config, events, cancel)
        )
    finally:
        runner.close()


def assert_tool_pairs(messages):
    pending = set()
    for message in messages:
        if not isinstance(message["content"], list):
            assert not pending
            continue
        for block in message["content"]:
            if block["type"] == "tool_use":
                assert not pending
                pending.add(block["id"])
            elif block["type"] == "tool_result":
                assert block["tool_use_id"] in pending
                pending.remove(block["tool_use_id"])
    assert not pending


def test_truncated_plain_text_continues_and_respects_model_output_limit(setup):
    _, _, config, _, _, workspace = setup
    config.max_tokens = 256
    config.model_max_tokens[config.model] = 400
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response(
                [{"type": "text", "text": "Partial progress: inspected the entry point."}],
                "max_tokens",
            )
        assert body["max_tokens"] == 400
        assert body["messages"][-2]["content"][0]["text"].startswith("Partial progress:")
        assert "without repeating completed work" in body["messages"][-1]["content"]
        return done()

    assert run(setup, handle)["status"] == "passed"
    ledger = UsageStore(workspace.run).read()
    assert len(requests) == 2 and ledger["summary"]["message_count"] == 2
    assert Decimal(ledger["summary"]["cost_usd"]) == Decimal(".00044")


def test_truncated_tool_json_is_discarded_and_completed_write_executes_once(setup):
    _, _, config, _, _, workspace = setup
    config.max_tokens = 256
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response(
                [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "partial_json": '{"path":"unsafe.txt","content":"unfinished',
                    }
                ],
                "max_tokens",
            )
        if len(requests) == 2:
            assert body["max_tokens"] == 512
            assert not any(message["role"] == "assistant" for message in body["messages"])
            assert "none were executed" in body["messages"][-1]["content"]
            return tool("write_file", {"path": "hello.py", "content": "VALUE = 2\n"})
        return done()

    assert run(setup, handle, role="coding")["status"] == "passed"
    assert not (workspace.root / "unsafe.txt").exists()
    assert (workspace.root / "hello.py").read_text() == "VALUE = 2\n"
    assert UsageStore(workspace.run).summary()["message_count"] == 3


def test_repeated_output_truncation_stops_after_bounded_retries(setup):
    _, _, config, _, _, workspace = setup
    config.max_tokens = 256
    config.max_recovery_tokens = 700
    limits = []

    def handle(request):
        body = json.loads(request.content)
        limits.append(body["max_tokens"])
        return response([{"type": "text", "text": "More incomplete text."}], "max_tokens")

    with pytest.raises(RuntimeError, match="after 2 automatic recovery"):
        run(setup, handle)
    assert limits == [256, 512, 700]
    assert UsageStore(workspace.run).summary()["message_count"] == 3


@pytest.mark.parametrize("summary_result", ["complete", "truncated", "tool", "error"])
def test_automatic_compaction_preserves_task_and_complete_recent_exchanges(setup, summary_result):
    project, home, config, events, _, workspace = setup
    (project / "notes.txt").write_text(("Evidence " + "x" * 1500 + "\n") * 30)
    config.compact_threshold = 1024
    config.max_turns = 7
    work_calls = 0
    summary_calls = 0
    original = "Inspect notes.txt; do not modify files. Preserve this exact constraint."

    def handle(request):
        nonlocal work_calls, summary_calls
        body = json.loads(request.content)
        if "tools" not in body:
            summary_calls += 1
            assert "Do not perform the task or call tools" in body["system"]
            payload = json.loads(body["messages"][0]["content"])
            assert payload["original_request"] == original
            assert len(payload["earlier_messages"]) == 2
            if summary_result == "error":
                return httpx.Response(
                    400,
                    json={
                        "error": {"type": "invalid_request_error", "message": "Summary unavailable"}
                    },
                )
            if summary_result == "tool":
                return tool("write_file", {"path": "unsafe.txt", "content": "never execute"})
            return response(
                [
                    {
                        "type": "text",
                        "text": "Read notes.txt. No files changed. Continue the inspection.",
                    }
                ],
                "end_turn" if summary_result == "complete" else "max_tokens",
            )
        work_calls += 1
        assert_tool_pairs(body["messages"])
        if work_calls <= 3:
            return tool("read_file", {"path": "notes.txt"})
        assert body["messages"][0]["content"].startswith(original)
        if summary_result == "complete":
            assert len(body["messages"]) == 5  # pinned task/summary plus two exchanges
            assert "Earlier work summary" in body["messages"][0]["content"]
        else:
            assert len(body["messages"]) == 7  # failed compaction leaves all history intact
        return done()

    assert run(setup, handle, prompt=original)["status"] == "passed"
    assert work_calls == 4 and summary_calls == 1
    assert not (workspace.root / "unsafe.txt").exists()
    ledger = UsageStore(workspace.run).read()
    assert ledger["summary"]["message_count"] == 5
    assert [message["phase"] for message in ledger["messages"]] == ["work"] * 3 + [
        "compact",
        "work",
    ]
    assert (workspace.run / "compactions.json").exists() == (summary_result == "complete")
    assert not any(
        event["kind"] == "text" and "No files changed" in event["text"]
        for event in events.queue.queue
    )


def test_repeated_compaction_carries_forward_previous_summary(setup):
    project, _, config, _, _, workspace = setup
    (project / "notes.txt").write_text(("Evidence " + "x" * 1500 + "\n") * 30)
    config.compact_threshold = 1024
    config.max_turns = 10
    work_calls = 0
    summaries = []

    def handle(request):
        nonlocal work_calls
        body = json.loads(request.content)
        if "tools" not in body:
            payload = json.loads(body["messages"][0]["content"])
            assert payload["previous_summary"] == (summaries[-1] if summaries else "")
            summary = f"Original decisions retained. Inspection checkpoint {len(summaries) + 1}."
            summaries.append(summary)
            return response([{"type": "text", "text": summary}], "end_turn")
        work_calls += 1
        assert_tool_pairs(body["messages"])
        return tool("read_file", {"path": "notes.txt"}) if work_calls < 6 else done()

    assert run(setup, handle)["status"] == "passed"
    assert len(summaries) == 3
    assert UsageStore(workspace.run).summary()["message_count"] == 9
    assert len(json.loads((workspace.run / "compactions.json").read_text())) == 3


@pytest.mark.parametrize("error_kind", ["http_context", "stop_reason"])
def test_context_window_exhaustion_compacts_and_recovers(setup, error_kind):
    project, _, config, _, _, workspace = setup
    (project / "notes.txt").write_text("Some evidence.\n" * 100)
    config.compact_threshold = 1000000  # exercise recovery instead of the proactive trigger
    work_calls = 0

    def handle(request):
        nonlocal work_calls
        body = json.loads(request.content)
        if "tools" not in body:
            return response(
                [{"type": "text", "text": "Inspected notes.txt three times. No changes."}],
                "end_turn",
            )
        work_calls += 1
        assert_tool_pairs(body["messages"])
        if work_calls <= 3:
            return tool("read_file", {"path": "notes.txt"})
        if work_calls == 4:
            if error_kind == "http_context":
                return httpx.Response(
                    400,
                    json={
                        "error": {"type": "invalid_request_error", "message": "prompt is too long"}
                    },
                )
            return response(
                [{"type": "text", "text": "Context is full."}], "model_context_window_exceeded"
            )
        assert "Earlier work summary" in body["messages"][0]["content"]
        return done()

    assert run(setup, handle)["status"] == "passed"
    assert work_calls == 5
    assert UsageStore(workspace.run).summary()["message_count"] == 6


@pytest.mark.parametrize("setting", ["disabled", "budget"])
def test_compaction_can_be_disabled_and_cannot_exceed_call_budget(setup, setting):
    _, _, config, _, _, workspace = setup
    config.compact_threshold = 1024
    config.auto_compact = setting != "disabled"
    config.max_turns = 4
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "tools" in body  # no budget for a summary after the third exchange
        return tool("read_file", {"path": "hello.py"}) if len(calls) <= 3 else done()

    assert run(setup, handle)["status"] == "passed"
    assert len(calls) == 4 and not (workspace.run / "compactions.json").exists()


def test_cancellation_during_compaction_does_not_continue_the_task(setup):
    _, _, config, _, cancel, workspace = setup
    config.compact_threshold = 1024
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if "tools" not in body:
            cancel.set()
            return response([{"type": "text", "text": "Not accepted."}], "end_turn")
        return tool("read_file", {"path": "hello.py"})

    with pytest.raises(Cancelled):
        run(setup, handle)
    assert len(calls) == 4
    assert not (workspace.run / "compactions.json").exists()
    assert not UsageStore(workspace.run).summary()["complete"]


def test_compaction_strips_invalidated_thinking_without_mutating_history():
    history = [{"role": "user", "content": "Original task"}]
    for index in range(3):
        history += [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "old-prefix"},
                    {
                        "type": "tool_use",
                        "id": str(index),
                        "name": "read_file",
                        "input": {"path": "hello.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": str(index), "content": "result"}
                ],
            },
        ]
    before = json.dumps(history)
    earlier, recent = split_history(history)
    assert "thinking" not in json.dumps([earlier, recent])
    assert_tool_pairs(recent)
    assert json.dumps(history) == before


def test_conversation_config_defaults_and_validation(setup):
    _, home, config, _, _, _ = setup
    assert Config.load(home).auto_compact
    assert config.recovery_tokens_for("father") == 32768
    config.model_context_tokens[config.model] = 16000
    assert config.compact_threshold_for("father", 4000) == 8000
    for key, value in [
        ("auto_compact", "yes"),
        ("compact_threshold", 0),
        ("max_token_retries", -1),
        ("max_recovery_tokens", 100000),
    ]:
        changed = Config.load(home)
        setattr(changed, key, value)
        with pytest.raises(ValueError):
            changed.save(home)
