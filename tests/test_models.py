import json

import httpx
import pytest
from anthropic import Anthropic
from filelock import FileLock

from ehv_agent import cli, models
from ehv_agent.config import Config
from ehv_agent.models import AvailableModel, fetch_models, select_model

CATALOG = [
    AvailableModel("claude-large", "Large Claude", 32000, 200000),
    AvailableModel("claude-small", "Small Claude", 4096, 100000),
]


def api_model(model_id, **extra):
    return {
        "id": model_id,
        "type": "model",
        "display_name": model_id.title(),
        "created_at": "2026-10-01T00:00:00Z",
        **extra,
    }


def test_sdk_model_scan_paginates_and_reads_limits_without_network():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/v1/models"
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [api_model("claude-large", max_tokens=32000, max_input_tokens=200000)],
                    "has_more": True,
                    "last_id": "claude-large",
                    "first_id": "claude-large",
                },
            )
        assert request.url.params["after_id"] == "claude-large"
        return httpx.Response(
            200,
            json={
                "data": [
                    api_model("claude-small", max_tokens=4096),
                    api_model("retired", lifecycle="retired"),
                ],
                "has_more": False,
                "last_id": "retired",
                "first_id": "claude-small",
            },
        )

    with Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        result = fetch_models(client)
    assert [model.id for model in result] == ["claude-large", "claude-small"]
    assert result[1].max_tokens == 4096
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected"), (403, "permission"), (429, "rate limited"), (500, "HTTP 500")],
)
def test_scan_api_errors_are_actionable_and_do_not_echo_api_credentials(status, message):
    def handle(request):
        return httpx.Response(
            status, json={"error": {"type": "api_error", "message": "private-test-key"}}
        )

    with Anthropic(
        api_key="private-test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        with pytest.raises(ValueError, match=message) as error:
            fetch_models(client)
    assert "private-test-key" not in str(error.value)


def test_missing_key_and_empty_catalogue_are_clear_errors(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        fetch_models()
    with Anthropic(
        api_key="test",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"data": [], "has_more": False})
            )
        ),
    ) as client:
        with pytest.raises(ValueError, match="no available models"):
            fetch_models(client)


def test_select_default_role_inheritance_and_all_agents_preserve_other_settings(setup):
    _, home, config, _, _, _ = setup
    config.agents["father"]["model"] = "father-custom"
    config.save(home)
    saved = select_model(home, "default", "claude-large", CATALOG)
    assert saved.model_for("father") == "father-custom"
    assert saved.model_for("coding") == "claude-large"
    saved = select_model(home, "coding", "claude-small", CATALOG)
    assert saved.max_tokens == 8192 and saved.max_tokens_for("coding") == 4096
    assert saved.max_tokens_for("father") == 8192
    assert saved.agents["coding"]["instructions"] == config.agents["coding"]["instructions"]
    saved = select_model(home, "coding", "", CATALOG)
    assert saved.model_for("coding") == "claude-large"
    saved = select_model(home, "all", "claude-small", CATALOG)
    assert all(saved.model_for(role) == "claude-small" for role in saved.agents)
    assert all(not item["model"] for item in saved.agents.values())
    assert saved.workspace == config.workspace


def test_unavailable_model_and_invalid_inheritance_do_not_modify_config(setup):
    _, home, _, _, _, _ = setup
    before = (home / "config.json").read_bytes()
    for target, model_id in [
        ("default", "absent"),
        ("default", ""),
        ("all", ""),
        ("missing-role", "claude-large"),
    ]:
        with pytest.raises(ValueError):
            select_model(home, target, model_id, CATALOG)
    assert (home / "config.json").read_bytes() == before


def test_cli_interactive_selection_works_while_dashboard_lock_is_held(setup, monkeypatch, capsys):
    _, home, _, _, _, _ = setup
    monkeypatch.setattr(models, "fetch_models", lambda: CATALOG)
    choices = iter(["invalid", "99", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    with FileLock(str(home / "app.lock")):
        assert cli.main(["models", "--select", "--agent", "coding", "--home", str(home)]) == 0
    assert Config.load(home).model_for("coding") == "claude-small"
    assert "Choose a number" in capsys.readouterr().out
    assert cli.main(["models", "--inherit", "--agent", "coding", "--home", str(home)]) == 0
    assert not Config.load(home).agents["coding"]["model"]


def test_cli_catalogue_json_and_cancelled_selection(setup, monkeypatch, capsys):
    _, home, _, _, _, _ = setup
    monkeypatch.setattr(models, "fetch_models", lambda: CATALOG)
    assert cli.main(["models", "--json", "--home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)[1]["id"] == "claude-small"
    before = (home / "config.json").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert cli.main(["models", "--select", "--home", str(home)]) == 0
    assert (home / "config.json").read_bytes() == before
    assert cli.main(["models", "--set", "absent", "--home", str(home)]) == 1
    assert (home / "config.json").read_bytes() == before
