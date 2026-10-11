"""Saved Cloudflare retry behavior through a real browser and isolated Setup/MCP."""
from __future__ import annotations
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
import httpx2 as httpx
import pytest
import re
from conftest import PKG_DIR
from config import dump_toml

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

@pytest.fixture(scope="module")
def no_agent_setup(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("cloudflare-no-agent")
    # Copy the current source verbatim, never credentials/configuration/executables.
    program = tmp_path / "program"
    program.mkdir()
    (program / "web").mkdir()
    for name in ("build_info.py", "config.py", "errors.py", "intel.py", "projects.py", "runtime.py", "security.py", "server.py", "setup.py", "startup.py", "tools_services.py", "tools.py", "writer.py", "setup.ps1", "setup.bat"):
        shutil.copyfile(PKG_DIR / name, program / name)
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(PKG_DIR / "web" / name, program / "web" / name)
    shutil.copytree(PKG_DIR / "local_agents", program / "local_agents", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PKG_DIR / "contracts", program / "contracts")
    project = tmp_path / "real-project"
    project.mkdir()
    (project / "README.md").write_text("# Actual fixture project\n", encoding="utf-8")
    setup_port, mcp_port = free_port(), free_port()
    while setup_port == mcp_port:
        setup_port = free_port()
    cfg = program / "config.toml"
    cfg.write_text(dump_toml({"projects": [{"id": "existing", "name": "Existing Project", "path": str(project)}],
                             "server": {"port": mcp_port},
                             "tunnel": {"mode": "cloudflare", "public_url": "https://leftover.ngrok.dev/mcp"},
                             "startup": {"enabled": False}}), encoding="utf-8")
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment["PATH"] = ""
    output = (tmp_path / "setup.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(program / "setup.py"), "--config", str(cfg), "--port", str(setup_port),
                             "--state-dir", str(program / "state")], cwd=program, env=environment, stdout=output, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{setup_port}"
    client = httpx.Client(base_url=url, timeout=60, trust_env=False)
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail((tmp_path / "setup.log").read_text(encoding="utf-8"))
            try:
                response = client.get("/api/session")
                if response.status_code == 200:
                    client.headers["X-Setup-Token"] = response.json()["token"]
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        else:
            pytest.fail("Isolated Setup did not start")
        yield {"url": url, "client": client, "config": cfg, "project": project, "base": tmp_path}
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

def test_saved_cloudflare_can_retry_without_config_draft_and_failure_is_actionable(no_agent_setup):
    playwright = pytest.importorskip("playwright.sync_api")
    env = no_agent_setup
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(env["url"])
        playwright.expect(page.locator("#configuration-fields")).to_be_enabled(timeout=15000)
        playwright.expect(page.locator("#tunnel-mode")).to_have_value("cloudflare")
        playwright.expect(page.locator("#save-config")).to_be_disabled()
        # Regression: saved CF used to be disabled because it shared the save action.
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled()
        assert page.locator("#tunnel-mode option").evaluate_all("items => items.map(item => item.value)") == ["ngrok", "cloudflare", "off"]
        assert "推荐首选" in page.locator("#tunnel-mode option").first.inner_text()
        assert page.locator("#mcp-url").input_value() == ""
        before = env["config"].read_bytes()
        view = env["client"].get("/api/config").json()
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/cloudflare"), timeout=65000) as response:
            page.locator("#enable-cloudflare").click()
        assert response.value.status == 503
        assert response.value.request.post_data_json == {"revision": view["revision"]}
        playwright.expect(page.locator("#cloudflare-message")).to_have_class(re.compile("error"))
        playwright.expect(page.locator("#cloudflare-message")).to_contain_text("cloudflared")
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled()
        assert "已连接" not in page.locator("#cloudflare-message").inner_text()
        assert env["config"].read_bytes() == before
        assert page.locator("#mcp-url").input_value() == ""
        page.screenshot(path=str(env["base"] / "saved-cloudflare-real-error.png"), full_page=True)
        browser.close()


def test_cloudflare_retries_protect_non_tunnel_drafts_on_mobile(no_agent_setup):
    playwright = pytest.importorskip("playwright.sync_api")
    env = no_agent_setup
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 812})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(env["url"])
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled(timeout=15000)
        playwright.expect(page.locator("#startup-enabled")).to_be_enabled()
        page.locator("#startup-enabled").check()
        playwright.expect(page.locator("#enable-cloudflare")).to_be_disabled()
        playwright.expect(page.locator("#cloudflare-draft-note")).to_be_visible()
        page.wait_for_timeout(5200)
        playwright.expect(page.locator("#startup-enabled")).to_be_checked()
        page.locator("#startup-enabled").uncheck()
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled()
        row = page.locator(".project").first
        row.locator("summary").click()
        row.locator(".project-name-input").fill("Unsaved project draft")
        playwright.expect(page.locator("#enable-cloudflare")).to_be_disabled()
        row.locator(".project-name-input").fill("Existing Project")
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled()
        page.locator(".connect .setup-preferences > .advanced > summary").click()
        original_port = page.locator("#server-port").input_value()
        page.locator("#server-port").fill("12345" if original_port != "12345" else "12346")
        playwright.expect(page.locator("#enable-cloudflare")).to_be_disabled()
        page.locator("#server-port").fill(original_port)
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors
        page.screenshot(path=str(env["base"] / "cloudflare-mobile.png"), full_page=True)
        browser.close()


@pytest.mark.parametrize("local_verified,public_verified,stale_address", [(False, True, False), (True, False, False), (True, True, True)])
def test_cloudflare_api_contract_never_reports_unverified_success(no_agent_setup, local_verified, public_verified, stale_address):
    # A narrow UI contract response is intentionally unverified. The other browser test
    # exercises the real activation failure; public success is covered by real tunnel E2E.
    playwright = pytest.importorskip("playwright.sync_api")
    env = no_agent_setup
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1024, "height": 768})
        page.goto(env["url"])
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled(timeout=15000)
        view = env["client"].get("/api/config").json()
        before = env["config"].read_bytes()
        def unverified(route):
            if route.request.method != "POST":
                route.fallback()
                return
            playwright.expect(page.locator("#enable-cloudflare")).to_be_disabled()
            assert page.locator("#enable-cloudflare").get_attribute("aria-busy") == "true"
            assert "正在" in page.locator("#cloudflare-message").inner_text()
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                **view, "ok": True, "applied": {"verified": local_verified, "public_verified": public_verified,
                                                "public_url": "https://unverified.trycloudflare.com/mcp"}}))
        if stale_address:
            status = env["client"].get("/api/status").json()
            status["tunnel"]["mode"] = "cloudflare"
            status["public"] = {"state": "online", "url": "https://leftover.ngrok.dev/mcp", "latency_ms": 10}
            page.route("**/api/status", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(status)))
        page.route("**/api/cloudflare", unverified)
        page.locator("#enable-cloudflare").click()
        playwright.expect(page.locator("#cloudflare-message")).to_have_class(re.compile("error"), timeout=15000)
        playwright.expect(page.locator("#cloudflare-message")).to_contain_text("尚未确认")
        playwright.expect(page.locator("#enable-cloudflare")).to_be_enabled()
        assert "已连接" not in page.locator("#cloudflare-message").inner_text()
        assert page.locator("#mcp-url").input_value() == ""
        assert env["config"].read_bytes() == before
        if stale_address:
            playwright.expect(page.locator('[data-layer="public"] .status-value')).to_contain_text("在线")
            playwright.expect(page.locator("#copy-url")).to_be_disabled()
        browser.close()
