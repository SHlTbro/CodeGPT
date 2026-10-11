"""LSP transport public behavior under simultaneous calls and on-disk edits."""
import sys,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import intel
from projects import Project

PEER=Path(__file__).resolve().parents[1]/'fixtures/reliability_lsp.py'
def session(tmp_path):
 return intel.LspSession(Project(id='p',name='P',path=tmp_path),[sys.executable,str(PEER)])

def test_simultaneous_requests_do_not_drop_each_others_reply(tmp_path):
 s=session(tmp_path)
 try:
  with ThreadPoolExecutor(2) as pool:
   a=pool.submit(s.request,'test/echo',{'value':'first'},.8)
   time.sleep(.03)
   b=pool.submit(s.request,'test/echo',{'value':'second'},.8)
   assert (a.result(),b.result())==('first','second')
 finally:s.close()

def test_opened_document_refreshes_and_has_correct_language(tmp_path):
 f=tmp_path/'a.ts';f.write_text('export const before = 1;',encoding='utf8')
 s=session(tmp_path)
 try:
  s.ensure_open(f)
  a=s.request('test/document',{'uri':f.as_uri()})
  f.write_text('export const after = 2;',encoding='utf8')
  s.ensure_open(f)
  b=s.request('test/document',{'uri':f.as_uri()})
  assert a['languageId']=='typescript'
  assert b['text']=='export const after = 2;'
  assert b['version']>a['version']
 finally:s.close()
