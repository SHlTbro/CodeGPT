"""One authenticated candidate task/report service; no direct execution route.

Reuses canonical Config, Scheduler/Runner/Store, T28 selection and Reports.
Only a proven adapter can enable admission. Current app-server refuses launch.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import sys

from config import load_config, ConfigError, _file_lock
from local_agents.backends.appserver import AppServerBackend
from local_agents.contracts import CONTRACT_VERSION, validation_errors
from local_agents.model_options import ModelOptionError
from local_agents.reports import Reports
from local_agents.runner import Runner, RunnerError
from local_agents.scheduler import Scheduler
from local_agents.snapshot import capture_snapshot
from local_agents.store import Store, StoreError
from security import DEFAULT_EXCLUDED_DIRS, DEFAULT_SENSITIVE_PATTERNS


def _wire(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _envelope(task, *, cancel=None, budget=12000):
    response={'schema_version':CONTRACT_VERSION,'kind':'task','task':task,'report':None,'page':None,
        'pagination':None,'response_chars':0}
    if cancel is not None:response['cancel_requested']=cancel
    for _ in range(5):response['response_chars']=len(_wire(response))
    if response['response_chars']>budget:raise StoreError('OUTPUT_BUDGET_TOO_SMALL')
    return response


class TaskService:
    def __init__(self, config_path, state_dir, *, owner_id):
        self.config_path=Path(config_path).absolute()
        self.state_dir=Path(state_dir).absolute()
        if self.state_dir.is_symlink() or self.state_dir.is_junction():raise StoreError('STORAGE_UNAVAILABLE')
        self.owner_id=owner_id
        cfg=self.config()
        self._closed=False
        self._lease=_file_lock(self.state_dir/'instance.lock',timeout=0)
        try:self._lease.__enter__()
        except ConfigError:
            self._lease=None
            raise StoreError('INSTANCE_ACTIVE') from None
        try:self._initialize(cfg)
        except BaseException:
            self._lease.__exit__(*sys.exc_info());self._lease=None
            raise

    def _initialize(self,cfg):
        self.backend=AppServerBackend(cfg.agent['codex']['executable'])
        self.store=Store(self.state_dir/'store.sqlite',self.state_dir/'results')
        self.runner=Runner(self.state_dir/'runner',self.backend.launch)  # Default gate CLOSED.
        self.backend.runner=self.runner
        self.runner.on_started=self.backend.start_worker
        self.runner.on_output=self.backend.on_output
        self.runner.recover()
        self.scheduler=Scheduler(self.store,self.runner,enabled=False)
        self.scheduler.recover()  # Entry owns this new candidate; never live state.
        self.scheduler.set_enabled(False)
        self.runner.on_exit=self._finish_worker
        key=self.state_dir/'cursor.key'
        if key.exists():
            if key.is_symlink() or key.stat().st_size!=32:raise StoreError('STORAGE_UNAVAILABLE')
            cursor_key=key.read_bytes()
        else:
            cursor_key=os.urandom(32)
            with key.open('xb') as output:output.write(cursor_key)
        self.reports=Reports(self.store,authorize=self._report_allowed,cursor_key=cursor_key,
            max_response_chars=cfg.agent['limits']['max_response_chars'])

    def _finish_worker(self,task_id,outcome):
        """Native cleanup + completed bounded output + authorized publication.

        A zero process exit alone can never make a reading task succeed.
        The actual provider/identity safety gate remains closed elsewhere.
        """
        try:
            if outcome=='succeeded':
                with self.store.atomic():
                    task=self.store.get_task(self.owner_id,task_id)
                    worker=self.backend.workers.get(task_id)
                    if task['status']!='running' or not worker or worker.phase!='completed' or worker.result is None or worker.buffer:
                        outcome='failed'
                    else:
                        project,snapshot=self.backend.bindings[task_id]
                        if not self.backend.scopes[task_id][1]():raise StoreError('POLICY_DENIED')
                        fields=('evidence_id','path','line_start','line_end','symbol','content_sha256','snapshot_id','excerpt','validation')
                        evidence=[{k:item[k] for k in fields} for item in worker.evidence]
                        result=worker.result
                        self.reports.publish(self.owner_id,task_id,project,snapshot,
                            snapshot_options=self.snapshot_options(project),permission_fingerprint=self.permission_fingerprint(project),
                            summary=result['summary'],sections=result['sections'],claims=result['claims'],unknowns=result['unknowns'],
                            evidence=evidence,observed_read_files=len({e['path'] for e in evidence}))
            self.scheduler.finish(self.owner_id,task_id,outcome)
        except Exception:
            self.scheduler.finish(self.owner_id,task_id,'failed')
        finally:self.backend.release(task_id)

    def config(self):
        return load_config(self.config_path)

    def snapshot_options(self,project):
        cfg=self.config()
        internal={'.git','.setup','.codex','artifacts'}
        for path in (self.state_dir,self.config_path):
            try:relative=path.resolve().relative_to(project.path.resolve()).as_posix()
            except ValueError:pass
            else:
                if relative=='.':raise StoreError('POLICY_DENIED')
                internal.add(relative)
        return {'paths':('.',),'internal_paths':tuple(sorted(internal)),
            'sensitive_patterns':tuple(sorted(set(DEFAULT_SENSITIVE_PATTERNS)|set(cfg.security['sensitive_patterns']))),
            'excluded_dirs':tuple(sorted(set(DEFAULT_EXCLUDED_DIRS)|set(cfg.security['default_excluded_dirs']))),
            'max_file_size_bytes':cfg.limits['max_file_size_bytes']}

    def _project(self,owner,project_id):
        if owner!=self.owner_id:raise StoreError('POLICY_DENIED')
        cfg=self.config()
        project=next((p for p in cfg.projects if p.id==project_id),None)
        if project is None:
            error=StoreError('POLICY_DENIED')
            error.safe_reason='PROJECT_NOT_IN_CANDIDATE_SCOPE'
            raise error
        return project

    def permission_fingerprint(self,project):
        cfg=self.config()
        return hashlib.sha256(_wire({'root':str(project.path.resolve()),'project_id':project.id,
            'security':cfg.security,'options':self.snapshot_options(project)}).encode()).hexdigest()

    def _report_allowed(self,owner,project_id,scope,fingerprint):
        try:
            project=self._project(owner,project_id)
            return scope==['.'] and fingerprint==self.permission_fingerprint(project)
        except (StoreError,ConfigError):return False

    def selection(self,project,snapshot,profile,*,model_option=None):
        if profile not in project.agent.allowed_profiles or profile not in self.config().agent['profiles']:
            raise StoreError('POLICY_DENIED')
        return self.backend.selection(self.config(),self.owner_id,profile,snapshot,model_option=model_option)

    def _call(self,definition,owner,payload,operation):
        try:
            if validation_errors(definition,payload) or len(_wire(payload).encode())>65536:
                raise StoreError('INVALID_ARGUMENT')
            if owner!=self.owner_id:raise StoreError('POLICY_DENIED')
            return operation()
        except (StoreError,ModelOptionError,ConfigError,RunnerError) as error:
            code=getattr(error,'code','CONFIG_ERROR')
            logging.getLogger('codegpt.task').warning('task_gate operation=%s code=%s',definition,code)
            hint='当前受控运行边界未证明，禁止推理；核对本机配置或授权后重试。' if code=='CAPABILITY_UNAVAILABLE' else '核对请求、项目授权或结果有效期。'
            if getattr(error,'safe_reason',None)=='PROJECT_NOT_IN_CANDIDATE_SCOPE':
                hint='PROJECT_NOT_IN_CANDIDATE_SCOPE：该项目 ID 不在当前候选授权范围；请核对候选项目 ID，不会自动继承其他 MCP 的项目。'
            return {'error':{'code':code,'message':'候选请求未完成。','recoverable':True,
                'hint':hint,
                'task_id':None,'retriable':False}}

    def project_context(self,owner,payload):
        def operation():
            project=self._project(owner,payload['project_id'])
            cfg=self.config()
            if not cfg.agent['enabled'] or not cfg.agent['codex']['enabled'] or not project.agent.context_enabled:
                raise StoreError('CAPABILITY_DISABLED')
            budget=payload.get('max_chars',12000)
            # Existing bindings are replayed before any new adapter activity.
            try:
                replay=self.scheduler.submit(owner_id=owner,operation='project_context',request=payload)
            except StoreError as error:
                if error.code!='INVALID_ARGUMENT':raise
            else:
                return self._accepted(owner,replay,budget)
            profile=payload.get('profile',cfg.agent['default_profile'])
            if profile not in project.agent.allowed_profiles:raise StoreError('POLICY_DENIED')
            if payload.get('intent','overview')=='overview':
                snapshot=capture_snapshot(project,**self.snapshot_options(project))
                try:selection=self.selection(project,snapshot,profile)
                except ModelOptionError:selection=None  # Draft parameters are not a verified runtime.
                cached=self.reports.find_cached(owner,project,snapshot,selection,self.permission_fingerprint(project)) if selection else None
                if cached:
                    accepted=self.scheduler.submit(owner_id=owner,operation='project_context',request=payload,cached_report_id=cached['report_id'])
                    return self._accepted(owner,accepted,budget)
            if not cfg.agent['enabled'] or not cfg.agent['codex']['enabled']:raise StoreError('CAPABILITY_DISABLED')
            if not self.backend.status()['available']:raise StoreError('CAPABILITY_UNAVAILABLE')
            # Admission remains behind proven boundary; no direct/parallel demo launch.
            snapshot=capture_snapshot(project,**self.snapshot_options(project))
            accepted=self.scheduler.submit(owner_id=owner,operation='project_context',request=payload,
                selection=self.selection(project,snapshot,profile))
            fingerprint=self.permission_fingerprint(project)
            self.backend.bind(accepted.value,project,snapshot,
                lambda:self._report_allowed(owner,project.id,snapshot['coverage']['scope']['paths'],fingerprint))
            self.scheduler.dispatch()
            return self._accepted(owner,accepted,budget)
        return self._call('project_context_request',owner,payload,operation)

    def _accepted(self,owner,accepted,budget):
        if accepted.kind=='report':return self._report_page(owner,accepted.value['report_id'],max_chars=budget)
        return _envelope(accepted.value,budget=budget)

    def _report_page(self,owner,report_id,**options):
        report=self.store.get_report(owner,report_id)
        project=self._project(owner,report['project_id'])
        snapshot=capture_snapshot(project,**self.snapshot_options(project))
        return self.reports.page(owner,report_id,current_snapshot=snapshot,**options)

    def task_poll(self,owner,payload):
        def operation():
            options={k:payload[k] for k in ('section','cursor','max_chars') if k in payload}
            if 'report_id' in payload:return self._report_page(owner,payload['report_id'],**options)
            task=self.store.get_task(owner,payload['task_id']);self._project(owner,task['project_id'])
            deadline=time.monotonic()+min(payload.get('wait_seconds',0),self.config().agent['limits']['max_wait_seconds'])
            while task['status'] in {'queued','running','cancelling'} and time.monotonic()<deadline:
                time.sleep(.1)
                self._project(owner,task['project_id'])
                current=self.store.get_task(owner,task['task_id'])
                if current!=task:task=current;break
            if options.get('section') is not None or options.get('cursor') is not None:
                if not task['report_id']:raise StoreError('INVALID_CURSOR')
                return self._report_page(owner,task['report_id'],**options)
            return _envelope(task,budget=payload.get('max_chars',12000))
        return self._call('task_poll_request',owner,payload,operation)

    def task_cancel(self,owner,payload):
        def operation():
            task=self.store.get_task(owner,payload['task_id'])
            cancelled=self.scheduler.cancel(owner,task['task_id'])
            return _envelope(cancelled.task,cancel=cancelled.cancel_requested)
        return self._call('task_cancel_request',owner,payload,operation)

    def status(self):
        return {'backend':self.backend.status(),'execution':{'product_cli_starts':self.backend.native_starts,'synthetic_turn_attempts':self.backend.turn_attempts(),'inference_attempts':0,
            'inference_completed':0,'actual_model':None,'usage':None,'cost':None,
            'scope':'current candidate instance; public real-model launch refused; native offline qualification separately counted'},
            'requested_profile':self.config().agent['profiles'][self.config().agent['default_profile']]}

    def close(self):
        if self._closed:return
        self._closed=True
        try:self.scheduler.set_enabled(False)
        finally:
            try:self.runner.close()
            finally:
                if self._lease:self._lease.__exit__(None,None,None);self._lease=None
