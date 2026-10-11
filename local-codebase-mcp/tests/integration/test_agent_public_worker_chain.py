"""Test-private public-entry qualification: actual native transport, simulated model.

All patches are scoped to this owned TaskService; product capability stays closed.
"""
import os,sys,time,json
from pathlib import Path
import pytest
from local_agents.runner import RunnerError
pytestmark=pytest.mark.skipif(os.name!='nt',reason='native Windows Job proof')

@pytest.mark.parametrize('valid_report',[True,False,'cancel'])
def test_public_project_context_native_worker_report_chain(tmp_path,valid_report,monkeypatch):
    import importlib.util,subprocess,threading
    from config import dump_toml
    from local_agents.service import TaskService
    from local_agents.snapshot import capture_snapshot
    fixture=Path(__file__).parents[1]/'support/appserver_fake_provider.py'
    module=importlib.util.spec_from_file_location('worker_provider_fixture',fixture)
    provider_module=importlib.util.module_from_spec(module);module.loader.exec_module(provider_module)
    native=Path(os.environ.get('CODEGPT_TEST_NATIVE',str(Path(os.environ['APPDATA'])/'npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')))
    if not native.is_file():pytest.skip('pinned native binary not installed; no downloads')
    source=tmp_path/'source';source.mkdir();(source/'main.py').write_text('value = 1\n')
    (source/'.env').write_text('OWNED_SENSITIVE_CANARY')
    outside=tmp_path/'outside.txt';outside.write_text('OWNED_OUTSIDE_CANARY')
    os.link(outside,source/'hardlink.txt')
    home=tmp_path/'worker-home';home.mkdir()
    env={name:os.environ[name] for name in ('SystemRoot','WINDIR') if name in os.environ}
    env.update(PATH='C:/Windows/System32;C:/Windows',CODEX_HOME=str(home),USERPROFILE=str(home),TEMP=str(home),TMP=str(home))
    metadata=subprocess.run([str(native),'debug','models','--bundled'],cwd=home,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert metadata.returncode==0
    original=next(model for model in json.loads(metadata.stdout)['models'] if model['slug']=='gpt-6-luna')
    model=dict(original);model.update(slug='fixture-no-inference',display_name='SIMULATED transport fixture')
    catalog=home/'catalog.json';catalog.write_text(json.dumps({'models':[model]}),encoding='utf-8')
    report={'summary':'SIMULATED native transport qualification; no real model','sections':{'overview':'owned page text '*700},'claims':[],
            'unknowns':[{'question':'Actual model and isolation','reason':'Offline synthetic response only','next_step':'Close actual runtime and identity gates'}]}
    calls=[('task_read',{'path':'main.py'}),('task_read',{'path':'.env'}),('task_read',{'path':'../outside.txt'}),('task_read',{'path':'hardlink.txt'}),('exec_command',{})] if valid_report else None
    with provider_module.FakeResponsesProvider(calls=calls,completion_text=report if valid_report else 'not a report') as provider:
        entered=threading.Event();release=threading.Event()
        if valid_report=='cancel':
            original_events=provider.events
            def pause_owned_response(index,body):
                entered.set()
                if not release.wait(30):raise ValueError('owned cancellation pause expired')
                return original_events(index,body)
            provider.events=pause_owned_response
        (home/'config.toml').write_text(dump_toml({'model':'fixture-no-inference','model_provider':'offline_fixture',
            'model_catalog_json':str(catalog),'project_doc_max_bytes':0,'notify':[],'web_search':'disabled',
            'agents':{'enabled':False},'features':{'shell_tool':False,'hooks':False,'plugins':False,'apps':False,
              'multi_agent':False,'multi_agent_v2':False,'view_image':False,'code_mode':False,'js_repl':False},
            'skills':{'include_instructions':False},'model_providers':{'offline_fixture':{'name':'SIMULATED only',
              'base_url':provider.url,'wire_api':'responses','requires_openai_auth':False}}}),encoding='utf-8')
        cfg=tmp_path/'config.toml'
        cfg.write_text(dump_toml({'projects':[{'id':'p','name':'Owned fixture','path':str(source),'agent':{'context_enabled':True}}],
            'agent':{'enabled':True,'codex':{'enabled':True,'executable':str(native),'auth_mode':'chatgpt'},
                     'profiles':{'cheap_reader':{'model':'gpt-6-luna','reasoning_effort':'medium','service_tier':'standard'}}}}),encoding='utf-8')
        candidate=TaskService(cfg,tmp_path/'state',owner_id='owned-worker-fixture')
        try:
            project=candidate.config().projects[0];snapshot=capture_snapshot(project,**candidate.snapshot_options(project))
            assert '.env' not in snapshot['files']
            assert candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'public-refusal'})['error']['code']=='CAPABILITY_UNAVAILABLE'
            assert candidate.backend.status()['available'] is False
            assert candidate.store.get_scheduler_control()['enabled'] is False
            assert candidate.runner.gate({},None) is False
            with pytest.raises(RunnerError,match='CAPABILITY_UNAVAILABLE'):
                candidate.backend.launch({}, {})
            from local_agents.model_catalog import historical_bundled
            observed_option=historical_bundled({'models':[original]},'chatgpt',observed_cli_version='0.157.1')['models'][0]
            original_selection=candidate.backend.selection
            monkeypatch.setattr(candidate.backend,'selection',lambda cfg,owner,profile,snapshot,**kw:
                original_selection(cfg,owner,profile,snapshot,model_option=observed_option))
            original_status=candidate.backend.status
            monkeypatch.setattr(candidate.backend,'status',lambda:original_status() | {'available':True})
            candidate.scheduler.set_enabled(True)
            candidate.runner.resolver=lambda accepted,args:candidate.backend.prepare_fixture_worker(accepted,home=home,turn_input='Owned synthetic fixture transport qualification only')
            candidate.runner.gate=lambda accepted,spec:spec.argv==(str(native),'app-server','--stdio') and spec.cwd==str(home) and spec.env['CODEX_HOME']==str(home)
            request={'project_id':'p','request_id':'public-owned-fixture'}
            accepted=candidate.project_context(candidate.owner_id,request)
            assert accepted['kind']=='task',accepted
            task=accepted['task']
            assert candidate.project_context(candidate.owner_id,request)['task']['task_id']==task['task_id']
            if valid_report=='cancel':
                try:
                    assert entered.wait(10),'native never reached owned provider'
                    assert candidate.store.get_task(candidate.owner_id,task['task_id'])['status']=='running'
                    cancelled=candidate.task_cancel(candidate.owner_id,{'task_id':task['task_id']})
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        polled=candidate.task_poll(candidate.owner_id,{'task_id':task['task_id']})
                        if polled['task']['status']=='cancelled':break
                        time.sleep(.05)
                    assert polled['task']['status']=='cancelled'
                    assert polled['task']['report_id'] is None
                    assert candidate.runner.cleanup_confirmed(task['task_id'])
                    assert task['task_id'] not in candidate.backend.scopes
                    assert task['task_id'] not in candidate.backend.workers
                    assert candidate.task_cancel(candidate.owner_id,{'task_id':task['task_id']})['task']['status']=='cancelled'
                    assert candidate.status()['execution']['product_cli_starts']==1
                    assert candidate.status()['execution']['inference_attempts']==0
                finally:release.set()
                return
            end=time.monotonic()+30
            while time.monotonic()<end:
                saved=candidate.store.get_task(candidate.owner_id,task['task_id'])
                if saved['status'] in {'succeeded','failed','timed_out'}:break
                time.sleep(.05)
            assert saved['status']==('succeeded' if valid_report else 'failed'),candidate.runner.result(task['task_id'])
            assert candidate.runner.cleanup_confirmed(task['task_id'])
            assert len(provider.requests)==(6 if valid_report else 3) and provider.errors==[]
            assert 'OWNED_SENSITIVE_CANARY' not in json.dumps(provider.requests) and 'OWNED_OUTSIDE_CANARY' not in json.dumps(provider.requests)
            assert 'value = 1' in json.dumps(provider.requests)
            assert all(request['body']['reasoning']['effort']=='medium' for request in provider.requests)
            if valid_report:
                polled=candidate.task_poll(candidate.owner_id,{'report_id':saved['report_id']})
                assert polled['kind']=='report' and polled['report']['verification']=='not_run'
                assert 'SIMULATED' in json.dumps(polled)
                assert candidate.task_poll(candidate.owner_id,{'task_id':task['task_id']})['task']['report_id']==saved['report_id']
                assert candidate.task_poll('foreign-owner',{'report_id':saved['report_id']})['error']['code']=='POLICY_DENIED'
                first=candidate.task_poll(candidate.owner_id,{'report_id':saved['report_id'],'section':'overview','max_chars':6000})
                assert first['pagination']['has_more']
                cursor=first['pagination']['next_cursor']
                page_request={'report_id':saved['report_id'],'cursor':cursor}
                second=candidate.task_poll(candidate.owner_id,page_request)
                assert second==candidate.task_poll(candidate.owner_id,page_request)
                assert second['page']['text']
                cfg.write_text(dump_toml({'projects':[]}),encoding='utf-8')
                assert candidate.task_poll(candidate.owner_id,{'report_id':saved['report_id']})['error']['code']=='POLICY_DENIED'

            else:assert saved['report_id'] is None
            # Dedicated owned queued task cancellation reuses the public API;
            # no unrelated user task or second Store is created.
            if not valid_report:
                monkeypatch.setattr(candidate.scheduler,'dispatch',lambda:[])
                pending=candidate.project_context(candidate.owner_id,{'project_id':'p','request_id':'owned-cancel'})['task']
                cancelled=candidate.task_cancel(candidate.owner_id,{'task_id':pending['task_id']})
                assert cancelled['task']['status']=='cancelled'
                assert candidate.task_cancel(candidate.owner_id,{'task_id':pending['task_id']})['task']['status']=='cancelled'
                candidate.backend.release(pending['task_id'])
            assert original_status()['available'] is False
            assert candidate.status()['execution']['product_cli_starts']==1
            assert candidate.status()['execution']['inference_attempts']==0
            (tmp_path/'native-evidence.json').write_text(json.dumps({'task_id':task['task_id'],'report_id':saved['report_id'],
                'status':saved['status'],'simulation':True,'real_model_requests':0,'worker_starts':1,'provider_requests':provider.requests},ensure_ascii=False),encoding='utf-8')
        finally:
            release.set()
            candidate.close()



