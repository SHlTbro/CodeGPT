"""Real task/report components and authenticated candidate; no model fixture CLI."""
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from config import dump_toml, load_config, config_revision
from local_agents.reports import SECTION_IDS
from local_agents.snapshot import capture_snapshot, read_snapshot_evidence
from local_agents.store import StoreError


def api():
    assert importlib.util.find_spec('local_agents.service'), 'candidate task service missing'
    return importlib.import_module('local_agents.service')


@pytest.fixture
def service(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'main.py').write_text('value = 1\n')
    cfg=tmp_path/'config.toml'
    cfg.write_text(dump_toml({'projects':[{'id':'p','name':'Owned','path':str(source),
        'agent':{'context_enabled':True}}], 'agent':{'enabled':True,
        'codex':{'enabled':True,'auth_mode':'chatgpt'},
        'profiles':{'cheap_reader':{'model':'manual-unverified-model','reasoning_effort':'','service_tier':'standard'}}}}),encoding='utf-8')
    from local_agents.access import owner_subject
    candidate=api().TaskService(cfg,tmp_path/'state',owner_id=owner_subject(tmp_path/'owner.json'))
    try:yield candidate,cfg
    finally:candidate.close()


def published(candidate):
    project=candidate.config().projects[0]
    options=candidate.snapshot_options(project)
    snapshot=capture_snapshot(project,**options)
    selection=candidate.selection(project,snapshot,'cheap_reader')
    # Explicit test-owned persisted material, not a fake model or public bypass.
    candidate.scheduler.set_enabled(True)  # Only owned test material admission; no dispatch.
    task=candidate.scheduler.submit(owner_id=candidate.owner_id,operation='project_context',
        request={'project_id':'p','request_id':'owned-report-material'},selection=selection).value
    candidate.scheduler.cancel(candidate.owner_id,task['task_id'])
    candidate.scheduler.set_enabled(False)
    evidence=read_snapshot_evidence(project,snapshot,'main.py',line_start=1,line_end=1)
    evidence={k:evidence[k] for k in ('evidence_id','path','line_start','line_end','symbol','content_sha256','snapshot_id','excerpt','validation')}
    report=candidate.reports.publish(candidate.owner_id,task['task_id'],project,snapshot,snapshot_options=options,
        permission_fingerprint=candidate.permission_fingerprint(project),summary='Owned persisted source evidence; model not run.',
        sections={k:'fixture-only '+('text '*1200) for k in SECTION_IDS if k not in {'summary','evidence','coverage','unknowns'}},
        claims=[],evidence=[evidence],unknowns=[{'question':'Model investigation','reason':'No model request', 'next_step':'Prove execution boundary first'}])
    return task,report


def test_cold_context_refuses_before_acceptance_and_keeps_actual_selection(service,caplog):
    caplog.set_level('WARNING',logger='codegpt.task')
    candidate,_=service
    response=candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'cold'})
    assert response['error']['code']=='CAPABILITY_UNAVAILABLE'
    assert 'task_gate operation=project_context_request code=CAPABILITY_UNAVAILABLE' in caplog.text
    assert candidate.owner_id not in caplog.text and str(candidate.config_path) not in caplog.text
    assert candidate.store.list_tasks()==[] and 'task_id' not in response
    status=candidate.status()
    assert status['backend']['mode']=='app-server'
    assert status['execution']['inference_attempts']==0 and status['execution']['inference_completed']==0
    assert status['execution']['actual_model'] is None and status['execution']['usage'] is None


