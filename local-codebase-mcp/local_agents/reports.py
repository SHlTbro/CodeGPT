"""Private immutable report bodies, Store publication, ACL-gated stable pages.

authorize(owner_id, project_id, scope_paths, permission_fingerprint) must consult
CURRENT authority. cursor_key is a persistent private service key, never a
credential supplied by MCP. No CLI/model dispatch, raw log or hidden reasoning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid

from local_agents.contracts import CONTRACT_VERSION, validation_errors
from local_agents.evidence import verify_evidence
from local_agents.snapshot import capture_snapshot, compare_snapshots
from local_agents.store import StoreError
from security import resolve_path

SECTION_TITLES = {'summary': '摘要', 'overview': '概览', 'entries': '入口', 'modules': '模块',
    'flows': '流程', 'configuration': '配置', 'navigation': '导航', 'risks': '风险',
    'evidence': '证据', 'coverage': '覆盖范围', 'unknowns': '未知项'}
SECTION_IDS = tuple(SECTION_TITLES)
_HEX = re.compile(r'^[a-f0-9]{64}$')
_BODY_NAME = re.compile(r'^([a-f0-9]{24})\.([a-f0-9]{32})\.json$')
_REASONS = ['source_changed', 'coverage_gaps', 'scope_changed', 'evidence_invalid']


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _validate(kind, value):
    if validation_errors(kind, value):
        raise StoreError('INVALID_ARGUMENT')


def _count(response):
    response['response_chars'] = 0
    while True:
        length = len(json.dumps(response, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
        if length == response['response_chars']:
            return response
        response['response_chars'] = length


class Reports:
    def __init__(self, store, *, authorize, cursor_key, max_body_bytes=16 * 1024 * 1024,
                 max_total_bytes=128 * 1024 * 1024, max_response_chars=32000, clock=time.time):
        if not callable(authorize) or not isinstance(cursor_key, bytes) or len(cursor_key) < 32:
            raise StoreError('INVALID_ARGUMENT')
        if any(type(v) is not int or v < 1 for v in (max_body_bytes, max_total_bytes)) or type(max_response_chars) is not int or not 2048 <= max_response_chars <= 32000:
            raise StoreError('INVALID_ARGUMENT')
        self.store, self.authorize, self.cursor_key = store, authorize, cursor_key
        self.max_body_bytes, self.max_total_bytes, self.max_response_chars = max_body_bytes, max_total_bytes, max_response_chars
        self.clock, self.lock = clock, threading.RLock()
        self.root = store.results_dir / 'reports'
        try:
            self.root.mkdir(exist_ok=True)
            if self._linked(self.root):
                raise OSError
            self.root_identity = self.root.resolve()
        except OSError:
            raise StoreError('STORAGE_UNAVAILABLE') from None

    @staticmethod
    def _linked(path):
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

    def _root_ok(self):
        if self._linked(self.root) or self.root.resolve() != self.root_identity:
            raise StoreError('STORAGE_UNAVAILABLE')

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

    def _tag(self, owner):
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
            raise StoreError('INVALID_ARGUMENT')
        return hmac.new(self.cursor_key, b'owner:' + owner.encode('utf-8'), hashlib.sha256).hexdigest()[:24]

    def _allow(self, owner, document, permission):
        if not isinstance(permission, str) or not _HEX.fullmatch(permission) or self.authorize(
                owner, document['metadata']['project_id'], document['coverage']['scope'], permission) is not True:
            raise StoreError('POLICY_DENIED')

    @contextmanager
    def _writing(self):
        """OS file lock covers quota/publication/expiry across owned instances."""
        with self.lock:
            self._root_ok()
            lock_path = self.root / '.publication.lock'
            try:
                if lock_path.exists() and (self._linked(lock_path) or lock_path.stat().st_nlink > 1):
                    raise OSError
                with lock_path.open('a+b') as lock:
                    if lock.tell() == 0:
                        lock.write(b'\0')
                        lock.flush()
                        os.fsync(lock.fileno())
                    lock.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        lock.seek(0)
                        if os.name == 'nt':
                            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                raise StoreError('STORAGE_UNAVAILABLE') from None

    def _quota(self, size):
        total = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                if entry.name == '.publication.lock':
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise StoreError('STORAGE_UNAVAILABLE')
                total += info.st_size
        if size > self.max_body_bytes or total + size > self.max_total_bytes:
            raise StoreError('STORAGE_UNAVAILABLE')

    def _atomic(self, target, data):
        """Fsync complete bytes, then atomically create a non-overwriting name."""
        self._root_ok()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='.report-', suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            # link() publishes the complete inode and rejects an existing target;
            # unlike replace(), it cannot overwrite an immutable accepted body.
            os.link(temporary, target)
            temporary.unlink()
            temporary = None
            if os.name != 'nt':
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            raise StoreError('STORAGE_UNAVAILABLE') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _body_path(self, ref):
        self._root_ok()
        if not isinstance(ref, str) or not ref.startswith('reports/') or not _BODY_NAME.fullmatch(ref[8:]):
            raise StoreError('STORAGE_UNAVAILABLE')
        path = self.root / ref[8:]
        try:
            info = path.lstat()
            if self._linked(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError
            if info.st_size > self.max_body_bytes:
                raise OSError
        except OSError:
            raise StoreError('RESULT_EXPIRED') from None
        return path

    def _load(self, owner, report_id, *, check_acl=True):
        metadata = self.store.get_report(owner, report_id)
        ref, expected = self.store.get_content_reference(owner, report_id)
        if not ref.startswith('reports/' + self._tag(owner) + '.'):
            raise StoreError('POLICY_DENIED')
        try:
            raw = self._body_path(ref).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise StoreError('RESULT_EXPIRED')
            body = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            raise StoreError('RESULT_EXPIRED') from None
        if not isinstance(body, dict) or set(body) != {'report', 'snapshot', 'permission_fingerprint', 'intent', 'draft_fingerprint', 'execution_provenance'}:
            raise StoreError('STORAGE_UNAVAILABLE')
        _validate('persisted_report', body['report'])
        if body['report']['metadata'] != metadata:
            raise StoreError('STORAGE_UNAVAILABLE')
        if check_acl:
            self._allow(owner, body['report'], body['permission_fingerprint'])
        # Catch tombstones published while bytes were being loaded.
        self.store.get_report(owner, report_id)
        return body

    def publish(self, owner_id, task_id, project, snapshot, *, snapshot_options,
                permission_fingerprint, summary, sections, claims=(), evidence=(), unknowns=(),
                parent_report_id=None, observed_read_files=None):
        task = self.store.get_task(owner_id, task_id)
        params = self.store.get_task_input(owner_id, task_id)
        if task['project_id'] != project.id or snapshot['project_id'] != project.id or task['purpose'] != 'project_context':
            raise StoreError('POLICY_DENIED')
        required_options = {'paths', 'internal_paths', 'sensitive_patterns', 'excluded_dirs', 'max_file_size_bytes'}
        if not isinstance(snapshot_options, dict) or set(snapshot_options) != required_options:
            raise StoreError('INVALID_ARGUMENT')
        root = resolve_path(project, '.')
        key = lambda value: os.path.normcase(str(value)).replace('\\', '/')
        try:
            policy = {name: sorted({key(resolve_path(project, path).relative_to(root).as_posix())
                                     for path in snapshot_options[name]})
                      for name in ('paths', 'internal_paths')}
            policy.update({name: sorted({key(value) for value in snapshot_options[name]})
                           for name in ('sensitive_patterns', 'excluded_dirs')})
            policy.update(max_file_size_bytes=snapshot_options['max_file_size_bytes'],
                          gitignore_is_authorization=False)
        except (TypeError, ValueError):
            raise StoreError('INVALID_ARGUMENT') from None
        if policy != snapshot['coverage']['scope']:
            raise StoreError('POLICY_DENIED')
        # A changed root is a different authorization boundary, even if its files
        # happen to match. Reject before reading evidence or rescanning contents.
        if snapshot['root_fingerprint'] != _digest(key(root)):
            raise StoreError('POLICY_DENIED')
        if not isinstance(sections, dict) or any(k not in SECTION_IDS or k in ('evidence', 'coverage', 'unknowns') or not isinstance(v, str) for k, v in sections.items()):
            raise StoreError('INVALID_ARGUMENT')
        if not isinstance(summary, str) or len(summary) > 2000:
            raise StoreError('INVALID_ARGUMENT')
        draft = {'summary': summary, 'sections': sections, 'claims': list(claims), 'evidence': list(evidence),
                 'unknowns': list(unknowns), 'snapshot': snapshot, 'permission': permission_fingerprint,
                 'parent': parent_report_id, 'read_files': observed_read_files}
        draft_hash = _digest(draft)
        shell = {'metadata': {'project_id': project.id}, 'coverage': {'scope': snapshot['coverage']['scope']['paths']}}
        self._allow(owner_id, shell, permission_fingerprint)
        with self._writing():
            task = self.store.get_task(owner_id, task_id)
            if task['report_id'] is not None:
                previous = self._load(owner_id, task['report_id'])
                if previous['draft_fingerprint'] != draft_hash:
                    raise StoreError('REQUEST_CONFLICT')
                return previous['report']['metadata']
            if parent_report_id is not None:
                parent = self._load(owner_id, parent_report_id)
                if parent['report']['metadata']['project_id'] != project.id:
                    raise StoreError('POLICY_DENIED')
            # If outputs are inside a project, they must already be excluded in
            # the investigation's source scope. Never silently widen that scope.
            if self.store.results_dir.resolve().is_relative_to(project.path.resolve()):
                output = self.store.results_dir.resolve().relative_to(project.path.resolve()).as_posix()
                policy_roots = snapshot['coverage']['scope']['internal_paths']
                if not any(output == p or output.startswith(p + '/') for p in policy_roots):
                    raise StoreError('POLICY_DENIED')
            final_evidence, issues = verify_evidence(project, snapshot, evidence)
            final_claims, final_unknowns = deepcopy(list(claims)), deepcopy(list(unknowns))
            by_id = {item['evidence_id']: item for item in final_evidence}
            claim_ids = set()
            for claim in final_claims:
                _validate('claim', claim)
                if claim['claim_id'] in claim_ids:
                    raise StoreError('INVALID_ARGUMENT')
                claim_ids.add(claim['claim_id'])
                if claim['classification'] == 'fact' and (not claim['evidence_ids'] or any(
                        eid not in by_id or by_id[eid]['validation'] != 'verified' for eid in claim['evidence_ids'])):
                    claim['classification'] = 'unknown'
                    issues.append({'reason': 'fact_without_verified_evidence'})
            for unknown in final_unknowns:
                _validate('unknown', unknown)
            for issue in issues:
                final_unknowns.append({'question': '需要核对来源或结论', 'reason': issue['reason'],
                                       'next_step': '补取合法来源并核对证据；不把未验证结论当事实。'})
            current = capture_snapshot(project, **snapshot_options)
            try:
                changes = compare_snapshots(snapshot, current)
                source_state = changes['state']
            except ValueError:
                changes, source_state = {'scope_changed': True}, 'stale'
            coverage = {'enumerated_files': snapshot['coverage']['enumerated_files'],
                        'read_files': observed_read_files, 'excluded_paths': snapshot['coverage']['excluded_paths'],
                        'scope': snapshot['coverage']['scope']['paths'], 'execution_performed': False}
            _validate('coverage', coverage)
            if observed_read_files is not None and coverage['enumerated_files'] is not None and observed_read_files > coverage['enumerated_files']:
                raise StoreError('INVALID_ARGUMENT')
            texts = deepcopy(sections)
            texts.setdefault('summary', summary)
            missing = []
            if params['intent'] == 'overview':
                for section in SECTION_IDS[:-3]:
                    if section not in texts:
                        missing.append(section)
                        texts[section] = '未知：尚未提供此章节的调查资料。'
                for section in missing:
                    final_unknowns.append({'question': SECTION_TITLES[section], 'reason': '章节尚未调查', 'next_step': '补充该范围的调查。'})
            texts['evidence'] = '```json\n' + _json({'claims': final_claims, 'evidence': final_evidence}) + '\n```'
            texts['coverage'] = '```json\n' + _json(coverage) + '\n```'
            texts['unknowns'] = '```json\n' + _json(final_unknowns) + '\n```'
            provenance = {key: task[key] for key in ('requested_model', 'resolved_model', 'actual_models', 'usage', 'usage_scope', 'cost', 'cost_basis')}
            if 'configuration' in texts:
                texts['configuration'] += '\n\n生成时非秘密溯源：\n```json\n' + _json({
                    'config_revision': task['config_revision'], 'execution_context': task['execution_context'],
                    'execution_provenance': provenance, 'reading_policy': snapshot['coverage']['scope']}) + '\n```'
            stored_sections = [{'id': sid, 'title': SECTION_TITLES[sid], 'text': texts[sid]} for sid in SECTION_IDS if sid in texts]
            reasons = []
            if source_state == 'stale': reasons.append('source_changed')
            if source_state == 'partial': reasons.append('coverage_gaps')
            if changes.get('scope_changed'): reasons.append('scope_changed')
            if issues: reasons.append('evidence_invalid')
            metadata = {'report_id': uuid.uuid4().hex, 'project_id': project.id, 'parent_report_id': parent_report_id,
                'report_schema_version': CONTRACT_VERSION, 'generated_at': self._now(), 'snapshot_id': snapshot['snapshot_id'],
                **{key: deepcopy(task[key]) for key in ('config_revision', 'backend', 'backend_version', 'profile', 'execution_context')},
                'freshness': {'state': source_state, 'checked_at': self._now(), 'reasons': reasons},
                'summary': summary, 'sections': [{'id': s['id'], 'title': s['title'], 'total_chars': len(s['text'])} for s in stored_sections],
                'completion': 'partial' if missing or issues or source_state != 'fresh' else 'complete', 'verification': 'not_run'}
            document = {'metadata': metadata, 'sections': stored_sections, 'claims': final_claims,
                        'evidence': final_evidence, 'coverage': coverage, 'unknowns': final_unknowns}
            _validate('persisted_report', document)
            body = {'report': document, 'snapshot': deepcopy(snapshot), 'permission_fingerprint': permission_fingerprint,
                    'intent': params['intent'], 'draft_fingerprint': draft_hash, 'execution_provenance': provenance}
            encoded = _json(body).encode('utf-8')
            self._quota(len(encoded))
            name = self._tag(owner_id) + '.' + metadata['report_id'] + '.json'
            self._atomic(self.root / name, encoded)
            self._allow(owner_id, document, permission_fingerprint)
            return self.store.publish_report(owner_id, task_id, metadata, content_ref='reports/' + name,
                                              content_sha256=hashlib.sha256(encoded).hexdigest())

    def read_document(self, owner_id, report_id):
        body = self._load(owner_id, report_id)
        self._allow(owner_id, body['report'], body['permission_fingerprint'])
        return deepcopy(body['report'])

    def _cursor(self, values):
        raw = _json(values).encode('utf-8')
        payload = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
        signature = hmac.new(self.cursor_key, raw, hashlib.sha256).digest()
        return payload + '.' + base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')

    def _decode_cursor(self, cursor):
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError
            payload, signature = cursor.split('.')
            raw = base64.b64decode(payload + '=' * (-len(payload) % 4), altchars=b'-_', validate=True)
            actual = base64.b64decode(signature + '=' * (-len(signature) % 4), altchars=b'-_', validate=True)
            if not hmac.compare_digest(actual, hmac.new(self.cursor_key, raw, hashlib.sha256).digest()):
                raise ValueError
            values = json.loads(raw)
            if set(values) != {'o', 'r', 'h', 's', 'p', 'b'} or self._cursor(values) != cursor:
                raise ValueError
            return values
        except (ValueError, TypeError, UnicodeError):
            raise StoreError('INVALID_CURSOR') from None

    def _freshness(self, body, current):
        if current is None:
            return {'state': 'unknown', 'checked_at': None, 'reasons': ['not_checked']}
        try:
            changes = compare_snapshots(body['snapshot'], current)
            state = changes['state']
        except (ValueError, KeyError):
            return {'state': 'stale', 'checked_at': self._now(), 'reasons': ['scope_changed']}
        reasons = []
        if state == 'stale': reasons.append('source_changed')
        if state == 'partial': reasons.append('coverage_gaps')
        if changes.get('scope_changed'): reasons.append('scope_changed')
        if 'evidence_invalid' in body['report']['metadata']['freshness']['reasons']:
            reasons.append('evidence_invalid')
        return {'state': state, 'checked_at': self._now(), 'reasons': reasons}

    @staticmethod
    def _envelope(metadata, section, text, cursor, next_cursor, total, remaining):
        return _count({'schema_version': CONTRACT_VERSION, 'kind': 'report', 'task': None,
            'report': metadata, 'page': {'section': section, 'text': text, 'format': 'markdown'},
            'pagination': {'cursor': cursor, 'next_cursor': next_cursor, 'has_more': next_cursor is not None,
                           'returned_chars': len(text), 'total_chars': total, 'remaining_chars': remaining}, 'response_chars': 0})

    def page(self, owner_id, report_id, *, section=None, cursor=None, max_chars=None, current_snapshot=None):
        values = self._decode_cursor(cursor) if cursor is not None else None
        if max_chars is not None and (type(max_chars) is not int or not 2048 <= max_chars <= 32000):
            raise StoreError('INVALID_ARGUMENT')
        if values is not None and (values['o'] != self._tag(owner_id) or values['r'] != report_id or
                (section is not None and section != values['s']) or
                (max_chars is not None and min(max_chars, self.max_response_chars) != values['b'])):
            raise StoreError('INVALID_CURSOR')
        budget = values['b'] if values else (12000 if max_chars is None else max_chars)
        if type(budget) is not int or not 2048 <= budget <= 32000:
            raise StoreError('INVALID_ARGUMENT')
        if values and budget > self.max_response_chars:
            raise StoreError('INVALID_CURSOR')
        budget = min(budget, self.max_response_chars)
        section = values['s'] if values else (section or 'summary')
        body = self._load(owner_id, report_id)
        _, digest = self.store.get_content_reference(owner_id, report_id)
        if values and (values['h'] != digest or type(values['p']) is not int or values['p'] < 0):
            raise StoreError('INVALID_CURSOR')
        selected = next((s for s in body['report']['sections'] if s['id'] == section), None)
        if selected is None:
            raise StoreError('SECTION_NOT_FOUND')
        text, offset = selected['text'], values['p'] if values else 0
        if offset > len(text):
            raise StoreError('INVALID_CURSOR')
        base = {'o': self._tag(owner_id), 'r': report_id, 'h': digest, 's': section, 'b': budget}
        metadata = deepcopy(body['report']['metadata'])
        metadata['freshness'] = self._freshness(body, current_snapshot)
        # Reserve the largest generated freshness and cursor/counter representation.
        # Layout depends only on immutable body+section+budget, not live freshness.
        reserved = deepcopy(metadata)
        reserved['freshness'] = {'state': 'partial', 'checked_at': self._now(), 'reasons': _REASONS}
        longest_cursor = self._cursor({**base, 'p': len(text)})
        overhead = self._envelope(reserved, section, '', longest_cursor, longest_cursor, len(text), len(text))
        overhead['pagination']['returned_chars'] = len(text)
        remaining_budget = budget - _count(overhead)['response_chars']
        left, right = 0, len(text) - offset
        while left < right:
            middle = (left + right + 1) // 2
            encoded_chars = len(json.dumps(text[offset:offset + middle], ensure_ascii=False)) - 2
            if encoded_chars <= remaining_budget:
                left = middle
            else:
                right = middle - 1
        if left == 0 and offset < len(text):
            raise StoreError('OUTPUT_BUDGET_TOO_SMALL')
        end = offset + left
        continuation = self._cursor({**base, 'p': end}) if end < len(text) else None
        response = self._envelope(metadata, section, text[offset:end], cursor, continuation, len(text), len(text) - end)
        if response['response_chars'] > budget:
            raise StoreError('OUTPUT_BUDGET_TOO_SMALL')
        _validate('task_poll_response', response)
        self._allow(owner_id, body['report'], body['permission_fingerprint'])
        return response

    def _owned_report_ids(self, owner):
        self._root_ok()
        prefix = self._tag(owner) + '.'
        with os.scandir(self.root) as entries:
            return [match.group(2) for entry in entries if entry.name.startswith(prefix)
                    if (match := _BODY_NAME.fullmatch(entry.name))]

    def find_cached(self, owner_id, project, snapshot, selection, permission_fingerprint):
        if not isinstance(permission_fingerprint, str) or not _HEX.fullmatch(permission_fingerprint) or snapshot['state'] != 'fresh' or snapshot['project_id'] != project.id:
            return None
        matches = []
        for report_id in self._owned_report_ids(owner_id):
            try:
                metadata = self.store.get_report(owner_id, report_id)
                if metadata['project_id'] != project.id:
                    continue
                body = self._load(owner_id, report_id)
                if (body['intent'] == 'overview' and body['permission_fingerprint'] == permission_fingerprint and
                        metadata['snapshot_id'] == snapshot['snapshot_id'] and metadata['freshness']['state'] == 'fresh' and
                        metadata['config_revision'] == selection['config_revision'] and metadata['profile'] == selection['profile'] and
                        metadata['execution_context'] == selection['execution_context'] and
                        body['snapshot']['scope_fingerprint'] == snapshot['scope_fingerprint'] and
                        body['execution_provenance']['requested_model'] == selection['requested_model']):
                    matches.append(metadata)
            except StoreError as error:
                if error.code not in ('RESULT_EXPIRED', 'REPORT_NOT_FOUND', 'POLICY_DENIED'):
                    raise
        return max(matches, key=lambda m: (m['generated_at'], m['report_id'])) if matches else None

    def expire(self, owner_id, report_id):
        """Trusted retention seam: durable tombstone before any body deletion."""
        with self._writing():
            journal = self.root / ('.expiry-' + self._tag(owner_id) + '.' + report_id + '.json')
            if not re.fullmatch(r'[a-f0-9]{32}', report_id):
                raise StoreError('INVALID_ARGUMENT')
            if journal.exists():
                try:
                    if self._linked(journal) or journal.stat().st_nlink > 1 or journal.stat().st_size > 4096:
                        raise ValueError
                    saved = json.loads(journal.read_bytes())
                    payload = saved['payload']
                    if saved['mac'] != hmac.new(self.cursor_key, _json(payload).encode(), hashlib.sha256).hexdigest() or payload['owner'] != self._tag(owner_id) or payload['report'] != report_id:
                        raise ValueError
                except (OSError, ValueError, KeyError, TypeError):
                    raise StoreError('CLEANUP_FAILED') from None
            else:
                ref, digest = self.store.get_content_reference(owner_id, report_id)
                if not ref.startswith('reports/' + self._tag(owner_id) + '.'):
                    raise StoreError('POLICY_DENIED')
                payload = {'owner': self._tag(owner_id), 'report': report_id, 'ref': ref, 'sha256': digest}
                saved = {'payload': payload, 'mac': hmac.new(self.cursor_key, _json(payload).encode(), hashlib.sha256).hexdigest()}
                self._atomic(journal, _json(saved).encode('utf-8'))
            self.store.expire_report(owner_id, report_id)
            try:
                target = self.root / payload['ref'][8:]
                if target.exists():
                    self._body_path(payload['ref']).unlink()
                journal.unlink()
            except (OSError, StoreError):
                raise StoreError('CLEANUP_FAILED') from None

    def expire_before(self, owner_id, timestamp, *, limit=100):
        if type(timestamp) not in (int, float) or type(limit) is not int or not 1 <= limit <= 1000:
            raise StoreError('INVALID_ARGUMENT')
        count = 0
        for report_id in self._owned_report_ids(owner_id):
            if count >= limit:
                break
            try:
                metadata = self.store.get_report(owner_id, report_id)
            except StoreError as error:
                if error.code in ('RESULT_EXPIRED', 'REPORT_NOT_FOUND'):
                    continue
                raise
            if datetime.fromisoformat(metadata['generated_at'].replace('Z', '+00:00')).timestamp() < timestamp:
                self.expire(owner_id, report_id)
                count += 1
        return count
