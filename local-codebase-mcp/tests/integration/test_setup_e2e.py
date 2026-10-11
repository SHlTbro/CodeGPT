"""Real browser -> Setup HTTP -> TOML -> child MCP regression coverage."""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from conftest import PKG_DIR
from config import dump_toml


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(callback, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = callback()
            if result:
                return result
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.2)
    pytest.fail("真实 Setup/MCP 在限定时间内没有达到预期状态")


@pytest.fixture(scope="module")
def setup_live(tmp_path_factory):
    base = tmp_path_factory.mktemp("setup-http-e2e")
    setup_port, mcp_port = free_port(), free_port()
    cfg = base / "config.toml"
    cfg.write_text(dump_toml({"projects": [], "server": {"host": "127.0.0.1", "port": mcp_port, "path": "/mcp"},
                             "tunnel": {"mode": "off", "public_url": ""}}), encoding="utf-8")
    output = (base / "setup.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(PKG_DIR / "setup.py"), "--config", str(cfg),
                             "--port", str(setup_port), "--state-dir", str(base / "state")],
                            cwd=PKG_DIR, stdout=output, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{setup_port}", timeout=55, trust_env=False)
    try:
        def ready():
            if proc.poll() is not None:
                pytest.fail((base / "setup.log").read_text(encoding="utf-8"))
            response = client.get("/api/session")
            return response.json() if response.status_code == 200 else False
        session = wait_until(ready)
        client.headers["X-Setup-Token"] = session["token"]
        yield {"client": client, "url": str(client.base_url), "config": cfg, "base": base,
               "proc": proc, "setup_port": setup_port, "mcp_port": mcp_port}
    finally:
        if proc.poll() is None:
            try:
                client.post("/api/stop", json={})
                proc.wait(timeout=20)
            except Exception:
                proc.terminate()
                proc.wait(timeout=10)
        client.close()
        output.close()


def mcp_projects(url):
    from fastmcp import Client
    async def call():
        async with Client(url) as client:
            result = await client.call_tool("list_projects", {})
            return result.data
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(call())["projects"]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(call())["projects"]).result(timeout=30)


