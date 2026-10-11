"""Real MCP and owned TCP relay failures; no vendor credentials or system network edits.

The relay is a local transport fixture, not an ngrok/Cloudflare or ChatGPT emulator.
Every online/offline assertion uses Runtime's real MCP initialize/ping probe.
"""
from __future__ import annotations

import json
import socket
import sys
import time
from dataclasses import replace

import pytest

from config import Config, ConfigStore, ServerConfig, TunnelConfig, config_to_dict, dump_toml, load_config
from projects import Project
from runtime import Runtime, probe_mcp


RELAY = r'''
import pathlib,select,socket,sys,threading
port,upstream=int(sys.argv[1]),int(sys.argv[2])
cut=pathlib.Path(sys.argv[3])
listener=socket.socket()
listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
listener.bind(('127.0.0.1',port))
listener.listen()
def forward(client):
    target=None
    try:
        if cut.exists(): return
        target=socket.create_connection(('127.0.0.1',upstream),timeout=1)
        while not cut.exists():
            ready,_,_=select.select([client,target],[],[],.05)
            for source in ready:
                data=source.recv(65536)
                if not data:return
                (target if source is client else client).sendall(data)
    except OSError:pass
    finally:
        client.close()
        if target:target.close()
while True:
    client,_=listener.accept()
    threading.Thread(target=forward,args=(client,),daemon=True).start()
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    pytest.fail("owned candidate runtime did not recover")


@pytest.fixture
def candidate(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / "README.md").write_text("# Recovery fixture", encoding="utf-8")
    config = Config(projects=(Project("fixture", "Fixture", project),),
                    server=ServerConfig(port=free_port()), tunnel=TunnelConfig(mode="cloudflare"))
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config_to_dict(config)), encoding="utf-8")
    relay = tmp_path / "relay.py"
    relay.write_text(RELAY, encoding="utf-8")
    cut = tmp_path / "cut"
    relay_port = free_port()

    class RelayRuntime(Runtime):
        # Only replace the vendor executable seam. Server, supervisor and protocol
        # probes are production code; fixture cannot pretend a probe succeeded.
        def _start_tunnel(self, desired):
            self._last_tunnel_retry = time.monotonic()
            self._tunnel_config = desired.tunnel
            self._tunnel_connections.clear()
            self._discovered_url = f"http://127.0.0.1:{relay_port}/mcp"
            self._status["tunnel"]["mode"] = "cloudflare"
            self._tunnel = self._spawn("tunnel", [sys.executable, str(relay), str(relay_port),
                                                  str(desired.server.port), str(cut)])

    runtime = RelayRuntime(path, tmp_path / "state", probe_interval=.2)
    try:
        runtime.start()
        wait_until(lambda: runtime.status()["public"]["state"] == "online")
        yield runtime, path, cut
    finally:
        runtime.stop()


def test_mcp_process_recovery_preserves_live_tunnel(candidate):
    runtime, path, _ = candidate
    before = path.read_bytes()
    tunnel_pid = runtime._tunnel.pid
    server_pid = runtime.status()["local"]["pid"]
    url = runtime.status()["public"]["url"]
    with runtime._operation_lock:
        runtime._stop_owned("server")
        assert runtime.probe()["local"]["state"] == "offline"
    wait_until(lambda: runtime.status()["local"]["state"] == "online"
               and runtime.status()["public"]["state"] == "online")
    assert runtime.status()["local"]["pid"] != server_pid
    assert runtime._tunnel.pid == tunnel_pid
    assert runtime.status()["public"]["url"] == url
    assert probe_mcp(url, projects=True)["projects"] == [{"id": "fixture", "name": "Fixture"}]
    assert path.read_bytes() == before


def test_twenty_real_transport_disconnect_recovery_cycles(candidate):
    runtime, path, cut = candidate
    original = path.read_bytes()
    public_url = runtime.status()["public"]["url"]
    server_pid, tunnel_pid = runtime._server.pid, runtime._tunnel.pid
    rounds = []
    for number in range(1, 21):
        started = time.monotonic()
        cut.touch()
        with runtime._operation_lock:
            down = runtime.probe()
        assert down["public"]["state"] == "offline"
        assert down["local"]["state"] == "online"
        cut.unlink()
        # Recovery is observed by the production monitoring loop, not a forced
        # reconnect/restart. Each probe creates its own legitimate MCP session.
        wait_until(lambda: runtime.status()["public"]["state"] == "online")
        projects = probe_mcp(public_url, projects=True)["projects"]
        assert projects == [{"id": "fixture", "name": "Fixture"}]
        assert path.read_bytes() == original
        assert (runtime._server.pid, runtime._tunnel.pid) == (server_pid, tunnel_pid)
        rounds.append({"round": number, "public_down": True, "public_recovered": True,
                       "url": public_url, "projects": projects,
                       "duration_seconds": round(time.monotonic() - started, 3)})
    (path.parent / "recovery-rounds.json").write_text(json.dumps(rounds, indent=2), encoding="utf-8")
    events = runtime.status()["events"]
    assert len([e for e in events if e["kind"] == "public_disconnect"]) == 20
    assert len([e for e in events if e["kind"] == "public_reconnect"]) >= 20


def test_owned_tunnel_crash_automatically_retries_without_config_loss(candidate):
    runtime, path, _ = candidate
    original = path.read_bytes()
    server_pid, tunnel_pid = runtime._server.pid, runtime._tunnel.pid
    url = runtime.status()["public"]["url"]
    with runtime._operation_lock:
        runtime._tunnel.terminate()
        runtime._tunnel.wait(timeout=5)
        assert runtime.probe()["public"]["state"] == "offline"
        # Exercise the actual production 15s retry delay, not a patched clock.
    wait_until(lambda: runtime.status()["public"]["state"] == "online", timeout=30)
    assert runtime._tunnel.pid != tunnel_pid
    assert runtime._server.pid == server_pid
    assert runtime.status()["public"]["url"] == url
    assert probe_mcp(url, projects=True)["projects"] == [{"id": "fixture", "name": "Fixture"}]
    assert path.read_bytes() == original


def test_listener_change_retargets_owned_tunnel(candidate):
    runtime, path, _ = candidate
    before = load_config(path)
    tunnel_pid = runtime._tunnel.pid
    desired = replace(before, server=replace(before.server, port=free_port()))
    store = ConfigStore(path)
    saved = store.save(config_to_dict(desired), store.read()["revision"], runtime.apply)
    assert saved["applied"]["verified"] is True
    assert saved["applied"]["action"] == "restart"
    wait_until(lambda: runtime.status()["public"]["state"] == "online")
    assert runtime._tunnel.pid != tunnel_pid
    assert runtime.status()["local"]["url"].endswith(f":{desired.server.port}/mcp")
    assert probe_mcp(runtime.status()["public"]["url"], projects=True)["projects"] == [{"id": "fixture", "name": "Fixture"}]


def test_local_mcp_probe_survives_bad_proxy_environment(candidate, monkeypatch):
    runtime, _, _ = candidate
    unavailable_proxy = f"http://127.0.0.1:{free_port()}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, unavailable_proxy)
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "")
    # Only this test process receives the fixture environment. No OS proxy/DNS
    # settings or existing service environment is touched.
    url = runtime.status()["local"]["url"]
    assert probe_mcp(url, projects=True)["projects"] == [{"id": "fixture", "name": "Fixture"}]
