import json

from filelock import FileLock
from test_engine import FakeRunner

from ehv_agent import cli
from ehv_agent.config import Config
from ehv_agent.engine import Engine
from ehv_agent.store import TaskStore


def test_cli_once_uses_real_queue_and_returns_success(setup, monkeypatch, capsys):
    project, home, _, _, _, _ = setup
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(cli, "Engine", lambda h, e: Engine(h, e, FakeRunner))
    assert cli.main(["enqueue", "Change VALUE to 2", "--home", str(home)]) == 0
    assert cli.main(["start", "--once", "--home", str(home)]) == 0
    output = capsys.readouterr().out
    assert "test-only" not in output
    assert '"kind": "output"' in output
    assert (project / "hello.py").read_text() == "VALUE = 2\n"
    assert cli.main(["start", "--once", "--home", str(home)]) == 0
    assert "No due pending tasks" in capsys.readouterr().out


def test_cli_missing_key_keeps_tasks_pending(setup, monkeypatch, capsys):
    _, home, _, _, _, _ = setup
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    task = TaskStore(home).submit("Don't consume this")
    assert cli.main(["start", "--once", "--home", str(home)]) == 1
    assert "ANTHROPIC_API_KEY is missing" in capsys.readouterr().err
    assert (home / "agents/father/pending" / f"{task['id']}.json").exists()


def test_cli_config_validation_and_doctor(setup, monkeypatch, capsys):
    _, home, _, _, _, _ = setup
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-secret")
    assert cli.main(["config", "max_rework", "2", "--home", str(home)]) == 0
    assert Config.load(home).max_rework == 2
    assert cli.main(["config", "poll_seconds", "-5", "--home", str(home)]) == 1
    assert Config.load(home).poll_seconds == 1
    assert cli.main(["doctor", "--home", str(home)]) == 0
    captured = capsys.readouterr()
    assert "private-secret" not in captured.out + captured.err
    assert "set (value hidden)" in captured.out
    assert json.loads((home / "config.json").read_text())["max_rework"] == 2


def test_existing_dashboard_lock_gives_recovery_instructions(setup, monkeypatch, capsys):
    _, home, _, _, _, _ = setup
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    with FileLock(str(home / "app.lock")):
        assert cli.main(["start", "--once", "--home", str(home)]) == 1
    error = capsys.readouterr().err
    assert "Another instance" in error
    assert "Ctrl+Q" in error and "models --select" in error


def test_proxy_cli_configuration_is_persisted_and_validated(setup, capsys):
    _, home, _, _, _, _ = setup
    assert (
        cli.main(["config", "proxy_url", "http://proxy.example.com:8080", "--home", str(home)]) == 0
    )
    assert cli.main(["config", "proxy_mode", "custom", "--home", str(home)]) == 0
    saved = Config.load(home)
    assert saved.proxy_mode == "custom"
    assert saved.proxy_url == "http://proxy.example.com:8080"
    assert cli.main(["config", "proxy_mode", "invalid", "--home", str(home)]) == 1
    assert Config.load(home).proxy_mode == "custom"
    assert "proxy_mode must be" in capsys.readouterr().err
    assert cli.main(["config", "proxy_mode", "direct", "--home", str(home)]) == 0
    assert Config.load(home).proxy_mode == "direct"


def test_conversation_cli_options_are_persisted_and_validated(setup, capsys):
    _, home, _, _, _, _ = setup
    assert cli.main(["config", "auto_compact", "off", "--home", str(home)]) == 0
    assert not Config.load(home).auto_compact
    assert cli.main(["config", "auto_compact", "invalid", "--home", str(home)]) == 1
    assert "auto_compact must be" in capsys.readouterr().err
    assert cli.main(["config", "compact_threshold", "45000", "--home", str(home)]) == 0
    assert Config.load(home).compact_threshold == 45000
    assert cli.main(["config", "max_token_retries", "1", "--home", str(home)]) == 0
    assert Config.load(home).max_token_retries == 1
    assert cli.main(["config", "max_recovery_tokens", "16000", "--home", str(home)]) == 0
    assert Config.load(home).max_recovery_tokens == 16000
