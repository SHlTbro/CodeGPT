"""Actual framed stdio peer exercising concurrent replies and document refresh."""
import json,queue,sys,threading
q=queue.Queue();docs={}
def read():
 while True:
  headers={}
  while True:
   line=sys.stdin.buffer.readline()
   if not line:return
   if line in (b'\r\n',b'\n'):break
   k,_,v=line.partition(b':');headers[k.lower()]=v.strip()
  q.put(json.loads(sys.stdin.buffer.read(int(headers[b'content-length']))))
def reply(r,result):
 b=json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}).encode()
 sys.stdout.buffer.write(b'Content-Length: %d\r\n\r\n'%len(b)+b);sys.stdout.buffer.flush()
threading.Thread(target=read,daemon=True).start()
while True:
 r=q.get();m=r.get('method');p=r.get('params',{})
 if m=='initialize':reply(r,{'capabilities':{}})
 elif m=='shutdown':reply(r,None)
 elif m=='exit':break
 elif m=='textDocument/didOpen':docs[p['textDocument']['uri']]=p['textDocument']
 elif m=='textDocument/didChange':docs[p['textDocument']['uri']].update(text=p['contentChanges'][0]['text'],version=p['textDocument']['version'])
 elif m=='test/document':reply(r,docs[p['uri']])
 elif m=='test/echo':
  try:second=q.get(timeout=.12)
  except queue.Empty:second=None
  if second:reply(second,second['params']['value'])
  reply(r,p['value'])
