"""Report component behavior on owned files/SQLite, never a model/CLI."""
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from local_agents.contracts import validation_errors
from local_agents.snapshot import capture_snapshot, read_snapshot_evidence
from local_agents.store import Store, StoreError
from projects import Project
from security import DEFAULT_EXCLUDED_DIRS, DEFAULT_SENSITIVE_PATTERNS, MAX_FILE_SIZE_BYTES


def api():
    assert importlib.util.find_spec('local_agents.reports') is not None, 'public report component missing'
    return importlib.import_module('local_agents.reports')


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def selection(model='fixture-model', fingerprint='b' * 64):
    return {'profile': 'reader', 'requested_model': model, 'config_revision': 'a' * 64,
        'backend_version': None, 'execution_context': {'auth_mode': 'chatgpt',
        'billing_source': 'chatgpt_plan', 'provider': 'openai', 'endpoint': None,
        'credential_context_id': 'fixture-owner', 'credential_revision': 'cred-1',
        'requested_reasoning_effort': None, 'resolved_reasoning_effort': None,
        'requested_service_tier': 'standard', 'resolved_service_tier': None,
        'selection_fingerprint': fingerprint, 'effective_auth_verified': False}}


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / '中文 source'
    source.mkdir()
    (source / 'main.py').write_bytes('第一行\nvalue = 1\n'.encode('utf-8'))
    project = Project('p', 'P', source)
    options = {'paths': ('.',), 'internal_paths': (), 'sensitive_patterns': DEFAULT_SENSITIVE_PATTERNS,
               'excluded_dirs': DEFAULT_EXCLUDED_DIRS, 'max_file_size_bytes': MAX_FILE_SIZE_BYTES}
    snapshot = capture_snapshot(project, **options)
    store = Store(tmp_path / 'private-state/tasks.sqlite', tmp_path / 'private-state/results')
    task = store.accept(owner_id='owner', operation='project_context',
        request={'project_id': 'p', 'request_id': 'req-1'}, selection=selection()).value
    permission = 'c' * 64
    allowed = {'value': True}
    def authorize(owner, project_id, scope, fingerprint):
        return allowed['value'] and owner == 'owner' and project_id == 'p' and scope == ['.']
    repository = (api().Reports(store, authorize=authorize, cursor_key=b'fixture-cursor-key-32-bytes-long!',
                               max_body_bytes=1_000_000, max_total_bytes=4_000_000)
                  if importlib.util.find_spec('local_agents.reports') is not None else None)
    proposed = read_snapshot_evidence(project, snapshot, 'main.py', line_start=2, line_end=2)
    proposed = {k: proposed[k] for k in ('evidence_id', 'path', 'line_start', 'line_end', 'symbol',
                                        'content_sha256', 'snapshot_id', 'excerpt', 'validation')}
    return repository, store, task, project, snapshot, options, permission, proposed, allowed


def publish(fixture, **changes):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    values = {'summary': '短摘要', 'sections': {'overview': 'Overview', 'entries': 'Entries',
        'modules': 'Modules', 'flows': 'Flows', 'configuration': 'Configuration',
        'navigation': 'Navigation', 'risks': 'Risks'},
        'claims': [{'claim_id': 'claim-1', 'text': 'value is one', 'classification': 'fact',
                    'evidence_ids': [proposed['evidence_id']]}], 'evidence': [proposed], 'unknowns': []}
    values.update(changes)
    return repository.publish('owner', task['task_id'], project, snapshot,
        snapshot_options=options, permission_fingerprint=permission, **values)


