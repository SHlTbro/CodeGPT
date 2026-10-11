"""App-server selection boundary. Native model-tool closure is NOT proven.

No inference or login from construction, status, selection or launch refusal.
Exec's event parser/acceptance is deliberately not imported as app-server proof.
"""
import hashlib
import json
from pathlib import Path

from config import config_revision
from local_agents.model_options import validate_selection, selection_fingerprint
from local_agents.read_tools import ReadTools
from local_agents.runner import RunnerError, LaunchSpec
from local_agents.backends.appserver_worker import WorkerSession
import os
import tomllib
from urllib.parse import urlsplit

CLI_VERSION = '0.157.1'
CLI_SHA256 = '8cb0e69e99ff2a158c54815db82d0f2e524d8f301bc30184722cfd1ae5973574'


class AppServerBackend:
    def __init__(self, executable):
        self.executable = str(executable)
        path = Path(executable)
        self.sha256 = None
        if path.is_absolute() and path.suffix.lower()=='.exe' and path.is_file():
            digest=hashlib.sha256()
            with path.open('rb') as source:
                while block:=source.read(65536):digest.update(block)
            self.sha256=digest.hexdigest()
        self.scopes={}
        self.workers={}
        self.bindings={}
        self.runner=None
        self.native_starts=0
        self.synthetic_turn_attempts=0

    def status(self):
        return {'mode':'app-server','available':False,'reason':'MODEL_TOOL_REGISTRY_UNPROVEN',
            'cli_version':CLI_VERSION if self.sha256 == CLI_SHA256 else None,
            'cli_sha256':self.sha256, 'version_verified':self.sha256 == CLI_SHA256,
            'native_tool_closure':'unproven','shared_login_input_closure':'unproven'}

    def selection(self, cfg, owner, profile, snapshot, *, model_option=None):
        choice=cfg.agent['profiles'][profile]
        mode=cfg.agent['codex']['auth_mode']
        if mode not in ('chatgpt','api_key'):
            raise RunnerError('AUTH_REQUIRED')
        provider=cfg.agent['codex']['api_provider'] if mode=='api_key' else 'openai'
        endpoint=cfg.agent['codex']['api_base_url'] if mode=='api_key' else None
        context={'auth_mode':mode,'billing_source':'chatgpt_plan' if mode=='chatgpt' else ('openai_api' if provider=='openai' else 'custom_api'),
            'provider':provider,'endpoint':endpoint,'credential_context_id':'unverified-backend-context',
            'credential_revision':'unverified','config_revision':config_revision(cfg),
            'cli_version':self.status()['cli_version'],'cli_sha256':self.sha256,'executable':self.executable or None}
        selected=validate_selection(choice['model'],reasoning_effort=choice['reasoning_effort'],service_tier=choice['service_tier'],option=model_option)
        scope=hashlib.sha256(json.dumps(snapshot['coverage']['scope'],sort_keys=True).encode()).hexdigest()
        fingerprint=selection_fingerprint(context,selected,profile=profile,scope_fingerprint=scope)
        return {'profile':profile,'requested_model':choice['model'],'config_revision':context['config_revision'],
            'backend_version':context['cli_version'],'execution_context':{k:context[k] for k in
                ('auth_mode','billing_source','provider','endpoint','credential_context_id','credential_revision')} |
                {'requested_reasoning_effort':choice['reasoning_effort'] or None,'resolved_reasoning_effort':None,
                 'requested_service_tier':choice['service_tier'],'resolved_service_tier':None,
                 'selection_fingerprint':fingerprint,'effective_auth_verified':False}}

    def launch(self, task, task_input):
        # Real version-pinned registration is not runtime tool admission proof.
        raise RunnerError('CAPABILITY_UNAVAILABLE')

    def bind(self,task,project,snapshot,authorize):
        if task['project_id']!=project.id:raise RunnerError('POLICY_DENIED')
        self.bindings[task['task_id']]=(project,snapshot)
        self.scopes[task['task_id']]=(ReadTools(project,snapshot,owner_id=task['owner_id'],task_id=task['task_id']),authorize)

    def dispatch_tool(self,task_id,name,arguments):
        reader,authorize=self.scopes[task_id]
        if not authorize():raise RunnerError('POLICY_DENIED')
        result=reader.dispatch(name,arguments)
        if not authorize():raise RunnerError('POLICY_DENIED')
        return result

    def prepare_fixture_worker(self,task,*,home,turn_input=None):
        """Internal qualification resolver; no production admission or auth.

        Same native Worker/Runner/producer seam, exclusively offline fixture.
        No public RPC accepts home, provider, commands or this mode.
        """
        if not self.status()['version_verified'] or task['task_id'] not in self.scopes:
            raise RunnerError('CAPABILITY_UNAVAILABLE')
        home=Path(home).resolve()
        if home.is_symlink() or home.is_junction():raise RunnerError('POLICY_DENIED')
        cfg=tomllib.loads((home/'config.toml').read_text(encoding='utf-8'))
        provider=cfg.get('model_providers',{}).get('offline_fixture',{})
        address=urlsplit(provider.get('base_url',''))
        catalog=Path(cfg.get('model_catalog_json','')).resolve()
        if cfg.get('model')!='fixture-no-inference' or cfg.get('model_provider')!='offline_fixture' or address.scheme!='http' or address.hostname!='127.0.0.1' or not address.port or address.username or address.password or address.path!='/v1' or provider.get('requires_openai_auth') is not False or not catalog.is_relative_to(home):
            raise RunnerError('POLICY_DENIED')
        if not self.scopes[task['task_id']][1]():raise RunnerError('POLICY_DENIED')
        env={name:os.environ[name] for name in ('SystemRoot','WINDIR') if name in os.environ}
        env.update(PATH='C:/Windows/System32;C:/Windows',CODEX_HOME=str(home),USERPROFILE=str(home),TEMP=str(home),TMP=str(home),
            NO_PROXY='127.0.0.1,localhost,::1')
        tid=task['task_id']
        reader=self.scopes[tid][0]
        self.workers[tid]=WorkerSession(send=lambda value:self.runner.send_input(tid,(json.dumps(value,ensure_ascii=False)+'\n').encode()),
            close=lambda:self.runner.close_input(tid),dispatch=lambda name,args:self.dispatch_tool(tid,name,args),
            tool_specs=reader.tool_specs(),thread_params={'model':'fixture-no-inference','modelProvider':'offline_fixture','cwd':str(home),'serviceTier':'default'},turn_input=turn_input,
            reasoning_effort=task['execution_context'].get('requested_reasoning_effort') or None)
        return LaunchSpec((self.executable,'app-server','--stdio'),str(home),env,30,interactive=True)

    def start_worker(self,task_id):
        worker=self.workers.get(task_id)
        if worker is None:raise RunnerError('CAPABILITY_UNAVAILABLE')
        self.native_starts+=1
        try:worker.start()
        except Exception:
            self.release(task_id)
            raise

    def on_output(self,task_id,stream,text):
        if stream=='stdout':
            worker=self.workers.get(task_id)
            if worker is None:raise RunnerError('CAPABILITY_UNAVAILABLE')
            try:worker.feed(text)
            except Exception:
                self.runner.close_input(task_id)
                raise

    def release(self,task_id):
        worker=self.workers.pop(task_id,None)
        if worker:self.synthetic_turn_attempts+=worker.turn_attempts
        self.scopes.pop(task_id,None)
        self.bindings.pop(task_id,None)

    def turn_attempts(self):
        return self.synthetic_turn_attempts+sum(worker.turn_attempts for worker in self.workers.values())