def test_real_report_poll_cache_idempotency_cursor_and_expiry_without_inference(service):
    candidate,_=service
    task,report=published(candidate)
    first=candidate.task_poll(candidate.owner_id,{'report_id':report['report_id'],'section':'navigation','max_chars':4096})
    assert first['kind']=='report' and first['report']['verification']=='not_run'
    cursor=first['pagination']['next_cursor'];assert cursor
    again=candidate.task_poll(candidate.owner_id,{'report_id':report['report_id'],'cursor':cursor})
    repeated=candidate.task_poll(candidate.owner_id,{'report_id':report['report_id'],'cursor':cursor})
    assert again['page']==repeated['page'] and again['pagination']==repeated['pagination']
    cache=candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'cached'})
    assert cache['kind']=='report' and cache['report']['report_id']==report['report_id']
    assert candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'cached'})['report']['report_id']==report['report_id']
    assert len(candidate.store.list_tasks())==1
    candidate.reports.expire(candidate.owner_id,report['report_id'])
    expired=candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'cached'})
    assert expired['error']['code']=='RESULT_EXPIRED' and len(candidate.store.list_tasks())==1
    assert candidate.status()['execution']['inference_attempts']==0


def test_revoked_project_or_wrong_owner_cannot_poll_cache_or_cancel(service):
    candidate,cfg=service
    task,report=published(candidate)
    assert candidate.task_poll('different-owner',{'report_id':report['report_id']})['error']['code']=='POLICY_DENIED'
    assert candidate.task_cancel('different-owner',{'task_id':task['task_id']})['error']['code']=='POLICY_DENIED'
    data=__import__('tomllib').loads(cfg.read_text(encoding='utf-8'))
    data['projects']=[]  # Actual project access revocation, not just disabling new work.
    cfg.write_text(dump_toml(data),encoding='utf-8')
    assert candidate.task_poll(candidate.owner_id,{'report_id':report['report_id']})['error']['code']=='POLICY_DENIED'
    assert candidate.task_cancel(candidate.owner_id,{'task_id':task['task_id']})['cancel_requested'] is False
    assert candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'cached'})['error']['code']=='POLICY_DENIED'


@pytest.mark.parametrize('payload',[{'project_id':'p','request_id':'x','command':'whoami'},
    {'project_id':'p','request_id':'x','owner_id':'owned-candidate-user'}, {'project_id':'p','request_id':'x','max_chars':True}])
def test_raw_unknown_fields_rejected_without_inference(service,payload):
    candidate,_=service
    assert candidate.project_context(candidate.owner_id,payload)['error']['code']=='INVALID_ARGUMENT'
    assert candidate.store.list_tasks()==[]


def test_real_state_inside_project_is_excluded(service):
    candidate,cfg=service
    project=candidate.config().projects[0]
    inside=api().TaskService(cfg,project.path/'private-state',owner_id=candidate.owner_id)
    try:
        snapshot=capture_snapshot(project,**inside.snapshot_options(project))
        assert not any(path.startswith('private-state/') for path in snapshot['files'])
        assert 'main.py' in snapshot['files']
    finally:inside.close()


def test_internal_state_exclusion_is_relative_to_current_project(tmp_path):
    first=tmp_path/'first';second=tmp_path/'second'
    first.mkdir();second.mkdir();(second/'state').mkdir()
    (first/'main.py').write_text('first = 1\n')
    (second/'state'/'legitimate.py').write_text('source_value = 2\n')
    cfg=tmp_path/'config.toml'
    cfg.write_text(dump_toml({'projects':[
        {'id':'first','name':'First','path':str(first)},
        {'id':'second','name':'Second','path':str(second)}]}),encoding='utf-8')
    candidate=api().TaskService(cfg,first/'state',owner_id='owned-project-scope')
    try:
        first_project,second_project=candidate.config().projects
        first_snapshot=capture_snapshot(first_project,**candidate.snapshot_options(first_project))
        second_snapshot=capture_snapshot(second_project,**candidate.snapshot_options(second_project))
        assert not any(path.startswith('state/') for path in first_snapshot['files'])
        assert 'state/legitimate.py' in second_snapshot['files']
    finally:candidate.close()