def test_overview_persists_complete_schema_and_reopens_with_frozen_identity(fixture):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    assert repository is not None, 'public report component missing after real SQLite/source setup'
    report = publish(fixture)
    assert validation_errors('report', report) == []
    assert report['execution_context'] == task['execution_context']
    assert 'requested_model' not in report['execution_context']
    assert {s['id'] for s in report['sections']} == set(api().SECTION_IDS)
    document = repository.read_document('owner', report['report_id'])
    assert validation_errors('persisted_report', document) == []
    assert document['evidence'][0]['validation'] == 'verified'
    assert document['evidence'][0]['excerpt'] == 'value = 1\n'
    assert document['coverage']['read_files'] is None  # Snapshot hashing is not model reading coverage.
    reopened = api().Reports(Store(store.database_path, store.results_dir),
        authorize=repository.authorize, cursor_key=b'fixture-cursor-key-32-bytes-long!')
    assert reopened.read_document('owner', report['report_id']) == document
    assert publish(fixture) == report
    with pytest.raises(StoreError) as error:
        publish(fixture, summary='changed')
    assert error.value.code == 'REQUEST_CONFLICT'


def test_question_can_publish_without_prior_overview(fixture):
    repository, store, _, project, snapshot, options, permission, proposed, allowed = fixture
    task = store.accept(owner_id='owner', operation='project_context', selection=selection(),
        request={'project_id': 'p', 'request_id': 'question-1', 'intent': 'question', 'question': 'Where is value?'}).value
    report = repository.publish('owner', task['task_id'], project, snapshot, snapshot_options=options,
        permission_fingerprint=permission, summary='直接回答', sections={'navigation': 'main.py:2'},
        evidence=[proposed], claims=[], unknowns=[])
    ids = {s['id'] for s in report['sections']}
    assert 'navigation' in ids and 'overview' not in ids
    assert {'summary', 'evidence', 'coverage', 'unknowns'} <= ids


@pytest.mark.parametrize('corruption', ['hash', 'excerpt', 'lines', 'snapshot'])
def test_forged_evidence_is_invalid_and_fact_becomes_unknown(fixture, corruption):
    proposed = deepcopy(fixture[7])
    if corruption == 'hash': proposed['content_sha256'] = 'd' * 64
    if corruption == 'excerpt': proposed['excerpt'] = 'FORGED QUOTE'
    if corruption == 'lines': proposed['line_start'] = proposed['line_end'] = 99
    if corruption == 'snapshot': proposed['snapshot_id'] = 'other-snapshot'
    report = publish(fixture, evidence=[proposed])
    document = fixture[0].read_document('owner', report['report_id'])
    assert document['evidence'][0]['validation'] == 'invalid'
    assert document['evidence'][0]['excerpt'] == ''
    assert document['claims'][0]['classification'] == 'unknown'
    assert document['unknowns'] and report['completion'] == 'partial'
    assert validation_errors('persisted_report', document) == []


def test_unsafe_or_excluded_evidence_cannot_be_published(fixture):
    proposed = deepcopy(fixture[7])
    proposed['path'] = '../foreign.txt'
    with pytest.raises(StoreError):
        publish(fixture, evidence=[proposed])
    proposed['path'] = '.env'
    (fixture[3].path / '.env').write_bytes(b'synthetic excluded data')
    with pytest.raises(StoreError) as error:
        publish(fixture, evidence=[proposed])
    assert error.value.code == 'POLICY_DENIED'
    assert fixture[1].get_task('owner', fixture[2]['task_id'])['report_id'] is None


def test_source_change_is_stale_and_invalid_diagnostic_is_not_final_evidence(fixture):
    (fixture[3].path / 'main.py').write_bytes(b'value = 9\n')
    report = publish(fixture)
    assert report['freshness']['state'] in ('stale', 'partial')
    document = fixture[0].read_document('owner', report['report_id'])
    assert document['evidence'][0]['validation'] == 'invalid'
    assert set(document['evidence'][0]) == {'evidence_id', 'path', 'line_start', 'line_end',
        'symbol', 'content_sha256', 'snapshot_id', 'excerpt', 'validation'}


