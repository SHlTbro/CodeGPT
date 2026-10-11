"""Real localhost control plane, TOML and child MCP; no synthetic agent API."""
import copy
import tomllib

from test_setup_e2e import setup_live, mcp_projects, wait_until
from local_agents.contracts import validation_errors
from config import dump_toml

HEADERS = {'X-UI-Contract-Version': '2.0.0', 'X-UI-Asset-Version': 'agent-ui-2.0.0-t18.2'}


def test_real_agent_preferences_save_toml_reload_and_project_discovery(setup_live):
    client = setup_live['client']
    info = client.get('/api/info').json()
    assert validation_errors('setup_info_response_overlay', info) == []
    view = client.get('/api/config').json()
    assert validation_errors('setup_config_get_response_overlay', view) == []
    data = copy.deepcopy(view['data'])
    folder = setup_live['base'] / 'real-agent-project'
    folder.mkdir()
    data['projects'] = [{'id': 'agent-api-real', 'name': 'Agent API Real', 'path': str(folder),
        'write_enabled': False, 'agent': {'context_enabled': False, 'allowed_profiles': ['cheap_reader']}}]
    data['agent']['profiles']['cheap_reader']['model'] = 'manual-model-id'
    data['agent']['codex']['auth_mode'] = 'chatgpt'
    saved = client.put('/api/config', json={'revision': view['revision'], 'data': data}, headers=HEADERS)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert validation_errors('setup_config_put_response_overlay', result) == []
    parsed = tomllib.loads(setup_live['config'].read_text(encoding='utf-8'))
    assert parsed['agent']['profiles']['cheap_reader']['model'] == 'manual-model-id'
    assert parsed['agent']['enabled'] is False
    status = client.post('/api/probe', json={}).json()
    assert validation_errors('setup_status_response_overlay', status) == []
    assert status['agent']['availability'] == 'disabled'
    assert status['agent']['enforcement']['state'] != 'verified'
    assert mcp_projects(status['local']['url']) == [{'id': 'agent-api-real', 'name': 'Agent API Real'}]
    assert result['applied']['checks']['config_loaded'] is True
    assert result['applied']['checks']['local_health'] is True
    assert wait_until(lambda: client.get('/api/status').json()['config']['applied_revision'] == result['applied']['revision'])


def test_new_writes_require_matching_assets_and_failed_save_keeps_last_good(setup_live):
    client = setup_live['client']
    view = client.get('/api/config').json()
    before = setup_live['config'].read_bytes()
    changed = copy.deepcopy(view['data'])
    changed['agent']['profiles']['cheap_reader']['model'] = 'new-choice'
    rejected = client.put('/api/config', json={'revision': view['revision'], 'data': changed})
    assert rejected.status_code == 409
    assert rejected.json()['error']['code'] == 'UI_VERSION_MISMATCH'
    assert setup_live['config'].read_bytes() == before
    wrong = dict(HEADERS, **{'X-UI-Asset-Version': 'old'})
    assert client.put('/api/config', json={'revision': view['revision'], 'data': changed}, headers=wrong).status_code == 409
    invalid = copy.deepcopy(view['data'])
    invalid['agent']['profiles']['cheap_reader']['service_tier'] = 'unknown-expensive-tier'
    # Syntax allows future wire values, so choose a definite type violation.
    invalid['agent']['limits']['max_running'] = True
    response = client.put('/api/config', json={'revision': view['revision'], 'data': invalid}, headers=HEADERS)
    assert response.status_code == 422 and 'Traceback' not in response.text
    assert setup_live['config'].read_bytes() == before


def test_unverified_execution_never_returns_a_fake_success(setup_live):
    client = setup_live['client']
    response = client.post('/api/agent/model-test', json={}, headers=HEADERS)
    assert response.status_code != 200
    assert 'task_id' not in response.json()
    status = client.get('/api/status').json()
    assert 'agent' in status and status['agent']['last_model_test'] is None


def test_status_reads_current_preferences_and_bad_external_toml_keeps_last_good(setup_live):
    client = setup_live['client']
    view = client.get('/api/config').json()
    before = setup_live['config'].read_bytes()
    edited = tomllib.loads(before.decode('utf-8'))
    edited['agent'] = copy.deepcopy(view['data']['agent'])
    original_model = edited['agent']['profiles']['cheap_reader']['model'] or None
    edited['agent']['profiles']['cheap_reader']['model'] = 'external-manual-choice'
    try:
        setup_live['config'].write_text(dump_toml(edited), encoding='utf-8')
        assert client.get('/api/status').json()['agent']['requested_model'] == 'external-manual-choice'
        setup_live['config'].write_text('broken = [', encoding='utf-8')
        status = client.get('/api/status')
        assert status.status_code == 200
        assert status.json()['agent']['requested_model'] == original_model
        assert status.json()['agent']['availability'] == 'disabled'
    finally:
        setup_live['config'].write_bytes(before)
