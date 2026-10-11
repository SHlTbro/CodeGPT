"""Owned metadata/config fixtures; never evidence of current account availability."""
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from local_agents.contracts import validation_errors
from local_agents.store import Store


def api():
    assert importlib.util.find_spec('local_agents.model_catalog'), 'catalog component missing'
    return importlib.import_module('local_agents.model_catalog')


def context(mode='chatgpt'):
    return {'auth_mode': mode, 'billing_source': 'chatgpt_plan' if mode == 'chatgpt' else 'openai_api',
        'provider': 'openai', 'endpoint': None if mode == 'chatgpt' else 'https://api.openai.com/v1',
        'credential_context_id': None, 'credential_revision': None, 'config_revision': 'a' * 64,
        'cli_version': 'fixture-cli', 'cli_sha256': 'b' * 64, 'executable': 'fixture-only.exe'}


def option(model='fixture-a', mode='chatgpt'):
    return {'model_id': model, 'display_name': model, 'source': 'cli_catalog', 'auth_mode': mode,
        'availability': 'unknown', 'compatibility': 'supported', 'reasoning_efforts': ['low', 'high'],
        'reasoning_effort_status': 'known', 'default_reasoning_effort': 'low',
        'service_tiers': ['standard', 'priority'], 'parameter_source': 'cli_catalog',
        'verified_selection_fingerprint': None, 'checked_at': None, 'reason': 'Synthetic metadata fixture only',
        'cost_hint': None, 'cost_hint_basis': 'unknown'}


def metadata(mode='chatgpt'):
    return {'source': 'cli_catalog', 'models': [option(mode=mode), option('fixture-b', mode)],
        'fetched_at': '2026-10-08T00:00:00Z'}


@pytest.mark.parametrize('mode', ['chatgpt', 'api_key'])
def test_off_and_cached_view_never_fetch_and_save_has_one_toml_authority(tmp_path, mode):
    calls = []
    catalog = api().ModelCatalog(tmp_path / 'cache.json', metadata_fetcher=lambda ctx: calls.append(ctx) or metadata(mode))
    ctx = context(mode)
    assert catalog.view(ctx, enabled=False)['state'] == 'disabled'
    assert catalog.refresh(ctx, enabled=False)['state'] == 'disabled'
    assert calls == [] and not (tmp_path / 'cache.json').exists()
    fresh = catalog.refresh(ctx, enabled=True)
    assert validation_errors('model_catalog', fresh) == []
    assert len(calls) == 1 and len(fresh['models']) == 2
    cached = api().ModelCatalog(tmp_path / 'cache.json').view(ctx, enabled=True)
    assert cached['state'] == 'cached' and len(cached['models']) == 2
    original = (tmp_path / 'cache.json').read_bytes()
    for model_id in ('fixture-a', 'fixture-b', 'manual-user-id'):
        saved = catalog.save_option(ctx, model_id, profile='reader', scope_fingerprint='c' * 64)
        assert validation_errors('model_selection', saved['selection']) == []
        assert saved['selection']['model_id'] == model_id
        assert saved['selection']['reasoning_effort'] == ''
        assert saved['selection']['service_tier'] == 'standard'
    assert (tmp_path / 'cache.json').read_bytes() == original and len(calls) == 1


def test_manual_only_outage_retains_actual_saved_id_and_does_not_guess_model(tmp_path):
    catalog = api().ModelCatalog(tmp_path / 'cache.json')
    result = catalog.view(context(), enabled=True, saved_model_id='my-manual-model')
    assert result['source'] == 'manual_only' and result['models'][0]['model_id'] == 'my-manual-model'
    assert result['models'][0]['availability'] == 'unknown'
    selected = catalog.save_option(context(), 'my-manual-model', profile='r', scope_fingerprint='c' * 64)
    with pytest.raises(api().ModelOptionError) as error:
        api().require_execution_capability(selected['selection'])
    assert error.value.code == 'CAPABILITY_UNAVAILABLE'
    assert not (tmp_path / 'cache.json').exists()