@pytest.mark.parametrize('budget', [2048, 4096, 32000])
def test_full_unicode_json_budget_and_stable_page_reassembly(fixture, budget):
    repository, store, _, project, snapshot, options, permission, proposed, allowed = fixture
    task = store.accept(owner_id='owner', operation='project_context', selection=selection(),
        request={'project_id': 'p', 'request_id': 'page-1', 'intent': 'question', 'question': 'Navigation?'}).value
    text = ('中文🙂\n"\\\x00' * 800)
    report = repository.publish('owner', task['task_id'], project, snapshot, snapshot_options=options,
        permission_fingerprint=permission, summary='导航', sections={'navigation': text})
    try:
        response = repository.page('owner', report['report_id'], section='navigation', max_chars=budget,
                                   current_snapshot=snapshot)
    except StoreError as error:
        assert budget == 2048 and error.code == 'OUTPUT_BUDGET_TOO_SMALL'
        return
    parts = []
    cursor = None
    while True:
        assert response['response_chars'] == len(wire(response)) <= budget
        assert validation_errors('task_poll_response', response) == []
        assert 'content_ref' not in wire(response) and str(store.results_dir) not in wire(response)
        parts.append(response['page']['text'])
        cursor = response['pagination']['next_cursor']
        if cursor is None:
            break
        response = repository.page('owner', report['report_id'], cursor=cursor, current_snapshot=snapshot)
        changed = deepcopy(snapshot)
        changed['state'] = 'partial'
        replay = repository.page('owner', report['report_id'], cursor=cursor, current_snapshot=changed)
        assert replay['page'] == response['page']
        assert replay['pagination'] == response['pagination']
        assert replay['response_chars'] == len(wire(replay)) <= budget
    assert ''.join(parts) == text


def test_cursor_binding_and_current_acl_are_rechecked(fixture):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    report = publish(fixture, sections={'navigation': '🙂' * 20000})
    response = repository.page('owner', report['report_id'], section='navigation', max_chars=4096)
    cursor = response['pagination']['next_cursor']
    assert cursor
    for kwargs in ({'cursor': cursor[:-1] + ('A' if cursor[-1] != 'A' else 'B')},
                   {'cursor': cursor, 'section': 'summary'}, {'cursor': cursor, 'max_chars': 32000}):
        with pytest.raises(StoreError) as error:
            repository.page('owner', report['report_id'], **kwargs)
        assert error.value.code == 'INVALID_CURSOR'
    with pytest.raises(StoreError):
        repository.page('other-owner', report['report_id'], cursor=cursor)
    allowed['value'] = False
    with pytest.raises(StoreError) as error:
        repository.page('owner', report['report_id'], cursor=cursor)
    assert error.value.code == 'POLICY_DENIED'


def test_cache_binds_selection_permission_snapshot_and_survives_task_expiration(fixture):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    report = publish(fixture)
    assert repository.find_cached('owner', project, snapshot, selection(), permission) == report
    assert repository.find_cached('owner', project, snapshot, selection(fingerprint='d' * 64), permission) is None
    assert repository.find_cached('owner', project, snapshot, selection(), 'e' * 64) is None
    assert repository.find_cached('owner', project, snapshot, selection(), None) is None
    terminal = store.get_task('owner', task['task_id'])
    terminal.update(status='succeeded', finished_at='2026-10-08T00:00:00Z', finish_reason='completed')
    store.replace_task('owner', terminal, expected_status='queued')
    store.expire_task('owner', task['task_id'])
    assert repository.find_cached('owner', project, snapshot, selection(), permission) == report
    allowed['value'] = False
    assert repository.find_cached('owner', project, snapshot, selection(), permission) is None


