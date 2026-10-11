"""Synthetic secrets only; actual user-scope Windows DPAPI and local ACLs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from local_agents.credentials import CredentialStore, CredentialError
from local_agents.contracts import validation_errors

ENDPOINT = 'https://api.openai.com/v1'
CONFIG = 'a' * 64

def resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]

def make(tmp_path, **kwargs):
    return CredentialStore(tmp_path / 'private', resolver=resolver, **kwargs)

def request(store, id='first', key='synthetic-not-a-real-api-key'):
    return {'revision': CONFIG, 'request_id': id, 'expected_credential_revision': store.status()['credential_revision'], 'api_key': key}

def put(store, data):
    return store.put(data, endpoint=ENDPOINT, provider='openai', config_revision=CONFIG)

def test_actual_dpapi_acl_roundtrip_reopen_delete(tmp_path):
    events = []
    store = make(tmp_path, revoke=lambda *args: events.append(args) or True)
    missing = store.status()
    assert not missing['configured'] and not validation_errors('credential_status', missing)
    data = request(store)
    response = put(store, data)
    assert not validation_errors('setup_credential_response', response)
    status = response['credential']
    assert status['storage'] == 'protected_file' and status['configured']
    assert data['api_key'] not in json.dumps(response)
    for file in store.root.iterdir():
        assert data['api_key'].encode() not in file.read_bytes()
    reopened = make(tmp_path, revoke=lambda *args: events.append(args) or True)
    assert reopened.resolve(endpoint=ENDPOINT, provider='openai', credential_revision=status['credential_revision']) == data['api_key']
    # Actual DACL inspection: protected inheritance, owner-rights only, no Everyone/Users ACE.
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    get_security = advapi.GetFileSecurityW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    size = wintypes.DWORD()
    get_security(str(store.path), 4, None, 0, ctypes.byref(size))
    security = ctypes.create_string_buffer(size.value)
    assert get_security(str(store.path), 4, security, size, ctypes.byref(size))
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    output = wintypes.LPWSTR()
    assert convert(security, 1, 4, ctypes.byref(output), None)
    sddl = output.value
    kernel = ctypes.WinDLL('kernel32')
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree(output)
    assert sddl.startswith('D:P') and 'OW)' in sddl
    assert 'WD)' not in sddl and 'BU)' not in sddl
    deleted = reopened.delete({'revision': CONFIG, 'request_id': 'delete', 'expected_credential_revision': status['credential_revision']}, config_revision=CONFIG)
    assert not deleted['credential']['configured'] and len(events) == 1
    assert not make(tmp_path).status()['configured']
    with pytest.raises(CredentialError, match='CREDENTIAL'):
        reopened.resolve(endpoint=ENDPOINT, provider='openai', credential_revision=status['credential_revision'])
    assert put(reopened, data) == response  # Lost response replay is stable but cannot resurrect Key.
    assert not reopened.status()['configured']

def test_cas_idempotency_conflict_and_no_secret_errors(tmp_path):
    store = make(tmp_path, revoke=lambda *args: True)
    data = request(store)
    response = put(store, data)
    assert put(store, data) == response
    changed = {**data, 'api_key': 'another-synthetic-key'}
    with pytest.raises(CredentialError) as failure:
        put(store, changed)
    assert failure.value.code == 'REQUEST_CONFLICT' and changed['api_key'] not in str(failure.value)
    stale = {**data, 'request_id': 'different'}
    with pytest.raises(CredentialError) as failure:
        put(store, stale)
    assert failure.value.code == 'CREDENTIAL_CONFLICT'
    with pytest.raises(CredentialError) as failure:
        store.put(request(store, 'wrong-config'), endpoint=ENDPOINT, provider='openai', config_revision='b' * 64)
    assert failure.value.code == 'REQUEST_CONFLICT'

def test_cross_instance_cas_allows_one_update(tmp_path):
    first, second = make(tmp_path), make(tmp_path)
    data = request(first)
    def update(pair):
        store, id = pair
        try:
            return put(store, {**data, 'request_id': id})['ok']
        except CredentialError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(update, [(first, 'a'), (second, 'b')]))
    assert sorted(map(str, result)) == ['CREDENTIAL_CONFLICT', 'True']

@pytest.mark.parametrize('endpoint,provider', [('http://example.com/v1', 'custom_responses'), ('https://api.openai.com/v1?key=secret', 'openai'), ('https://127.0.0.1/v1', 'custom_responses'), ('https://user:password@example.com/v1', 'custom_responses'), ('https://other.example/v1', 'openai')])
def test_bad_endpoints_zero_write(tmp_path, endpoint, provider):
    store = make(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(CredentialError):
        store.put(request(store), endpoint=endpoint, provider=provider, config_revision=CONFIG)
    assert store.path.read_bytes() == before

def test_dns_private_mix_and_changed_endpoint_deny(tmp_path):
    store = make(tmp_path)
    response = put(store, request(store))
    version = response['credential']['credential_revision']
    with pytest.raises(CredentialError) as failure:
        store.resolve(endpoint='https://example.com/v1', provider='custom_responses', credential_revision=version)
    assert failure.value.code == 'CREDENTIAL_CONFLICT'
    store.resolver = lambda *args, **kwargs: resolver(*args, **kwargs) + [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    with pytest.raises(CredentialError) as failure:
        store.resolve(endpoint=ENDPOINT, provider='openai', credential_revision=version)
    assert failure.value.code == 'ENDPOINT_NOT_APPROVED'

def test_corruption_fails_closed_no_reset(tmp_path):
    store = make(tmp_path)
    put(store, request(store))
    store.path.write_bytes(b'corrupt cipher')
    with pytest.raises(CredentialError) as failure:
        make(tmp_path)
    assert failure.value.code == 'CREDENTIAL_UNAVAILABLE'
    assert store.path.read_bytes() == b'corrupt cipher'

def test_delete_revoke_failure_is_durable_and_retryable(tmp_path):
    store = make(tmp_path, revoke=lambda *args: False)
    response = put(store, request(store))
    deletion = {'revision': CONFIG, 'request_id': 'delete', 'expected_credential_revision': response['credential']['credential_revision']}
    with pytest.raises(CredentialError) as failure:
        store.delete(deletion, config_revision=CONFIG)
    assert failure.value.code == 'CLEANUP_FAILED'
    assert not make(tmp_path).status()['configured']
    store.revoke = lambda *args: True
    deleted = store.delete(deletion, config_revision=CONFIG)
    assert not deleted['credential']['configured']

def test_revocation_callback_runs_outside_credential_lock(tmp_path, monkeypatch):
    import local_agents.credentials as module
    store, observer = make(tmp_path), make(tmp_path)
    response = put(store, request(store))
    original_lock = module._file_lock
    monkeypatch.setattr(module, '_file_lock', lambda path: original_lock(path, timeout=0.05))
    # Trusted consumer can read latest tombstone; credential→scheduler lock inversion is avoided.
    store.revoke = lambda *args: observer.status()['configured'] is False
    result = store.delete({'revision': CONFIG, 'request_id': 'consumer-delete', 'expected_credential_revision': response['credential']['credential_revision']}, config_revision=CONFIG)
    assert not result['credential']['configured']

def test_save_failure_cannot_overwrite_later_revision(tmp_path, monkeypatch):
    import local_agents.credentials as module
    store = make(tmp_path, revoke=lambda *args: True)
    first = put(store, request(store))
    original = store.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('synthetic secret must never surface')))
        with pytest.raises(CredentialError) as failure:
            put(store, request(store, 'failed', 'failed-synthetic-key'))
        assert failure.value.code == 'CREDENTIAL_UNAVAILABLE'
    assert store.path.read_bytes() == original
    second = put(store, request(store, 'later', 'later-synthetic-key'))
    assert second['credential']['credential_revision'] != first['credential']['credential_revision']
    assert store.resolve(endpoint=ENDPOINT, provider='openai', credential_revision=second['credential']['credential_revision']) == 'later-synthetic-key'

@pytest.mark.parametrize('patch', [{'api_key': None}, {'api_key': True}, {'request_id': False}, {'password': 'synthetic'}, {'expected_credential_revision': None}])
def test_strict_request_rejects_without_secret_exception_or_writes(tmp_path, patch):
    store = make(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(CredentialError) as failure:
        put(store, {**request(store), **patch})
    assert failure.value.code == 'INVALID_ARGUMENT' and store.path.read_bytes() == before

def test_status_has_no_dns_or_secret_resolution(tmp_path):
    store = make(tmp_path)
    put(store, request(store))
    store.resolver = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('status did DNS'))
    status = store.status(endpoint='https://different.example/v1')
    assert status['status'] == 'endpoint_mismatch'
    assert not validation_errors('credential_status', status)

@pytest.mark.parametrize('dns_mode', ['offline', 'private'])
def test_key_save_and_replay_are_offline_but_delivery_requires_public_dns(tmp_path, dns_mode):
    store = make(tmp_path)
    calls = []
    def unavailable_dns(*args, **kwargs):
        calls.append(True)
        if dns_mode == 'offline':
            raise OSError('synthetic DNS unavailable')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.74', 443))]
    store.resolver = unavailable_dns
    data = request(store)
    accepted = put(store, data)
    before = store.path.read_bytes()
    assert put(store, data) == accepted
    assert store.path.read_bytes() == before and calls == []
    assert not validation_errors('setup_credential_response', accepted)
    with pytest.raises(CredentialError) as failure:
        store.resolve(endpoint=ENDPOINT, provider='openai', credential_revision=accepted['credential']['credential_revision'])
    assert failure.value.code == 'ENDPOINT_NOT_APPROVED'
    assert data['api_key'] not in str(failure.value)
    assert calls == [True]

def test_unselected_auth_context_uses_frozen_error_code(tmp_path):
    from local_agents.auth_context import resolve_auth_context
    from local_agents.config import default_agent_config
    agent = default_agent_config()
    agent['enabled'] = agent['codex']['enabled'] = True
    agent['profiles']['cheap_reader']['model'] = 'fixture-model'
    with pytest.raises(CredentialError) as failure:
        resolve_auth_context(agent, make(tmp_path))
    assert failure.value.code == 'AUTH_MODE_UNSELECTED'

def test_two_real_python_processes_share_credential_cas(tmp_path):
    import sys
    store = make(tmp_path)
    version = store.status()['credential_revision']
    script = '''import sys,socket
from local_agents.credentials import CredentialStore,CredentialError
s=CredentialStore(sys.argv[1],resolver=lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
try:
 s.put({'revision':'a'*64,'request_id':sys.argv[2],'expected_credential_revision':sys.argv[3],'api_key':'synthetic-child-key'},endpoint='https://api.openai.com/v1',provider='openai',config_revision='a'*64)
 print('accepted')
except CredentialError as e: print(e.code)
'''
    def child(id):
        result = subprocess.run([sys.executable, '-B', '-c', script, str(store.root), id, version], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(child, ['first-child', 'second-child']))
    assert sorted(outcomes) == ['CREDENTIAL_CONFLICT', 'accepted']

def test_auth_context_and_child_environment_are_explicit_and_nonsecret(tmp_path):
    from local_agents.auth_context import resolve_auth_context, child_environment
    from local_agents.config import normalize_agent_config
    store = make(tmp_path)
    put(store, request(store))
    agent = normalize_agent_config({'enabled': True, 'codex': {'enabled': True, 'auth_mode': 'api_key'}, 'profiles': {'cheap_reader': {'model': 'fixture-model'}}})
    context = resolve_auth_context(agent, store)
    assert context.auth_mode == 'api_key' and context.billing_source == 'openai_api'
    assert context.effective_auth_verified is False
    assert 'synthetic-not-a-real-api-key' not in repr(context)
    parent = {'PATH': os.environ['PATH'], 'SystemRoot': os.environ['SystemRoot'], 'OPENAI_API_KEY': 'unselected-synthetic-key', 'OPENAI_BASE_URL': 'https://bad.example', 'CODEX_HOME': 'shared-parent', 'CUSTOM_API_KEY': 'custom-conflict', 'CODEX_AUTH_TOKEN': 'fixture-token'}
    before = deepcopy(parent)
    env = child_environment(context, parent_env=parent, managed_home_root=tmp_path / 'managed', credential_store=store)
    assert env['OPENAI_API_KEY'] == 'synthetic-not-a-real-api-key'
    assert 'OPENAI_BASE_URL' not in env and 'CUSTOM_API_KEY' not in env and 'CODEX_AUTH_TOKEN' not in env
    assert env['CODEX_HOME'] != parent['CODEX_HOME'] and parent == before
    again = child_environment(context, parent_env=parent, managed_home_root=tmp_path / 'managed', credential_store=store)
    assert again['CODEX_HOME'] != env['CODEX_HOME']  # A fresh server-owned home per run.
    agent['codex']['auth_mode'] = 'chatgpt'
    with pytest.raises(CredentialError) as failure:
        resolve_auth_context(agent, store, login_method='api_key', login_context_id='fixture-account', login_revision='observed-1')
    assert failure.value.code == 'AUTH_MODE_MISMATCH'
    with pytest.raises(CredentialError):
        resolve_auth_context(agent, store)  # No inferred identity from parent env or a catalog.
    chatgpt = resolve_auth_context(agent, store, login_method='chatgpt', login_context_id='fixture-account', login_revision='observed-1')
    shared = tmp_path / 'existing-shared-home'
    shared.mkdir()
    marker = shared / 'config.toml'
    marker.write_text('synthetic config, not credentials')
    original = marker.read_bytes()
    env = child_environment(chatgpt, parent_env=parent, shared_home=shared)
    assert env['CODEX_HOME'] == str(shared) and 'OPENAI_API_KEY' not in env
    assert marker.read_bytes() == original and parent == before
    agent['enabled'] = False
    with pytest.raises(CredentialError) as failure:
        resolve_auth_context(agent, store)
    assert failure.value.code == 'CAPABILITY_DISABLED'

def test_actual_scheduler_sqlite_delete_callback_seam(tmp_path):
    import importlib.util
    fixture_spec = importlib.util.spec_from_file_location('scheduler_fixture', Path(__file__).parents[1] / 'unit/test_agent_scheduler.py')
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    scheduler, tasks, executor = fixture.make(tmp_path / 'tasks', max_running=1)
    credentials = make(tmp_path)
    response = put(credentials, request(credentials))
    binding = credentials.binding()
    selection = fixture.selection('api_key')
    selection['execution_context'].update({k: binding[k] for k in ('credential_context_id', 'credential_revision')})
    accepted = []
    for i in (1, 2):
        accepted.append(scheduler.submit(owner_id='owner', operation='project_context', request={'project_id': f'project-{i}', 'request_id': f'task-{i}'}, selection=selection))
    scheduler.dispatch()
    def revoke(context, revision):
        affected = [t for t in tasks.list_tasks(statuses={'queued', 'running', 'cancelling'}) if t['execution_context']['credential_context_id'] == context and t['execution_context']['credential_revision'] == revision]
        for task in affected:
            scheduler.cancel(task['owner_id'], task['task_id'])
        return all(tasks.get_task(t['owner_id'], t['task_id'])['status'] == 'cancelled' for t in affected)
    credentials.revoke = revoke
    deletion = {'revision': CONFIG, 'request_id': 'revoke-delete', 'expected_credential_revision': response['credential']['credential_revision']}
    with pytest.raises(CredentialError, match='CLEANUP_FAILED'):
        credentials.delete(deletion, config_revision=CONFIG)
    statuses = [tasks.get_task('owner', t.value['task_id'])['status'] for t in accepted]
    assert sorted(statuses) == ['cancelled', 'cancelling']
    executor.cleaned.add(accepted[0].value['task_id'])
    scheduler.finish('owner', accepted[0].value['task_id'], 'failed')
    assert not credentials.delete(deletion, config_revision=CONFIG)['credential']['configured']
