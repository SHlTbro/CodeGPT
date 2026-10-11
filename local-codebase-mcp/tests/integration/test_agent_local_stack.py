"""T19-L real local seams, with owned Python fixtures and NO model/CLI execution.

The test-only qualification gate is restricted to its exact Python argv. It does
not enable a production capability, implement an adapter, or impersonate Codex.
Frontend resource wiring is covered separately by the existing real browser suite.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest
from fastmcp import Client
import asyncio

from config import config_revision, load_config
from local_agents.contracts import validation_errors
from local_agents.model_options import validate_selection, selection_fingerprint, require_execution_capability, ModelOptionError
from local_agents.runner import Runner, LaunchSpec, RunnerError, process_identity
from local_agents.scheduler import Scheduler, SchedulerError
from local_agents.store import Store
from local_agents.reports import Reports, SECTION_IDS
from local_agents.snapshot import capture_snapshot, read_snapshot_evidence
from security import DEFAULT_SENSITIVE_PATTERNS, DEFAULT_EXCLUDED_DIRS, MAX_FILE_SIZE_BYTES
from test_setup_e2e import setup_live
from test_agent_setup_api import HEADERS

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='T19-L needs actual native Windows Job proof')
OWNER = 'owned-local-fixture'


def child_env():
    return {**{name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH') if name in os.environ},
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}


def eventually(check, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.02)
    pytest.fail('Actual local seam failed to reach its expected durable state')


def saved_preferences(env):
    client = env['client']
    view = client.get('/api/config').json()
    project = env['base'] / 'owned-stack-source'
    project.mkdir(exist_ok=True)
    (project / 'main.py').write_text('value = 1\n', encoding='utf-8')
    data = deepcopy(view['data'])
    data['projects'] = [{'id': 'local-stack', 'name': 'Owned local fixture', 'path': str(project),
        'write_enabled': False, 'agent': {'context_enabled': False, 'allowed_profiles': ['cheap_reader']}}]
    data['agent']['enabled'] = data['agent']['codex']['enabled'] = False
    data['agent']['codex']['auth_mode'] = 'chatgpt'
    data['agent']['profiles']['cheap_reader'].update(model='saved-manual-unverified-model', reasoning_effort='', service_tier='standard', timeout_seconds=120)
    response = client.put('/api/config', headers=HEADERS, json={'revision': view['revision'], 'data': data})
    assert response.status_code == 200, response.text
    assert response.json()['applied']['checks']['config_loaded'] is True
    cfg = load_config(env['config'])
    assert cfg.agent == data['agent'] and cfg.projects[0].agent.context_enabled is False
    return cfg, response.json()['revision']


def frozen_selection(cfg, snapshot):
    profile = cfg.agent['profiles'][cfg.agent['default_profile']]
    option = validate_selection(profile['model'], reasoning_effort=profile['reasoning_effort'], service_tier=profile['service_tier'])
    with pytest.raises(ModelOptionError) as failure:
        require_execution_capability(option)
    assert failure.value.code == 'CAPABILITY_UNAVAILABLE'
    context = {'auth_mode': 'chatgpt', 'billing_source': 'chatgpt_plan', 'provider': 'openai', 'endpoint': None,
        'credential_context_id': 'owned-unverified-fixture', 'credential_revision': 'fixture-metadata-only',
        'config_revision': config_revision(cfg), 'cli_version': None, 'cli_sha256': None, 'executable': None}
    scope = hashlib.sha256(json.dumps(snapshot['coverage']['scope'], sort_keys=True).encode()).hexdigest()
    fingerprint = selection_fingerprint(context, option, profile=cfg.agent['default_profile'], scope_fingerprint=scope)
    return {'profile': cfg.agent['default_profile'], 'requested_model': profile['model'], 'config_revision': context['config_revision'],
        'backend_version': None, 'execution_context': {name: context[name] for name in
            ('auth_mode', 'billing_source', 'provider', 'endpoint', 'credential_context_id', 'credential_revision')} | {
            'requested_reasoning_effort': None, 'resolved_reasoning_effort': None, 'requested_service_tier': profile['service_tier'],
            'resolved_service_tier': None, 'selection_fingerprint': fingerprint, 'effective_auth_verified': False}}


def test_real_toml_native_process_terminal_report_and_exclusive_recovery(setup_live):
    env = setup_live
    cfg, disk_revision = saved_preferences(env)
    options = {'paths': ('.',), 'internal_paths': (), 'sensitive_patterns': DEFAULT_SENSITIVE_PATTERNS,
        'excluded_dirs': DEFAULT_EXCLUDED_DIRS, 'max_file_size_bytes': MAX_FILE_SIZE_BYTES}
    project = cfg.projects[0]
    snapshot = capture_snapshot(project, **options)
    selection = frozen_selection(cfg, snapshot)
    assert selection['config_revision'] == config_revision(cfg) != disk_revision
    state = env['base'] / 'state/agent'
    tasks = Store(state / 'store.sqlite', state / 'results')  # Same actual namespace as Setup.
    ownership = state / 'owned-python-fixture'
    marker = ownership / 'result.json'
    ownership.mkdir(exist_ok=True)
    program = "import json;from pathlib import Path;value={'fixture_kind':'owned_python_not_codex','result':'complete'};Path(" + repr(str(marker)) + ").write_text(json.dumps(value));print(json.dumps(value))"
    argv = (sys.executable, '-B', '-c', program)
    spec = LaunchSpec(argv, str(ownership), child_env(), cfg.agent['profiles']['cheap_reader']['timeout_seconds'])
    # Production Runner default gate stays CLOSED, even for this harmless argv.
    closed = Runner(state / 'closed-gate-check', lambda task, params: spec)
    try:
        with pytest.raises(RunnerError) as failure:
            closed.start({'task_id': 'closed-gate', 'owner_id': OWNER, 'purpose': 'project_context'}, {})
        assert failure.value.code == 'CAPABILITY_UNAVAILABLE'
        assert closed.identity('closed-gate')['pid'] is None and not marker.exists()
    finally:
        closed.close()
    qualified = []
    def resolve(task, params):
        assert task['config_revision'] == config_revision(cfg)
        assert task['requested_model'] == cfg.agent['profiles']['cheap_reader']['model']
        qualified.append(task['task_id'])
        return spec
    runner = Runner(ownership, resolve, capability_gate=lambda task, launch: launch.argv == argv and launch.env == spec.env)
    scheduler = Scheduler(tasks, runner, enabled=False, max_running=1, max_queued=20)
    runner.on_exit = lambda task_id, outcome: scheduler.finish(OWNER, task_id, outcome)
    first_request = {'project_id': project.id, 'request_id': 'owned-local-first'}
    try:
        with pytest.raises(SchedulerError) as failure:
            scheduler.submit(owner_id=OWNER, operation='project_context', request=first_request, selection=selection)
        assert failure.value.code == 'CAPABILITY_DISABLED'
        assert tasks.list_tasks() == []
        scheduler.set_enabled(True)  # Test qualification ONLY; canonical TOML remains OFF.
        first = scheduler.submit(owner_id=OWNER, operation='project_context', request=first_request, selection=selection)
        pending = scheduler.submit(owner_id=OWNER, operation='project_context', request={'project_id': project.id, 'request_id': 'owned-local-recovery'}, selection=selection)
        assert env['client'].get('/api/status').json()['agent']['queued_tasks'] == 2
        assert scheduler.dispatch() == [first.value['task_id']]
        outcome = runner.wait(first.value['task_id'], 10)
        assert outcome['outcome'] == 'succeeded' and outcome['exit_code'] == 0 and outcome['cleanup_confirmed']
        assert json.loads(marker.read_text()) == json.loads(outcome['stdout_tail'])
        identity = runner.identity(first.value['task_id'])
        native = process_identity(identity['pid'])
        assert native is None or native['alive'] is False
        eventually(lambda: tasks.get_task(OWNER, first.value['task_id'])['status'] == 'succeeded')
        terminal = tasks.get_task(OWNER, first.value['task_id'])
        assert terminal['actual_models'] is None and terminal['cost'] is None and terminal['cost_basis'] == 'unknown'
        assert terminal['execution_context']['effective_auth_verified'] is False
        repository = Reports(tasks, authorize=lambda owner, pid, scope, fingerprint: owner == OWNER and pid == project.id and scope == ['.'],
            cursor_key=b'owned-local-cursor-key-fixture-32-bytes')
        evidence = read_snapshot_evidence(project, snapshot, 'main.py', line_start=1, line_end=1)
        evidence = {key: evidence[key] for key in ('evidence_id', 'path', 'line_start', 'line_end', 'symbol', 'content_sha256', 'snapshot_id', 'excerpt', 'validation')}
        summary = 'Owned Python fixture completed; no Codex, model or project-code execution.'
        report = repository.publish(OWNER, first.value['task_id'], project, snapshot, snapshot_options=options,
            permission_fingerprint='c'*64, summary=summary, sections={name: summary for name in SECTION_IDS if name not in {'summary', 'evidence', 'coverage', 'unknowns'}},
            claims=[{'claim_id': 'fixture-source', 'text': 'Owned fixture source contains value = 1', 'classification': 'fact', 'evidence_ids': [evidence['evidence_id']]}],
            evidence=[evidence], unknowns=[{'question': 'Actual model project investigation', 'reason': 'Only an owned Python process ran', 'next_step': 'Complete real identity and read-isolation gates before production execution'}])
        assert validation_errors('report', report) == []
        page = repository.page(OWNER, report['report_id'], section='summary')
        assert page['page']['text'] == summary and page['report']['verification'] == 'not_run'
        document = repository.read_document(OWNER, report['report_id'])
        assert document['coverage']['execution_performed'] is False
        persisted_terminal = tasks.get_task(OWNER, first.value['task_id'])
    finally:
        runner.close()
    # Exclusive test-owned service restart: old Runner closed; no production daemon shares it.
    new_calls = []
    reopened = Runner(ownership, lambda task, params: new_calls.append(task['task_id']) or spec,
        capability_gate=lambda task, launch: launch.argv == argv)
    try:
        assert reopened.recover()[first.value['task_id']] is True
        scheduler = Scheduler(Store(tasks.database_path, tasks.results_dir), reopened)
        scheduler.recover()
        scheduler.set_enabled(cfg.agent['enabled'] and cfg.agent['codex']['enabled'])
        assert scheduler.store.get_task(OWNER, pending.value['task_id'])['status'] == 'interrupted'
        assert scheduler.store.get_task(OWNER, first.value['task_id']) == persisted_terminal
        assert scheduler.dispatch() == [] and new_calls == [] and qualified == [first.value['task_id']]
        replay = scheduler.submit(owner_id=OWNER, operation='project_context', request=first_request, selection=selection)
        assert replay.created is False and replay.value['task_id'] == first.value['task_id'] and new_calls == []
        with pytest.raises(RunnerError) as failure:
            reopened.start(persisted_terminal, scheduler.store.get_task_input(OWNER, first.value['task_id']))
        assert failure.value.code == 'REQUEST_CONFLICT'
        reopened_reports = Reports(scheduler.store, authorize=repository.authorize, cursor_key=repository.cursor_key)
        assert reopened_reports.read_document(OWNER, report['report_id']) == document
        (env['base'] / 'owned-local-stack-proof.json').write_text(json.dumps({'fixture_only': True, 'task_id': first.value['task_id'], 'report_id': report['report_id'],
            'config_revision': selection['config_revision'], 'disk_revision': disk_revision, 'native_cleanup_confirmed': True, 'terminal': 'succeeded',
            'pending_recovery': 'interrupted', 'replays_started': len(new_calls), 'model_calls': 0}), encoding='utf-8')
    finally:
        reopened.close()


def test_off_local_http_keeps_old_tools_and_model_execution_gate_closed(setup_live):
    client = setup_live['client']
    view = client.get('/api/config').json()
    assert view['data']['agent']['enabled'] is False
    status = client.post('/api/probe', json={}).json()
    assert status['local']['state'] == 'online' and status['agent']['availability'] == 'disabled'
    assert status['agent']['active_tasks'] == status['agent']['queued_tasks'] == 0
    async def old_tools():
        async with Client(status['local']['url'], mode='legacy') as mcp:
            assert await mcp.ping()
            return await mcp.list_tools()
    assert len(asyncio.run(old_tools())) == 15
    credential = client.get('/api/agent/credential').json()['credential']
    response = client.post('/api/agent/model-test', headers=HEADERS, json={'revision': view['revision'], 'request_id': 'no-model-local-gate',
        'project_id': 'local-stack', 'profile': 'cheap_reader', 'expected_credential_revision': credential['credential_revision'], 'allow_model_request': True})
    assert response.status_code == 503 and response.json()['error']['code'] == 'CAPABILITY_UNAVAILABLE'
    assert 'task_id' not in response.json() and status['agent']['last_model_test'] is None
