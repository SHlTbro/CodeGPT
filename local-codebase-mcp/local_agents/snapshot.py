"""Content snapshots for an already-authorized Project, not an OS sandbox.

Public seam: capture_snapshot, compare_snapshots, read_snapshot_evidence.
Callers must pass configured sensitive/excluded rules and every in-project
task/log/report storage path via internal_paths. Gitignore is deliberately not
a reading authorization policy: ignored visible source is content-hashed too.
Two independent inventories detect observed changes; they are not an atomic
filesystem transaction. Evidence is re-read and verified before returning text.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from errors import McpError
from projects import Project
from security import (
    DEFAULT_EXCLUDED_DIRS, DEFAULT_SENSITIVE_PATTERNS, MAX_CHARS,
    MAX_FILE_SIZE_BYTES, is_sensitive, resolve_path,
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _key(path):
    return os.path.normcase(str(path)).replace('\\', '/')


def _relative(root, path):
    rel = path.relative_to(root).as_posix()
    return '.' if rel == '.' else rel


def _under(path, prefixes):
    path = os.path.normcase(path).replace('\\', '/')
    return any(prefix == '.' or path == prefix or path.startswith(prefix + '/') for prefix in prefixes)


def _exclusion(rel, scope):
    if _under(rel, scope['internal_paths']):
        return 'internal_output'
    if any(_key(part) in scope['excluded_dirs'] for part in rel.split('/')):
        return 'excluded_directory'
    if is_sensitive(rel, scope['sensitive_patterns']) or is_sensitive(rel + '/', scope['sensitive_patterns']):
        return 'sensitive'
    return None


def _opened_path(file, fallback):
    """Check the actual Windows file handle before reading its contents."""
    if os.name != 'nt':
        return fallback.resolve()
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_path = kernel.GetFinalPathNameByHandleW
    get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    count = get_path(msvcrt.get_osfhandle(file.fileno()), buffer, len(buffer), 0)
    if count == 0 or count >= len(buffer):
        raise OSError('cannot verify opened file path')
    value = buffer.value
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return Path(value)


class _FileGap(Exception):
    def __init__(self, reason):
        self.reason = reason


def _read(project, rel, scope, limit, root):
    if _key(resolve_path(project, '.')) != _key(root):
        raise _FileGap('root_changed')
    target = resolve_path(project, rel)
    resolved_rel = _relative(root, target)
    if _exclusion(resolved_rel, scope) or not _under(os.path.normcase(resolved_rel), scope['paths']):
        raise _FileGap('outside_scope')
    metadata = (root / rel).lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
        raise _FileGap('link_not_followed')
    if metadata.st_nlink > 1:
        raise _FileGap('multiple_hard_links')
    with target.open('rb') as file:
        opened = _opened_path(file, target)
        try:
            opened_rel = _relative(root, opened)
        except ValueError:
            raise _FileGap('outside_scope') from None
        if _exclusion(opened_rel, scope) or not _under(os.path.normcase(opened_rel), scope['paths']):
            raise _FileGap('outside_scope')
        before = os.fstat(file.fileno())
        if before.st_nlink > 1:
            raise _FileGap('multiple_hard_links')
        if before.st_size > limit:
            raise _FileGap('too_large')
        data = file.read(limit + 1)
        after = os.fstat(file.fileno())
    if len(data) > limit:
        raise _FileGap('too_large')
    if _key(resolve_path(project, '.')) != _key(root):
        raise _FileGap('root_changed')
    current = resolve_path(project, rel).stat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_nlink, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise _FileGap('changed_during_read')
    return data


def _scan(project, scope, limit, root):
    files, directories, excluded, gaps = {}, set(), set(), []
    visited = set()
    enumerated, unknown_enumeration = 0, False

    def visit(rel):
        nonlocal enumerated, unknown_enumeration
        if _exclusion(rel, scope):
            excluded.add(rel)
            return
        metadata = None
        try:
            if _key(resolve_path(project, '.')) != _key(root):
                raise _FileGap('root_changed')
            target = resolve_path(project, rel)
            resolved_rel = _relative(root, target)
            if _exclusion(resolved_rel, scope) or not _under(os.path.normcase(resolved_rel), scope['paths']):
                excluded.add(rel)
                return
            if _key(target) in visited:
                return
            metadata = (root / rel).lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                excluded.add(rel)
                gaps.append({'path': rel, 'reason': 'link_not_followed'})
                return
            rel = resolved_rel
            visited.add(_key(target))
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(rel)
                with os.scandir(target) as entries:
                    children = sorted(entry.name for entry in entries)
                for name in children:
                    visit(name if rel == '.' else rel + '/' + name)
            elif stat.S_ISREG(metadata.st_mode):
                enumerated += 1
                data = _read(project, rel, scope, limit, root)
                files[rel] = {'content_sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
            else:
                gaps.append({'path': rel, 'reason': 'unsupported_file_type'})
        except McpError:
            excluded.add(rel)
            gaps.append({'path': rel, 'reason': 'outside_scope'})
        except _FileGap as error:
            gaps.append({'path': rel, 'reason': error.reason})
            if error.reason == 'root_changed':
                unknown_enumeration = True
        except OSError:
            gaps.append({'path': rel, 'reason': 'unreadable'})
            if metadata is None or stat.S_ISDIR(metadata.st_mode):
                unknown_enumeration = True

    for rel in scope['paths']:
        visit(rel)
    return files, sorted(directories), sorted(excluded), gaps, None if unknown_enumeration else enumerated


def _git(project, scope, root):
    if _key(resolve_path(project, '.')) != _key(root):
        return None, [{'path': '.', 'reason': 'root_changed'}]
    marker = root / '.git'
    try:
        metadata = marker.lstat()
        git_dir = resolve_path(project, '.git')
    except FileNotFoundError:
        return None, []
    except McpError:
        return None, [{'path': '.git', 'reason': 'git_metadata_outside_scope'}]
    except OSError:
        return None, [{'path': '.git', 'reason': 'git_metadata_unavailable'}]
    # A gitfile can point at another project's index/tree. No metadata-root
    # authorization exists here, so refuse all gitfiles without even reading
    # their pointer text. Linked metadata directories are also unsupported.
    if not stat.S_ISDIR(metadata.st_mode):
        return None, [{'path': '.git', 'reason': 'gitfile_metadata_not_supported'}]
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
        return None, [{'path': '.git', 'reason': 'git_metadata_link_not_supported'}]
    # Positive allowlist: inherited GIT_DIR, credentials and config injections
    # cannot redirect the fixture command or load global hooks/fsmonitor.
    env = {name: os.environ[name] for name in ('PATH', 'PATHEXT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP') if name in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    base = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
            '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=' + os.devnull,
            '--git-dir=' + str(git_dir), '--work-tree=' + str(root)]

    def run(arguments, unborn=False):
        current = marker.lstat()
        if (_key(resolve_path(project, '.')) != _key(root) or
                _key(resolve_path(project, '.git')) != _key(git_dir) or
                not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode) or
                getattr(current, 'st_file_attributes', 0) & 0x400):
            raise OSError('Git metadata root changed')
        proc = subprocess.run(base + arguments, cwd=root, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=15, check=False)
        if unborn and proc.returncode == 1:
            return None
        if proc.returncode:
            raise OSError('git metadata unavailable')
        return proc.stdout

    def visible(path):
        return _under(os.path.normcase(path), scope['paths']) and not _exclusion(path, scope)

    try:
        head = run(['rev-parse', '--verify', '--quiet', 'HEAD'], unborn=True)
        index = {}
        for item in run(['ls-files', '--stage', '-z']).split(b'\0'):
            if item:
                meta, path = item.split(b'\t', 1)
                path = path.decode('utf-8')
                if visible(path):
                    index.setdefault(path, []).append(meta.decode('ascii'))
        tree = {}
        if head:
            for item in run(['ls-tree', '-r', '-z', head.decode('ascii').strip()]).split(b'\0'):
                if item:
                    meta, path = item.split(b'\t', 1)
                    path = path.decode('utf-8')
                    if visible(path):
                        mode, kind, oid = meta.decode('ascii').split()
                        tree[path] = mode + ' ' + oid + ' 0'
        staged = {}
        for path in sorted(index.keys() | tree.keys()):
            if path not in index:
                staged[path] = 'deleted'
            elif path not in tree:
                staged[path] = 'added'
            elif index[path] != [tree[path]]:
                staged[path] = 'modified'
        # Never call status/diff/diff-files: even read-only Git worktree checks
        # can execute clean filters. Index/tree plumbing does not inspect worktree
        # bytes; those are independently hashed by _scan without Git conversion.
        return {'head': head.decode('ascii').strip() if head else None,
                'index': index, 'head_tree': tree, 'staged': staged}, []
    except (OSError, McpError, subprocess.TimeoutExpired, UnicodeError, ValueError, StopIteration):
        return None, [{'path': '.git', 'reason': 'git_metadata_unavailable'}]


def _changes(before, after):
    left, right = before['files'], after['files']
    return {'added': sorted(right.keys() - left.keys()), 'removed': sorted(left.keys() - right.keys()),
            'modified': sorted(path for path in left.keys() & right.keys() if left[path] != right[path]),
            'directories_changed': before['directories'] != after['directories'],
            'git_changed': before['git'] != after['git']}


def _changed(changes):
    return any(changes.values())


def capture_snapshot(project: Project, *, paths=('.',), internal_paths=(),
                     sensitive_patterns=DEFAULT_SENSITIVE_PATTERNS,
                     excluded_dirs=DEFAULT_EXCLUDED_DIRS,
                     max_file_size_bytes=MAX_FILE_SIZE_BYTES):
    """Hash visible source twice; return relative evidence and honest coverage.

    Project must come from the authorized registry, never an untrusted MCP path.
    internal_paths are project-relative output roots; excluded contents are never
    hashed, so writes to task/log/report storage do not invalidate the snapshot.
    """
    if not paths or isinstance(paths, str) or isinstance(internal_paths, str):
        raise ValueError('paths/internal_paths must be sequences of project-relative paths')
    if isinstance(max_file_size_bytes, bool) or not isinstance(max_file_size_bytes, int) or max_file_size_bytes < 1:
        raise ValueError('max_file_size_bytes must be a positive integer')
    root = resolve_path(project, '.')
    scope = {'paths': sorted({_key(_relative(root, resolve_path(project, path))) for path in paths}),
             'internal_paths': sorted({_key(_relative(root, resolve_path(project, path))) for path in internal_paths}),
             'sensitive_patterns': sorted({_key(pattern) for pattern in sensitive_patterns}),
             'excluded_dirs': sorted({_key(directory) for directory in excluded_dirs}),
             'max_file_size_bytes': max_file_size_bytes, 'gitignore_is_authorization': False}
    if '.' in scope['internal_paths']:
        raise ValueError('internal outputs cannot exclude the entire project')
    identity = {'project_id': project.id, 'root_fingerprint': _digest(_key(root))}
    scope_fingerprint = _digest({**identity, 'scope': scope})

    def sample():
        files, directories, excluded, gaps, enumerated = _scan(project, scope, max_file_size_bytes, root)
        git, git_gaps = _git(project, scope, root)
        return {'files': files, 'directories': directories, 'git': git,
                'excluded': excluded, 'gaps': gaps + git_gaps, 'enumerated': enumerated}

    first, last = sample(), sample()
    changes = _changes(first, last)
    gaps = [dict(item) for item in {tuple(sorted(gap.items())) for gap in first['gaps'] + last['gaps']}]
    gaps.sort(key=lambda gap: (gap['path'], gap['reason']))
    partial = any(gap['reason'] != 'changed_during_read' for gap in gaps)
    state = 'partial' if partial else 'stale' if gaps or _changed(changes) else 'fresh'
    snapshot = {**identity, 'scope_fingerprint': scope_fingerprint, 'files': first['files'],
                'directories': first['directories'], 'git': first['git'], 'state': state,
                'changes_during_capture': changes,
                'coverage': {'enumerated_files': first['enumerated'],
                             'read_files': len(first['files']), 'excluded_paths': sorted(set(first['excluded'] + last['excluded'])),
                             'scope': scope, 'execution_performed': False, 'gaps': gaps}}
    # Excluded path existence and timestamps are display metadata, not source
    # identity. Creating a configured internal output directory is harmless.
    snapshot['snapshot_id'] = _digest({'scope': scope_fingerprint, 'files': first['files'],
                                       'directories': first['directories'], 'git': first['git'],
                                       'gaps': gaps, 'state': state, 'changes': changes})
    return snapshot


def compare_snapshots(before, after):
    """Changes relative to the original report; gaps can never become fresh."""
    if (before['project_id'], before['root_fingerprint']) != (after['project_id'], after['root_fingerprint']):
        raise ValueError('snapshots belong to different authorized projects')
    changes = _changes(before, after)
    changes['scope_changed'] = before['scope_fingerprint'] != after['scope_fingerprint']
    changes['state'] = ('partial' if 'partial' in (before['state'], after['state']) else
                        'stale' if _changed(changes) or 'stale' in (before['state'], after['state']) else 'fresh')
    return changes


def read_snapshot_evidence(project: Project, snapshot, path: str, *, line_start=1, line_end=None):
    """Return bounded UTF-8 lines only when current bytes match the snapshot.

    This verifies source identity and line bounds, not model semantic claims.
    A changed/unreadable source returns no excerpt instead of old line numbers.
    """
    if snapshot['project_id'] != project.id or snapshot['root_fingerprint'] != _digest(_key(resolve_path(project, '.'))):
        raise ValueError('snapshot belongs to a different authorized project')
    root = resolve_path(project, '.')
    rel = _relative(root, resolve_path(project, path))
    if rel not in snapshot['files']:
        raise ValueError('path was not captured in the authorized snapshot')
    expected = snapshot['files'][rel]['content_sha256']
    result = {'path': rel, 'snapshot_id': snapshot['snapshot_id'], 'content_sha256': expected,
              'line_start': line_start, 'line_end': line_end, 'symbol': None, 'excerpt': None,
              'validation': 'invalid', 'freshness': 'stale'}
    try:
        data = _read(project, rel, snapshot['coverage']['scope'], snapshot['coverage']['scope']['max_file_size_bytes'], root)
    except (OSError, McpError, _FileGap):
        return result
    if hashlib.sha256(data).hexdigest() != expected:
        return result
    try:
        lines = data.decode('utf-8').splitlines(keepends=True)
    except UnicodeError:
        raise ValueError('evidence source is not UTF-8 text') from None
    line_end = len(lines) if line_end is None else line_end
    if (isinstance(line_start, bool) or isinstance(line_end, bool) or
            not isinstance(line_start, int) or not isinstance(line_end, int) or
            not 1 <= line_start <= line_end <= len(lines)):
        raise ValueError('evidence line range is outside the captured source')
    excerpt = ''.join(lines[line_start - 1:line_end])
    if len(excerpt) > MAX_CHARS:
        raise ValueError('evidence excerpt exceeds character budget; request fewer lines')
    result.update(line_end=line_end, excerpt=excerpt, validation='verified', freshness=snapshot['state'])
    result['evidence_id'] = _digest({key: result[key] for key in ('snapshot_id', 'path', 'line_start', 'line_end', 'content_sha256')})
    return result
