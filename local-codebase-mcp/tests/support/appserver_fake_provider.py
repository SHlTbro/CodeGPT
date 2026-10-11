"""Owned loopback Responses/SSE fixture. No authentication or real inference.

Capture complete request.tools; all returned tokens/text are SIMULATED. Never
use this provider for user requests or identity/model/billing acceptance.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


def registry_names(tools):
    names=[]
    for tool in tools:
        kind=tool.get('type')
        if kind=='namespace':
            names.extend(registry_names(tool.get('tools',[])))
        elif isinstance(tool.get('name'),str):names.append(tool['name'])
        elif isinstance(tool.get('function'),dict):names.append(tool['function'].get('name'))
        else:names.append('TYPE:'+str(kind))
    return names


def request_tools(body):
    """Codex Responses-lite puts tool schemas in additional_tools input items."""
    tools=list(body.get('tools',[]))
    for item in body.get('input',[]):
        if isinstance(item,dict) and item.get('type')=='additional_tools':
            tools.extend(item.get('tools',[]))
    return tools


class FakeResponsesProvider:
    def __init__(self, calls=None, *, completion_text=None):
        self.calls=list(calls if calls is not None else [('task_read',{'path':'main.py'}),
                         ('exec_command',{'command':'OWNED_NEVER_EXECUTE_FIXTURE'})])
        self.completion_text = ('SIMULATED fixture completion; not a real model result.' if completion_text is None else
                                json.dumps(completion_text) if isinstance(completion_text,dict) else completion_text)
        if not isinstance(self.completion_text,str):raise TypeError('completion_text must be str or dict')
        self.requests=[]
        self.errors=[]
        self.lock=threading.Lock()
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                # Never record/replay an Authorization value, even in a fixture.
                if self.headers.get('Authorization'):
                    fixture.errors.append('AUTHORIZATION_REFUSED');self.send_error(401);return
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=2097152:raise ValueError('body budget')
                    raw=self.rfile.read(size);body=json.loads(raw)
                    if not isinstance(body,dict):raise ValueError('shape')
                    with fixture.lock:
                        index=len(fixture.requests)
                        if index>=8:raise ValueError('request budget')
                        fixture.requests.append({'path':self.path,'body':body,'raw_utf8':raw.decode('utf-8'),
                                                 'simulation':True})
                    events=fixture.events(index,body)
                    payload=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode('utf-8')
                    self.send_response(200);self.send_header('Content-Type','text/event-stream')
                    self.send_header('Content-Length',str(len(payload)));self.end_headers()
                    self.wfile.write(payload);self.wfile.flush()
                except (ValueError,OSError,TypeError) as error:
                    fixture.errors.append(type(error).__name__)
                    if not isinstance(error,OSError):self.send_error(400)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/v1'

    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(2)

    def events(self,index,body):
        identifier='resp_fixture_'+str(index)
        events=[{'type':'response.created','response':{'id':identifier,'status':'in_progress','output':[]}}]
        if index<len(self.calls):
            name,args=self.calls[index]
            # A purportedly unknown call must not execute a discovered native
            # implementation. Report that bad fixture plan rather than invoke it.
            known=registry_names(request_tools(body))
            if name not in ('task_list','task_search','task_read') and name in known:
                self.errors.append('NATIVE_TOOL_PRESENT:'+name)
                name='unregistered_fixture_tool'
            argument=json.dumps(args)
            item={'id':'fc_fixture_'+str(index),'call_id':'call_fixture_'+str(index),
                  'type':'function_call','name':name,'arguments':'','status':'in_progress'}
            events.append({'type':'response.output_item.added','output_index':0,'item':dict(item)})
            events.append({'type':'response.function_call_arguments.delta','item_id':item['id'],
                           'output_index':0,'delta':argument})
            item.update(arguments=argument,status='completed')
            events.append({'type':'response.function_call_arguments.done','item_id':item['id'],
                           'output_index':0,'arguments':argument})
        else:
            text=self.completion_text
            item={'id':'msg_fixture_'+str(index),'type':'message','role':'assistant','content':[],
                  'status':'in_progress'}
            events.append({'type':'response.output_item.added','output_index':0,'item':dict(item)})
            events.append({'type':'response.output_text.delta','item_id':item['id'],
                           'output_index':0,'content_index':0,'delta':text})
            item.update(status='completed',content=[{'type':'output_text','text':text,'annotations':[]}])
        events.append({'type':'response.output_item.done','output_index':0,'item':item})
        events.append({'type':'response.completed','response':{'id':identifier,'status':'completed',
                      'output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2,
                      'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}})
        return events