def test_expire_tombstone_precedes_unlink_and_same_request_never_relaunches(fixture, monkeypatch):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    report = publish(fixture)
    ref, digest = store.get_content_reference('owner', report['report_id'])
    target = store.results_dir / ref
    original_unlink = Path.unlink
    failed = [False]
    def failing_unlink(path, *args, **kwargs):
        if path == target and not failed[0]:
            failed[0] = True
            with sqlite3.connect(store.database_path) as connection:
                assert connection.execute('SELECT expired FROM reports').fetchone()[0] == 1
            raise PermissionError('injected cleanup failure')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', failing_unlink)
    with pytest.raises(StoreError) as error:
        repository.expire('owner', report['report_id'])
    assert error.value.code == 'CLEANUP_FAILED' and target.exists()
    with pytest.raises(StoreError) as error:
        repository.page('owner', report['report_id'])
    assert error.value.code == 'RESULT_EXPIRED'
    repository.expire('owner', report['report_id'])
    assert not target.exists()
    with pytest.raises(StoreError) as error:
        store.accept(owner_id='owner', operation='project_context',
            request={'project_id': 'p', 'request_id': 'req-1'}, selection=selection())
    assert error.value.code == 'RESULT_EXPIRED'
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM bindings').fetchone()[0] == 1


def test_atomic_body_precedes_sqlite_publication_and_fsync_failure_leaves_no_binding(fixture, monkeypatch):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    original_publish = store.publish_report
    def observing_publish(owner, task_id, metadata, *, content_ref, content_sha256):
        body = store.results_dir / content_ref
        assert body.is_file()
        assert hashlib.sha256(body.read_bytes()).hexdigest() == content_sha256
        assert not list(body.parent.glob('*.tmp'))
        return original_publish(owner, task_id, metadata, content_ref=content_ref, content_sha256=content_sha256)
    monkeypatch.setattr(store, 'publish_report', observing_publish)
    publish(fixture)
    second = store.accept(owner_id='owner', operation='project_context',
        request={'project_id': 'p', 'request_id': 'fsync-fail'}, selection=selection()).value
    def fsync_failure(fd):
        raise OSError('injected fsync failure')
    monkeypatch.setattr(api().os, 'fsync', fsync_failure)
    with pytest.raises(StoreError) as error:
        repository.publish('owner', second['task_id'], project, snapshot, snapshot_options=options,
            permission_fingerprint=permission, summary='失败', sections={'navigation': 'nav'})
    assert error.value.code == 'STORAGE_UNAVAILABLE'
    assert store.get_task('owner', second['task_id'])['report_id'] is None


def test_body_quota_rejects_without_publication(fixture):
    repository, store, task, project, snapshot, options, permission, proposed, allowed = fixture
    small = api().Reports(store, authorize=repository.authorize,
        cursor_key=b'fixture-cursor-key-32-bytes-long!', max_body_bytes=512, max_total_bytes=512)
    with pytest.raises(StoreError) as error:
        small.publish('owner', task['task_id'], project, snapshot, snapshot_options=options,
            permission_fingerprint=permission, summary='summary', sections={'navigation': 'large' * 1000})
    assert error.value.code == 'STORAGE_UNAVAILABLE'
    assert store.get_task('owner', task['task_id'])['report_id'] is None


def test_body_tampering_is_expired_instead_of_returning_unverified_page(fixture):
    report = publish(fixture)
    ref, _ = fixture[1].get_content_reference('owner', report['report_id'])
    (fixture[1].results_dir / ref).write_bytes(b'forged body')
    with pytest.raises(StoreError) as error:
        fixture[0].page('owner', report['report_id'])
    assert error.value.code == 'RESULT_EXPIRED'


def test_cache_checks_full_frozen_identity_even_if_fingerprint_is_reused(fixture):
    publish(fixture)
    changed = selection()
    changed['execution_context']['credential_revision'] = 'another-identity'
    assert fixture[0].find_cached('owner', fixture[3], fixture[4], changed, fixture[6]) is None


def test_cursor_cannot_be_repartitioned_by_lowered_service_budget(fixture):
    report = publish(fixture, sections={'navigation': '中文' * 10000})
    first = fixture[0].page('owner', report['report_id'], section='navigation', max_chars=4096)
    cursor = first['pagination']['next_cursor']
    assert cursor
    smaller = api().Reports(fixture[1], authorize=fixture[0].authorize,
        cursor_key=b'fixture-cursor-key-32-bytes-long!', max_response_chars=2048)
    with pytest.raises(StoreError) as error:
        smaller.page('owner', report['report_id'], cursor=cursor)
    assert error.value.code == 'INVALID_CURSOR'


