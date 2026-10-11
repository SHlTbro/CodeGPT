"""Core recovery through real HTTP/processes, isolated from the production owner."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import httpx2 as httpx
import pytest

from conftest import PKG_DIR
from local_agents.contracts import validation_errors
from runtime import Runtime
from config import config_to_dict, dump_toml
from setup import SetupApplication
from test_runtime import fixture_config, wait_until
from test_setup_e2e import free_port


def rpc(client, url, method, request_id, params=None):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        result = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    else:
        result = response.json()
    assert "error" not in result, result
    return response, result["result"]


def test_default_core_status_never_initializes_optional_agent_storage(tmp_path, monkeypatch):
    path, _ = fixture_config(tmp_path)
    app = SetupApplication(path, tmp_path / "state", setup_port=free_port())
    app.config_view()
    def forbidden():
        raise AssertionError("Core status initialized optional Agent storage")
    monkeypatch.setattr(app, "agent_storage", forbidden)
    try:
        app.runtime.start()
        status = app.status()
        assert status["version"]["release"] == "1.2.0"
        # A live status response must identify the package loaded by this process,
        # never the mutable Git checkout observed during a later poll.
        assert status["version"]["source_commit"] is None
        assert status["version"]["worktree_dirty"] is None
        assert status["version"]["build_id"] is None
        assert status["setup"]["pid"] == os.getpid()
        assert status["setup"]["build_id"] is None
        assert status["setup"]["manifest_state"] == "missing"
        assert status["setup"]["effective_config_version"] == status["config"]["applied_revision"]
        assert status["mcp"]["pid"] == status["local"]["pid"]
        assert status["mcp"]["manifest_state"] == "missing"
        assert status["mcp"]["effective_config_version"] == status["config"]["applied_revision"]
        assert status["local"]["state"] == "online"
        assert status["agent"]["availability"] == "disabled"
        assert validation_errors("setup_status_response_overlay", status) == []
        assert not app.agent_root.exists()
    finally:
        app.runtime.stop()


def test_same_http_client_survives_mcp_restart_without_new_handshake(tmp_path):
    path, config = fixture_config(tmp_path)
    runtime = Runtime(path, tmp_path / "state", probe_interval=.2)
    url = f"http://127.0.0.1:{config.server.port}/mcp"
    with httpx.Client(headers={"Accept": "application/json, text/event-stream"}, trust_env=False, timeout=10) as client:
        try:
            runtime.start()
            initialized, _ = rpc(client, url, "initialize", 1, {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "core-recovery-fixture", "version": "1"}})
            assert initialized.headers["content-type"].startswith("application/json")
            client.headers["MCP-Protocol-Version"] = "2025-06-18"
            if initialized.headers.get("mcp-session-id"):
                client.headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
            client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            _, first = rpc(client, url, "tools/list", 2)
            assert len(first["tools"]) == 15
            old_pid = runtime.status()["local"]["pid"]
            before = path.read_bytes()
            with runtime._operation_lock:
                runtime._stop_owned("server")
            def recovered_endpoint():
                pid = runtime.status()["local"]["pid"]
                if pid is None or pid == old_pid:
                    return False
                try:
                    return client.get(url.removesuffix("/mcp") + "/health").status_code == 200
                except httpx.HTTPError:
                    return False
            wait_until(recovered_endpoint)
            # Retain the exact client/headers; no initialize, login or pairing again.
            _, recovered = rpc(client, url, "tools/list", 3)
            assert recovered == first
            _, projects = rpc(client, url, "tools/call", 4, {"name": "list_projects", "arguments": {}})
            assert projects["structuredContent"]["projects"] == [{"id": "alpha", "name": "Alpha"}]
            assert path.read_bytes() == before
        finally:
            runtime.stop()


@pytest.mark.parametrize('auth_mode', ['unselected', 'api_key'])
def test_core_status_keeps_corrupt_optional_task_store_and_stays_online(tmp_path, auth_mode):
    path, cfg = fixture_config(tmp_path)
    data = config_to_dict(cfg)
    data['agent']['codex']['auth_mode'] = auth_mode
    path.write_text(dump_toml(data), encoding='utf-8')
    app = SetupApplication(path, tmp_path / 'state', setup_port=free_port())
    app.config_view()
    app.agent_root.mkdir(parents=True)
    database = app.agent_root / 'store.sqlite'
    original = b'owned invalid SQLite fixture'
    database.write_bytes(original)
    try:
        app.runtime.start()
        status = app.status()
        assert status['local']['state'] == 'online'
        assert status['agent']['enabled'] is False
        assert status['agent']['last_error']['code'] == 'STORAGE_UNAVAILABLE'
        assert validation_errors('setup_status_response_overlay', status) == []
        assert database.read_bytes() == original
        assert app.agent_resources is None
    finally:
        app.runtime.stop()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction boundary')
@pytest.mark.parametrize('junction_kind', ['agent_root', 'results'])
@pytest.mark.parametrize('auth_mode', ['unselected', 'api_key'])
def test_core_status_rejects_existing_task_store_junction_without_opening_it(tmp_path, junction_kind, auth_mode):
    from local_agents.store import Store
    path, cfg = fixture_config(tmp_path)
    data = config_to_dict(cfg)
    data['agent']['codex']['auth_mode'] = auth_mode
    path.write_text(dump_toml(data), encoding='utf-8')
    app = SetupApplication(path, tmp_path / 'state', setup_port=free_port())
    app.config_view()
    outside = tmp_path / 'outside'
    outside.mkdir()
    if junction_kind == 'agent_root':
        Store(outside / 'store.sqlite', outside / 'results')
        app.agent_root.parent.mkdir(parents=True, exist_ok=True)
        link = app.agent_root
        target = outside
    else:
        Store(app.agent_root / 'store.sqlite', app.agent_root / 'results')
        link = app.agent_root / 'results'
        link.rmdir()
        target = outside
    completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
    assert completed.returncode == 0
    assert link.is_junction() and not link.is_symlink()
    database = (outside if junction_kind == 'agent_root' else app.agent_root) / 'store.sqlite'
    before = database.read_bytes()
    try:
        status = app.status()
        assert status['agent']['last_error']['code'] == 'STORAGE_UNAVAILABLE'
        assert app.agent_tasks is None and app.agent_resources is None
        assert database.read_bytes() == before
    finally:
        link.rmdir()  # Remove only the owned junction, never its target.


@pytest.mark.skipif(os.name != "nt", reason="Windows Setup launcher")
def test_setup_launcher_recovers_its_crashed_owner_and_duplicate_start(tmp_path):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path, initial = fixture_config(tmp_path)
    setup_port = free_port()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    env["PATH"] = os.pathsep.join(part for part in env["PATH"].split(os.pathsep)
        if not any((Path(part) / name).is_file() for name in ("codex.exe", "codex.cmd", "codex.ps1")))
    assert shutil.which("codex", path=env["PATH"]) is None
    assert (PKG_DIR / ".venv/Scripts/python.exe").is_file()
    powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    argv = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PKG_DIR / "setup.ps1"),
            "-NoBrowser", "-Config", str(path), "-SetupPort", str(setup_port), "-Port", str(initial.server.port)]
    output = (tmp_path / "launcher.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=output, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{setup_port}", trust_env=False, timeout=3)
    owned_handles = {}
    def online():
        try:
            status = client.get("/api/status").json()
            pid = status.get("owner_pid")
            if pid and pid not in owned_handles:
                # Keep the exact process object, never terminate a reused PID.
                handle = kernel.OpenProcess(0x0001 | 0x1000, False, pid)
                if handle:
                    owned_handles[pid] = handle
            return status if status.get("local", {}).get("state") == "online" else False
        except (httpx.HTTPError, ValueError):
            return False
    try:
        wait_until(online, timeout=40)
        first = online()
        session = client.get("/api/session").json()
        view = client.get("/api/config").json()
        new_port = free_port()
        view["data"]["server"]["port"] = new_port
        saved = client.put("/api/config", json={"revision": view["revision"], "data": view["data"]},
                           headers={"X-Setup-Token": session["token"]}, timeout=30)
        assert saved.status_code == 200, saved.text
        before = path.read_bytes()
        # The PID came from this candidate listener and is the launcher's owned child.
        assert kernel.TerminateProcess(owned_handles[first["owner_pid"]], 1)
        wait_until(lambda: (current := online()) and current["owner_pid"] != first["owner_pid"], timeout=25)
        restored = online()
        assert restored["agent"]["availability"] == "disabled"
        assert restored["local"]["url"] == f"http://127.0.0.1:{new_port}/mcp"
        assert path.read_bytes() == before
        duplicate = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, timeout=25)
        assert duplicate.returncode == 0, duplicate.stdout.decode(errors="replace")
        assert online()["owner_pid"] == restored["owner_pid"]
    finally:
        try:
            session = client.get("/api/session").json()
            client.post("/api/stop", json={}, headers={"X-Setup-Token": session["token"]})
        except (httpx.HTTPError, ValueError):
            for handle in owned_handles.values():
                kernel.TerminateProcess(handle, 1)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=10)
        client.close()
        output.close()
        for handle in owned_handles.values():
            kernel.CloseHandle(handle)


def test_installed_codex_without_login_does_not_gate_core(tmp_path, monkeypatch):
    executable = shutil.which("codex.cmd") or shutil.which("codex")
    if not executable:
        pytest.skip("Installed Codex branch requires an actual CLI")
    owned_home = tmp_path / "owned-codex-home"
    owned_home.mkdir()
    environment = dict(os.environ, CODEX_HOME=str(owned_home))
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        environment.pop(key, None)
    login = subprocess.run([executable, "login", "status"], env=environment,
                           capture_output=True, timeout=25)
    assert login.returncode != 0
    assert "not logged in" in (login.stdout + login.stderr).decode(errors="replace").lower()
    monkeypatch.setenv("CODEX_HOME", str(owned_home))
    path, _ = fixture_config(tmp_path)
    app = SetupApplication(path, tmp_path / "state", setup_port=free_port())
    app.config_view()
    try:
        app.runtime.start()
        assert app.status()["local"]["state"] == "online"
        assert app.status()["agent"]["availability"] == "disabled"
        assert app.agent_resources is None
        assert not (owned_home / "auth.json").exists()
    finally:
        app.runtime.stop()


def test_optional_preferences_cannot_open_an_anonymous_agent_execution_route(tmp_path):
    path, cfg = fixture_config(tmp_path)
    data = config_to_dict(cfg)
    data["agent"]["enabled"] = data["agent"]["codex"]["enabled"] = True
    data["agent"]["codex"]["auth_mode"] = "chatgpt"
    data["agent"]["profiles"]["cheap_reader"]["model"] = "gpt-6-luna"
    data["projects"][0]["agent"]["context_enabled"] = True
    path.write_text(dump_toml(data), encoding="utf-8")
    app = SetupApplication(path, tmp_path / "state", setup_port=free_port())
    app.config_view()
    try:
        app.runtime.start()
        with httpx.Client(headers={"Accept": "application/json, text/event-stream"}, trust_env=False) as client:
            url = app.runtime.status()["local"]["url"]
            _, tools = rpc(client, url, "tools/list", 1)
            names = {item["name"] for item in tools["tools"]}
            assert len(names) == 15
            assert not names & {"project_context", "task_poll", "task_cancel", "auth_test_ping"}
            assert client.post(url.removesuffix("/mcp") + "/api/agent/model-test", json={}).status_code == 404
        assert app.status()["local"]["state"] == "online"
        assert app.status()["agent"]["availability"] == "capability_unavailable"
        assert app.agent_tasks is None and app.agent_resources is None
    finally:
        app.runtime.stop()
