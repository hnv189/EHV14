import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_provider import sse_response

from ehv_agent.config import Config
from ehv_agent.models import fetch_models
from ehv_agent.provider import ClaudeRunner
from ehv_agent.tools import Toolset


@pytest.fixture
def local_api():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(("GET", self.path))
            body = json.dumps(
                {
                    "data": [
                        {
                            "id": "claude-test",
                            "type": "model",
                            "display_name": "Test Claude",
                            "created_at": "2026-10-01T00:00:00Z",
                        }
                    ],
                    "has_more": False,
                }
            ).encode()
            self.respond("application/json", body)

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(("POST", self.path))
            response = sse_response("complete_task", {"status": "passed", "summary": "Connected"})
            self.respond("text/event-stream", response.content)

        def respond(self, content_type, body):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode", ["system", "custom", "direct"])
def test_model_scans_and_agent_streams_obey_proxy_policy(setup, monkeypatch, local_api, mode):
    _, home, config, events, cancel, workspace = setup
    address, requests = local_api
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-test-only")
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", address if mode == "system" else "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    # An explicit proxy also overrides NO_PROXY, so the selected route is predictable.
    monkeypatch.setenv("NO_PROXY", "*" if mode == "custom" else "")
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", address if mode == "direct" else "http://claude.invalid"
    )
    config.proxy_mode = mode
    config.proxy_url = address if mode == "custom" else ""
    config.save(home)
    config = Config.load(home)

    assert fetch_models(config=config)[0].id == "claude-test"
    workspace.prepare()
    runner = ClaudeRunner(config, events, cancel)
    try:
        result = runner.run(
            "requirement",
            "work",
            "Check connection",
            Toolset("requirement", workspace, home, config, events, cancel),
        )
    finally:
        runner.close()
    assert result["status"] == "passed"
    prefix = "" if mode == "direct" else "http://claude.invalid"
    assert requests == [("GET", prefix + "/v1/models?limit=100"), ("POST", prefix + "/v1/messages")]


@pytest.mark.parametrize(
    "url",
    [
        "proxy.example.com:8080",
        "ftp://proxy.example.com",
        "http://",
        "http://host:bad",
        "http://host:0",
        "http://host:70000",
        "http://host/path",
        "http://host?x=1",
        "http://host#fragment",
        "http://bad host",
        "http://user:private-password@host:bad",
    ],
)
def test_invalid_proxy_urls_are_rejected_without_exposing_credentials(setup, url):
    _, home, config, _, _, _ = setup
    before = (home / "config.json").read_bytes()
    config.proxy_mode = "custom"
    config.proxy_url = url
    with pytest.raises(ValueError, match="Proxy URL") as error:
        config.save(home)
    assert "private-password" not in str(error.value)
    assert (home / "config.json").read_bytes() == before


def test_old_configs_default_to_environment_and_custom_requires_url(setup):
    _, home, config, _, _, _ = setup
    value = json.loads((home / "config.json").read_text())
    del value["proxy_mode"], value["proxy_url"]
    (home / "config.json").write_text(json.dumps(value))
    assert Config.load(home).proxy_mode == "system"
    config.proxy_mode = "custom"
    with pytest.raises(ValueError, match="Enter a proxy URL"):
        config.save(home)
    config.proxy_url = " http://user:password@proxy.example.com:8080 "
    config.save(home)
    assert Config.load(home).proxy_url == config.proxy_url.strip()