def test_state_at_project_root_refuses_snapshot_scope(service):
    candidate,cfg=service
    project=candidate.config().projects[0]
    inside=api().TaskService(cfg,project.path,owner_id=candidate.owner_id)
    try:
        with pytest.raises(StoreError) as failure:
            capture_snapshot(project,**inside.snapshot_options(project))
        assert failure.value.code=='POLICY_DENIED'
    finally:inside.close()


def test_second_instance_cannot_recover_or_disable_existing_queue(service):
    candidate,cfg=service
    project=candidate.config().projects[0]
    snapshot=capture_snapshot(project,**candidate.snapshot_options(project))
    candidate.scheduler.set_enabled(True)  # Own test queue, never dispatch.
    accepted=candidate.scheduler.submit(owner_id=candidate.owner_id,operation='project_context',
        request={'project_id':'p','request_id':'queue-owned'},selection=candidate.selection(project,snapshot,'cheap_reader'))
    second=None
    try:
        with pytest.raises(StoreError) as failure:
            second=api().TaskService(cfg,candidate.state_dir,owner_id=candidate.owner_id)
        assert failure.value.code=='INSTANCE_ACTIVE'
        assert candidate.store.get_task(candidate.owner_id,accepted.value['task_id'])['status']=='queued'
        assert candidate.store.get_scheduler_control()['enabled'] is True
    finally:
        if second is not None:second.close()


def test_off_unselected_needs_no_identity_or_backend_probe(service):
    candidate,cfg=service
    data=__import__('tomllib').loads(cfg.read_text(encoding='utf-8'))
    data['agent']['enabled']=False;data['agent']['codex']['auth_mode']='unselected'
    cfg.write_text(dump_toml(data),encoding='utf-8')
    response=candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'off'})
    assert response['error']['code']=='CAPABILITY_DISABLED' and candidate.store.list_tasks()==[]


def test_opt_in_off_keeps_owned_history_and_managed_cancel(service):
    candidate,cfg=service
    task,report=published(candidate)
    data=__import__('tomllib').loads(cfg.read_text(encoding='utf-8'))
    data['agent']['enabled']=False;data['projects'][0]['agent']['context_enabled']=False
    cfg.write_text(dump_toml(data),encoding='utf-8')
    assert candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'disabled'})['error']['code']=='CAPABILITY_DISABLED'
    assert candidate.task_poll(candidate.owner_id,{'report_id':report['report_id']})['kind']=='report'
    assert candidate.task_cancel(candidate.owner_id,{'task_id':task['task_id']})['cancel_requested'] is False


