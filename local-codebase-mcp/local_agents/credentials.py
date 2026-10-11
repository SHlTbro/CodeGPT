"""Trusted local-service credential seam. Only DPAPI ciphertext reaches disk.

Root is chosen by the server, never HTTP/MCP input. resolve is runner-only.
User-scope DPAPI/owner DACL does not isolate same-user model tool processes:
T14/T19 must prove that boundary before enabling execution. No HTTP transport
is implemented here; T11 must enforce pinned public destinations/no redirects.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
from urllib.parse import urlsplit

from config import _file_lock
from local_agents.config import normalize_agent_config
from local_agents.contracts import validation_errors


class CredentialError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(raw, *, decrypt=False):
    if os.name != 'nt':
        raise CredentialError('CREDENTIAL_UNAVAILABLE')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    operation.restype = wintypes.BOOL
    data = ctypes.create_string_buffer(raw)
    entropy = ctypes.create_string_buffer(b'local-codebase-mcp-agent-model-key-v1')
    incoming = _Blob(len(raw), ctypes.cast(data, ctypes.POINTER(ctypes.c_ubyte)))
    binding = _Blob(len(entropy.value), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    if not operation(ctypes.byref(incoming), None, ctypes.byref(binding), None, None, 1, ctypes.byref(outgoing)):
        raise CredentialError('CREDENTIAL_UNAVAILABLE')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        # Wipe the DPAPI allocated plaintext before releasing it.
        if decrypt:
            ctypes.memset(outgoing.data, 0, outgoing.size)
        kernel.LocalFree(outgoing.data)


def protect_directory(path):
    """Owner-rights protected DACL on a service-owned folder, no host account changes."""
    path = Path(path).absolute()
    if os.name != 'nt' or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise CredentialError('CREDENTIAL_UNAVAILABLE')
    path.mkdir(parents=True, exist_ok=True)
    _protect_acl(path)
    return path


def _protect_acl(path):
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not convert('D:P(A;OICI;FA;;;OW)', 1, ctypes.byref(descriptor), None):
        raise CredentialError('CREDENTIAL_UNAVAILABLE')
    try:
        if not set_security(str(path), 4 | 0x80000000, descriptor):
            raise CredentialError('CREDENTIAL_UNAVAILABLE')
    finally:
        kernel.LocalFree(descriptor)


def _endpoint_syntax(endpoint, provider):
    try:
        normalize_agent_config({'codex': {'api_provider': provider, 'api_base_url': endpoint}})
    except Exception:
        raise CredentialError('ENDPOINT_NOT_APPROVED') from None


def approved_endpoint(endpoint, provider, resolver=socket.getaddrinfo):
    """Contract syntax plus all current DNS answers public. No HTTP or redirects.

    Returns addresses for a future pinned transport. Rechecking DNS here alone
    cannot prove that a separate CLI will pin DNS or reject HTTP redirects.
    """
    try:
        _endpoint_syntax(endpoint, provider)
        parsed = urlsplit(endpoint)
        answers = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        addresses = sorted({row[4][0] for row in answers})
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError()
        return tuple(addresses)
    except Exception:
        raise CredentialError('ENDPOINT_NOT_APPROVED') from None


class CredentialStore:
    def __init__(self, service_owned_root, *, revoke=None, resolver=socket.getaddrinfo):
        self.root = protect_directory(service_owned_root)
        self.path = self.root / 'credential.dpapi'
        self.lock_path = self.root / 'credential.lock'
        self.revoke, self.resolver = revoke, resolver
        try:
            with _file_lock(self.lock_path):
                _protect_acl(self.lock_path)
                if not self.path.exists():
                    self._write({'version': 1, 'revision': secrets.token_hex(16), 'context_id': secrets.token_hex(16),
                        'key': None, 'endpoint': None, 'provider': None, 'updated_at': None,
                        'hmac_key': secrets.token_hex(32), 'requests': {}, 'pending_revoke': []})
                self._load()
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('CREDENTIAL_UNAVAILABLE') from None

    def _load(self):
        try:
            if self.path.is_symlink() or self.path.is_junction():
                raise ValueError()
            if self.path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError()
            state = json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
            if set(state) != {'version', 'revision', 'context_id', 'key', 'endpoint', 'provider', 'updated_at', 'hmac_key', 'requests', 'pending_revoke'} or type(state['version']) is not int or state['version'] != 1:
                raise ValueError()
            if any(type(state[k]) is not str or not state[k] for k in ('revision', 'context_id', 'hmac_key')):
                raise ValueError()
            if type(state['requests']) is not dict or type(state['pending_revoke']) is not list:
                raise ValueError()
            if len(state['hmac_key']) != 64 or len(bytes.fromhex(state['hmac_key'])) != 32:
                raise ValueError()
            for item in state['pending_revoke']:
                if type(item) is not list or len(item) != 2 or any(type(v) is not str or len(v) != 32 or len(bytes.fromhex(v)) != 16 for v in item):
                    raise ValueError()
            for request_id, record in state['requests'].items():
                if type(request_id) is not str or not request_id.strip() or len(request_id) > 128 or type(record) is not dict or set(record) != {'fingerprint', 'response'}:
                    raise ValueError()
                if type(record['fingerprint']) is not str or len(record['fingerprint']) != 64 or len(bytes.fromhex(record['fingerprint'])) != 32 or validation_errors('setup_credential_response', record['response']):
                    raise ValueError()
            if state['key'] is not None and (type(state['key']) is not str or not state['key'] or not state['endpoint'] or not state['provider']):
                raise ValueError()
            if state['key'] is None and (state['endpoint'] is not None or state['provider'] is not None):
                raise ValueError()
            if validation_errors('credential_status', self._status(state)):
                raise ValueError()
            return state
        except Exception:
            raise CredentialError('CREDENTIAL_UNAVAILABLE') from None

    def _write(self, state):
        temporary = None
        try:
            ciphertext = _dpapi(json.dumps(state, separators=(',', ':'), ensure_ascii=False).encode())
            fd, temporary = tempfile.mkstemp(prefix='.credential-', dir=self.root)
            with os.fdopen(fd, 'wb') as stream:
                _protect_acl(Path(temporary))
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except Exception:
            raise CredentialError('CREDENTIAL_UNAVAILABLE') from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _status(state, endpoint=None):
        configured = state['key'] is not None
        status = 'missing' if not configured else ('endpoint_mismatch' if endpoint is not None and endpoint != state['endpoint'] else 'configured')
        if configured and state['pending_revoke']:
            status = 'unavailable'
        return {'configured': configured, 'credential_revision': state['revision'], 'storage': 'protected_file',
            'bound_endpoint': state['endpoint'], 'status': status, 'updated_at': state['updated_at']}

    def status(self, *, endpoint=None):
        with _file_lock(self.lock_path):
            return self._status(self._load(), endpoint)

    def binding(self):
        """Nonsecret runtime metadata; no key/account identity/DNS lookup."""
        with _file_lock(self.lock_path):
            state = self._load()
            return {'credential_context_id': state['context_id'], 'credential_revision': state['revision'],
                'provider': state['provider'], 'endpoint': state['endpoint'], 'configured': state['key'] is not None}

    def put(self, request, *, endpoint, provider, config_revision):
        if validation_errors('setup_credential_put_request', request):
            raise CredentialError('INVALID_ARGUMENT')
        _endpoint_syntax(endpoint, provider)
        return self._change('put', request, endpoint, provider, config_revision)

    def delete(self, request, *, config_revision):
        if validation_errors('setup_credential_delete_request', request):
            raise CredentialError('INVALID_ARGUMENT')
        return self._change('delete', request, None, None, config_revision)

    def _change(self, operation, request, endpoint, provider, config_revision):
        response = self._commit_change(operation, request, endpoint, provider, config_revision)
        self._revoke()
        return response

    def _commit_change(self, operation, request, endpoint, provider, config_revision):
        with _file_lock(self.lock_path):
            state = self._load()
            fingerprint = hmac.new(bytes.fromhex(state['hmac_key']), json.dumps([operation, request, endpoint, provider], sort_keys=True).encode(), hashlib.sha256).hexdigest()
            old = state['requests'].get(request['request_id'])
            if old is not None:
                if old['fingerprint'] != fingerprint:
                    raise CredentialError('REQUEST_CONFLICT')
                return deepcopy(old['response'])
            if request['revision'] != config_revision:
                raise CredentialError('REQUEST_CONFLICT')
            if request['expected_credential_revision'] != state['revision']:
                raise CredentialError('CREDENTIAL_CONFLICT')
            if len(state['requests']) >= 4096:
                raise CredentialError('CREDENTIAL_UNAVAILABLE')
            if state['key'] is not None:
                state['pending_revoke'].append([state['context_id'], state['revision']])
            state.update(key=request.get('api_key'), endpoint=endpoint, provider=provider,
                revision=secrets.token_hex(16), updated_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
            # Response describes the accepted mutation; cleanup still must confirm.
            response_state = dict(state, pending_revoke=[])
            response = {'ok': True, 'credential': self._status(response_state), 'config_revision': config_revision}
            state['requests'][request['request_id']] = {'fingerprint': fingerprint, 'response': response}
            self._write(state)  # Commit deletion/replacement BEFORE revoking; never restore an old key.
            return deepcopy(response)

    def _revoke(self):
        with _file_lock(self.lock_path):
            pending = self._load()['pending_revoke']
        if not pending:
            return
        for context, revision in pending:
            try:
                if self.revoke is None or self.revoke(context, revision) is not True:
                    raise ValueError()
            except Exception:
                raise CredentialError('CLEANUP_FAILED') from None
        with _file_lock(self.lock_path):
            # Another accepted mutation may have occurred during callback.
            # Never rewrite the earlier key/revision or discard new revocations.
            latest = self._load()
            latest['pending_revoke'] = [item for item in latest['pending_revoke'] if item not in pending]
            self._write(latest)

    def resolve(self, *, endpoint, provider, credential_revision):
        approved_endpoint(endpoint, provider, self.resolver)
        with _file_lock(self.lock_path):
            state = self._load()
            if state['key'] is None or state['revision'] != credential_revision:
                raise CredentialError('CREDENTIAL_CONFLICT')
            if endpoint != state['endpoint'] or provider != state['provider']:
                raise CredentialError('CREDENTIAL_CONFLICT')
            if state['pending_revoke']:
                raise CredentialError('CLEANUP_FAILED')
            return state['key']