def test_refresh_outage_preserves_previous_candidate_without_permissions(tmp_path):
    catalog = api().ModelCatalog(tmp_path / 'cache.json', metadata_fetcher=lambda _: metadata())
    catalog.refresh(context(), enabled=True)
    def outage(_):
        raise RuntimeError('secret-looking failure detail must not be returned')
    catalog.metadata_fetcher = outage
    result = catalog.refresh(context(), enabled=True)
    assert result['state'] == 'unavailable' and len(result['models']) == 2
    assert 'secret-looking' not in json.dumps(result)
    assert all(m['availability'] != 'verified' for m in result['models'])


@pytest.mark.parametrize('field,value', [('auth_mode','api_key'), ('provider','custom_responses'),
    ('endpoint','https://example.com/v1'), ('cli_version','other'), ('cli_sha256','d'*64),
    ('executable','other.exe'), ('credential_context_id','other'), ('credential_revision','new'),
    ('config_revision','e'*64)])
def test_partition_and_selection_fingerprint_invalidate_every_boundary(tmp_path, field, value):
    catalog = api().ModelCatalog(tmp_path / 'cache.json', metadata_fetcher=lambda _: metadata())
    ctx = context(); catalog.refresh(ctx, enabled=True)
    original = catalog.save_option(ctx, 'fixture-a', profile='reader', scope_fingerprint='c'*64)
    changed = deepcopy(ctx); changed[field] = value
    if field == 'auth_mode':
        changed.update(billing_source='openai_api', endpoint='https://api.openai.com/v1')
    if field in ('provider','endpoint'):
        changed.update(auth_mode='api_key', billing_source='custom_api', provider='custom_responses', endpoint='https://example.com/v1')
    result = catalog.view(changed, enabled=True, saved_model_id='fixture-a')
    assert result['source'] == 'manual_only' and result['models'][0]['availability'] == 'unknown'
    assert catalog.save_option(changed, 'fixture-a', profile='reader', scope_fingerprint='c'*64)['selection_fingerprint'] != original['selection_fingerprint']


def test_effort_tier_and_policy_are_strict_and_defaults_clear_inheritance(tmp_path):
    catalog = api().ModelCatalog(tmp_path / 'cache.json', metadata_fetcher=lambda _: metadata())
    ctx=context(); catalog.refresh(ctx, enabled=True)
    for model, effort, tier in [(' ', '', 'standard'), ('fixture-a','ultra','standard'),
                               ('fixture-a','low','fast'), ('manual-user','low','standard')]:
        with pytest.raises(api().ModelOptionError):
            catalog.save_option(ctx, model, reasoning_effort=effort, service_tier=tier,
                profile='reader', scope_fingerprint='c'*64)
    for policy in ({'forced_model': 'fixture-b'}, {'allowed_models': ['fixture-b']}):
        with pytest.raises(api().ModelOptionError) as error:
            catalog.save_option(ctx, 'manual-user', profile='reader', scope_fingerprint='c'*64, **policy)
        assert error.value.code == 'POLICY_DENIED'
    saved=catalog.save_option(ctx,'fixture-a',profile='reader',scope_fingerprint='c'*64)
    assert api().require_execution_capability(saved['selection']) == {
        'model': 'fixture-a', 'reasoning_effort': None, 'service_tier': None,
        'inherit_reasoning_effort': False, 'inherit_service_tier': False}
    changed=catalog.save_option(ctx,'fixture-a',reasoning_effort='high',service_tier='priority',
        profile='reader',scope_fingerprint='c'*64)
    assert saved['selection_fingerprint'] != changed['selection_fingerprint']
    assert saved['selection_fingerprint'] != catalog.save_option(ctx,'fixture-a',profile='other',scope_fingerprint='c'*64)['selection_fingerprint']
    assert saved['selection_fingerprint'] != catalog.save_option(ctx,'fixture-a',profile='reader',scope_fingerprint='d'*64)['selection_fingerprint']