def test_same_real_oauth_entry_protects_ping_three_tools_and_revocation(service):
    import asyncio,base64,hashlib,secrets,socket,threading,time
    from urllib.parse import parse_qs,urlparse
    import httpx2 as httpx
    import uvicorn
    from fastmcp import Client
    from local_agents.access import LocalApprovalProvider
    from local_agents.mcp_entry import build_agent_server
    candidate,cfg=service
    sockets=[socket.socket(),socket.socket()]
    for sock in sockets:sock.bind(('127.0.0.1',0));sock.listen(128)
    public,owner=[f'http://127.0.0.1:{sock.getsockname()[1]}' for sock in sockets]
    callback='https://chatgpt.com/connector_platform_oauth_redirect'
    provider=LocalApprovalProvider(base_url=public,owner_base_url=owner,owner_subject=candidate.owner_id,
        allowed_redirects=[callback],task_access=True)
    apps=[build_agent_server(provider,candidate).http_app(path='/mcp',host_origin_protection=True),provider.owner_app()]
    servers=[uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False)) for app in apps]
    threads=[threading.Thread(target=s.run,kwargs={'sockets':[sock]},daemon=True) for s,sock in zip(servers,sockets)]
    try:
        for thread in threads:thread.start()
        end=time.monotonic()+10
        while not all(s.started for s in servers) and time.monotonic()<end:time.sleep(.02)
        assert all(s.started for s in servers)
        assert httpx.post(public+'/mcp',json={},headers={'X-Setup-Probe':'1'}).status_code==401
        assert httpx.get(public+'/owner-api/session').status_code==404
        scopes='test:ping codegpt:context'
        client=httpx.post(public+'/register',json={'redirect_uris':[callback],'client_name':'Owned test browser',
            'token_endpoint_auth_method':'none','grant_types':['authorization_code'],'response_types':['code'],'scope':scopes}).json()
        verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        auth=httpx.get(public+'/authorize',params={'response_type':'code','client_id':client['client_id'],
            'redirect_uri':callback,'scope':scopes,'state':'owned-state','resource':public+'/mcp',
            'code_challenge':challenge,'code_challenge_method':'S256'},follow_redirects=False)
        assert auth.status_code==302
        with httpx.Client(timeout=4) as browser:
            session=browser.get(owner+'/owner-api/session').json()
            headers={'Origin':owner,'X-Setup-Token':session['setup_token']}
            pending=browser.post(owner+'/owner-api/pending',json={},headers=headers).json()['pending'][0]
            assert set(pending['scopes'])=={'test:ping','codegpt:context'}
            assert browser.post(owner+'/owner-api/approve',headers=headers,json={'transaction_id':pending['transaction_id'],
                'action_nonce':pending['action_nonce'],'allow':True}).status_code==200
        redirect=httpx.get(auth.headers['location'],follow_redirects=False)
        params=parse_qs(urlparse(redirect.headers['location']).query)
        assert params['iss']==[public+'/']
        exchange=httpx.post(public+'/token',data={'grant_type':'authorization_code','client_id':client['client_id'],
            'code':params['code'][0],'code_verifier':verifier,'redirect_uri':callback,'resource':public+'/mcp'})
        assert exchange.status_code==200
        access=exchange.json()['access_token']
        task,report=published(candidate)
        async def checks():
            async with Client(public+'/mcp',auth=access) as caller:
                tools=await caller.list_tools()
                assert {t.name for t in tools}=={'auth_test_ping','project_context','task_poll','task_cancel'}
                assert max(len(json.dumps(t.inputSchema)) for t in tools)<5000
                ping=(await caller.call_tool('auth_test_ping',{})).data
                assert ping['ok'] and not ping['project_reading_enabled']
                cached=(await caller.call_tool('project_context',{'project_id':'p','request_id':'http-cached'})).data
                assert cached['kind']=='report' and cached['report']['verification']=='not_run'
                polled=(await caller.call_tool('task_poll',{'report_id':report['report_id']})).data
                assert polled['report']['report_id']==report['report_id']
                cancelled=(await caller.call_tool('task_cancel',{'task_id':task['task_id']})).data
                assert cancelled['task']['status']=='cancelled' and not cancelled['cancel_requested']
                # Explicit public nonce flow issued the grant; revoke real in-memory token.
                await provider.revoke_token(await provider.load_access_token(access))
                with pytest.raises(Exception):await caller.call_tool('task_poll',{'report_id':report['report_id']})
        asyncio.run(checks())
        assert candidate.status()['execution']['inference_attempts']==0
    finally:
        for server in servers:server.should_exit=True
        for thread in threads:thread.join(10)
        for sock in sockets:sock.close()


def test_unregistered_project_has_safe_reason_but_no_scope_expansion(service):
    candidate,_=service
    response=candidate.project_context(candidate.owner_id,{'project_id':'unregistered','request_id':'reason-test'})
    assert response['error']['code']=='POLICY_DENIED'
    assert 'PROJECT_NOT_IN_CANDIDATE_SCOPE' in response['error']['hint']
    assert str(candidate.config().projects[0].path) not in json.dumps(response)
    assert candidate.store.list_tasks()==[] and candidate.status()['execution']['inference_attempts']==0
    other=candidate.project_context('wrong-owner',{'project_id':'unregistered','request_id':'wrong-owner'})
    assert 'PROJECT_NOT_IN_CANDIDATE_SCOPE' not in other['error']['hint']
