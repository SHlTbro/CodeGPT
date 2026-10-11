"""Opt-in real Cloudflare edge acceptance; no credentials and only synthetic source."""
import json
import os
from pathlib import Path
import threading
import time

import httpx2 as httpx
import pytest

from config import TunnelConfig
from runtime import probe_mcp
from setup import SetupApplication, SetupServer
from test_runtime import fixture_config


@pytest.mark.skipif(os.environ.get("CODEGPT_VENDOR_E2E") != "cloudflare",
                    reason="Explicit real provider acceptance")
def test_real_cloudflare_mcp_recovery_and_control_plane_isolation(tmp_path, monkeypatch):
    binary = Path(os.environ["CODEGPT_CLOUDFLARED"])
    assert binary.is_file() and binary.name == "cloudflared.exe"
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    path, cfg = fixture_config(tmp_path, tunnel=TunnelConfig(mode="cloudflare"))
    app = SetupApplication(path, tmp_path / "state", setup_port=0)
    app.runtime.probe_interval = .5
    server = SetupServer(("127.0.0.1", 0), app)
    app.set_port(server.server_port)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    before = path.read_bytes()
    try:
        app.config_view()
        app.runtime.start()
        public = app.runtime.verify_public(cfg, timeout=90)
        url = public["public_url"]
        first_pid = app.runtime._tunnel.pid
        assert probe_mcp(url, projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
        with httpx.Client(trust_env=False, timeout=20) as client:
            for route in ("/api/config", "/api/session", "/app.js"):
                assert client.get(url.removesuffix("/mcp") + route).status_code == 404
        with app.runtime._operation_lock:
            old_mcp_pid = app.runtime.status()["local"]["pid"]
            app.runtime._stop_owned("server")
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            status = app.runtime.status()
            if status["local"]["pid"] not in (None, old_mcp_pid):
                try:
                    assert probe_mcp(url, projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
                    break
                except Exception:
                    pass
            time.sleep(.5)
        else:
            pytest.fail("real Cloudflare endpoint did not recover its MCP origin")
        assert app.runtime._tunnel.pid == first_pid
        assert app.runtime.status()["public"]["url"] == url
        with app.runtime._operation_lock:
            app.runtime._stop_owned("tunnel")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = app.runtime.status()
            if app.runtime._tunnel is not None and app.runtime._tunnel.pid != first_pid and status["public"]["state"] == "online":
                changed_url = status["public"]["url"]
                assert probe_mcp(changed_url, projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
                break
            time.sleep(.5)
        else:
            current = app.runtime.status()
            diagnostic = {"initial_verified": True, "mcp_restart_kept_url": True,
                          "state": current, "long_probe": None}
            try:
                diagnostic["long_probe"] = probe_mcp(current["public"]["url"], projects=True, timeout=10)
            except Exception as error:
                diagnostic["long_probe"] = {"error_type": type(error).__name__, "message": str(error)}
            (tmp_path / "provider-failure.json").write_text(json.dumps(diagnostic), encoding="utf-8")
            pytest.fail("owned real Cloudflare process restart did not reach a verified MCP endpoint")
        assert path.read_bytes() == before
        assert app.agent_resources is None
        (tmp_path / "provider-result.json").write_text(json.dumps({"provider": "cloudflare",
            "initial_url": url, "mcp_restart_kept_url_and_tunnel": True,
            "restarted_tunnel_url": changed_url, "connector_url_update_needed": changed_url != url,
            "projects_preserved": True, "setup_public_routes": "404", "model_requests": 0,
            "chatgpt_client_recovery": "not tested"}), encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        app.runtime.stop()
        serving.join(timeout=5)
