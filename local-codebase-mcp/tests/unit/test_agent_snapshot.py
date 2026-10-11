"""Public snapshot behavior on disposable plain/Git and native Windows fixtures."""
from __future__ import annotations

import ctypes
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from errors import McpError
from projects import Project


def api():
    assert importlib.util.find_spec('local_agents.snapshot') is not None, 'snapshot public interface missing'
    return importlib.import_module('local_agents.snapshot')


@pytest.fixture
def project(tmp_path):
    root = tmp_path / '中文 项目'
    root.mkdir()
    (root / 'main.py').write_text('value = 1\n', encoding='utf-8')
    return Project('fixture', 'Fixture', root)


def test_plain_content_hash_detects_equal_size_equal_mtime(project):
    module = api()
    old = module.capture_snapshot(project)
    path = project.path / 'main.py'
    before = path.stat()
    path.write_text('value = 2\n', encoding='utf-8')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    new = module.capture_snapshot(project)
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert old['snapshot_id'] != new['snapshot_id']
    assert module.compare_snapshots(old, new)['modified'] == ['main.py']
    assert new['files']['main.py']['content_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_plain_add_delete_rename_and_stable_repetition(project):
    module = api()
    old = module.capture_snapshot(project)
    assert module.capture_snapshot(project)['snapshot_id'] == old['snapshot_id']
    (project.path / 'main.py').rename(project.path / 'renamed.py')
    (project.path / 'new.txt').write_bytes(b'new')
    new = module.capture_snapshot(project)
    diff = module.compare_snapshots(old, new)
    assert diff['added'] == ['new.txt', 'renamed.py']
    assert diff['removed'] == ['main.py']
    assert diff['state'] == 'stale'
    (project.path / 'new.txt').unlink()
    assert module.compare_snapshots(new, module.capture_snapshot(project))['removed'] == ['new.txt']


def test_sensitive_and_internal_outputs_pruned_without_invalidation(project, monkeypatch):
    module = api()
    root = project.path
    for name in ('.ssh', '.setup', 'node_modules', 'private-runtime'):
        (root / name).mkdir()
        (root / name / 'fixture.txt').write_text('synthetic', encoding='utf-8')
    (root / '.env').write_text('SYNTHETIC=1', encoding='utf-8')
    (root / '.env.example').write_text('public', encoding='utf-8')
    real_scandir = os.scandir

    def guarded_scandir(path):
        assert Path(path).name not in {'.ssh', '.setup', 'node_modules', 'private-runtime'}
        return real_scandir(path)

    monkeypatch.setattr(os, 'scandir', guarded_scandir)
    old = module.capture_snapshot(project, internal_paths=('private-runtime',))
    for name in ('.ssh', '.setup', 'node_modules', 'private-runtime'):
        (root / name / 'fixture.txt').write_text('changed internal data', encoding='utf-8')
    (root / '.env').write_text('SYNTHETIC=2', encoding='utf-8')
    new = module.capture_snapshot(project, internal_paths=('private-runtime',))
    assert old['snapshot_id'] == new['snapshot_id']
    assert set(new['files']) == {'main.py', '.env.example'}
    assert {'.ssh', '.setup', 'node_modules', 'private-runtime', '.env'} <= set(new['coverage']['excluded_paths'])
    assert new['coverage']['scope']['internal_paths'] == ['private-runtime']
    assert new['coverage']['execution_performed'] is False


@pytest.mark.parametrize('path', ['../outside', 'D:/outside', 'd:outside', '//server/share', '\\\\server\\share'])
def test_scope_rejects_escape_drive_and_unc(project, path):
    with pytest.raises(McpError):
        api().capture_snapshot(project, paths=(path,))


def test_scope_fingerprint_and_cross_project_separation(project):
    module = api()
    (project.path / 'src').mkdir()
    (project.path / 'src/code.py').write_bytes(b'code')
    full = module.capture_snapshot(project)
    scoped = module.capture_snapshot(project, paths=('src',))
    assert set(scoped['files']) == {'src/code.py'}
    assert scoped['scope_fingerprint'] != full['scope_fingerprint']
    assert module.compare_snapshots(full, scoped)['state'] == 'stale'
    other = Project('different-owner-project', 'Different', project.path)
    with pytest.raises(ValueError):
        module.compare_snapshots(full, module.capture_snapshot(other))


def test_evidence_matches_hash_lines_and_rejects_changed_source(project):
    module = api()
    (project.path / 'main.py').write_bytes('第一行\nsecond\nthird\n'.encode('utf-8'))
    snapshot = module.capture_snapshot(project)
    evidence = module.read_snapshot_evidence(project, snapshot, 'main.py', line_start=2, line_end=2)
    assert evidence['validation'] == 'verified'
    assert evidence['excerpt'] == 'second\n'
    assert evidence['snapshot_id'] == snapshot['snapshot_id']
    assert evidence['line_start'] == evidence['line_end'] == 2
    with pytest.raises(ValueError):
        module.read_snapshot_evidence(project, snapshot, 'main.py', line_start=8, line_end=8)
    (project.path / 'main.py').write_text('different\n', encoding='utf-8')
    evidence = module.read_snapshot_evidence(project, snapshot, 'main.py', line_start=2, line_end=2)
    assert evidence['validation'] == 'invalid'
    assert evidence['excerpt'] is None
    assert evidence['freshness'] == 'stale'


def test_change_during_capture_is_not_fresh(project, monkeypatch):
    module = api()
    (project.path / 'z.txt').write_bytes(b'last')
    original_open = Path.open
    changed = False

    def mutating_open(path, *args, **kwargs):
        nonlocal changed
        if path.name == 'z.txt' and not changed:
            changed = True
            (project.path / 'main.py').write_bytes(b'value = 9\n')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', mutating_open)
    snapshot = module.capture_snapshot(project)
    assert changed
    assert snapshot['state'] == 'stale'
    assert snapshot['changes_during_capture']['modified'] == ['main.py']


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result.stdout


def test_git_staged_unstaged_untracked_ignored_and_index_changes(project):
    module = api()
    root = project.path
    git(root, 'init')
    git(root, 'add', 'main.py')
    git(root, '-c', 'user.name=T12 Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
    clean = module.capture_snapshot(project)
    (root / 'main.py').write_bytes(b'value = 2\n')
    unstaged = module.capture_snapshot(project)
    assert unstaged['files']['main.py'] != clean['files']['main.py']
    assert unstaged['git']['index'] == clean['git']['index']
    git(root, 'add', 'main.py')
    index_hash = hashlib.sha256((root / '.git/index').read_bytes()).hexdigest()
    staged = module.capture_snapshot(project)
    assert hashlib.sha256((root / '.git/index').read_bytes()).hexdigest() == index_hash
    assert staged['git']['staged']['main.py'] == 'modified'
    assert module.compare_snapshots(unstaged, staged)['git_changed']
    (root / 'main.py').write_bytes(b'value = 3\n')
    (root / 'untracked.py').write_bytes(b'untracked')
    (root / '.gitignore').write_text('ignored.txt\n', encoding='utf-8')
    (root / 'ignored.txt').write_bytes(b'ignored A')
    dirty = module.capture_snapshot(project)
    assert dirty['files']['main.py'] != staged['files']['main.py']
    assert dirty['git']['staged']['main.py'] == 'modified'
    assert {'untracked.py', 'ignored.txt'} <= set(dirty['files'])
    (root / 'ignored.txt').write_bytes(b'ignored B')
    assert module.compare_snapshots(dirty, module.capture_snapshot(project))['modified'] == ['ignored.txt']
    assert module.compare_snapshots(clean, dirty)['state'] == 'stale'


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows filesystem test')
def test_windows_case_normalization_and_real_junction_exclusion(project, tmp_path):
    module = api()
    normal = module.capture_snapshot(project)
    cased = Project(project.id, project.name, Path(str(project.path).swapcase()))
    assert module.capture_snapshot(cased)['scope_fingerprint'] == normal['scope_fingerprint']
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_text('synthetic outside', encoding='utf-8')
    link = project.path / 'escape'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
    assert result.returncode == 0
    try:
        snapshot = module.capture_snapshot(project)
        assert 'escape/private.txt' not in snapshot['files']
        assert snapshot['state'] == 'partial'
        assert any(g['path'] == 'escape' and g['reason'] == 'outside_scope' for g in snapshot['coverage']['gaps'])
        with pytest.raises(McpError):
            module.capture_snapshot(project, paths=('escape',))
    finally:
        os.rmdir(link)  # Remove verified junction only, never recursively delete target.
    assert (outside / 'private.txt').read_text(encoding='utf-8') == 'synthetic outside'


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows sharing violation test')
def test_unreadable_file_is_real_coverage_gap(project):
    module = api()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    handle = kernel.CreateFileW(str(project.path / 'main.py'), 0x80000000, 0, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        snapshot = module.capture_snapshot(project)
        assert snapshot['state'] == 'partial'
        assert any(g['path'] == 'main.py' and g['reason'] == 'unreadable' for g in snapshot['coverage']['gaps'])
        assert module.compare_snapshots(snapshot, snapshot)['state'] == 'partial'
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows junction pruning test')
def test_in_project_junction_to_private_directory_is_not_traversed(project):
    module = api()
    private = project.path / '.setup'
    private.mkdir()
    (private / 'synthetic.txt').write_bytes(b'private test only')
    alias = project.path / 'innocent-alias'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(private)], capture_output=True)
    assert result.returncode == 0
    try:
        snapshot = module.capture_snapshot(project)
        assert set(snapshot['files']) == {'main.py'}
        assert 'innocent-alias' in snapshot['coverage']['excluded_paths']
    finally:
        os.rmdir(alias)


def test_creation_of_internal_output_and_case_insensitive_evidence(project):
    module = api()
    old = module.capture_snapshot(project, internal_paths=('runtime',))
    (project.path / 'runtime').mkdir()
    (project.path / 'runtime/report.txt').write_bytes(b'generated')
    new = module.capture_snapshot(project, internal_paths=('runtime',))
    assert old['snapshot_id'] == new['snapshot_id']
    (project.path / 'MixedCASE.txt').write_bytes(b'line\n')
    latest = module.capture_snapshot(project)
    path = 'mixedcase.txt' if os.name == 'nt' else 'MixedCASE.txt'
    assert module.read_snapshot_evidence(project, latest, path)['validation'] == 'verified'


def test_snapshot_does_not_execute_git_worktree_extensions(project, tmp_path):
    module = api()
    root = project.path
    git(root, 'init')
    git(root, 'add', 'main.py')
    git(root, '-c', 'user.name=T12 Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
    sentinel = tmp_path / 'extension-executed.txt'
    script = tmp_path / 'git_extension.py'
    script.write_text('import sys\nfrom pathlib import Path\nPath(' + repr(str(sentinel)) + ').write_text("EXECUTED")\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n', encoding='utf-8')
    command = '"' + sys.executable + '" "' + str(script) + '"'
    for setting in ('core.fsmonitor', 'filter.fixture.clean', 'diff.external', 'diff.fixture.textconv'):
        git(root, 'config', setting, command)
    (root / '.gitattributes').write_bytes(b'*.py filter=fixture diff=fixture\n')
    original = (root / 'main.py').stat()
    # Equal length forces Git to inspect content instead of concluding from size.
    (root / 'main.py').write_bytes(b'value = 7\r\n' if original.st_size == 11 else b'value = 7\n')
    os.utime(root / 'main.py', ns=(original.st_atime_ns, original.st_mtime_ns + 2_000_000_000))
    index = (root / '.git/index').read_bytes()
    config = (root / '.git/config').read_bytes()
    snapshot = module.capture_snapshot(project)
    assert snapshot['state'] == 'fresh'
    assert not sentinel.exists(), 'snapshot executed repository Git extension'
    assert (root / '.git/index').read_bytes() == index
    assert (root / '.git/config').read_bytes() == config


def test_multiple_hard_links_are_not_read(project, tmp_path):
    source = tmp_path / 'synthetic-private.key'
    source.write_bytes(b'synthetic private bytes')
    os.link(source, project.path / 'innocent.txt')
    snapshot = api().capture_snapshot(project)
    assert 'innocent.txt' not in snapshot['files']
    assert snapshot['state'] == 'partial'
    assert any(g['path'] == 'innocent.txt' and g['reason'] == 'multiple_hard_links' for g in snapshot['coverage']['gaps'])


def test_directory_enumeration_failure_does_not_invent_file_count(project, monkeypatch):
    (project.path / 'blocked-dir').mkdir()
    real_scandir = os.scandir

    def failing_scandir(path):
        if Path(path).name == 'blocked-dir':
            raise PermissionError('injected enumeration failure')
        return real_scandir(path)

    monkeypatch.setattr(os, 'scandir', failing_scandir)
    snapshot = api().capture_snapshot(project)
    assert snapshot['state'] == 'partial'
    assert snapshot['coverage']['enumerated_files'] is None
    assert snapshot['coverage']['read_files'] == 1


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows root rebind test')
def test_project_root_rebinding_during_capture_cannot_read_new_target(project, tmp_path, monkeypatch):
    alias = tmp_path / 'registered-root'
    outside = tmp_path / 'different-root'
    outside.mkdir()
    (outside / 'main.py').write_bytes(b'outside synthetic bytes')
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(project.path)], capture_output=True)
    assert result.returncode == 0
    registered = Project(project.id, project.name, alias)
    original_open = Path.open
    changed = False

    def rebinding_open(path, *args, **kwargs):
        nonlocal changed
        assert path.parent != outside, 'snapshot attempted new unauthorized root contents'
        if path.name == 'main.py' and not changed:
            changed = True
            os.rmdir(alias)
            rebound = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(outside)], capture_output=True)
            assert rebound.returncode == 0
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', rebinding_open)
    try:
        snapshot = api().capture_snapshot(registered)
        assert changed
        assert snapshot['state'] in ('partial', 'stale')
        assert any(g['reason'] == 'root_changed' for g in snapshot['coverage']['gaps'])
    finally:
        os.rmdir(alias)


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows case-insensitive exclusions')
def test_windows_uppercase_excluded_directories_are_pruned_before_read(project, monkeypatch):
    module = api()
    for name in ('.GIT', 'NODE_MODULES'):
        directory = project.path / name
        directory.mkdir()
        (directory / 'synthetic.txt').write_bytes(b'excluded synthetic content')
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert os.path.normcase(path.parent.name) not in ('.git', 'node_modules'), 'excluded content read'
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded_open)
    snapshot = module.capture_snapshot(project, excluded_dirs=('.git', 'NODE_MODULES'))
    assert set(snapshot['files']) == {'main.py'}
    assert {'.GIT', 'NODE_MODULES'} <= set(snapshot['coverage']['excluded_paths'])
    assert snapshot['coverage']['scope']['excluded_dirs'] == ['.git', 'node_modules']
    with pytest.raises(ValueError):
        module.read_snapshot_evidence(project, snapshot, '.GIT/synthetic.txt')


