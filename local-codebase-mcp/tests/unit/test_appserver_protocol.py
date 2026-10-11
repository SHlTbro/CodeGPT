import json
import pytest
from local_agents.runner import RunnerError

def api():
    from local_agents.backends.appserver_worker import WorkerSession
    return WorkerSession

def test_protocol_binds_thread_turn_and_rejects_unknown_server_requests():
    sent=[];closed=[];calls=[]
    session=api()(send=sent.append,close=lambda:closed.append(True),tool_specs=[],
        dispatch=lambda name,args:calls.append((name,args)),thread_params={'model':'owned-no-model','ephemeral':True,'environments':[]})
    session.start()
    assert sent[0]['method']=='initialize'
    session.feed(json.dumps({'id':1,'result':{'userAgent':'fixture'}})+'\n')
    assert [x['method'] for x in sent]==['initialize','initialized','thread/start']
    session.feed(json.dumps({'id':2,'result':{'thread':{'id':'owned-thread'}}})+'\n')
    assert session.ready and closed and session.result is None
    assert calls==[]

@pytest.mark.parametrize('line',['{bad}\n',json.dumps({'id':99,'method':'command/exec','params':{}})+'\n',
    json.dumps({'id':1,'error':{'code':-32600,'message':'private'}})+'\n'])
def test_protocol_failures_never_become_completed_reports(line):
    session=api()(send=lambda value:None,close=lambda:None,tool_specs=[],dispatch=lambda *args:None,thread_params={})
    session.start()
    with pytest.raises(RunnerError):session.feed(line)
    assert session.result is None

def test_turn_completion_requires_valid_bound_report_not_normal_exit():
    sent=[]
    session=api()(send=sent.append,close=lambda:None,tool_specs=[],dispatch=lambda *a:{},thread_params={},turn_input='owned fixture')
    session.start();session.feed(json.dumps({'id':1,'result':{}})+'\n')
    session.feed(json.dumps({'id':2,'result':{'thread':{'id':'t'}}})+'\n')
    session.feed(json.dumps({'id':3,'result':{'turn':{'id':'u','status':'inProgress'}}})+'\n')
    session.feed(json.dumps({'method':'item/completed','params':{'threadId':'t','turnId':'u','item':{'id':'x','type':'agentMessage','text':'not report json'}}})+'\n')
    with pytest.raises(RunnerError):
        session.feed(json.dumps({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'u','status':'completed'}}})+'\n')
    assert session.result is None

def test_product_backend_has_worker_consumer_and_keeps_launch_closed(tmp_path):
    from local_agents.backends.appserver import AppServerBackend
    backend=AppServerBackend('')
    assert callable(backend.on_output) and callable(backend.start_worker)
    with pytest.raises(RunnerError) as error:backend.launch({'task_id':'x'},{})
    assert error.value.code=='CAPABILITY_UNAVAILABLE'

def test_task_service_has_report_producer_not_only_exit_callback():
    from local_agents.service import TaskService
    assert callable(TaskService._finish_worker)


def test_turn_attempts_count_transport_attempt_not_worker_construction():
    sent=[]
    session=api()(send=sent.append,close=lambda:None,tool_specs=[],dispatch=lambda *a:{},thread_params={},turn_input='owned fixture')
    session.start()
    assert session.turn_attempts==0
    session.feed(json.dumps({'id':1,'result':{}})+'\n')
    assert session.turn_attempts==0
    session.feed(json.dumps({'id':2,'result':{'thread':{'id':'t'}}})+'\n')
    assert session.turn_attempts==1


def test_saved_reasoning_effort_is_explicit_in_actual_turn_request():
    sent=[]
    session=api()(send=sent.append,close=lambda:None,tool_specs=[],dispatch=lambda *a:{},
        thread_params={'model':'gpt-6-luna','serviceTier':'default'},turn_input='owned fixture',reasoning_effort='medium')
    session.start()
    session.feed(json.dumps({'id':1,'result':{}})+'\n')
    session.feed(json.dumps({'id':2,'result':{'thread':{'id':'t'}}})+'\n')
    assert sent[-1]['method']=='turn/start'
    assert sent[-1]['params']['effort']=='medium'
    assert sent[-1]['params']['threadId']=='t' and session.turn_attempts==1


def test_invalid_native_effort_is_rejected_before_worker_starts():
    sent=[]
    with pytest.raises(RunnerError) as error:
        api()(send=sent.append,close=lambda:None,tool_specs=[],dispatch=lambda *a:{},
            thread_params={},turn_input='owned fixture',reasoning_effort=123)
    assert error.value.code=='UNSUPPORTED_MODEL_PARAMETER' and sent==[]
