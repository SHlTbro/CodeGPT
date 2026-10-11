"""Three task-bound snapshot readers. Not an OS or model-tool sandbox.

Trusted task service constructs this instance; model input cannot select owner,
project, root, scope or snapshot. No shell, writes, live registry or network IO.
"""
from copy import deepcopy
import hashlib
import json
import os

from errors import McpError
from security import MAX_CHARS, MAX_SEARCH_RESULTS, LIST_FILES_MAX_ENTRIES, resolve_path
from local_agents.snapshot import read_snapshot_evidence


class ReadToolError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)  # Never echo untrusted paths or content.


def _text(value, limit):
    return isinstance(value,str) and bool(value) and len(value)<=limit and not any(ord(c)<32 for c in value)


class ReadTools:
    def __init__(self, project, snapshot, *, owner_id, task_id):
        if not _text(owner_id,128) or not _text(task_id,128):
            raise ReadToolError('INVALID_ARGUMENT')
        root=resolve_path(project,'.')
        key=os.path.normcase(str(root)).replace('\\','/')
        expected=hashlib.sha256(json.dumps(key,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if (not isinstance(snapshot,dict) or snapshot.get('project_id')!=project.id or
                snapshot.get('root_fingerprint')!=expected or not isinstance(snapshot.get('files'),dict) or
                not isinstance(snapshot.get('snapshot_id'),str)):
            raise ReadToolError('INVALID_ARGUMENT')
        # These are a capability closed over trusted accepted task state.
        self._project,self._snapshot=project,deepcopy(snapshot)
        self._owner,self._task=owner_id,task_id

    def tool_specs(self):
        result=[]
        for name,fields,required in (
            ('task_list',{},[]),
            ('task_search',{'query':{'type':'string','minLength':1,'maxLength':256}},['query']),
            ('task_read',{'path':{'type':'string','minLength':1,'maxLength':4096},
                          'line_start':{'type':'integer','minimum':1},
                          'line_end':{'type':'integer','minimum':1}},['path'])):
            result.append({'name':name,'description':'Read only within this accepted task snapshot.',
                'inputSchema':{'type':'object','properties':fields,'required':required,'additionalProperties':False}})
        return result

    def dispatch(self,name,args):
        schemas={item['name']:item['inputSchema'] for item in self.tool_specs()}
        if not isinstance(name,str) or name not in schemas:
            raise ReadToolError('CAPABILITY_UNAVAILABLE')
        schema=schemas[name]
        if (not isinstance(args,dict) or set(args)-schema['properties'].keys() or
                set(schema['required'])-args.keys()):
            raise ReadToolError('INVALID_ARGUMENT')
        if name=='task_list':
            files=sorted(self._snapshot['files'])
            selected=[];budget=0
            for path in files[:LIST_FILES_MAX_ENTRIES]:
                budget+=len(path)+4
                if budget>MAX_CHARS:break
                selected.append(path)
            return {'files':selected,'truncated':len(selected)!=len(files),
                    'snapshot_id':self._snapshot['snapshot_id'],'source':'captured_inventory',
                    'freshness':self._snapshot['state']}
        if name=='task_read':
            return self._read(args['path'],args.get('line_start',1),args.get('line_end'))
        query=args['query']
        if not _text(query,256):raise ReadToolError('INVALID_ARGUMENT')
        matches,gaps=[],[];budget=0;truncated=False
        paths=sorted(self._snapshot['files'])
        if len(paths)>LIST_FILES_MAX_ENTRIES:truncated=True
        for path in paths[:LIST_FILES_MAX_ENTRIES]:
            try: excerpt=self._read(path)['excerpt']
            except ReadToolError as error:
                budget+=len(path)+80
                if budget>MAX_CHARS:truncated=True;break
                gaps.append({'path':path,'reason':error.code});continue
            for number,line in enumerate(excerpt.splitlines(),1):
                if query not in line:continue
                text=line[:500];budget+=len(path)+len(text)+80
                if len(matches)>=MAX_SEARCH_RESULTS or budget>MAX_CHARS:
                    truncated=True;break
                match={'path':path,'line':number,'text':text}
                if len(line)>500:match['text_truncated']=True
                matches.append(match)
            if truncated and (len(matches)>=MAX_SEARCH_RESULTS or budget>MAX_CHARS):break
        return {'matches':matches,'gaps':gaps,'truncated':truncated,
                'snapshot_id':self._snapshot['snapshot_id'],'source':'verified_snapshot_bytes'}

    def _read(self,path,line_start=1,line_end=None):
        if (not _text(path,4096) or path.startswith('@') or
                any(part=='..' for part in path.replace('\\','/').split('/')) or
                type(line_start) is not int or line_start<1 or
                line_end is not None and (type(line_end) is not int or line_end<line_start)):
            raise ReadToolError('INVALID_ARGUMENT')
        try:
            result=read_snapshot_evidence(self._project,self._snapshot,path,
                                         line_start=line_start,line_end=line_end)
        except (ValueError,McpError,OSError,UnicodeError):
            raise ReadToolError('SOURCE_UNAVAILABLE') from None
        if result['validation']!='verified':raise ReadToolError('SOURCE_CHANGED')
        return result
