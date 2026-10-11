"""Actual disposable OAuth/owner/MCP HTTP. No ChatGPT or model credentials."""
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
import uvicorn
from fastmcp import Client

CALLBACK='https://chatgpt.com/connector/oauth/T06-owned-synthetic-callback-only'


def api():
    assert importlib.util.find_spec('local_agents.access'), 'owner authentication component missing'
    return importlib.import_module('local_agents.access')


@contextmanager
def owned_pair(tmp_path,callbacks=(CALLBACK,),public_context=None):
    # Fresh actual listeners; approval capability is fetched via session route,
    # never supplied as a fixed test proof to the provider.
    module=api()
    sockets=[socket.socket(),socket.socket()]
    for sock in sockets: sock.bind(('127.0.0.1',0));sock.listen(128)
    public,owner=[f'http://127.0.0.1:{sock.getsockname()[1]}' for sock in sockets]
    clock=[time.time()]
    subject=module.owner_subject(tmp_path/'owner.json')
    provider=module.LocalApprovalProvider(base_url=public_context or public,owner_base_url=owner,
        owner_subject=subject,allowed_redirects=callbacks,clock=lambda:clock[0])
    apps=[module.build_candidate_server(provider).http_app(path='/mcp'),provider.owner_app()]
    servers=[];threads=[]
    for app,sock in zip(apps,sockets):
        server=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        servers.append(server);threads.append(thread);thread.start()
    try:
        deadline=time.monotonic()+10
        while not all(server.started for server in servers) and time.monotonic()<deadline: time.sleep(.01)
        assert all(server.started for server in servers)
        yield public,owner,provider,clock
    finally:
        for server in servers:server.should_exit=True
        for thread in threads:thread.join(10);assert not thread.is_alive()
        for sock in sockets:sock.close()


def register(public,callback=CALLBACK):
    return httpx.post(public+'/register',json={'redirect_uris':[callback],
        'client_name':'Self-reported client, not verified ChatGPT identity',
        'token_endpoint_auth_method':'none','grant_types':['authorization_code'],
        'response_types':['code'],'scope':'test:ping'},timeout=3)


def authorize(public,client,*,resource=None,callback=CALLBACK,method='S256'):
    verifier=secrets.token_urlsafe(48)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    response=httpx.get(public+'/authorize',params={'response_type':'code','client_id':client['client_id'],
        'redirect_uri':callback,'scope':'test:ping','state':'owned-client-state',
        'code_challenge':challenge,'code_challenge_method':method,'resource':resource or public+'/mcp'},
        follow_redirects=False,timeout=3)
    return response,verifier


def session(browser,owner):
    response=browser.get(owner+'/owner-api/session')
    assert response.status_code==200
    return {'Origin':owner,'X-Setup-Token':response.json()['setup_token']}


def pending(browser,owner,headers):
    response=browser.post(owner+'/owner-api/pending',headers=headers,json={})
    assert response.status_code==200
    return response.json()['pending']


def decide(browser,owner,headers,row,allow):
    return browser.post(owner+'/owner-api/approve',headers=headers,
        json={'transaction_id':row['transaction_id'],'action_nonce':row['action_nonce'],'allow':allow})


def token(public,client,code,verifier,resource=None):
    return httpx.post(public+'/token',data={'grant_type':'authorization_code','client_id':client['client_id'],
        'code':code,'code_verifier':verifier,'redirect_uri':CALLBACK,'resource':resource or public+'/mcp'},timeout=3)


def approved(public,owner,browser,headers,client):
    response,verifier=authorize(public,client)
    assert response.status_code==302
    row=next(row for row in pending(browser,owner,headers) if row['transaction_id']==response.headers['location'].rsplit('/',1)[1])
    assert decide(browser,owner,headers,row,True).status_code==200
    ready=httpx.get(response.headers['location'],follow_redirects=False,timeout=3)
    fields=parse_qs(urlparse(ready.headers['location']).query)
    assert fields['state']==['owned-client-state']
    assert fields['iss']==[str(browser.get(public+'/.well-known/oauth-authorization-server').json()['issuer'])]
    return fields['code'][0],verifier,row