@pytest.mark.parametrize('metadata_kind', ['gitfile', 'junction'])
def test_git_metadata_cannot_follow_foreign_repository(project, tmp_path, metadata_kind, monkeypatch):
    if metadata_kind == 'junction' and sys.platform != 'win32':
        pytest.skip('native Windows Git metadata junction test')
    foreign = tmp_path / 'foreign-repo'
    foreign.mkdir()
    (foreign / 'foreign-only-private-name.txt').write_bytes(b'synthetic foreign content')
    git(foreign, 'init')
    git(foreign, 'add', 'foreign-only-private-name.txt')
    metadata = project.path / '.git'
    if metadata_kind == 'gitfile':
        metadata.write_bytes(('gitdir: ' + str(foreign / '.git') + '\n').encode('utf-8'))
    else:
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(metadata), str(foreign / '.git')], capture_output=True)
        assert result.returncode == 0
    def unexpected_subprocess(*args, **kwargs):
        pytest.fail('Git child started for unsupported/foreign metadata')
    monkeypatch.setattr(subprocess, 'run', unexpected_subprocess)
    try:
        snapshot = api().capture_snapshot(project)
        assert snapshot['git'] is None, 'foreign Git metadata leaked through project .git pointer'
        assert snapshot['state'] == 'partial'
        assert set(snapshot['files']) == {'main.py'}
        assert any(g['path'] == '.git' and g['reason'] in ('gitfile_metadata_not_supported', 'git_metadata_outside_scope') for g in snapshot['coverage']['gaps'])
    finally:
        if metadata_kind == 'junction':
            os.rmdir(metadata)
