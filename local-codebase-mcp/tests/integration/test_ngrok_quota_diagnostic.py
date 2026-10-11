"""Real FastMCP HTTP requests to a local 403 fixture, not a vendor E2E claim."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from config import Config, ServerConfig, TunnelConfig, config_to_dict, dump_toml
from runtime import Runtime, RuntimeError


@pytest.fixture
def denied_endpoint():
    class Handler(BaseHTTPRequestHandler):
        calls = 0
        body = b'<html>ERR_NGROK_725 private_fixture_secret_DO_NOT_ECHO</html>'

        def do_POST(self):
            type(self).calls += 1
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_public_hint_reports_quota_without_leaking_response(tmp_path, denied_endpoint):
    url, handler = denied_endpoint
    runtime = Runtime(tmp_path / "config.toml", tmp_path / "state")
    try:
        runtime._sample_network("public", url)
        public = runtime.status()["public"]
        assert public["state"] == "offline"
        assert "ERR_NGROK_725" in public["hint"]
        assert "额度" in public["hint"]
        assert "重新复制完整 authtoken" not in public["hint"]
        assert "private_fixture_secret" not in json.dumps(runtime.status())
        assert handler.calls == 1
    finally:
        runtime.stop()


def test_public_enable_quota_failure_stops_without_repeating_requests(tmp_path, denied_endpoint):
    url, handler = denied_endpoint
    config = Config(projects=(), server=ServerConfig(), tunnel=TunnelConfig(mode="ngrok"))
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config_to_dict(config)), encoding="utf-8")
    runtime = Runtime(path, tmp_path / "state")
    try:
        runtime._config = config
        # An exact owned harmless process supplies lifecycle only; HTTP diagnostics
        # always come from a real FastMCP request to the fixture above.
        runtime._tunnel = runtime._spawn("tunnel", [sys.executable, "-c", "import time; time.sleep(60)"])
        runtime._discovered_url = url
        runtime._tunnel_connections.add("ngrok")
        with pytest.raises(RuntimeError, match="ERR_NGROK_725") as failure:
            runtime.verify_public(config, timeout=1)
        assert "额度" in str(failure.value)
        assert "private_fixture_secret" not in str(failure.value)
        assert handler.calls == 1
    finally:
        runtime.stop()


def test_unknown_http_403_stays_generic_and_never_echoes_body(tmp_path, denied_endpoint):
    url, handler = denied_endpoint
    handler.body = b'unknown denial private_fixture_secret_DO_NOT_ECHO ERR_NGROK_999999'
    runtime = Runtime(tmp_path / "config.toml", tmp_path / "state")
    try:
        runtime._sample_network("public", url)
        public = runtime.status()["public"]
        assert public["state"] == "offline"
        assert "ERR_NGROK_725" not in public["hint"]
        assert "private_fixture_secret" not in json.dumps(runtime.status())
        assert "ERR_NGROK_999999" not in json.dumps(runtime.status())
    finally:
        runtime.stop()


def test_authentication_denial_is_distinct_from_quota(tmp_path, denied_endpoint):
    url, handler = denied_endpoint
    handler.body = b'ERR_NGROK_105 private_fixture_secret_DO_NOT_ECHO'
    runtime = Runtime(tmp_path / "config.toml", tmp_path / "state")
    try:
        runtime._sample_network("public", url)
        hint = runtime.status()["public"]["hint"]
        assert "ERR_NGROK_105" in hint
        assert "认证失败" in hint
        assert "额度" not in hint
        assert "private_fixture_secret" not in hint
    finally:
        runtime.stop()


def test_denial_body_inspection_is_bounded(tmp_path, denied_endpoint):
    url, handler = denied_endpoint
    handler.body = b'x' * 16384 + b'ERR_NGROK_725 private_fixture_secret_DO_NOT_ECHO'
    runtime = Runtime(tmp_path / "config.toml", tmp_path / "state")
    try:
        runtime._sample_network("public", url)
        hint = runtime.status()["public"]["hint"]
        assert "ERR_NGROK_725" not in hint
        assert "private_fixture_secret" not in hint
    finally:
        runtime.stop()
