"""Real Setup resources and protected credential storage; zero model requests."""
import copy
import tomllib
import uuid
import pytest

from test_setup_e2e import setup_live
from test_agent_setup_api import HEADERS
from local_agents.contracts import validation_errors
from config import dump_toml


def test_real_cached_resources_and_disabled_refresh(setup_live):
    client = setup_live['client']
    key = client.get('/api/agent/credential')
    assert key.status_code == 200, key.text
    assert validation_errors('setup_credential_response', key.json()) == []
    catalog = client.get('/api/agent/models')
    assert catalog.status_code == 200, catalog.text
    assert validation_errors('setup_models_response', catalog.json()) == []
    assert catalog.json()['catalog']['model_request_performed'] is False
    view = client.get('/api/config').json()
    refreshed = client.post('/api/agent/models', json={'revision': view['revision'], 'backend': 'codex'}, headers=HEADERS)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()['catalog']['state'] == 'disabled'
    probe = client.post('/api/agent/probe', json={'revision': view['revision'], 'backend': 'codex'}, headers=HEADERS)
    assert validation_errors('setup_agent_probe_response', probe.json()) == []
    assert probe.json()['probe']['availability'] == 'disabled'
    assert probe.json()['probe']['model_request_performed'] is False


def test_real_key_save_reopen_replace_delete_and_conflicts(setup_live):
    client = setup_live['client']
    view = client.get('/api/config').json()
    data = copy.deepcopy(view['data'])
    data['agent']['codex']['auth_mode'] = 'api_key'
    data['agent']['profiles']['cheap_reader']['model'] = 'manual-choice'
    saved = client.put('/api/config', json={'revision': view['revision'], 'data': data}, headers=HEADERS)
    assert saved.status_code == 200, saved.text
    revision = saved.json()['revision']
    credential = client.get('/api/agent/credential').json()['credential']
    key = 'owned-no-provider-test-key-' + uuid.uuid4().hex
    request = {'revision': revision, 'request_id': str(uuid.uuid4()),
               'expected_credential_revision': credential['credential_revision'], 'api_key': key}
    result = client.put('/api/agent/credential', json=request, headers=HEADERS)
    assert result.status_code == 200, result.text
    assert validation_errors('setup_credential_response', result.json()) == []
    current = result.json()['credential']
    assert current['configured'] is True and current['storage'] == 'protected_file'
    assert key not in result.text
    assert client.put('/api/agent/credential', json=request, headers=HEADERS).json() == result.json()
    stale = dict(request, request_id=str(uuid.uuid4()))
    assert client.put('/api/agent/credential', json=stale, headers=HEADERS).status_code == 409
    status = client.get('/api/status').json()
    assert status['agent']['credential_status'] == current
    assert key not in str(status)
    for path in setup_live['base'].rglob('*'):
        if path.is_file() and not path.name.endswith('.lock'):
            assert key.encode() not in path.read_bytes(), path.name
    replacement = dict(request, request_id=str(uuid.uuid4()), expected_credential_revision=current['credential_revision'],
                       api_key='owned-replacement-no-provider-key')
    replaced = client.put('/api/agent/credential', json=replacement, headers=HEADERS)
    assert replaced.status_code == 200, replaced.text
    current = replaced.json()['credential']
    assert current['credential_revision'] != result.json()['credential']['credential_revision']
    deleted = client.request('DELETE', '/api/agent/credential', headers=HEADERS,
        json={'revision': revision, 'request_id': str(uuid.uuid4()),
              'expected_credential_revision': current['credential_revision']})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()['credential']['configured'] is False
    assert client.get('/api/agent/credential').json()['credential'] == deleted.json()['credential']
    assert tomllib.loads(setup_live['config'].read_text(encoding='utf-8'))['agent']['profiles']['cheap_reader']['model'] == 'manual-choice'


def test_resources_reject_old_assets_and_unverified_model_execution(setup_live):
    client = setup_live['client']
    view = client.get('/api/config').json()
    request = {'revision': view['revision'], 'backend': 'codex'}
    rejected = client.post('/api/agent/models', json=request)
    assert rejected.status_code == 409
    assert rejected.json()['error']['code'] == 'UI_VERSION_MISMATCH'
    response = client.post('/api/agent/model-test', headers=HEADERS, json={
        'revision': view['revision'], 'request_id': str(uuid.uuid4()), 'project_id': 'unknown',
        'profile': 'cheap_reader', 'expected_credential_revision': client.get('/api/agent/credential').json()['credential']['credential_revision'],
        'allow_model_request': True})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'CAPABILITY_UNAVAILABLE'
    assert 'task_id' not in response.text


