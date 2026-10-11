"""Real owned filesystem, no CLI or model inference."""
import os
from pathlib import Path

import pytest

from local_agents.read_tools import ReadTools, ReadToolError
from local_agents.snapshot import capture_snapshot
from projects import Project


def tools(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'src').mkdir();(root/'other').mkdir()
    (root/'src/main.py').write_text('alpha = 1\nbeta = 2\n',encoding='utf-8',newline='\n')
    (root/'src/.env').write_text('OWNED_FAKE_SECRET',encoding='utf-8')
    (root/'other/out.py').write_text('OUTSIDE_TASK_SCOPE',encoding='utf-8')
    (root/'src/report').mkdir();(root/'src/report/body.txt').write_text('PRIVATE_REPORT',encoding='utf-8')
    project=Project('fixture-project','owned fixture',root)
    snapshot=capture_snapshot(project,paths=('src',),internal_paths=('src/report',))
    return ReadTools(project,snapshot,owner_id='trusted-owner',task_id='trusted-task'),root,snapshot


def test_only_three_bound_tools_with_no_identity_or_path_root_in_model_schema(tmp_path):
    reader,root,snapshot=tools(tmp_path)
    schemas=reader.tool_specs()
    assert {item['name'] for item in schemas}=={'task_list','task_search','task_read'}
    assert all(item['inputSchema']['additionalProperties'] is False for item in schemas)
    listed=reader.dispatch('task_list',{})
    assert listed['files']==['src/main.py']
    result=reader.dispatch('task_read',{'path':'src/main.py','line_start':1,'line_end':1})
    assert result['excerpt']=='alpha = 1\n' and result['validation']=='verified'
    assert result['snapshot_id']==snapshot['snapshot_id']
    assert str(root) not in str(result)
    assert 'trusted-owner' not in str(result) and 'trusted-task' not in str(result)
    hits=reader.dispatch('task_search',{'query':'beta'})
    assert hits['matches']==[{'path':'src/main.py','line':2,'text':'beta = 2'}]
    assert hits['gaps']==[]


@pytest.mark.parametrize('name,args',[
    ('shell',{}),('task_write',{}),('exec_command',{}),('view_image',{}),('spawn_agent',{}),
    ('fs/readFile',{'path':'src/main.py'}),('task_read',{'path':'src/.env'}),
    ('task_read',{'path':'other/out.py'}),('task_read',{'path':'../outside.txt'}),
    ('task_read',{'path':'@local/src/main.py'}),('task_read',{'path':'C:/Windows/win.ini'}),
    ('task_list',{'owner_id':'attacker'}),('task_read',{'path':'src/main.py','root':'other'}),
    ('task_search',{'query':'alpha','scope':['other']}),('task_read',{'path':'src/report/body.txt'}),
])
def test_forbidden_entries_and_scope_mutation_rejected_without_reading(tmp_path,name,args):
    reader,root,_=tools(tmp_path)
    with pytest.raises(ReadToolError): reader.dispatch(name,args)


def test_changed_source_does_not_return_old_hash_as_verified(tmp_path):
    reader,root,_=tools(tmp_path)
    (root/'src/main.py').write_text('changed source\n',encoding='utf-8')
    with pytest.raises(ReadToolError,match='SOURCE_CHANGED'):
        reader.dispatch('task_read',{'path':'src/main.py'})
    result=reader.dispatch('task_search',{'query':'changed'})
    assert result['matches']==[] and result['gaps']==[{'path':'src/main.py','reason':'SOURCE_CHANGED'}]


def test_link_swapped_after_capture_never_returns_outside_bytes(tmp_path):
    reader,root,_=tools(tmp_path)
    outside=tmp_path/'outside';outside.write_text('OUTSIDE_SECRET_FIXTURE',encoding='utf-8')
    (root/'src/main.py').unlink()
    os.link(outside,root/'src/main.py')
    with pytest.raises(ReadToolError):reader.dispatch('task_read',{'path':'src/main.py'})
    result=reader.dispatch('task_search',{'query':'SECRET'})
    assert result['matches']==[] and result['gaps']


def test_snapshot_is_copied_and_binding_cannot_retarget(tmp_path):
    reader,root,snapshot=tools(tmp_path)
    snapshot['files']['other/out.py']={'content_sha256':'a'*64,'size_bytes':1}
    assert reader.dispatch('task_list',{})['files']==['src/main.py']
    with pytest.raises(ReadToolError):reader.dispatch('task_read',{'path':'other/out.py'})
    with pytest.raises(ReadToolError):reader.dispatch('task_list',{'project_id':'other'})


@pytest.mark.parametrize('args',[{'path':'src/main.py','line_start':True},
    {'path':'src/main.py','line_start':0},{'path':'src/main.py','line_start':2,'line_end':1},
    {'path':'src/main.py','line_end':1000000}])
def test_invalid_read_bounds_fail_closed(tmp_path,args):
    reader,_,_=tools(tmp_path)
    with pytest.raises(ReadToolError):reader.dispatch('task_read',args)


def test_empty_binding_and_foreign_snapshot_are_not_trusted(tmp_path):
    reader,root,snapshot=tools(tmp_path)
    project=Project('different-id','other',root)
    with pytest.raises(ReadToolError):ReadTools(project,snapshot,owner_id='trusted',task_id='task')
    with pytest.raises(ReadToolError):ReadTools(Project('fixture-project','fixture',root),snapshot,owner_id='',task_id='task')


@pytest.mark.parametrize('name',[None,[],{}])
def test_unknown_malformed_entry_names_reject_with_stable_code(tmp_path,name):
    reader,_,_=tools(tmp_path)
    with pytest.raises(ReadToolError,match='CAPABILITY_UNAVAILABLE'):reader.dispatch(name,{})


def test_search_is_literal_and_large_text_is_honestly_unavailable(tmp_path):
    reader,root,_=tools(tmp_path)
    assert reader.dispatch('task_search',{'query':'alpha.*'})['matches']==[]
    (root/'src/main.py').write_text('x'*60000,encoding='utf-8')
    project=Project('fixture-project','fixture',root)
    reader=ReadTools(project,capture_snapshot(project,paths=('src',),internal_paths=('src/report',)),owner_id='owner',task_id='task')
    result=reader.dispatch('task_search',{'query':'x'})
    assert result['matches']==[] and result['gaps']==[{'path':'src/main.py','reason':'SOURCE_UNAVAILABLE'}]
