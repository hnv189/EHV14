import json

import httpx
import pytest
from anthropic import Anthropic

from ehv_agent.provider import ClaudeRunner
from ehv_agent.tools import Toolset


def sse_response(name, arguments):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Inspecting the project.\n"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tool_test", "name": name, "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 20},
        },
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def test_real_sdk_streams_and_roundtrips_tool_results_without_network(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse_response("read_file", {"path": "hello.py"})
        result = body["messages"][-1]["content"][0]
        assert result["type"] == "tool_result"
        assert "VALUE = 1" in result["content"]
        return sse_response(
            "complete_task", {"status": "passed", "summary": "Inspected requirements"}
        )

    client = Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    runner = ClaudeRunner(config, events, cancel, client=client)
    try:
        result = runner.run(
            "requirement",
            "work",
            "Inspect the project",
            Toolset("requirement", workspace, home, config, events, cancel),
        )
    finally:
        runner.close()
    assert result["status"] == "passed"
    assert len(requests) == 2
    assert requests[0]["tool_choice"]["disable_parallel_tool_use"] is True
    assert "write_file" not in {t["name"] for t in requests[0]["tools"]}
    assert any(e["kind"] == "text" and "Inspecting" in e["text"] for e in list(events.queue.queue))


def test_provider_enforces_role_tool_permissions_and_recovers_from_tool_error(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse_response("write_file", {"path": "hello.py", "content": "oops"})
        assert body["messages"][-1]["content"][0]["is_error"] is True
        return sse_response(
            "complete_task", {"status": "blocked", "summary": "Cannot write as requirement agent"}
        )

    client = Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    runner = ClaudeRunner(config, events, cancel, client=client)
    try:
        result = runner.run(
            "requirement",
            "work",
            "Do work",
            Toolset("requirement", workspace, home, config, events, cancel),
        )
    finally:
        runner.close()
    assert result["status"] == "blocked"
    assert (workspace.root / "hello.py").read_text() == "VALUE = 1\n"


def test_invalid_plan_is_rejected():
    with pytest.raises(ValueError, match="task_type"):
        ClaudeRunner.validate_terminal("submit_plan", {"steps": []})
    with pytest.raises(ValueError, match="summary"):
        ClaudeRunner.validate_terminal("complete_task", {"status": "passed"})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"task_type": "other"}, "task_type"),
        ({"task_type": []}, "task_type"),
        ({"reason": " "}, "reason"),
        ({"steps": None}, "list"),
        ({"answer": " "}, "answer"),
        ({"answer": None}, "answer"),
        ({"task_type": "implementation"}, "coding assignment"),
        ({"task_type": "validation"}, "testing assignment"),
        ({"steps": [{"role": "father", "prompt": "x"}]}, "roles"),
        ({"steps": [{"role": "coding", "prompt": " "}]}, "prompts"),
        ({"steps": [{"role": "coding", "prompt": "x"}] * 2}, "unique"),
        ({"steps": [{"role": role, "prompt": "x"} for role in ["testing", "coding"]]}, "order"),
        ({"steps": [{"role": "coding", "prompt": "x"}] * 5}, "four"),
        ({"steps": [{"role": "requirement", "prompt": "x"}]}, "only for a direct"),
    ],
)
def test_plan_routing_rejects_invalid_or_ambiguous_assignments(changes, message):
    plan = {
        "session_title": "Explain the Current Project Value",
        "task_type": "question",
        "reason": "The father can answer from the file.",
        "steps": [],
        "answer": "VALUE is 1.",
        **changes,
    }
    with pytest.raises(ValueError, match=message):
        ClaudeRunner.validate_terminal("submit_plan", plan)


@pytest.mark.parametrize(
    ("task_type", "roles"),
    [
        ("question", []),
        ("analysis", []),
        ("analysis", ["requirement", "design"]),
        ("implementation", ["coding"]),
        ("implementation", ["coding", "testing"]),
        ("implementation", ["requirement", "design", "coding", "testing"]),
        ("validation", ["testing"]),
    ],
)
def test_plan_routing_accepts_direct_answers_and_specialist_subsets(task_type, roles):
    plan = {
        "session_title": "Inspect and Update the Project Value",
        "task_type": task_type,
        "reason": "Choose only the necessary work.",
        "steps": [{"role": role, "prompt": "Inspect the relevant files."} for role in roles],
    }
    if not roles:
        plan["answer"] = "VALUE is 1."
    ClaudeRunner.validate_terminal("submit_plan", plan)


def test_real_sdk_father_recovers_from_missing_answer_then_finishes_directly(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    # Existing homes can still have the old role instructions.
    config.agents["father"]["instructions"] = "Always delegate to all four specialists."
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "These phase rules replace any older instruction" in body["system"]
        definitions = {tool["name"]: tool for tool in body["tools"]}
        assert "write_file" not in definitions and "run_command" not in definitions
        plan_schema = definitions["submit_plan"]["input_schema"]
        assert plan_schema["properties"]["steps"]["minItems"] == 0
        assert "answer" not in plan_schema["required"]
        plan = {
            "session_title": "Explain the Current Project Value",
            "task_type": "question",
            "reason": "This needs only a direct explanation.",
            "steps": [],
        }
        if len(requests) > 1:
            assert body["messages"][-1]["content"][0]["is_error"] is True
            plan["answer"] = "The project defines VALUE as 1."
        return sse_response("submit_plan", plan)

    with Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        result = ClaudeRunner(config, events, cancel, client=client).run(
            "father",
            "plan",
            "Explain this project",
            Toolset("father", workspace, home, config, events, cancel),
        )
    assert len(requests) == 2
    assert result["steps"] == []
    assert result["answer"] == "The project defines VALUE as 1."


def test_selected_models_output_limit_is_used_in_real_sdk_request(setup):
    _, home, config, events, cancel, workspace = setup
    workspace.prepare()
    config.model_max_tokens[config.model] = 4096

    def handle(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 4096
        return sse_response(
            "complete_task", {"status": "passed", "summary": "Requirements reviewed"}
        )

    with Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        runner = ClaudeRunner(config, events, cancel, client=client)
        result = runner.run(
            "requirement",
            "work",
            "Inspect",
            Toolset("requirement", workspace, home, config, events, cancel),
        )
    assert result["status"] == "passed"


def test_historical_naming_uses_only_father_title_tool_in_real_sdk(setup):
    _, home, config, events, cancel, workspace = setup

    def handle(request):
        body = json.loads(request.content)
        assert {tool["name"] for tool in body["tools"]} == {"name_session"}
        assert "session_title" in body["tools"][0]["input_schema"]["required"]
        return sse_response(
            "name_session", {"session_title": "Billing Function Integration and Tests"}
        )

    with Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        result = ClaudeRunner(config, events, cancel, client=client).run(
            "father",
            "name",
            "Name the saved billing task",
            Toolset("father", workspace, home, config, events, cancel),
        )
    assert result["session_title"] == "Billing Function Integration and Tests"
    assert not workspace.root.exists()
