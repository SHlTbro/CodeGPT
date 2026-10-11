"""User-reported failures: real locked operational state and cold TypeScript workspace."""
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
import pytest
from config import Config
from projects import Project
import tools_services as svc

@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_search_prunes_locked_control_plane_before_reading(tmp_path):
    import msvcrt
    p=Project(id='locked',name='Locked',path=tmp_path)
    cfg=Config(projects=(p,))
    (tmp_path/'source.py').write_text('needle = 1\n',encoding='utf8')
    (tmp_path/'.setup').mkdir()
    hidden=tmp_path/'.setup'/'server.log'
    hidden.write_text('needle private\n',encoding='utf8')
    owner=tmp_path/'config.toml.setup-owner.lock'
    owner.write_bytes(b'needle locked')
    with hidden.open('r+b') as hf, owner.open('r+b') as of:
        for f in (hf,of):msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        try:
            result=asyncio.run(svc.search_code(cfg,'locked','needle'))
            assert [m['file'] for m in result['matches']]==['source.py']
            assert svc.read_batch(cfg,'locked',['*'])['files'][0]['path']=='source.py'
        finally:
            for f in (hf,of):f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

@pytest.mark.skipif(shutil.which('typescript-language-server') is None, reason='real TS server required')
def test_workspace_symbols_cold_includes_unopened_files(tmp_path):
    (tmp_path/'package.json').write_text('{"name":"cold"}',encoding='utf8')
    (tmp_path/'tsconfig.json').write_text('{"compilerOptions":{"strict":true}}',encoding='utf8')
    (tmp_path/'a.ts').write_text('export function uniqueAlpha() { return 1; }\n',encoding='utf8')
    (tmp_path/'b.ts').write_text('export function uniqueBeta() { return 2; }\n',encoding='utf8')
    cfg=Config(projects=(Project(id='ts',name='TS',path=tmp_path),))
    try:
        result=svc.workspace_symbols(cfg,'ts','unique')
        assert {s['name'] for s in result['symbols']} >= {'uniqueAlpha','uniqueBeta'}
    finally:
        if svc._INTEL[0] is cfg:svc._INTEL[1].close_all();svc._INTEL[:]=[None,None]

@pytest.mark.skipif(shutil.which('pyright-langserver') is None and not __import__('importlib').util.find_spec('pyright'),reason='real Pyright required')
def test_python_default_server_without_activated_venv(tmp_path):
    (tmp_path/'requirements.txt').write_text('',encoding='utf8')
    (tmp_path/'main.py').write_text('def uniquePython():\n    return 1\n\nuniquePython()\n',encoding='utf8')
    cfg=Config(projects=(Project(id='py',name='Python',path=tmp_path),))
    try:
        result=svc.find_definition(cfg,'py','main.py',4,3)
        assert result['locations']==[{'path':'main.py','line':1,'character':5}]
    finally:
        if svc._INTEL[0] is cfg:svc._INTEL[1].close_all();svc._INTEL[:]=[None,None]


@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing semantics')
def test_locked_file_tools_return_actionable_error(tmp_path):
    import hashlib,msvcrt
    from fastmcp import Client
    from server import build_server
    (tmp_path/'docs').mkdir()
    f=tmp_path/'docs'/'locked.md';f.write_bytes(b'original\n')
    cfg=Config(projects=(Project(id='locked',name='Locked',path=tmp_path,write_enabled=True),))
    sha=hashlib.sha256(f.read_bytes()).hexdigest()
    async def calls():
        async with Client(build_server(cfg),mode='legacy') as c:
            for name,args in [('read_file',{'path':'docs/locked.md'}),('write_file',{'path':'docs/locked.md','content':'changed','content_sha256':sha})]:
                result=await c.call_tool(name,{'project_id':'locked',**args})
                d=result.data or result.structured_content
                assert d['error']['code']=='INTERNAL_ERROR'
                assert '关闭' in d['error']['hint'] and '权限' in d['error']['hint']
                assert 'Traceback' not in d['error']['message']
    with f.open('r+b') as stream:
        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        try:asyncio.run(calls())
        finally:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    assert f.read_bytes()==b'original\n'


@pytest.mark.parametrize('language',['python','typescript'])
def test_first_diagnostics_and_real_rename_plan(tmp_path,language):
    if language=='python':
        pytest.importorskip('pyright')
        marker='requirements.txt';ext='py'
        source='def uniqueGreet(name: str) -> str:\n    return "hi " + name\n\nuniqueGreet("world")\n'
        bad='broken: int = "wrong"\n';line=4
    else:
        if shutil.which('typescript-language-server') is None:pytest.skip('real TS required')
        marker='package.json';ext='ts'
        source='export function uniqueGreet(name: string) { return "hi " + name; }\nuniqueGreet("world");\n'
        bad='const broken: number = "wrong";\n';line=2
        (tmp_path/'tsconfig.json').write_text('{"compilerOptions":{"strict":true}}',encoding='utf8')
    (tmp_path/marker).write_text('{}' if ext=='ts' else '',encoding='utf8')
    (tmp_path/f'a.{ext}').write_text(source,encoding='utf8')
    (tmp_path/f'bad.{ext}').write_text(bad,encoding='utf8')
    cfg=Config(projects=(Project(id='p',name=language,path=tmp_path,write_enabled=True),))
    try:
        diagnostics=svc.get_diagnostics(cfg,'p',f'bad.{ext}')
        assert any(d['severity']=='error' for d in diagnostics['diagnostics'])
        assert len(diagnostics['diagnostics'])==1
        preview=svc.rename_preview(cfg,'p',f'a.{ext}',line,3,'betterGreet')
        assert preview['edit_count']==2 and preview['file_count']==1
        before=svc.read_file(cfg,'p',f'a.{ext}')
        result=svc.apply_workspace_edit(cfg,'p',preview['files'],{f'a.{ext}':before['content_sha256']})
        assert result['files_changed']==1
        text=(tmp_path/f'a.{ext}').read_text(encoding='utf8')
        assert 'uniqueGreet' not in text and text.count('betterGreet')==2
        definitions=svc.find_definition(cfg,'p',f'a.{ext}',line,3)
        assert definitions['locations']
        (tmp_path/f'bad.{ext}').write_text('broken: int = 1\n' if ext=='py' else 'const broken: number = 1;\n',encoding='utf8')
        assert svc.get_diagnostics(cfg,'p',f'bad.{ext}')['diagnostics']==[]
    finally:
        if svc._INTEL[0] is cfg:svc._INTEL[1].close_all();svc._INTEL[:]=[None,None]