def test_real_browser_model_draft_and_protected_key(setup_live):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(setup_live['url'])
        page.locator('#agent-settings-disclosure > summary').click()
        page.locator('#agent-auth-mode').select_option('api_key')
        profile = page.locator('[data-profile="cheap_reader"]')
        if not profile.get_attribute('open'):
            profile.locator('summary').click()
        profile.locator('.profile-model').fill('browser-manual-model')
        with page.expect_response(lambda r: r.request.method == 'PUT' and r.url.endswith('/api/config'), timeout=55000) as saved:
            page.get_by_role('button', name='保存并生效').click()
        assert saved.value.status == 200, saved.value.text()
        parsed = tomllib.loads(setup_live['config'].read_text(encoding='utf-8'))
        assert parsed['agent']['profiles']['cheap_reader']['model'] == 'browser-manual-model'
        # Re-render intentionally folds disabled settings. Explicitly reopen it.
        if not page.locator('#agent-settings-disclosure').get_attribute('open'):
            page.locator('#agent-settings-disclosure > summary').click()
        key = 'owned-browser-non-provider-key-' + uuid.uuid4().hex
        page.locator('#agent-api-key').fill(key)
        with page.expect_response(lambda r: r.request.method == 'PUT' and r.url.endswith('/api/agent/credential')) as accepted:
            page.locator('#agent-save-key').click()
        assert accepted.value.status == 200, accepted.value.text()
        assert accepted.value.json()['credential']['configured'] is True
        playwright.expect(page.locator('#agent-api-key')).to_have_value('')
        assert key not in page.content()
        page.reload()
        page.locator('#agent-settings-disclosure > summary').click()
        playwright.expect(page.locator('#agent-credential-state')).to_contain_text('已保存')
        assert key not in page.content()
        assert not errors
        page.screenshot(path=str(setup_live['base'] / 'agent-real-browser.png'), full_page=True)
        browser.close()


def test_credential_uses_revision_checked_bytes_even_with_external_edit(tmp_path, monkeypatch):
    from pathlib import Path
    from setup import SetupApplication
    cfg = tmp_path / 'config.toml'
    cfg.write_text(dump_toml({'projects': [], 'tunnel': {'mode': 'off'},
        'agent': {'codex': {'auth_mode': 'api_key'}}}), encoding='utf-8')
    app = SetupApplication(cfg, tmp_path / 'state')
    view = app.config_view()
    initial = app.agent_storage()[0].status()
    edited = copy.deepcopy(view['data'])
    edited['agent']['codex'].update(api_provider='custom_responses', api_base_url='https://changed-domain.example/v1')
    original_read = Path.read_bytes
    armed = True
    def external_edit_after_read(path):
        nonlocal armed
        raw = original_read(path)
        if path == cfg and armed:
            armed = False
            cfg.write_text(dump_toml(edited), encoding='utf-8')
        return raw
    monkeypatch.setattr(Path, 'read_bytes', external_edit_after_read)
    result = app.agent_request('/api/agent/credential', 'PUT', {'revision': view['revision'],
        'request_id': str(uuid.uuid4()), 'expected_credential_revision': initial['credential_revision'],
        'api_key': 'owned-no-provider-cas-key'})
    assert result['config_revision'] == view['revision']
    assert result['credential']['bound_endpoint'] == 'https://api.openai.com/v1'
    assert tomllib.loads(cfg.read_text(encoding='utf-8'))['agent']['codex']['api_base_url'] == 'https://changed-domain.example/v1'


def test_disabled_credential_failure_preserves_real_legacy_status(setup_live):
    client = setup_live['client']
    path = setup_live['base'] / 'state/agent/credentials/credential.dpapi'
    assert client.get('/api/status').status_code == 200
    before = path.read_bytes()
    try:
        path.write_bytes(b'owned-corrupt-dpapi-fixture')
        response = client.get('/api/status')
        assert response.status_code == 200, response.text
        status = response.json()
        assert status['local']['state'] == 'online' and isinstance(status['history'], list)
        assert status['agent']['credential_status']['status'] == 'unavailable'
        assert status['agent']['last_error']['code'] == 'CREDENTIAL_UNAVAILABLE'
        assert client.get('/api/agent/credential').status_code == 503
    finally:
        path.write_bytes(before)
