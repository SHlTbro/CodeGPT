"""Cold archives must leave discovery/search and cannot be served through MCP."""
import asyncio
import os
import subprocess
from pathlib import Path
import pytest
from config import Config
from projects import Project
from errors import McpError
import tools_services as svc


def test_archive_is_hidden_even_with_empty_custom_sensitive_patterns(tmp_path):
    private=tmp_path/'.agent-archive/project-bath/old';private.mkdir(parents=True)
    (private/'note.md').write_text('archive_marker private history',encoding='utf8')
    (tmp_path/'README.md').write_text('live source',encoding='utf8')
    cfg=Config(projects=(Project(id='p',name='P',path=tmp_path),),security={'sensitive_patterns':[],'default_excluded_dirs':[]})
    with pytest.raises(McpError) as err:svc.read_file(cfg,'p','.agent-archive/project-bath/old/note.md')
    assert err.value.payload['error']['code']=='FILE_NOT_VISIBLE'
    assert '.agent-archive' not in svc.list_files(cfg,'p')['dirs']
    assert asyncio.run(svc.search_code(cfg,'p','archive_marker'))['matches']==[]


def test_archives_exit_default_rg_and_pytest_discovery(tmp_path):
    root=Path(__file__).resolve().parents[2]
    for name in ('.ignore','pytest.ini'):
        assert (root/name).is_file(),name+' must explicitly exclude cold archives'
        (tmp_path/name).write_bytes((root/name).read_bytes())
    archive=tmp_path/'.agent-archive/project-bath/old';archive.mkdir(parents=True)
    (archive/'test_obsolete.py').write_text('def test_archived_should_not_collect():\n    assert False\n',encoding='utf8')
    tests=tmp_path/'tests';tests.mkdir()
    (tests/'test_active.py').write_text('def test_active():\n    assert True\n',encoding='utf8')
    rg=subprocess.run(['rg','--files','--hidden'],cwd=tmp_path,capture_output=True,text=True,check=True)
    assert '.agent-archive' not in rg.stdout
    import sys
    collected=subprocess.run([sys.executable,'-m','pytest','--collect-only','-q','-p','no:cacheprovider'],cwd=tmp_path,capture_output=True,text=True)
    assert collected.returncode==0,collected.stdout+collected.stderr
    assert 'test_active' in collected.stdout and 'test_archived' not in collected.stdout
