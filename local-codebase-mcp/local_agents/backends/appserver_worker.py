"""Pinned app-server stdio consumer. No process/login/permission selection.

Runner owns the sole process tree; TaskService owns admission and publication.
A fresh session never resumes a previous tool registry. Native tool closure is
still an independent gate, not something this protocol parser can establish.
"""
import json
from local_agents.runner import RunnerError
from local_agents.reports import SECTION_IDS

def _object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError
        value[key]=item
    return value

class WorkerSession:
    def __init__(self,*,send,close,tool_specs,dispatch,thread_params,turn_input=None,reasoning_effort=None):
        if reasoning_effort is not None and (not isinstance(reasoning_effort,str) or not reasoning_effort or len(reasoning_effort)>128 or any(ord(c)<32 for c in reasoning_effort)):
            raise RunnerError('UNSUPPORTED_MODEL_PARAMETER')
        self.reasoning_effort=reasoning_effort
        self.send,self.close,self.dispatch=send,close,dispatch
        self.params=dict(thread_params)
        self.params.update(ephemeral=True,environments=[],dynamicTools=tool_specs)
        self.turn_input=turn_input
        self.buffer='';self.text='';self.phase='new';self.thread=None;self.turn=None
        self.ready=False;self.result=None;self.evidence=[];self.tool_calls=0;self.turn_attempts=0

    def _send(self,value):
        encoded=json.dumps(value,ensure_ascii=False)
        if len(encoded.encode('utf-8'))>65536:raise RunnerError('OUTPUT_TOO_LARGE')
        if value.get("method")=="turn/start":self.turn_attempts+=1
        self.send(value)

    def start(self):
        if self.phase!='new':raise RunnerError('REQUEST_CONFLICT')
        self.phase='initializing'
        self._send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'codegpt-task-worker','version':'1.2.0'},'capabilities':{'experimentalApi':True}}})

    def feed(self,text):
        self.buffer+=text
        if len(self.buffer)>65536:raise RunnerError('OUTPUT_TOO_LARGE')
        while '\n' in self.buffer:
            line,self.buffer=self.buffer.split('\n',1)
            if not line:continue
            try:value=json.loads(line,object_pairs_hook=_object)
            except (ValueError,TypeError):raise RunnerError('EXECUTION_FAILED') from None
            if not isinstance(value,dict):raise RunnerError('EXECUTION_FAILED')
            self._event(value)

    def _event(self,value):
        if 'method' not in value:
            if 'error' in value:raise RunnerError('EXECUTION_FAILED')
            result=value.get('result',{})
            if value.get('id')==1 and self.phase=='initializing':
                self._send({'method':'initialized','params':{}})
                self.phase='thread-starting'
                self._send({'id':2,'method':'thread/start','params':self.params})
            elif value.get('id')==2 and self.phase=='thread-starting':
                self.thread=result.get('thread',{}).get('id')
                if not isinstance(self.thread,str) or not self.thread:raise RunnerError('EXECUTION_FAILED')
                self.ready=True
                if self.turn_input is None:
                    self.phase='ready';self.close()
                else:
                    if not isinstance(self.turn_input,str) or not 1<=len(self.turn_input)<=4096:raise RunnerError('INVALID_ARGUMENT')
                    self.phase='turn-starting'
                    self._send({'id':3,'method':'turn/start','params':{'threadId':self.thread,'input':[{'type':'text','text':self.turn_input}], 'effort':self.reasoning_effort}})
            elif value.get('id')==3 and self.phase in {'turn-starting','running'}:
                self._bind_turn(result.get('turn',{}).get('id'));self.phase='running'
            else:raise RunnerError('EXECUTION_FAILED')
            return
        method=value['method'];params=value.get('params',{})
        if not isinstance(params,dict):raise RunnerError('EXECUTION_FAILED')
        if 'id' in value:
            if method!='item/tool/call' or params.get('threadId')!=self.thread or params.get('turnId')!=self.turn or self.phase!='running':
                self._send({'id':value['id'],'error':{'code':-32601,'message':'Unsupported worker request'}})
                raise RunnerError('CAPABILITY_UNAVAILABLE')
            self.tool_calls+=1
            if self.tool_calls>100:raise RunnerError('OUTPUT_TOO_LARGE')
            try:
                result=self.dispatch(params.get('tool'),params.get('arguments'))
                if params.get('tool')=='task_read':self.evidence.append(result)
                response={'contentItems':[{'type':'inputText','text':json.dumps(result,ensure_ascii=False)}],'success':True}
            except Exception as error:
                response={'contentItems':[{'type':'inputText','text':json.dumps({'error':getattr(error,'code','POLICY_DENIED')})}],'success':False}
            self._send({'id':value['id'],'result':response})
            return
        if method=='turn/started':
            if params.get('threadId')!=self.thread:raise RunnerError('POLICY_DENIED')
            self._bind_turn(params.get('turn',{}).get('id'));self.phase='running'
        elif method in {'item/completed','turn/completed'}:
            if params.get('threadId')!=self.thread or params.get('turnId',params.get('turn',{}).get('id'))!=self.turn:
                raise RunnerError('POLICY_DENIED')
            if method=='item/completed':
                item=params.get('item',{})
                if item.get('type')=='agentMessage':
                    self.text+=item.get('text','')
                    if len(self.text)>65536:raise RunnerError('OUTPUT_TOO_LARGE')
            else:
                if params.get('turn',{}).get('status')!='completed':raise RunnerError('EXECUTION_FAILED')
                try:
                    report=json.loads(self.text,object_pairs_hook=_object)
                    if not isinstance(report,dict) or set(report)!={'summary','sections','claims','unknowns'}:raise ValueError
                    if not isinstance(report['summary'],str) or not report['summary'] or not isinstance(report['sections'],dict):raise ValueError
                    if set(report['sections'])-set(SECTION_IDS) or not all(isinstance(t,str) for t in report['sections'].values()):raise ValueError
                    if not isinstance(report['claims'],list) or not isinstance(report['unknowns'],list):raise ValueError
                except (ValueError,TypeError):raise RunnerError('EXECUTION_FAILED') from None
                self.result=report;self.phase='completed';self.close()
        elif method=='error':raise RunnerError('EXECUTION_FAILED')
        # Other notifications are observational, never commands or approval.

    def _bind_turn(self,turn):
        if not isinstance(turn,str) or not turn or self.turn not in (None,turn):raise RunnerError('POLICY_DENIED')
        self.turn=turn