def test_api_models_never_claim_cli_compatibility_or_verified_account(tmp_path):
    data=metadata('api_key'); data['source']='api_models'
    for model in data['models']:
        model.update(source='api_models', availability='verified', verified_selection_fingerprint='f'*64,
                     checked_at='2026-10-08T00:00:00Z')
    catalog=api().ModelCatalog(tmp_path/'cache.json',metadata_fetcher=lambda _: data)
    result=catalog.refresh(context('api_key'),enabled=True)
    assert all(m['compatibility']=='unknown' and m['availability']=='unverified' and m['verified_selection_fingerprint'] is None
               and m['reasoning_effort_status']=='unknown' and m['cost_hint'] is None for m in result['models'])


def test_historical_native_bundled_is_candidate_only_without_cli_launch(tmp_path):
    path=Path(__file__).resolve().parents[2]/'docs/evidence/local-agent-v1/T02/catalog-summary.json'
    payload=json.loads(path.read_bytes())
    data=api().historical_bundled(payload, 'chatgpt', observed_cli_version='0.157.1')
    assert len(data['models']) >= 2
    assert all(m['source']=='cli_bundled' and m['availability']=='unverified' and m['compatibility']=='unknown'
               and m['cost_hint'] is None for m in data['models'])
    catalog=api().ModelCatalog(tmp_path/'cache.json', metadata_fetcher=lambda _:data)
    result=catalog.refresh(context(),enabled=True)
    assert validation_errors('model_catalog',result)==[]


def test_single_candidate_and_bad_metadata_do_not_create_account_choices(tmp_path):
    data=metadata(); data['models']=data['models'][:1]
    catalog=api().ModelCatalog(tmp_path/'cache.json',metadata_fetcher=lambda _:data)
    assert len(catalog.refresh(context(),enabled=True)['models'])==1
    data['models'][0]['api_key']='not-a-real-secret-fixture'
    result=catalog.refresh(context(),enabled=True)
    assert result['state']=='unavailable' and len(result['models'])==1
    assert 'not-a-real-secret' not in json.dumps(result)


def test_verification_requires_owner_bound_actual_runtime_record_exact_identity(tmp_path):
    # This Store record is a controlled producer fixture, not a real model run.
    ctx=context(); ctx.update(credential_context_id='fixture-owner', credential_revision='fixture-rev')
    catalog=api().ModelCatalog(tmp_path/'cache.json', metadata_fetcher=lambda _:metadata())
    catalog.refresh(ctx, enabled=True)
    saved=catalog.save_option(ctx,'fixture-a',profile='reader',scope_fingerprint='c'*64)
    fp=saved['selection_fingerprint']
    store=Store(tmp_path/'owned-state/tasks.sqlite',tmp_path/'owned-state/results')
    frozen={'profile':'reader','requested_model':'fixture-a','backend_version':'fixture-cli',
        'config_revision':ctx['config_revision'],'execution_context':{
            **{name:ctx[name] for name in ('auth_mode','billing_source','provider','endpoint','credential_context_id','credential_revision')},
            'requested_reasoning_effort':None,'resolved_reasoning_effort':None,
            'requested_service_tier':'standard','resolved_service_tier':None,
            'selection_fingerprint':fp,'effective_auth_verified':True}}
    task=store.accept(owner_id='fixture-owner',operation='model_test',
        request={'project_id':'fixture-project','profile':'reader','request_id':'fixture-proof',
                 'revision':'a'*64,'expected_credential_revision':'fixture-rev',
                 'allow_model_request':True},selection=frozen).value
    task.update(status='succeeded',finished_at='2026-10-08T00:00:00Z',finish_reason='completed',
                actual_models=['fixture-a'],verification='passed')
    proof={'task':task,'test':{'task_id':task['task_id'],'profile':'reader','model_id':'fixture-a',
        'selection_fingerprint':fp,'status':'succeeded','verification':'passed',
        'billing_source':'chatgpt_plan','message':'Controlled runtime-producer fixture only',
        'updated_at':'2026-10-08T00:00:00Z','model_request_performed':True}}
    assert validation_errors('task',task)==[]
    with pytest.raises(api().ModelOptionError):
        catalog.record_verification(ctx,saved['selection'],profile='reader',scope_fingerprint='c'*64,task_id=task['task_id'])
    catalog.verification_reader=lambda _:deepcopy(proof)
    result=catalog.record_verification(ctx,saved['selection'],profile='reader',scope_fingerprint='c'*64,task_id=task['task_id'])
    assert result['models'][0]['availability']=='verified'
    assert result['models'][0]['verified_selection_fingerprint']==fp
    assert catalog.view(ctx,enabled=True)['models'][0]['availability']=='unverified'
    for mutation in ('model','fp','mode','event','task_verification'):
        bad=deepcopy(proof)
        if mutation=='model': bad['task']['actual_models']=['fixture-b']
        if mutation=='fp': bad['test']['selection_fingerprint']='d'*64
        if mutation=='mode': bad['task']['execution_context']['effective_auth_verified']=False
        if mutation=='event': bad['test']['model_request_performed']=False
        if mutation=='task_verification': bad['task']['verification']='not_run'
        catalog.verification_reader=lambda _, bad=bad:bad
        with pytest.raises(api().ModelOptionError) as error:
            catalog.record_verification(ctx,saved['selection'],profile='reader',scope_fingerprint='c'*64,task_id=task['task_id'])
        assert error.value.code=='VERIFICATION_REQUIRED'