def test_real_session_explicit_consent_pkce_token_mcp_and_read_gate(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        assert httpx.post(public+'/mcp',headers={'X-Setup-Probe':'1'},json={}).status_code==401
        assert httpx.get(public+'/.well-known/oauth-protected-resource/mcp').json()['resource']==public+'/mcp'
        headers=session(browser,owner)
        client=register(public).json()
        response,verifier=authorize(public,client)
        assert httpx.get(response.headers['location'],timeout=3).status_code==202
        row=pending(browser,owner,headers)[0]
        assert row['resource']==public+'/mcp' and row['redirect_uri']==CALLBACK
        assert decide(browser,owner,headers,row,True).status_code==200
        redirect=httpx.get(response.headers['location'],follow_redirects=False,timeout=3)
        fields=parse_qs(urlparse(redirect.headers['location']).query)
        code=fields['code'][0]
        assert token(public,client,code,'wrong-owned-verifier').status_code==401
        assert token(public,client,code,verifier,public+'/different').status_code==400
        exchanged=token(public,client,code,verifier)
        assert exchanged.status_code==200
        access=exchanged.json()['access_token']
        assert token(public,client,code,verifier).status_code==401
        async def invoke():
            async with Client(public+'/mcp',auth=access) as caller:
                tools=await caller.list_tools()
                assert [tool.name for tool in tools]==['auth_test_ping']
                result=await caller.call_tool('auth_test_ping',{})
                assert result.data=={'ok':True,'candidate':'T06','project_reading_enabled':False,'model_request_performed':False}
        asyncio.run(invoke())
        assert asyncio.run(provider.resolve_owner(access))==provider.owner_subject
        with pytest.raises(PermissionError):asyncio.run(provider.authorize_project(access,'owned-project'))
        for private in ('/owner-api/session','/owner-api/pending','/owner-api/approve','/api/config','/api/session'):
            assert httpx.get(public+private,timeout=3).status_code==404


@pytest.mark.parametrize('boundary',['origin','host','forwarded','token','cookie','nonce'])
def test_owner_session_and_nonce_boundaries_no_public_or_cross_browser_approval(tmp_path,boundary):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        headers=session(browser,owner);client=register(public).json();response,_=authorize(public,client)
        row=pending(browser,owner,headers)[0]
        if boundary=='origin':headers['Origin']='https://cross-site.invalid'
        if boundary=='host':headers['Host']='cross-site.invalid'
        if boundary=='forwarded':headers['X-Forwarded-For']='127.0.0.1'
        if boundary=='token':headers['X-Setup-Token']='wrong-owned-token'
        if boundary=='cookie':browser.cookies.clear()
        if boundary=='nonce':row['action_nonce']='wrong-owned-nonce'
        assert decide(browser,owner,headers,row,True).status_code==403
        assert httpx.get(response.headers['location'],timeout=3).status_code==202


def test_deny_replay_and_cross_session_nonce(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as one,httpx.Client(timeout=3) as two:
        first=session(one,owner);second=session(two,owner);client=register(public).json()
        response,_=authorize(public,client);row=pending(one,owner,first)[0]
        assert decide(two,owner,second,row,True).status_code==403
        assert decide(one,owner,first,row,False).status_code==200
        assert decide(one,owner,first,row,True).status_code==410
        ready=httpx.get(response.headers['location'],follow_redirects=False,timeout=3)
        fields=parse_qs(urlparse(ready.headers['location']).query)
        assert fields['error']==['access_denied'] and 'code' not in fields and 'iss' in fields


def test_exact_redirect_resource_pkce_and_discovery_metadata(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock):
        assert register(public,CALLBACK+'-different').status_code==400
        client=register(public).json()
        wrong_resource=authorize(public,client,resource=public+'/different')[0]
        assert wrong_resource.status_code==302
        wrong_fields=parse_qs(urlparse(wrong_resource.headers['location']).query)
        assert wrong_fields['error']==['invalid_target'] and 'iss' in wrong_fields and 'code' not in wrong_fields
        wrong_pkce=authorize(public,client,method='plain')[0]
        assert wrong_pkce.status_code in (400,302)
        if wrong_pkce.status_code==302:
            assert 'error' in parse_qs(urlparse(wrong_pkce.headers['location']).query)
        bad,_=authorize(public,client,callback=CALLBACK+'-different')
        assert bad.status_code==400
        metadata=httpx.get(public+'/.well-known/oauth-authorization-server',timeout=3).json()
        assert metadata['grant_types_supported']==['authorization_code','refresh_token']
        assert metadata['token_endpoint_auth_methods_supported']==['none']
        assert metadata['authorization_response_iss_parameter_supported'] is True


def test_pending_token_session_ttl_restart_and_owner_subject_persistence(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        headers=session(browser,owner);client=register(public).json()
        response,_=authorize(public,client);clock[0]+=121
        assert httpx.get(response.headers['location'],timeout=3).status_code==410
        code,verifier,row=approved(public,owner,browser,headers,client)
        access=token(public,client,code,verifier).json()['access_token']
        restarted=api().LocalApprovalProvider(base_url=public,owner_base_url=owner,
            owner_subject=api().owner_subject(tmp_path/'owner.json'),allowed_redirects=(CALLBACK,))
        assert restarted.owner_subject==provider.owner_subject
        assert asyncio.run(restarted.load_access_token(access)) is None
        assert asyncio.run(restarted.get_client(client['client_id'])) is None
        clock[0]+=301
        assert asyncio.run(provider.load_access_token(access)) is None
        clock[0]+=301
        assert browser.post(owner+'/owner-api/pending',headers=headers,json={}).status_code==403


def test_owner_page_bootstrap_guards_and_no_storage_or_fixed_proof(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        page=browser.get(owner+'/')
        assert page.status_code==200 and 'Content-Security-Policy' in page.headers
        js=browser.get(owner+'/owner.js').text
        assert 'localStorage' not in js and 'sessionStorage' not in js and 'innerHTML' not in js
        assert '允许本次连接' in js and '拒绝' in js
        for headers in ({'Origin':'https://cross-site.invalid'},{'Sec-Fetch-Site':'cross-site'},{'Forwarded':'for=127.0.0.1'}):
            assert browser.get(owner+'/owner-api/session',headers=headers).status_code==403
        session(browser,owner)
        state=(tmp_path/'owner.json').read_text()
        assert 'token' not in state and 'secret' not in state and 'proof' not in state


def test_real_headless_owner_browser_click_completes_http_oauth_mcp(tmp_path):
    from playwright.sync_api import sync_playwright, expect
    with owned_pair(tmp_path) as (public,owner,provider,clock):
        client=register(public).json();response,verifier=authorize(public,client)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,args=['--disable-background-networking'])
            page=browser.new_page()
            page.route('**/*',lambda route:route.continue_() if urlparse(route.request.url).hostname=='127.0.0.1' else route.abort())
            page.goto(owner+'/')
            expect(page.get_by_role('button',name='允许本次连接')).to_be_visible()
            expect(page.get_by_text('完整回调：'+CALLBACK,exact=False)).to_be_visible()
            page.screenshot(path=str(tmp_path/'owner-consent-before.png'))
            page.get_by_role('button',name='允许本次连接').click()
            expect(page.get_by_text('没有等待批准的连接。',exact=True)).to_be_visible()
            page.screenshot(path=str(tmp_path/'owner-consent-after.png'))
            browser.close()
        ready=httpx.get(response.headers['location'],follow_redirects=False,timeout=3)
        code=parse_qs(urlparse(ready.headers['location']).query)['code'][0]
        access=token(public,client,code,verifier).json()['access_token']
        async def invoke():
            async with Client(public+'/mcp',auth=access) as caller:
                assert (await caller.call_tool('auth_test_ping',{})).data['project_reading_enabled'] is False
        asyncio.run(invoke())




def test_documented_stable_callback_has_issuer_on_success_and_sdk_error(tmp_path):
    stable='https://chatgpt.com/connector_platform_oauth_redirect'
    with owned_pair(tmp_path,callbacks=(CALLBACK,stable)) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        client=register(public,stable).json();headers=session(browser,owner)
        error,_=authorize(public,client,callback=stable,resource=public+'/other')
        assert error.status_code==302
        fields=parse_qs(urlparse(error.headers['location']).query)
        assert fields['iss']==[str(provider.issuer_url)] and fields['error']==['invalid_target']
        response,verifier=authorize(public,client,callback=stable)
        assert decide(browser,owner,headers,pending(browser,owner,headers)[0],True).status_code==200
        ready=httpx.get(response.headers['location'],follow_redirects=False,timeout=3)
        fields=parse_qs(urlparse(ready.headers['location']).query)
        assert fields['iss']==[str(provider.issuer_url)] and 'code' in fields




def test_request_limits_and_denial_revokes_client_tokens(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        assert httpx.post(public+'/register',content=b'x'*9000,timeout=3).status_code==413
        headers=session(browser,owner);client=register(public).json()
        code,verifier,_=approved(public,owner,browser,headers,client)
        access=token(public,client,code,verifier).json()['access_token']
        assert asyncio.run(provider.resolve_owner(access))==provider.owner_subject
        authorize(public,client)
        assert decide(browser,owner,headers,pending(browser,owner,headers)[0],False).status_code==200
        assert asyncio.run(provider.load_access_token(access)) is None
        assert httpx.post(public+'/mcp',headers={'Authorization':'Bearer '+access},json={},timeout=3).status_code==401


def test_resource_context_matches_actual_sdk_metadata_canonical_url(tmp_path):
    with owned_pair(tmp_path,public_context='https://Owned-Public.Example') as (public,owner,provider,clock):
        metadata=httpx.get(public+'/.well-known/oauth-protected-resource/mcp',timeout=3).json()
        assert provider.resource==metadata['resource']


def test_public_context_and_callback_reject_all_userinfo(tmp_path):
    subject=api().owner_subject(tmp_path/'owner.json')
    for public,callback in [('https://:fixture-not-real-password@owned.example',CALLBACK),
                             ('https://owned.example','https://:fixture-not-real-password@chatgpt.com/callback')]:
        with pytest.raises(ValueError):
            api().LocalApprovalProvider(base_url=public,owner_base_url='http://127.0.0.1:12345',
                owner_subject=subject,allowed_redirects=(callback,))


def test_root_and_path_discovery_are_identical_and_audit_redacts(tmp_path,caplog):
    import logging
    caplog.set_level(logging.WARNING,logger='codegpt.oauth')
    with owned_pair(tmp_path) as (public,owner,provider,clock):
        path=httpx.get(public+'/.well-known/oauth-protected-resource/mcp')
        root=httpx.get(public+'/.well-known/oauth-protected-resource')
        assert root.status_code==200 and root.json()==path.json()
        assert root.json()['resource']==public+'/mcp'
        assert httpx.get(public+'/mcp').status_code==401
        response=httpx.post(public+'/register',json={'redirect_uris':[CALLBACK],
            'client_name':'PRIVATE_NAME_MARKER','token_endpoint_auth_method':'none',
            'grant_types':['authorization_code'],'response_types':['code'],'scope':'test:ping'})
        assert response.status_code==201
        records='\n'.join(record.getMessage() for record in caplog.records if record.name=='codegpt.oauth')
        assert 'status=200' in records and 'status=201' in records
        assert 'PRIVATE_NAME_MARKER' not in records and response.json()['client_id'] not in records


def register_refresh(public):
    return httpx.post(public+'/register',json={'redirect_uris':[CALLBACK],'token_endpoint_auth_method':'none',
        'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'scope':'test:ping'},timeout=3)

def refresh(public,client,value,**extra):
    data={'grant_type':'refresh_token','client_id':client['client_id'],'refresh_token':value,'resource':public+'/mcp'}
    data.update(extra)
    return httpx.post(public+'/token',data=data,timeout=3)

def test_dcr_refresh_rotation_replay_and_expiry_real_http(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        registration=register_refresh(public)
        assert registration.status_code==201
        client=registration.json();headers=session(browser,owner)
        code,verifier,_=approved(public,owner,browser,headers,client)
        original=token(public,client,code,verifier).json()
        assert original.get('refresh_token')
        clock[0]+=301
        assert asyncio.run(provider.load_access_token(original['access_token'])) is None
        assert refresh(public,client,original['refresh_token'],resource=public+'/wrong').status_code==400
        assert refresh(public,client,original['refresh_token'],scope='test:ping admin').status_code==400
        other=register_refresh(public).json()
        assert refresh(public,other,original['refresh_token']).status_code==401
        response=refresh(public,client,original['refresh_token'])
        assert response.status_code==200
        rotated=response.json()
        assert rotated['refresh_token']!=original['refresh_token']
        assert asyncio.run(provider.resolve_owner(rotated['access_token']))==provider.owner_subject
        assert refresh(public,client,original['refresh_token']).status_code==401
        assert asyncio.run(provider.load_access_token(rotated['access_token'])) is None
        assert refresh(public,client,rotated['refresh_token']).status_code==401
        code,verifier,_=approved(public,owner,browser,headers,client)
        expiring=token(public,client,code,verifier).json()
        clock[0]+=1801
        assert refresh(public,client,expiring['refresh_token']).status_code==401


def test_refresh_revocation_and_owner_denial_revoke_entire_family(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        registration=register_refresh(public);assert registration.status_code==201
        client=registration.json();headers=session(browser,owner)
        code,verifier,_=approved(public,owner,browser,headers,client)
        issued=token(public,client,code,verifier).json()
        response=httpx.post(public+'/revoke',data={'client_id':client['client_id'],'token':issued['refresh_token'],'token_type_hint':'refresh_token'})
        assert response.status_code==200
        assert asyncio.run(provider.load_access_token(issued['access_token'])) is None
        assert refresh(public,client,issued['refresh_token']).status_code==401
        code,verifier,_=approved(public,owner,browser,headers,client)
        issued=token(public,client,code,verifier).json()
        response,_=authorize(public,client)
        row=pending(browser,owner,headers)[0]
        assert decide(browser,owner,headers,row,False).status_code==200
        assert refresh(public,client,issued['refresh_token']).status_code==401
        assert asyncio.run(provider.load_access_token(issued['access_token'])) is None


def test_refresh_stale_preloaded_token_replay_revokes_family(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        registration=register_refresh(public);assert registration.status_code==201
        client=registration.json();headers=session(browser,owner)
        code,verifier,_=approved(public,owner,browser,headers,client)
        original=token(public,client,code,verifier).json()
        metadata=asyncio.run(provider.get_client(client['client_id']))
        stale=asyncio.run(provider.load_refresh_token(metadata,original['refresh_token']))
        newer=asyncio.run(provider.exchange_refresh_token(metadata,stale,['test:ping']))
        with pytest.raises(api().TokenError):
            asyncio.run(provider.exchange_refresh_token(metadata,stale,['test:ping']))
        assert asyncio.run(provider.load_access_token(newer.access_token)) is None


def test_dcr_still_rejects_other_grants_and_duplicate_grants(tmp_path):
    with owned_pair(tmp_path) as (public,owner,provider,clock):
        for grants in (['authorization_code','client_credentials'],['authorization_code','authorization_code']):
            response=httpx.post(public+'/register',json={'redirect_uris':[CALLBACK],'token_endpoint_auth_method':'none',
                'grant_types':grants,'response_types':['code'],'scope':'test:ping'})
            assert response.status_code==400


def test_safe_token_audit_distinguishes_expiry_replay_binding_and_unknown(tmp_path,caplog):
    import logging
    caplog.set_level(logging.WARNING,logger='codegpt.oauth')
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        client=register_refresh(public).json();headers=session(browser,owner)
        code,verifier,_=approved(public,owner,browser,headers,client)
        response=token(public,client,code,verifier)
        assert response.headers.get('X-CodeGPT-Auth-Request')
        original=response.json()
        other=register_refresh(public).json()
        assert refresh(public,other,original['refresh_token']).status_code==401
        assert refresh(public,client,'UNRECOGNIZED_CREDENTIAL').status_code==401
        rotated=refresh(public,client,original['refresh_token']).json()
        assert refresh(public,client,original['refresh_token']).status_code==401
        code,verifier,_=approved(public,owner,browser,headers,client)
        expiring=token(public,client,code,verifier).json()
        clock[0]+=1801
        # An access lookup may already have pruned the expired refresh token.
        asyncio.run(provider.load_access_token(expiring['access_token']))
        assert refresh(public,client,expiring['refresh_token']).status_code==401
        captured='\n'.join(r.getMessage() for r in caplog.records if r.name=='codegpt.oauth')
        for reason in ('REFRESH_CLIENT_MISMATCH','REFRESH_UNKNOWN','REFRESH_REPLAY','REFRESH_EXPIRED'):
            assert reason in captured
        for value in (client['client_id'],other['client_id'],code,verifier,original['access_token'],original['refresh_token'],rotated['refresh_token'],expiring['refresh_token'],'UNRECOGNIZED_CREDENTIAL'):
            assert value not in captured
        assert 'grant=refresh_token' in captured and 'request=' in captured
        assert 'reason=REFRESH_ROTATED' in captured
        assert 'reason=TOKEN_FAMILY_REVOKED' in captured and 'cause=REFRESH_REPLAY' in captured


def test_safe_request_ids_and_proven_sdk_client_errors_never_echo_input(tmp_path,caplog):
    with owned_pair(tmp_path) as (public,owner,provider,clock):
        markers=('PRIVATE_CLIENT_ID','PRIVATE_GRANT','PRIVATE_REFRESH','PRIVATE_SECRET','PRIVATE_QUERY','PRIVATE_HEADER','PRIVATE_RID')
        requests=[]
        for client_id in (None,markers[0]):
            body={'resource':public+'/mcp','grant_type':markers[1],'refresh_token':markers[2],'client_secret':markers[3]}
            if client_id is not None:body['client_id']=client_id
            response=httpx.post(public+'/token?'+markers[4],data=body,
                headers={'Authorization':markers[5],'X-CodeGPT-Auth-Request':markers[6]})
            assert response.status_code==401
            request_id=response.headers['X-CodeGPT-Auth-Request']
            assert len(request_id)==24 and all(c in '0123456789abcdef' for c in request_id)
            requests.append(request_id)
        assert requests[0]!=requests[1]
        captured='\n'.join(r.getMessage() for r in caplog.records if r.name=='codegpt.oauth')
        assert 'SDK_CLIENT_ID_MISSING' in captured and 'SDK_CLIENT_UNKNOWN' in captured
        assert 'grant=unsupported' in captured
        assert all('request='+value in captured for value in requests)
        assert not any(value in captured for value in markers)


def test_expiry_audit_metadata_is_short_lived_and_never_restores_refresh(tmp_path,caplog):
    with owned_pair(tmp_path) as (public,owner,provider,clock),httpx.Client(timeout=3) as browser:
        client=register_refresh(public).json();headers=session(browser,owner)
        code,verifier,_=approved(public,owner,browser,headers,client)
        original=token(public,client,code,verifier).json()
        clock[0]+=1801
        assert refresh(public,client,original['refresh_token']).status_code==401
        captured='\n'.join(r.getMessage() for r in caplog.records if r.name=='codegpt.oauth')
        assert 'REFRESH_EXPIRED' in captured
        caplog.clear();clock[0]+=301
        assert refresh(public,client,original['refresh_token']).status_code==401
        captured='\n'.join(r.getMessage() for r in caplog.records if r.name=='codegpt.oauth')
        assert 'REFRESH_UNKNOWN' in captured and 'REFRESH_EXPIRED' not in captured
        assert provider.expired_refresh=={}