def test_http_hot_reload_restart_rollback_and_isolation(setup_live):
    env = setup_live
    client = env["client"]
    view = client.get("/api/config").json()
    assert view["data"]["projects"] == []
    assert client.get("/").status_code == 200
    status = client.post("/api/probe", json={}).json()
    assert status["local"]["state"] == "online"
    assert status["tunnel"]["state"] == "disabled"
    assert status["public"]["state"] == "unconfigured"
    first_pid = status["local"]["pid"]
    project = env["base"] / "real-project"
    project.mkdir()
    changed = copy.deepcopy(view["data"])
    changed["projects"] = [{"id": "http-added", "name": "HTTP Added", "path": str(project), "write_enabled": False}]
    saved = client.put("/api/config", json={"revision": view["revision"], "data": changed})
    assert saved.status_code == 200, saved.text
    saved_view = saved.json()
    assert saved_view["applied"]["verified"] is True
    assert saved_view["applied"]["action"] == "hot_reload"
    assert "http-added" in env["config"].read_text(encoding="utf-8")
    assert mcp_projects(status["local"]["url"]) == [{"id": "http-added", "name": "HTTP Added"}]
    assert client.get("/api/status").json()["local"]["pid"] == first_pid
    assert client.put("/api/config", json={"revision": view["revision"], "data": changed}).status_code == 409

    before = env["config"].read_bytes()
    invalid = copy.deepcopy(changed)
    invalid["projects"][0]["path"] = str(project / "missing")
    bad = client.put("/api/config", json={"revision": saved_view["revision"], "data": invalid})
    assert bad.status_code == 422 and "Traceback" not in bad.text
    assert env["config"].read_bytes() == before

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(2)
    try:
        failed_restart = copy.deepcopy(changed)
        failed_restart["server"]["port"] = blocker.getsockname()[1]
        response = client.put("/api/config", json={"revision": saved_view["revision"], "data": failed_restart})
        assert response.status_code == 503, response.text
        assert env["config"].read_bytes() == before
        assert mcp_projects(status["local"]["url"])[0]["id"] == "http-added"
    finally:
        blocker.close()

    latest = client.get("/api/config").json()
    restarted = copy.deepcopy(latest["data"])
    restarted["server"]["port"] = free_port()
    response = client.put("/api/config", json={"revision": latest["revision"], "data": restarted})
    assert response.status_code == 200, response.text
    assert response.json()["applied"]["action"] == "restart"
    new_url = f"http://127.0.0.1:{restarted['server']['port']}/mcp"
    assert mcp_projects(new_url)[0]["id"] == "http-added"
    for route in ("/", "/api/session", "/api/config", "/app.js", "/.setup/activity.json"):
        assert httpx.get(new_url.removesuffix("/mcp") + route, trust_env=False).status_code == 404
    assert client.get("/api/config", headers={"Host": "public.example"}).status_code == 403
    assert client.get("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"X-Forwarded-Host": "public.example"}).status_code == 403
    assert client.put("/api/config", json={"revision": latest["revision"], "data": restarted},
                      headers={"X-Setup-Token": ""}).status_code == 403
    assert client.get("/config.toml").status_code == 404
    assert client.get("/ngrok-token.txt").status_code == 404
    assert client.put("/api/config", json={"revision": latest["revision"],
                      "data": {"intel": {"servers": {"python": {"command": ["cmd"]}}}}}).status_code == 400

    env["config"].write_text('projects = [broken', encoding="utf-8")
    assert mcp_projects(new_url)[0]["id"] == "http-added"
    recovered = client.get("/api/config").json()
    assert recovered["config_error"]
    assert "http-added" in env["config"].read_text(encoding="utf-8")
    restart_response = client.post("/api/restart", json={})
    assert restart_response.status_code == 200, restart_response.text
    assert restart_response.json()["applied"]["verified"] is True


def test_real_browser_save_toml_mcp_and_error(setup_live):
    playwright = pytest.importorskip("playwright.sync_api")
    env, client = setup_live, setup_live["client"]
    project = env["base"] / "browser-project"
    project.mkdir()
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(env["url"])
        page.get_by_role("button", name="添加本地项目").click()
        row = page.locator(".project").last
        row.locator(".project-name-input").fill("Browser Added")
        row.locator(".project-id-input").fill("browser-added")
        row.locator(".project-path-input").fill(str(project))
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/config"), timeout=55000) as response:
            page.get_by_role("button", name="保存并生效").click()
        assert response.value.status == 200, response.value.text()
        assert response.value.json()["applied"]["verified"] is True
        assert "browser-added" in env["config"].read_text(encoding="utf-8")
        status = client.post("/api/probe", json={}).json()
        assert any(p["id"] == "browser-added" for p in mcp_projects(status["local"]["url"]))
        page.screenshot(path=str(env["base"] / "desktop.png"), full_page=True)

        row = page.locator(".project").last
        if not row.get_attribute("open"):
            row.locator("summary").click()
        row.locator(".project-path-input").fill(str(project / "missing"))
        before = env["config"].read_bytes()
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/config")) as rejected:
            page.get_by_role("button", name="保存并生效").click()
        assert rejected.value.status == 422
        assert env["config"].read_bytes() == before
        page.wait_for_function("document.querySelector('#page-message').hidden === false")
        assert "失败" in page.locator("#page-message").inner_text() or "不存在" in page.locator("#page-message").inner_text()
        assert not errors
        mobile = browser.new_page(viewport={"width": 375, "height": 812})
        mobile.goto(env["url"])
        mobile.get_by_role("button", name="添加本地项目").wait_for(state="visible")
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        mobile.screenshot(path=str(env["base"] / "mobile.png"), full_page=True)
        browser.close()



def run_setup_again(env, port=None, config=None, timeout=15):
    import os
    process_env = dict(os.environ)
    process_env.pop("PYTHONUTF8", None)
    process_env.pop("PYTHONIOENCODING", None)
    return subprocess.run(
        [sys.executable, str(PKG_DIR / "setup.py"), "--config", str(config or env["config"]),
         "--port", str(port or env["setup_port"]), "--state-dir", str(env["base"] / "second-state")],
        cwd=PKG_DIR, env=process_env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def test_repeated_setup_same_port_reuses_owner_without_restarting_mcp(setup_live):
    env = setup_live
    before = env["client"].get("/api/status").json()
    repeated = run_setup_again(env)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert "配置页面已经运行" in repeated.stdout
    assert "Traceback" not in repeated.stdout + repeated.stderr
    after = env["client"].get("/api/status").json()
    assert after["owner_pid"] == before["owner_pid"]
    assert after["local"]["pid"] == before["local"]["pid"]
    assert env["proc"].poll() is None


def test_same_config_other_setup_port_is_rejected_without_restarting_mcp(setup_live):
    env = setup_live
    before = env["client"].get("/api/status").json()
    rejected = run_setup_again(env, port=free_port())
    assert rejected.returncode != 0
    assert "同一配置已经由另一个 Setup 管理" in rejected.stdout
    assert "Traceback" not in rejected.stdout + rejected.stderr
    after = env["client"].get("/api/status").json()
    assert after["owner_pid"] == before["owner_pid"]
    assert after["local"]["pid"] == before["local"]["pid"]


def test_setup_does_not_replace_unknown_existing_listener(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class UnknownHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'{"name":"unrelated-listener"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    other = ThreadingHTTPServer(("127.0.0.1", 0), UnknownHandler)
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    cfg = tmp_path / "config.toml"
    cfg.write_text(dump_toml({"projects": [], "server": {"port": free_port()}}), encoding="utf-8")
    env = {"config": cfg, "base": tmp_path, "setup_port": other.server_port}
    try:
        rejected = run_setup_again(env)
        assert rejected.returncode != 0
        assert "端口" in rejected.stdout and "被占用" in rejected.stdout
        assert "配置页面已经运行" not in rejected.stdout
        assert "Traceback" not in rejected.stdout + rejected.stderr
        response = httpx.get(f"http://127.0.0.1:{other.server_port}/api/info", trust_env=False)
        assert response.json() == {"name": "unrelated-listener"}
        assert not (tmp_path / "second-state" / "server.log").exists()
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch exit-code contract")
def test_setup_batch_preserves_original_failure_exit_after_pause(tmp_path):
    launch_dir = tmp_path / "launcher with spaces"
    launch_dir.mkdir()
    bat = launch_dir / "setup.bat"
    bat.write_bytes((PKG_DIR / "setup.bat").read_bytes())
    (launch_dir / "setup.ps1").write_text("exit 7\n", encoding="ascii")
    failed = subprocess.run(["cmd", "/c", str(bat)], input="\n", capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=10)
    assert failed.returncode == 7, failed.stdout + failed.stderr


def test_setup_restarts_same_port_after_real_http_requests_and_owner_stop(tmp_path):
    port = free_port()
    cfg = tmp_path / "config.toml"
    cfg.write_text(dump_toml({"projects": [], "server": {"port": free_port()}}), encoding="utf-8")
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False)
    owner_pids = []
    try:
        for generation in range(2):
            output = (tmp_path / f"owner-{generation}.log").open("w", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, str(PKG_DIR / "setup.py"), "--config", str(cfg),
                 "--port", str(port), "--state-dir", str(tmp_path / "state")],
                cwd=PKG_DIR, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            )
            try:
                def ready():
                    if proc.poll() is not None:
                        pytest.fail((tmp_path / f"owner-{generation}.log").read_text(encoding="utf-8"))
                    response = client.get("/api/session")
                    return response.json() if response.status_code == 200 else False
                session = wait_until(ready)
                assert client.get("/api/info").json()["config"] == str(cfg.resolve())
                status = client.get("/api/status").json()
                owner_pids.append(status["owner_pid"])
                assert status["local"]["state"] == "online"
                response = client.post("/api/stop", json={}, headers={"X-Setup-Token": session["token"]})
                assert response.status_code == 200
                proc.wait(timeout=20)
                assert proc.returncode == 0
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                output.close()
        assert owner_pids[0] != owner_pids[1]
    finally:
        client.close()


def test_real_first_start_recovers_credential_and_toml_after_owner_crash(tmp_path):
    """A killed one-click transaction must not strand the next real MCP startup."""
    from config import ensure_config, load_config
    setup_port, mcp_port = free_port(), free_port()
    cfg = tmp_path / "config.toml"
    cfg.write_text(dump_toml({"projects": [], "server": {"host": "127.0.0.1", "port": mcp_port, "path": "/mcp"},
                             "tunnel": {"mode": "off", "public_url": ""}}), encoding="utf-8")
    ensure_config(cfg)
    original = cfg.read_bytes()
    credential = tmp_path / "ngrok-token.txt"
    credential.write_text("old_authtoken_ABCDEFGHIJKLMNOP012345", encoding="ascii")
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / ("ngrok.exe" if sys.platform == "win32" else "ngrok")).write_bytes(b"fixture; never launched")
    crash_program = """
import os
import sys
from pathlib import Path
import setup
from config import ConfigStore
class CrashRuntime:
    def __init__(self, *args):
        pass
    def apply(self, config, force_tunnel_restart=False):
        return {"verified": True, "action": "restart"}
    def verify_public(self, config, timeout=30):
        os._exit(86)
setup.Runtime = CrashRuntime
setup.BASE_DIR = Path(sys.argv[2])
app = setup.SetupApplication(Path(sys.argv[1]), Path(sys.argv[1]).parent / "state")
app.setup_ngrok({"revision": ConfigStore(app.config_path).read()["revision"],
                 "token": "test_authtoken_ABCDEFGHIJKLMNOP012345"})
"""
    crashed = subprocess.run([sys.executable, "-c", crash_program, str(cfg), str(agent)], cwd=PKG_DIR,
                             capture_output=True, encoding="utf-8", errors="replace", timeout=15)
    assert crashed.returncode == 86, crashed.stderr
    assert load_config(cfg).tunnel.mode == "ngrok"
    assert credential.read_text(encoding="ascii") == "test_authtoken_ABCDEFGHIJKLMNOP012345"
    record = tmp_path / ".setup" / "ngrok-credential.pending"
    assert record.exists() and cfg.with_name("config.toml.pending").exists()
    log = (tmp_path / "recovered-setup.log").open("w", encoding="utf-8")
    owner = subprocess.Popen([sys.executable, str(PKG_DIR / "setup.py"), "--config", str(cfg),
                              "--port", str(setup_port), "--state-dir", str(tmp_path / "custom-private-state")],
                             cwd=PKG_DIR, stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{setup_port}", timeout=25, trust_env=False)
    try:
        def ready():
            if owner.poll() is not None:
                pytest.fail((tmp_path / "recovered-setup.log").read_text(encoding="utf-8"))
            response = client.get("/api/session")
            return response.json() if response.status_code == 200 else False
        session = wait_until(ready)
        client.headers["X-Setup-Token"] = session["token"]
        view = client.get("/api/config").json()
        assert view["data"]["tunnel"]["mode"] == "off"
        status = client.get("/api/status").json()
        assert status["local"]["state"] == "online"
        assert mcp_projects(status["local"]["url"]) == []
        assert cfg.read_bytes() == original
        assert cfg.with_name("config.last-good.toml").read_bytes() == original
        assert credential.read_text(encoding="ascii") == "old_authtoken_ABCDEFGHIJKLMNOP012345"
        assert client.get("/api/ngrok").json()["credential_saved"] is True
        assert "old_authtoken_" not in client.get("/api/config").text
        assert "test_authtoken_" not in client.get("/api/status").text
        assert not record.exists() and not cfg.with_name("config.toml.pending").exists()
        assert client.get("/.setup/ngrok-credential.pending").status_code == 404
        client.post("/api/stop", json={}).raise_for_status()
        owner.wait(timeout=20)
        assert owner.returncode == 0
    finally:
        if owner.poll() is None:
            try:
                client.post("/api/stop", json={})
                owner.wait(timeout=20)
            except Exception:
                owner.terminate()
                owner.wait(timeout=10)
        client.close()
        log.close()