@pytest.mark.parametrize('field,value',[('config_revision',int('1'*64)),('cli_sha256',int('2'*64)),('api_key','fixture-no-real-key')])
def test_context_rejects_noncanonical_types_and_secrets(tmp_path,field,value):
    ctx=context();ctx[field]=value
    with pytest.raises(api().ModelOptionError) as error:
        api().ModelCatalog(tmp_path/'cache.json').view(ctx,enabled=True)
    assert error.value.code=='INVALID_ARGUMENT'


def test_public_cached_operations_have_no_process_or_network_path(tmp_path,monkeypatch):
    import socket
    import subprocess
    import urllib.request
    def forbidden(*args,**kwargs):
        pytest.fail('cache/view/save must not invoke process or network')
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(urllib.request,'urlopen',forbidden)
    catalog=api().ModelCatalog(tmp_path/'cache.json',metadata_fetcher=lambda _:metadata())
    catalog.refresh(context(),enabled=True)
    for enabled in (True,False):
        assert validation_errors('model_catalog',catalog.view(context(),enabled=enabled))==[]
    catalog.save_option(context(),'fixture-a',profile='reader',scope_fingerprint='c'*64)


def test_atomic_cache_failure_preserves_previous_bytes_and_selection(tmp_path,monkeypatch):
    catalog=api().ModelCatalog(tmp_path/'cache.json',metadata_fetcher=lambda ctx:metadata(ctx['auth_mode']))
    catalog.refresh(context(),enabled=True)
    previous=(tmp_path/'cache.json').read_bytes()
    def failed_write(*args):
        raise OSError('private storage failure detail')
    monkeypatch.setattr(api(),'_atomic_write',failed_write)
    with pytest.raises(api().ModelOptionError) as error:
        catalog.refresh(context('api_key'),enabled=True)
    assert error.value.code=='STORAGE_UNAVAILABLE'
    assert (tmp_path/'cache.json').read_bytes()==previous


def test_api_listing_cannot_be_loaded_as_chatgpt_catalog(tmp_path):
    data=metadata();data['source']='api_models'
    catalog=api().ModelCatalog(tmp_path/'cache.json',metadata_fetcher=lambda _:data)
    result=catalog.refresh(context(),enabled=True,saved_model_id='my-saved-model')
    assert result['state']=='unavailable'
    assert result['source']=='manual_only' and [m['model_id'] for m in result['models']]==['my-saved-model']


@pytest.mark.parametrize('fingerprint',[{}, 'wrong', 123])
def test_catalog_verification_selector_is_exact_sha256(tmp_path,fingerprint):
    with pytest.raises(api().ModelOptionError) as error:
        api().ModelCatalog(tmp_path/'cache.json').view(context(),enabled=True,selection_fingerprint=fingerprint)
    assert error.value.code=='INVALID_ARGUMENT'