def test_same_requested_budget_continues_after_service_clamp(fixture):
    report = publish(fixture, sections={'navigation': '中文' * 10000})
    limited = api().Reports(fixture[1], authorize=fixture[0].authorize,
        cursor_key=b'fixture-cursor-key-32-bytes-long!', max_response_chars=4096)
    first = limited.page('owner', report['report_id'], section='navigation', max_chars=12000)
    cursor = first['pagination']['next_cursor']
    explicit = limited.page('owner', report['report_id'], cursor=cursor, max_chars=12000)
    implicit = limited.page('owner', report['report_id'], cursor=cursor)
    assert explicit['page'] == implicit['page']
    assert explicit['pagination'] == implicit['pagination']
    assert explicit['response_chars'] <= 4096


@pytest.mark.parametrize('limit', [4096.5, True, '4096'])
def test_service_response_budget_requires_exact_integer(fixture, limit):
    with pytest.raises(StoreError) as error:
        api().Reports(fixture[1], authorize=fixture[0].authorize,
            cursor_key=b'fixture-cursor-key-32-bytes-long!', max_response_chars=limit)
    assert error.value.code == 'INVALID_ARGUMENT'


def test_publication_cannot_broaden_frozen_snapshot_policy(fixture, monkeypatch):
    repository, store, task, project, _, options, permission, _, _ = fixture
    (project.path / 'allowed').mkdir()
    (project.path / 'allowed/a.py').write_text('safe', encoding='utf-8')
    narrowed = {**options, 'paths': ('allowed',)}
    snapshot = capture_snapshot(project, **narrowed)
    repository.authorize = lambda *args: True
    original = api().capture_snapshot
    reads = []
    def observed(*args, **kwargs):
        reads.append(kwargs['paths'])
        return original(*args, **kwargs)
    monkeypatch.setattr(api(), 'capture_snapshot', observed)
    with pytest.raises(StoreError) as error:
        repository.publish('owner', task['task_id'], project, snapshot,
            snapshot_options=options, permission_fingerprint=permission,
            summary='scope test', sections={'navigation': 'nav'})
    assert error.value.code == 'POLICY_DENIED'
    assert reads == []


def test_two_instances_share_publication_quota_and_retention_is_bounded(fixture):
    repository, store, _, project, snapshot, options, permission, _, _ = fixture
    sample = publish(fixture)
    ref, _ = store.get_content_reference('owner', sample['report_id'])
    size = (store.results_dir / ref).stat().st_size
    repository.expire('owner', sample['report_id'])
    tasks = [store.accept(owner_id='owner', operation='project_context',
        request={'project_id': 'p', 'request_id': 'quota-' + str(i)}, selection=selection()).value
        for i in range(2)]
    barrier = threading.Barrier(2)
    def run(task):
        other = api().Reports(Store(store.database_path, store.results_dir),
            authorize=repository.authorize, cursor_key=b'fixture-cursor-key-32-bytes-long!',
            max_body_bytes=1_000_000, max_total_bytes=size + 1000)
        barrier.wait(timeout=5)
        try:
            return other.publish('owner', task['task_id'], project, snapshot,
                snapshot_options=options, permission_fingerprint=permission,
                summary='quota', sections={'navigation': 'nav'})
        except StoreError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, tasks))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count('STORAGE_UNAVAILABLE') == 1
    assert sum(path.stat().st_size for path in repository.root.iterdir()) <= size + 1000
    winner = next(result for result in results if isinstance(result, dict))
    assert repository.expire_before('owner', time.time() + 60, limit=1) == 1
    assert repository.expire_before('owner', time.time() + 60, limit=1) == 0
    with pytest.raises(StoreError) as error:
        repository.read_document('owner', winner['report_id'])
    assert error.value.code == 'RESULT_EXPIRED'
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM reports WHERE expired = 1').fetchone()[0] == 2
