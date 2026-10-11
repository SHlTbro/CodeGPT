"""Real candidate Setup -> TOML -> MCP, with the optional Agent left off."""
import asyncio
import tomllib
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from test_setup_e2e import setup_live, mcp_projects, free_port


def test_core_browser_default_off_real_save_and_explicit_optional_settings(setup_live):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        requests, errors = [], []
        page.on('request', lambda request: requests.append((request.method, request.url)))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(setup_live['url'])
        playwright.expect(page.locator('#core-availability')).to_contain_text('Core 在线', timeout=30000)
        playwright.expect(page.locator('#agent-enabled')).not_to_be_checked()
        assert not page.locator('#agent-auth-mode').is_visible()
        assert not [request for request in requests if '/api/agent/' in request[1]]
        assert '仅供本机' in page.locator('#connection-recovery').inner_text()
        assert '严格沙箱' in page.locator('#agent-execution-access').inner_text()
        assert page.locator('#projects-title').evaluate('(node) => node.compareDocumentPosition(document.querySelector("#agent-title")) & Node.DOCUMENT_POSITION_FOLLOWING')

        folder = setup_live['base'] / 'browser-core-project'
        folder.mkdir()
        page.locator('#add-project').click()
        page.locator('.project-id-input').fill('core-browser-real')
        page.locator('.project-name-input').fill('Core Browser Real')
        page.locator('.project-path-input').fill(str(folder))
        with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith('/api/config'), timeout=60000) as saved:
            page.locator('#save-config').click()
        assert saved.value.status == 200, saved.value.text()
        assert saved.value.json()['applied']['verified'] is True
        data = tomllib.loads(setup_live['config'].read_text(encoding='utf-8'))
        assert data['agent']['enabled'] is False
        assert data['projects'][0]['id'] == 'core-browser-real'
        status = setup_live['client'].get('/api/status').json()
        assert status['version']['release'] == '1.2.0'
        assert status['version']['source_commit'] is None
        assert status['version']['worktree_dirty'] is None
        assert status['version']['build_id'] is None
        assert status['setup']['pid'] == status['owner_pid']
        assert status['mcp']['pid'] == status['local']['pid']
        assert status['setup']['effective_config_version'] == status['mcp']['effective_config_version']
        assert mcp_projects(status['local']['url']) == [{'id': 'core-browser-real', 'name': 'Core Browser Real'}]
        from fastmcp import Client
        async def names():
            async with Client(status['local']['url']) as client:
                return [tool.name for tool in await client.list_tools()]
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert len(pool.submit(lambda: asyncio.run(names())).result(timeout=30)) == 15
        assert not [request for request in requests if '/api/agent/' in request[1]]

        page.screenshot(path=str(setup_live['base'] / 'core-default-off-real-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 375, 'height': 812})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(setup_live['base'] / 'core-default-off-real-mobile.png'), full_page=True)
        page.locator('#agent-settings-disclosure > summary').click()
        playwright.expect(page.locator('#agent-credential-state')).not_to_contain_text('尚未读取', timeout=10000)
        assert len([request for request in requests if request[0] == 'GET' and request[1].endswith('/api/agent/credential')]) == 1
        assert len([request for request in requests if request[0] == 'GET' and request[1].endswith('/api/agent/models')]) == 1
        assert not [request for request in requests if request[0] != 'GET' and '/api/agent/' in request[1]]
        assert not errors
        browser.close()


def test_open_page_recovers_setup_session_once_without_replaying_other_failures(tmp_path):
    """Rebuild actual owned Setup/MCP on the same port; no browser refresh."""
    playwright = pytest.importorskip('playwright.sync_api')
    from setup import SetupApplication, SetupServer
    from config import dump_toml
    folder = tmp_path / 'source'
    folder.mkdir()
    cfg = tmp_path / 'config.toml'
    port, mcp_port = free_port(), free_port()
    cfg.write_text(dump_toml({'projects': [{'id': 'session-recovery', 'name': 'Original', 'path': str(folder)}],
        'server': {'host': '127.0.0.1', 'port': mcp_port, 'path': '/mcp'},
        'tunnel': {'mode': 'off', 'public_url': ''}}), encoding='utf-8')

    def start():
        app = SetupApplication(cfg, tmp_path / 'state', port)
        app.config_view()
        app.runtime.start()
        server = SetupServer(('127.0.0.1', port), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return app, server, thread

    def stop(owned):
        app, server, thread = owned
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.runtime.stop()

    owned = start()
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            page = browser.new_page()
            responses, requests = [], []
            page.on('response', lambda response: responses.append((response.request.method, response.url, response.status)))
            page.on('request', lambda request: requests.append((request.method, request.url)))
            page.goto(f'http://127.0.0.1:{port}')
            playwright.expect(page.locator('#core-availability')).to_contain_text('Core 在线', timeout=30000)
            old_token = owned[0].token
            stop(owned)
            playwright.expect(page.locator('#core-availability')).to_contain_text('当前状态未知', timeout=15000)
            assert page.locator('.project-name-input').input_value() == 'Original'
            # Playwright owns an event loop here; run normal synchronous startup
            # in another thread, as the actual launcher does outside the browser.
            with ThreadPoolExecutor(max_workers=1) as pool:
                owned = pool.submit(start).result(timeout=45)
            assert owned[0].token != old_token
            playwright.expect(page.locator('#core-availability')).to_contain_text('Core 在线', timeout=15000)
            page.locator('.project summary').click()
            page.locator('.project-name-input').fill('Recovered without login')
            before = len(responses)
            page.locator('#save-config').click()
            playwright.expect(page.locator('#page-message')).to_have_text('已生效。', timeout=30000)
            puts = [response[2] for response in responses[before:] if response[0] == 'PUT' and response[1].endswith('/api/config')]
            assert puts == [403, 200]
            assert tomllib.loads(cfg.read_text(encoding='utf-8'))['projects'][0]['name'] == 'Recovered without login'
            assert owned[0].agent_resources is None

            # A backend validation failure is not replayed automatically.
            page.locator('.project summary').click()
            page.locator('.project-path-input').fill(str(folder / 'missing'))
            before = len(responses)
            page.locator('#save-config').click()
            playwright.expect(page.locator('#page-message')).not_to_have_text('已生效。')
            playwright.expect(page.locator('#page-message')).not_to_have_text('正在保存…')
            puts = [response[2] for response in responses[before:] if response[0] == 'PUT' and response[1].endswith('/api/config')]
            assert puts == [422]
            assert not [request for request in requests if '/api/agent/' in request[1]]
            browser.close()
    finally:
        stop(owned)
