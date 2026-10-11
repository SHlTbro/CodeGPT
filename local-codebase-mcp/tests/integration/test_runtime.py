"""Real TOML -> owned server -> MCP protocol coverage, with no public credentials required."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from conftest import PKG_DIR
from config import Config, ConfigApplyError, ConfigStore, ServerConfig, TunnelConfig, config_revision, config_to_dict, dump_toml, load_config
from projects import Project
from runtime import Runtime, RuntimeError, probe_mcp


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture_config(tmp_path, **kwargs):
    project = tmp_path / "alpha"
    project.mkdir()
    (project / "README.md").write_text("# alpha", encoding="utf-8")
    config = Config(projects=(Project("alpha", "Alpha", project),), server=ServerConfig(port=free_port()), **kwargs)
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config_to_dict(config)), encoding="utf-8")
    return path, config


def external_call(url):
    from fastmcp import Client
    async def run():
        async with Client(url) as client:
            return (await client.call_tool("list_projects", {})).data
    return asyncio.run(run())


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail("real backend did not reach expected state")


def test_real_toml_hot_reload_and_activity(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    try:
        initial = runtime.start()
        assert initial["verified"] is True
        assert runtime.status()["local"]["state"] == "online"
        original_pid = runtime.status()["local"]["pid"]
        assert runtime.status()["activity"]["count"] == 0
        store = ConfigStore(path)
        beta = tmp_path / "beta"
        beta.mkdir()
        updated = replace(config, projects=config.projects + (Project("beta", "Beta", beta, write_enabled=True, write_roots=("docs",)),))
        result = store.save(config_to_dict(updated), store.read()["revision"], runtime.apply)
        assert result["applied"]["action"] == "hot_reload"
        assert result["applied"]["verified"] is True
        assert load_config(path) == updated
        assert runtime.status()["local"]["pid"] == original_pid
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}, {"id": "beta", "name": "Beta"}]
        assert runtime.probe()["activity"]["count"] == 0
        assert len(external_call(runtime.status()["local"]["url"])["projects"]) == 2
        assert runtime.probe()["activity"]["count"] == 1
        assert runtime.status()["activity"]["tool"] == "list_projects"
        assert runtime.status()["config"]["applied_revision"] == config_revision(updated)
    finally:
        runtime.stop()


def test_runtime_status_tracks_mcp_process_and_applied_config_version(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    try:
        runtime.start()
        first = runtime.status()
        identity = first["mcp"]
        assert identity["pid"] == first["local"]["pid"]
        assert identity["build_id"] is None
        assert identity["manifest_state"] == "missing"
        assert identity["effective_config_version"] == config_revision(config)
        assert identity["started_at"]
        state = json.loads((tmp_path / "state" / "runtime-state.json").read_text(encoding="utf-8"))
        assert state["pid"] == identity["pid"]
        assert state["started_at"] == identity["started_at"]
        assert state["revision"] == identity["effective_config_version"]

        store = ConfigStore(path)
        beta = tmp_path / "beta-identity"
        beta.mkdir()
        updated = replace(config, projects=config.projects + (Project("beta", "Beta", beta),))
        store.save(config_to_dict(updated), store.read()["revision"], runtime.apply)
        after = runtime.status()["mcp"]
        assert after["pid"] == identity["pid"]
        assert after["started_at"] == identity["started_at"]
        assert after["build_id"] == identity["build_id"]
        assert after["effective_config_version"] == config_revision(updated)
    finally:
        runtime.stop()


def test_listener_change_and_bind_failure_restore_last_good(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=60, startup_timeout=10)
    store = ConfigStore(path)
    try:
        runtime.start()
        original_pid = runtime.status()["local"]["pid"]
        updated = replace(config, server=replace(config.server, port=free_port()))
        result = store.save(config_to_dict(updated), store.read()["revision"], runtime.apply)
        assert result["applied"]["action"] == "restart"
        assert runtime.status()["local"]["pid"] != original_pid
        runtime.probe()
        assert runtime.status()["local"]["url"].endswith(f":{updated.server.port}/mcp")
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            bad = replace(updated, server=replace(updated.server, port=blocker.getsockname()[1]))
            with pytest.raises(ConfigApplyError, match="已回退 last-good"):
                store.save(config_to_dict(bad), store.read()["revision"], runtime.apply)
        assert load_config(path) == updated
        assert runtime.probe()["local"]["state"] == "online"
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"][0]["id"] == "alpha"
    finally:
        runtime.stop()


def test_external_edit_invalid_last_good_and_supervised_restart(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=0.3, startup_timeout=15)
    try:
        runtime.start()
        original_pid = runtime.status()["local"]["pid"]
        valid = path.read_text(encoding="utf-8")
        path.write_text("this is not TOML = [", encoding="utf-8")
        wait_until(lambda: runtime.status()["config"]["error"])
        assert runtime.status()["local"]["pid"] == original_pid
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
        updated = replace(config, server=replace(config.server, port=free_port()))
        path.write_text(dump_toml(config_to_dict(updated)), encoding="utf-8")
        wait_until(lambda: runtime.status()["config"]["applied_revision"] == config_revision(updated))
        assert runtime.status()["local"]["pid"] != original_pid
        assert runtime.probe()["local"]["state"] == "online"
        assert not runtime.status()["config"]["error"]
    finally:
        runtime.stop()


def test_network_disconnect_reconnect_and_mcp_plane_isolation(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    try:
        runtime.start()
        base = f"http://127.0.0.1:{config.server.port}"
        for route in ("/", "/api/config", "/api/status", "/setup"):
            with pytest.raises(HTTPError) as failure:
                urlopen(base + route, timeout=3)
            assert failure.value.code == 404
        health = json.load(urlopen(base + "/health", timeout=3))
        assert set(health) == {"server", "status", "revision"}
        assert health["server"] == "CodeGPT"
        from fastmcp import Client
        async def initialize_name():
            async with Client(base + "/mcp", mode="legacy") as client:
                return client.initialize_result.server_info.name
        assert asyncio.run(initialize_name()) == "CodeGPT"
        runtime._server.terminate()
        runtime._server.wait(timeout=5)
        disconnected = runtime.probe()
        assert disconnected["local"]["state"] == "offline"
        assert disconnected["local"]["last_disconnect"]
        runtime.restart()
        recovered = runtime.status()
        assert recovered["local"]["state"] == "online"
        assert recovered["local"]["last_reconnect"] >= disconnected["local"]["last_disconnect"]
        assert {point["local"] for point in recovered["history"]} == {"online", "offline"}
        assert recovered["public"]["state"] == "unconfigured"
        assert recovered["tunnel"]["state"] == "disabled"
    finally:
        runtime.stop()


def test_explicit_mcp_restart_keeps_live_tunnel_process_and_public_origin(tmp_path, monkeypatch):
    path, config = fixture_config(tmp_path, tunnel=TunnelConfig(mode="cloudflare"))
    runtime = Runtime(path, tmp_path / "state", probe_interval=60, startup_timeout=10)
    tunnel_processes = []

    def start_owned_tunnel(candidate):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tunnel_processes.append(process)
        runtime._tunnel = process
        runtime._tunnel_config = candidate.tunnel
        runtime._discovered_url = "https://stable.example.test/mcp"

    monkeypatch.setattr(runtime, "_start_tunnel", start_owned_tunnel)
    sample_network = runtime._sample_network

    def sample_without_external_request(key, url, verified=None):
        if key == "public" and url == runtime._discovered_url:
            verified = {"latency_ms": 1}
        sample_network(key, url, verified)

    monkeypatch.setattr(runtime, "_sample_network", sample_without_external_request)
    try:
        runtime.start()
        before = runtime.status()
        old_server_pid = before["local"]["pid"]
        old_tunnel_pid = runtime._tunnel.pid
        old_public_url = before["public"]["url"]

        runtime.restart()

        after = runtime.status()
        assert after["local"]["pid"] != old_server_pid
        assert old_public_url == "https://stable.example.test/mcp"
        assert after["public"]["url"] == old_public_url
        assert runtime._tunnel is not None and runtime._tunnel.poll() is None
        assert runtime._tunnel.pid == old_tunnel_pid
        assert tunnel_processes == [runtime._tunnel]
        assert probe_mcp(after["local"]["url"], projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
    finally:
        runtime.stop()


def test_generic_http_is_not_mcp():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(Exception):
            probe_mcp(f"http://127.0.0.1:{server.server_port}/mcp", timeout=0.5)
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_does_not_adopt_or_kill_occupied_port(tmp_path):
    path, config = fixture_config(tmp_path)
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", config.server.port))
        owner.listen()
        runtime = Runtime(path, tmp_path / "state", probe_interval=60)
        try:
            with pytest.raises(RuntimeError, match="Setup 不会关闭外部进程"):
                runtime.start()
            assert runtime.status()["local"]["pid"] is None
            assert owner.fileno() >= 0
        finally:
            runtime.stop()


def test_named_tunnel_fails_closed(tmp_path):
    path, config = fixture_config(tmp_path, tunnel=TunnelConfig(mode="named", public_url="https://mcp.example.com/mcp"))
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    try:
        with pytest.raises(RuntimeError, match="远端 ingress"):
            runtime.start()
        assert runtime._tunnel is None
        assert runtime.status()["config"]["error"]
    finally:
        runtime.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership contract")
def test_owner_kill_closes_windows_job_and_owned_child(tmp_path):
    path, config = fixture_config(tmp_path)
    ready = tmp_path / "ready.json"
    source = "from runtime import Runtime;import json,time,os;from pathlib import Path;r=Runtime(Path(__import__('sys').argv[1]),Path(__import__('sys').argv[2]),probe_interval=60);r.start();ready=Path(__import__('sys').argv[3]);stage=ready.with_suffix('.tmp');stage.write_text(json.dumps({**r.status(),'owner_pid':os.getpid()}));os.replace(stage,ready);time.sleep(90)"
    owner = subprocess.Popen([sys.executable, "-c", source, str(path), str(tmp_path / "state"), str(ready)], cwd=PKG_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_handle = None
    try:
        wait_until(ready.exists, timeout=30)
        ready_data = json.loads(ready.read_text())
        child_pid = ready_data["local"]["pid"]
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        child_handle = kernel.OpenProcess(0x100000, False, child_pid)
        assert child_handle
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        owner_handle = kernel.OpenProcess(0x100001, False, ready_data["owner_pid"])
        assert owner_handle
        try:
            assert kernel.TerminateProcess(owner_handle, 1)
            assert kernel.WaitForSingleObject(owner_handle, 5000) == 0
        finally:
            kernel.CloseHandle(owner_handle)
        owner.wait(timeout=5)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
        def port_closed():
            try:
                with socket.create_connection(("127.0.0.1", config.server.port), timeout=0.3):
                    return False
            except OSError:
                return True
        wait_until(port_closed, timeout=5)
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)
        if child_handle:
            kernel.CloseHandle(child_handle)


def test_external_occupied_listener_keeps_last_good_then_recovers(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=0.3, startup_timeout=8)
    try:
        runtime.start()
        original_url = runtime.status()["local"]["url"]
        original_pid = runtime.status()["local"]["pid"]
        with socket.socket() as external:
            external.bind(("127.0.0.1", 0))
            external.listen()
            broken = replace(config, server=replace(config.server, port=external.getsockname()[1]))
            path.write_text(dump_toml(config_to_dict(broken)), encoding="utf-8")
            wait_until(lambda: bool(runtime.status()["config"]["error"]))
            # Public behavior: a failed external edit must leave the last-good MCP usable.
            assert probe_mcp(original_url, projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
            assert runtime.status()["local"]["pid"] == original_pid
            assert runtime.status()["config"]["applied_revision"] == config_revision(config)
            assert external.fileno() >= 0  # The unrelated listener was never stopped.
        corrected = replace(config, server=replace(config.server, port=free_port()))
        path.write_text(dump_toml(config_to_dict(corrected)), encoding="utf-8")
        wait_until(lambda: runtime.status()["config"]["applied_revision"] == config_revision(corrected))
        assert runtime.status()["local"]["url"].endswith(f":{corrected.server.port}/mcp")
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"][0]["id"] == "alpha"
        assert not runtime.status()["config"]["error"]
    finally:
        runtime.stop()


def test_external_restart_startup_failure_restores_verified_backend(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=0.3, startup_timeout=10)
    try:
        runtime.start()
        original_url = runtime.status()["local"]["url"]
        temporary_project = tmp_path / "temporary-project"
        temporary_project.mkdir()
        candidate = replace(config, server=replace(config.server, port=free_port()),
                            projects=config.projects + (Project("temporary", "Temporary", temporary_project),))
        path.write_text(dump_toml(config_to_dict(candidate)), encoding="utf-8")
        def old_listener_closed():
            try:
                with socket.create_connection((config.server.host, config.server.port), timeout=0.1):
                    return False
            except OSError:
                return True
        wait_until(old_listener_closed)
        # Change a real prerequisite after the supervisor's validation but before the
        # new server's startup validation; no fake server or injected exception.
        temporary_project.rmdir()
        wait_until(lambda: "已恢复 last-good" in (runtime.status()["config"]["error"] or ""), timeout=25)
        assert load_config(path) == config
        assert path.with_name(path.name + ".rejected").exists()
        assert probe_mcp(original_url, projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
        assert runtime.status()["config"]["applied_revision"] == config_revision(config)
        # The recovery warning remains visible while the recovered file is unchanged.
        time.sleep(0.7)
        assert "已恢复 last-good" in runtime.status()["config"]["error"]
    finally:
        runtime.stop()


def test_ngrok_missing_fixture_token_keeps_local_mcp_and_never_uses_production(tmp_path):
    path, config = fixture_config(tmp_path, tunnel=TunnelConfig(mode="ngrok"))
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    try:
        with pytest.raises(RuntimeError, match="本机配置页面粘贴"):
            runtime.start()
        assert runtime._tunnel is None
        assert not (tmp_path / "ngrok-token.txt").exists()
        # The real MCP survived the tunnel error; no production credential fallback.
        assert probe_mcp(f"http://127.0.0.1:{config.server.port}/mcp", projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
    finally:
        runtime.stop()


def test_real_ngrok_invalid_token_is_redacted_and_transaction_restores_backend(tmp_path):
    try:
        Runtime._executable("ngrok")
    except RuntimeError:
        pytest.skip("official ngrok agent not installed")
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    token = "deliberately_invalid_fixture_authtoken_873625"
    (tmp_path / "ngrok-token.txt").write_text(token, encoding="utf-8")
    store = ConfigStore(path)
    try:
        runtime.start()
        original_pid = runtime.status()["local"]["pid"]
        candidate = replace(config, tunnel=TunnelConfig(mode="ngrok"))
        def verified_apply(value):
            applied = runtime.apply(value, force_tunnel_restart=True)
            if value.tunnel.mode == "ngrok":
                applied.update(runtime.verify_public(value, timeout=8))
            return applied
        with pytest.raises(ConfigApplyError) as failed:
            store.save(config_to_dict(candidate), store.read()["revision"], verified_apply)
        assert token not in str(failed.value)
        assert "ngrok" in str(failed.value)
        assert "认证失败" in str(failed.value)
        assert load_config(path) == config
        assert runtime.status()["local"]["pid"] == original_pid
        assert runtime.status()["local"]["state"] == "online"
        assert runtime.status()["tunnel"]["state"] == "disabled"
        assert runtime.status()["public"]["state"] == "unconfigured"
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
        assert runtime.status()["activity"]["count"] == 0
        assert token not in json.dumps(runtime.status(), ensure_ascii=False)
        assert token not in (tmp_path / "state" / "tunnel.log").read_text(encoding="utf-8")
        agent_config = (tmp_path / "state" / "ngrok-agent.conf").read_text(encoding="utf-8")
        assert token not in agent_config
        assert "web_addr: false" in agent_config
    finally:
        runtime.stop()


def test_ngrok_malformed_fixed_credential_file_never_launches_or_echoes_value(tmp_path):
    path, config = fixture_config(tmp_path, tunnel=TunnelConfig(mode="ngrok"))
    malformed = 'bad_credential_with_embedded\nnewline_and_"quote"'
    (tmp_path / "ngrok-token.txt").write_text(malformed, encoding="utf-8")
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    try:
        with pytest.raises(RuntimeError, match="格式不正确") as failed:
            runtime.start()
        assert malformed not in str(failed.value)
        assert runtime._tunnel is None
        assert not (tmp_path / "state" / "tunnel.log").exists()
        assert probe_mcp(f"http://127.0.0.1:{config.server.port}/mcp", projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
    finally:
        runtime.stop()


def recorded_ngrok_tunnel_event():
    # Actual ngrok 3.39.11 JSON event captured during the authorized public E2E.
    return {"msg": "started tunnel", "addr": {
        "Scheme": "http", "Opaque": "", "User": None, "Host": "127.0.0.1:55400",
        "Path": "", "Fragment": "", "RawQuery": "", "RawPath": "", "RawFragment": "",
        "ForceQuery": False, "OmitHost": False,
    }, "url": "https://enviably-skirt-promptly.ngrok-free.dev"}


def test_ngrok_started_event_accepts_actual_go_url_and_legacy_string():
    from runtime import _ngrok_public_url
    cfg = Config(projects=(), server=ServerConfig(port=55400, path="/custom-mcp"))
    event = recorded_ngrok_tunnel_event()
    expected = "https://enviably-skirt-promptly.ngrok-free.dev/custom-mcp"
    assert _ngrok_public_url(event, cfg) == expected
    event["addr"] = "http://127.0.0.1:55400"
    assert _ngrok_public_url(event, cfg) == expected


@pytest.mark.parametrize(("key", "value"), [
    ("Scheme", "https"), ("Host", "127.0.0.1:8169"), ("Host", "other-host:55400"),
    ("Path", "/api/config"), ("User", {"Username": "secret"}), ("Opaque", "opaque"),
    ("RawQuery", "token=secret"), ("Fragment", "fragment"), ("RawPath", "/api/config"),
    ("RawFragment", "fragment"), ("ForceQuery", True), ("OmitHost", True),
])
def test_ngrok_started_event_rejects_go_url_routing_extras(key, value):
    from runtime import _ngrok_public_url
    cfg = Config(projects=(), server=ServerConfig(port=55400))
    event = recorded_ngrok_tunnel_event()
    event["addr"][key] = value
    assert _ngrok_public_url(event, cfg) == ""


@pytest.mark.parametrize("public_url", [
    "http://example.ngrok.dev", "https://user:secret@example.ngrok.dev",
    "https://example.ngrok.dev?token=secret", "https://example.ngrok.dev/#fragment",
    "https://example.ngrok.dev/api/config",
])
def test_ngrok_started_event_rejects_unsafe_public_address(public_url):
    from runtime import _ngrok_public_url
    cfg = Config(projects=(), server=ServerConfig(port=55400))
    event = recorded_ngrok_tunnel_event()
    event["url"] = public_url
    assert _ngrok_public_url(event, cfg) == ""


@pytest.mark.parametrize("name,accepted", [("local-codebase-mcp", True), ("unrelated-mcp", False)])
def test_protocol_probe_accepts_legacy_name_but_rejects_unrelated_mcp(tmp_path, name, accepted):
    # Real initialized serverInfo is the compatibility boundary; each fixture owns its tree.
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=60)
    code = ("import sys;from fastmcp import FastMCP;"
            "FastMCP(sys.argv[1]).run(transport='streamable-http',host='127.0.0.1',"
            "port=int(sys.argv[2]),path='/mcp',show_banner=False)")
    try:
        runtime._server = runtime._spawn("server", [sys.executable, "-c", code, name, str(config.server.port)])
        base = f"http://127.0.0.1:{config.server.port}/mcp"
        def ready():
            try:
                with socket.create_connection(("127.0.0.1", config.server.port), timeout=0.2):
                    return True
            except OSError:
                return False
        wait_until(ready, timeout=20)
        if accepted:
            assert probe_mcp(base)["latency_ms"] >= 0
        else:
            with pytest.raises(RuntimeError, match="没有连接到 CodeGPT MCP"):
                probe_mcp(base)
    finally:
        runtime.stop()


@pytest.mark.parametrize("failure", ["exception", "unverified"])
def test_windows_startup_hook_failure_rolls_back_real_backend(tmp_path, failure):
    from config import StartupConfig
    class Startup:
        def __init__(self):
            self.values = []
            self.enabled = False
        def apply(self, enabled):
            self.values.append(enabled)
            self.enabled = enabled  # A write can fail after partially changing the OS setting.
            if enabled and failure == "exception":
                raise RuntimeError("无法写入开机启动设置；检查当前用户权限后重试。")
            return {"verified": not enabled}
    manager = Startup()
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", startup=manager, probe_interval=60)
    try:
        runtime.start()
        assert manager.values == [False]
        beta = tmp_path / "beta"
        beta.mkdir()
        desired = replace(config, startup=StartupConfig(enabled=True),
                          projects=config.projects + (Project("beta", "Beta", beta),))
        store = ConfigStore(path)
        with pytest.raises(ConfigApplyError, match="已回退 last-good"):
            store.save(config_to_dict(desired), store.read()["revision"], runtime.apply)
        assert manager.values == [False, True, False]
        assert manager.enabled is False
        assert load_config(path) == config
        assert runtime.status()["config"]["applied_revision"] == config_revision(config)
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
    finally:
        runtime.stop()


def test_external_startup_failure_recovers_verified_backend_and_os_setting(tmp_path):
    from config import StartupConfig
    class Startup:
        def __init__(self):
            self.values = []
            self.enabled = False
        def apply(self, enabled):
            self.values.append(enabled)
            self.enabled = enabled
            if enabled:
                raise RuntimeError("开机启动设置失败；请检查用户权限。")
            return {"verified": True}
    manager = Startup()
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", startup=manager, probe_interval=0.3)
    try:
        runtime.start()
        desired = replace(config, startup=StartupConfig(enabled=True))
        path.write_text(dump_toml(config_to_dict(desired)), encoding="utf-8")
        wait_until(lambda: "已恢复 last-good" in (runtime.status()["config"]["error"] or ""), timeout=25)
        assert load_config(path) == config
        assert manager.values == [False, True, False]
        assert manager.enabled is False
        assert runtime.status()["config"]["applied_revision"] == config_revision(config)
        assert probe_mcp(runtime.status()["local"]["url"], projects=True)["projects"] == [{"id": "alpha", "name": "Alpha"}]
    finally:
        runtime.stop()
