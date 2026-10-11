"""Single logical owner consent candidate; real project access stays OFF.

Trusts direct local OS peers and the owner browser, not Windows SID identity.
Dedicated private browser session capability + per-transaction nonce + explicit
decision; mature OAuthProvider/SDK DCR/code/PKCE/token handlers. Never exposes
Setup/config/secrets or automatically approves public authorization. Every
temporary credential/client expires or is invalidated by restart. Only the
nonsecret random logical owner is persisted. No model/CLI authentication reuse.
"""
import ipaddress
import logging
import json
from pathlib import Path
import re
import secrets
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlparse

from config import _atomic_write, _file_lock
from fastmcp import FastMCP
from fastmcp.server.auth.auth import OAuthProvider, TokenHandler
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from starlette.datastructures import FormData
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
from mcp.server.auth.provider import RefreshToken, AccessToken, AuthorizationCode, AuthorizeError, RegistrationError, TokenError, construct_redirect_uri
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthToken
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

_SAFE_HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY', 'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}
_COOKIE = 't06-owner-session'
_OWNER = re.compile(r'^owner-[a-f0-9]{32}$')
_AUDIT = ContextVar('codegpt_oauth_audit',default=None)


def _token_audit(reason, *, access_expires=None, family_expires=None, cause=None):
    context=_AUDIT.get()
    if context is not None:context['reason']=reason
    logging.getLogger('codegpt.oauth').warning(
        'oauth_token request=%s grant=%s reason=%s at=%s access_expires=%s family_expires=%s cause=%s',
        context['request'] if context else 'direct',context['grant'] if context else 'none',reason,
        datetime.now(timezone.utc).isoformat(),access_expires,family_expires,cause)


def owner_subject(state_path):
    """Nonsecret installation identity only; corrupt state fails closed."""
    path = Path(state_path)
    if path.suffix.lower() != '.json':
        raise ValueError('owner state requires dedicated JSON path')
    with _file_lock(path.with_suffix('.json.lock')):
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 512:
                raise ValueError('invalid owner state')
            try:
                saved = json.loads(path.read_bytes())
            except (OSError, ValueError):
                raise ValueError('invalid owner state') from None
            if not isinstance(saved,dict) or set(saved) != {'layout', 'owner_subject'} or type(saved['layout']) is not int or saved['layout'] != 1 or not isinstance(saved['owner_subject'],str) or not _OWNER.fullmatch(saved['owner_subject']):
                raise ValueError('invalid owner state')
            return saved['owner_subject']
        subject = 'owner-' + secrets.token_hex(16)
        _atomic_write(path, json.dumps({'layout': 1, 'owner_subject': subject}).encode())
        return subject


class SafeOAuthAudit:
    """Protocol stage/status only. No query, body, credentials or identifiers."""
    def __init__(self,app,path):self.app,self.path=app,path
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        context={'request':secrets.token_hex(12),'grant':'none','reason':'none'}
        marker=_AUDIT.set(context)
        async def audited(message):
            if message['type']=='http.response.start':
                headers=[(name,value) for name,value in message.get('headers',[]) if name.lower()!=b'x-codegpt-auth-request']
                message={**message,'headers':[*headers,(b'x-codegpt-auth-request',context['request'].encode('ascii'))]}
                method=scope.get('method','')
                logging.getLogger('codegpt.oauth').warning('oauth_http path=%s method=%s status=%s request=%s grant=%s reason=%s at=%s',
                    self.path,method if method in {'GET','POST','OPTIONS','DELETE','PUT'} else 'OTHER',message['status'],
                    context['request'],context['grant'],context['reason'],datetime.now(timezone.utc).isoformat())
            await send(message)
        try:await self.app(scope,receive,audited)
        finally:_AUDIT.reset(marker)


class SafeClientAuthenticator(ClientAuthenticator):
    async def authenticate_request(self,request):
        try:return await super().authenticate_request(request)
        except AuthenticationError:
            form=await request.form()
            client_id=form.get('client_id')
            reason=('SDK_CLIENT_ID_MISSING' if not client_id else
                'SDK_CLIENT_UNKNOWN' if await self.provider.get_client(str(client_id)) is None else 'SDK_CLIENT_AUTH_REJECTED')
            _token_audit(reason)
            raise


class IssuerAuthorizationHandler(AuthorizationHandler):
    async def handle(self, request):
        response = await super().handle(request)
        location = response.headers.get('location')
        if location and location.split('?', 1)[0] in self.provider.allowed_redirects:
            response.headers['location'] = construct_redirect_uri(location, iss=str(self.provider.issuer_url))
        return response


class PublicClientRevocationHandler(RevocationHandler):
    async def handle(self,request):
        # Pinned SDK requires nullable client_secret even for public clients.
        # Supply its empty optional field only for an already registered none
        # client; the mature handler still authenticates and checks ownership.
        form=await request.form()
        client=await self.provider.get_client(str(form.get('client_id','')))
        if client and client.token_endpoint_auth_method=='none' and 'client_secret' not in form:
            request._form=FormData([*form.multi_items(),('client_secret','')])
        return await super().handle(request)


class ResourceBoundTokenHandler(TokenHandler):
    async def handle(self, request):
        form = await request.form()
        context=_AUDIT.get()
        if context is not None:
            grant=form.get('grant_type')
            context['grant']=grant if grant in ('authorization_code','refresh_token') else 'unsupported'
        if form.get('resource') != self.provider.resource:
            _token_audit('RESOURCE_MISMATCH')
            return JSONResponse({'error': 'invalid_target'}, status_code=400,
                                headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
        return await super().handle(request)


class LocalApprovalProvider(OAuthProvider):
    def __init__(self, *, base_url, owner_base_url, owner_subject,
                 allowed_redirects, clock=time.time, task_access=False):
        parsed = urlparse(owner_base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or not parsed.port or parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
            raise ValueError('owner approval listener must be direct loopback HTTP')
        public = urlparse(base_url)
        if (public.scheme != 'https' and not (public.scheme == 'http' and public.hostname in ('127.0.0.1', '::1'))) or public.path or public.query or public.fragment or public.username is not None or not public.hostname:
            raise ValueError('public root must be exact HTTPS or owned loopback HTTP')
        if not isinstance(owner_subject, str) or not _OWNER.fullmatch(owner_subject) or not allowed_redirects or len(allowed_redirects) > 8:
            raise ValueError('explicit logical owner and exact callbacks required')
        for callback in allowed_redirects:
            url = urlparse(callback)
            if url.scheme != 'https' or not url.hostname or url.username is not None or url.query or url.fragment or '*' in callback:
                raise ValueError('exact HTTPS callback required, without query/fragment/wildcard')
        if type(task_access) is not bool:raise ValueError('task access must be explicit')
        self.grant_scopes=['test:ping','codegpt:context'] if task_access else ['test:ping']
        self.task_access=task_access
        super().__init__(base_url=base_url, required_scopes=self.grant_scopes,
            client_registration_options=ClientRegistrationOptions(enabled=True,
                valid_scopes=self.grant_scopes, default_scopes=self.grant_scopes),
            revocation_options=RevocationOptions(enabled=True))
        self.resource = str(self.base_url).rstrip('/') + '/mcp'
        self.owner_base_url = owner_base_url
        self.owner_subject = owner_subject
        self.auth_epoch = secrets.token_hex(16)
        self.allowed_redirects = frozenset(allowed_redirects)
        self.clock = clock
        self.lock = threading.RLock()
        self.clients, self.pending, self.codes, self.access, self.sessions = {}, {}, {}, {}, {}
        self.refresh,self.used_refresh,self.token_families={},{},{}
        self.expired_refresh={}

    def _prune(self):
        now = self.clock()
        for mapping in (self.pending, self.codes, self.access, self.sessions,self.refresh,self.used_refresh):
            for key, value in list(mapping.items()):
                expiry = value['expires_at'] if isinstance(value, dict) else value.expires_at
                if expiry <= now:
                    if (mapping is self.refresh or mapping is self.used_refresh) and expiry+300>now:
                        # In-memory lookup metadata only; never valid credentials.
                        client_id=value['client_id'] if isinstance(value,dict) else value.client_id
                        self.expired_refresh[key]={'client_id':client_id,'expires_at':expiry,'forget_at':expiry+300}
                    del mapping[key]
        for key,value in list(self.expired_refresh.items()):
            if value['forget_at']<=now:del self.expired_refresh[key]
        while len(self.expired_refresh)>256:del self.expired_refresh[next(iter(self.expired_refresh))]
        retained=set(self.access)|set(self.refresh)|set(self.used_refresh)
        for key in list(self.token_families):
            if key not in retained:del self.token_families[key]

    async def get_client(self, client_id):
        with self.lock:
            return self.clients.get(client_id)

    async def register_client(self, client_info):
        if (not client_info.redirect_uris or
                any(str(uri) not in self.allowed_redirects for uri in client_info.redirect_uris) or
                client_info.token_endpoint_auth_method != 'none' or
                'authorization_code' not in client_info.grant_types or
                set(client_info.grant_types)-{'authorization_code','refresh_token'} or
                len(client_info.grant_types)!=len(set(client_info.grant_types))):
            logging.getLogger('codegpt.oauth').warning('oauth_registration denied redirect_exact=%s public_client=%s code_only=%s',
                bool(client_info.redirect_uris) and all(str(uri) in self.allowed_redirects for uri in client_info.redirect_uris),
                client_info.token_endpoint_auth_method=='none',client_info.grant_types==['authorization_code'])
            raise RegistrationError('invalid_client_metadata', 'Exact approved callback and public authorization-code client required')
        with self.lock:
            if len(self.clients) >= 64:
                raise RegistrationError('invalid_client_metadata', 'Candidate registration capacity reached')
            self.clients[client_info.client_id] = client_info.model_copy(deep=True)

    async def authorize(self, client, params):
        if params.resource != self.resource:
            raise AuthorizeError('invalid_target', 'Resource must match the protected MCP endpoint')
        if str(params.redirect_uri) not in self.allowed_redirects:
            raise AuthorizeError('invalid_request', 'Redirect is not approved')
        if set(params.scopes or ()) != set(self.grant_scopes) or len(params.scopes or ())!=len(self.grant_scopes):
            raise AuthorizeError('invalid_scope', 'Exact candidate consent scopes required')
        with self.lock:
            self._prune()
            if len(self.pending) >= 32:
                raise AuthorizeError('temporarily_unavailable', 'Candidate approval queue full')
            tx = secrets.token_urlsafe(32)
            self.pending[tx] = {'client_id': client.client_id, 'params': params.model_copy(deep=True),
                                'expires_at': self.clock() + 120, 'result': None, 'decided': False, 'nonces': {}}
        return str(self.base_url).rstrip('/') + '/approval/' + tx

    def _local_allowed(self, request, *, mutation=False):
        peer = request.client.host if request.client else ''
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        origin = request.headers.get('origin')
        return (local and request.headers.get('host') == urlparse(self.owner_base_url).netloc and
                (origin == self.owner_base_url if mutation else origin in (None, self.owner_base_url)) and
                request.headers.get('sec-fetch-site') not in ('cross-site',) and
                not any(key.lower().startswith('x-forwarded-') or key.lower() == 'forwarded' for key in request.headers))

    def _session(self, request):
        if not self._local_allowed(request, mutation=True):
            return None
        with self.lock:
            self._prune()
            sid = request.cookies.get(_COOKIE)
            row = self.sessions.get(sid)
            try:
                matches = row and secrets.compare_digest(row['token'].encode('ascii'),
                    request.headers.get('x-setup-token', '').encode('ascii'))
            except UnicodeEncodeError:
                matches = False
            return sid if matches else None

    async def session_handler(self, request):
        if not self._local_allowed(request):
            return JSONResponse({'error':'local_session_required'},status_code=403,headers=_SAFE_HEADERS)
        with self.lock:
            self._prune()
            sid = request.cookies.get(_COOKIE)
            if sid not in self.sessions:
                if len(self.sessions) >= 16:
                    return JSONResponse({'error':'session_capacity'},status_code=429,headers=_SAFE_HEADERS)
                sid = secrets.token_urlsafe(32)
                self.sessions[sid] = {'token':secrets.token_urlsafe(32),'expires_at':self.clock()+600}
            response = JSONResponse({'setup_token':self.sessions[sid]['token'],
                'owner_subject':self.owner_subject,'auth_instance':self.auth_epoch,
                'expires_in':max(0,int(self.sessions[sid]['expires_at']-self.clock()))},headers=_SAFE_HEADERS)
            response.set_cookie(_COOKIE,sid,httponly=True,samesite='strict',max_age=600,path='/owner-api')
            return response

    async def pending_handler(self, request):
        sid = self._session(request)
        if sid is None:
            return JSONResponse({'error': 'local_session_required'}, status_code=403,headers=_SAFE_HEADERS)
        with self.lock:
            self._prune()
            queue = [{'transaction_id': tx, 'pairing_label': tx[:8], 'client_id': row['client_id'],
                      'redirect_uri': str(row['params'].redirect_uri), 'resource': row['params'].resource,
                      'issuer': str(self.issuer_url), 'scopes': row['params'].scopes,
                      'action_nonce': row['nonces'].setdefault(sid,secrets.token_urlsafe(32))}
                     for tx, row in self.pending.items() if not row['decided']]
        return JSONResponse({'pending': queue}, headers=_SAFE_HEADERS)

    async def approve_handler(self, request):
        sid = self._session(request)
        if sid is None:
            return JSONResponse({'error': 'local_session_required'}, status_code=403,headers=_SAFE_HEADERS)
        try:
            body = await request.json()
            if (not isinstance(body, dict) or set(body) != {'transaction_id', 'allow','action_nonce'} or
                    not isinstance(body['allow'], bool) or
                    not isinstance(body['transaction_id'], str) or
                    not 1 <= len(body['transaction_id']) <= 128 or not isinstance(body['action_nonce'],str)):
                raise ValueError
        except (ValueError, TypeError):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        with self.lock:
            self._prune()
            row = self.pending.get(body['transaction_id'])
            if row is None or row['decided']:
                return JSONResponse({'error': 'approval_expired_or_used'}, status_code=410)
            expected = row['nonces'].get(sid)
            try:
                matches = expected and secrets.compare_digest(expected.encode('ascii'),body['action_nonce'].encode('ascii'))
            except UnicodeEncodeError:
                matches = False
            if not matches:
                return JSONResponse({'error':'approval_nonce_required'},status_code=403,headers=_SAFE_HEADERS)
            params = row['params']
            row['decided'] = True
            row['nonces'].clear()
            # The result needs a delivery window after the owner decides,
            # including a decision near the original pending deadline.
            row['expires_at'] = self.clock() + 60
            if not body['allow']:
                families={self.token_families[key] for mapping in (self.access,self.refresh)
                    for key,value in mapping.items() if value.client_id==row['client_id'] and key in self.token_families}
                for family in families:self._revoke_family(family,cause='OWNER_DENIED')
                for mapping in (self.codes,self.access,self.refresh):
                    for key,value in list(mapping.items()):
                        if value.client_id == row['client_id']: del mapping[key]
                for key,value in list(self.used_refresh.items()):
                    if value['client_id']==row['client_id']:del self.used_refresh[key]
                self._prune()
                _token_audit('OWNER_DENIED')
                row['result'] = construct_redirect_uri(str(params.redirect_uri), error='access_denied', state=params.state,iss=str(self.issuer_url))
            else:
                value = secrets.token_urlsafe(32)
                self.codes[value] = AuthorizationCode(code=value, client_id=row['client_id'],
                    scopes=params.scopes or [], expires_at=row['expires_at'],
                    code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource, subject=self.owner_subject)
                row['result'] = construct_redirect_uri(str(params.redirect_uri), code=value, state=params.state,iss=str(self.issuer_url))
        return JSONResponse({'approved': body['allow']}, headers=_SAFE_HEADERS)

    async def wait_handler(self, request):
        with self.lock:
            self._prune()
            row = self.pending.get(request.path_params['tx'])
            if row is None:
                return JSONResponse({'error': 'approval_expired'}, status_code=410)
            result = row['result']
        headers = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'}
        if result:
            return RedirectResponse(result, status_code=302, headers=headers)
        return HTMLResponse('<!doctype html><meta http-equiv="refresh" content="2">'
            '<title>Local owner approval required</title><p>Approve this connection in the protected local owner UI.</p>'
            '<p>Pairing label: ' + request.path_params['tx'][:8] + '</p>', status_code=202, headers=headers)

    async def load_authorization_code(self, client, authorization_code):
        with self.lock:
            self._prune()
            code = self.codes.get(authorization_code)
            return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client, authorization_code):
        with self.lock:
            self._prune()
            code = self.codes.pop(authorization_code.code, None)
            if code is None or code.client_id != client.client_id or code.resource != self.resource:
                raise TokenError('invalid_grant', 'Code expired, consumed or wrong binding')
            refresh_until=int(self.clock()+1800) if 'refresh_token' in client.grant_types else None
            return self._issue(client.client_id,code.scopes,refresh_until=refresh_until)

    def _issue(self,client_id,scopes,*,refresh_until=None,family=None):
        # Caller holds lock; refresh never extends the initial family's window.
        if len(self.access)>=128 or (refresh_until is not None and (len(self.refresh)>=128 or refresh_until<=self.clock())):
            raise TokenError('invalid_grant','Candidate token capacity or lifetime reached')
        value=secrets.token_urlsafe(32)
        expires=min(int(self.clock()+300),refresh_until) if refresh_until else int(self.clock()+300)
        self.access[value]=AccessToken(token=value,client_id=client_id,scopes=list(scopes),expires_at=expires,
            resource=self.resource,subject=self.owner_subject,claims={'iss':str(self.issuer_url),'sub':self.owner_subject,
            'aud':self.resource,'auth_epoch':self.auth_epoch})
        refresh_value=None
        if refresh_until is not None:
            family=family or secrets.token_urlsafe(32)
            refresh_value=secrets.token_urlsafe(32)
            self.refresh[refresh_value]=RefreshToken(token=refresh_value,client_id=client_id,scopes=list(scopes),
                expires_at=refresh_until,resource=self.resource,subject=self.owner_subject)
            self.token_families[value]=family;self.token_families[refresh_value]=family
        _token_audit('TOKEN_ISSUED',access_expires=expires,family_expires=refresh_until)
        return OAuthToken(access_token=value,token_type='Bearer',expires_in=max(1,expires-int(self.clock())),
            refresh_token=refresh_value,scope=' '.join(scopes))

    def _revoke_family(self,family,*,cause='REVOKE_REQUEST'):
        revoked=False
        for key,value in list(self.token_families.items()):
            if value==family:
                self.access.pop(key,None);self.refresh.pop(key,None);self.token_families.pop(key,None)
                revoked=True
        if revoked:_token_audit('TOKEN_FAMILY_REVOKED',cause=cause)
        # used-refresh tombstones remain until expiry, so reuse stays rejected.

    async def load_access_token(self, token):
        with self.lock:
            self._prune()
            access = self.access.get(token)
            if access and access.resource == self.resource and access.subject == self.owner_subject and access.claims == {'iss':str(self.issuer_url),'sub':self.owner_subject,'aud':self.resource,'auth_epoch':self.auth_epoch}:
                return access
        return None

    async def load_refresh_token(self,client,refresh_token):
        with self.lock:
            self._prune()
            used=self.used_refresh.get(refresh_token)
            current=self.refresh.get(refresh_token)
            reason=self._refresh_rejection(client,refresh_token,current,used)
            if reason:
                if reason=='REFRESH_REPLAY':self._revoke_family(used['family'],cause='REFRESH_REPLAY')
                _token_audit(reason)
            else:return current
        return None

    def _refresh_rejection(self,client,value,current,used):
        expired=self.expired_refresh.get(value)
        record=current or used or expired
        if record is None:return 'REFRESH_UNKNOWN'
        client_id=record['client_id'] if isinstance(record,dict) else record.client_id
        if client_id!=client.client_id:return 'REFRESH_CLIENT_MISMATCH'
        if expired and not current and not used:return 'REFRESH_EXPIRED'
        if used and not current:return 'REFRESH_REPLAY'
        if current.resource!=self.resource or current.subject!=self.owner_subject:return 'REFRESH_BINDING_MISMATCH'
        if current.expires_at<=self.clock():return 'REFRESH_EXPIRED'
        return None

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        with self.lock:
            self._prune()
            current=self.refresh.get(refresh_token.token)
            used=self.used_refresh.get(refresh_token.token)
            reason=self._refresh_rejection(client,refresh_token.token,current,used)
            if reason:
                if reason=='REFRESH_REPLAY':self._revoke_family(used['family'],cause='REFRESH_REPLAY')
                _token_audit(reason)
                raise TokenError('invalid_grant','Refresh expired, consumed or wrong binding')
            if set(scopes)-set(current.scopes) or len(scopes)!=len(set(scopes)):
                _token_audit('REFRESH_SCOPE_REJECTED')
                raise TokenError('invalid_scope','Refresh cannot expand granted scopes')
            if len(self.used_refresh)>=128:
                _token_audit('REFRESH_CAPACITY_REACHED')
                raise TokenError('invalid_grant','Candidate rotation capacity reached')
            family=self.token_families[current.token]
            self._revoke_family(family,cause='REFRESH_ROTATION')
            self.used_refresh[current.token]={'family':family,'client_id':client.client_id,'expires_at':current.expires_at}
            result=self._issue(client.client_id,scopes,refresh_until=current.expires_at,family=family)
            _token_audit('REFRESH_ROTATED',family_expires=current.expires_at)
            return result

    async def revoke_token(self,token):
        with self.lock:
            family=self.token_families.get(token.token)
            if family:self._revoke_family(family)
            else:
                if self.access.pop(token.token,None) is not None:_token_audit('TOKEN_REVOKED')

    async def resolve_owner(self, bearer_token):
        access = await self.load_access_token(bearer_token)
        if access is None or set(access.scopes)!=set(self.grant_scopes) or len(access.scopes)!=len(self.grant_scopes):
            raise PermissionError('MCP authorization required')
        return self.owner_subject

    async def authorize_project(self, bearer_token, project_id):
        await self.resolve_owner(bearer_token)
        raise PermissionError('Product project access is disabled pending real provider acceptance')

    async def page_handler(self, request):
        if not self._local_allowed(request):
            return JSONResponse({'error':'local_session_required'},status_code=403,headers=_SAFE_HEADERS)
        explanation=('批准候选 ping 与项目上下文、任务和报告权限。每次访问重验项目授权；受控执行未证明时不会启动模型。' if self.task_access else
                     '仅批准无害 ping，没有项目读取、模型调用或永久信任。')
        return HTMLResponse('<!doctype html><html lang="zh"><meta charset="utf-8"><title>CodeGPT 本机连接批准</title>'
            '<h1>本机连接批准</h1><p>'+explanation+'核对配对标签与完整回调。</p>'
            '<p id="status" role="status">载入本机会话…</p><section id="pending"></section>'
            '<script src="/owner.js" defer></script></html>',headers=_SAFE_HEADERS)

    async def script_handler(self, request):
        if not self._local_allowed(request):
            return JSONResponse({'error':'local_session_required'},status_code=403,headers=_SAFE_HEADERS)
        return HTMLResponse(_OWNER_JS,media_type='application/javascript',headers=_SAFE_HEADERS)

    def owner_app(self):
        app = Starlette(routes=[Route('/',self.page_handler),Route('/owner.js',self.script_handler),
            Route('/owner-api/session',self.session_handler),Route('/owner-api/pending',self.pending_handler,methods=['POST']),
            Route('/owner-api/approve', self.approve_handler, methods=['POST'])])
        for route in app.routes:route.app=SafeOAuthAudit(route.app,route.path)
        return RequestBodyLimitMiddleware(app, 4096)

    def get_routes(self, mcp_path=None):
        routes = []
        for route in super().get_routes(mcp_path):
            if route.path == '/token':
                handler = ResourceBoundTokenHandler(self, SafeClientAuthenticator(self))
                routes.append(Route('/token', endpoint=RequestBodyLimitMiddleware(
                    cors_middleware(handler.handle, ['POST', 'OPTIONS']), 8192), methods=['POST', 'OPTIONS']))
            elif route.path == '/revoke':
                handler=PublicClientRevocationHandler(self,SafeClientAuthenticator(self))
                routes.append(Route('/revoke',endpoint=RequestBodyLimitMiddleware(
                    cors_middleware(handler.handle,['POST','OPTIONS']),8192),methods=['POST','OPTIONS']))
            elif route.path == '/authorize':
                handler = IssuerAuthorizationHandler(self)
                routes.append(Route('/authorize',endpoint=RequestBodyLimitMiddleware(
                    cors_middleware(handler.handle,['GET','POST','OPTIONS']),8192),methods=['GET','POST','OPTIONS']))
            elif route.path == '/register':
                routes.append(Route('/register',endpoint=RequestBodyLimitMiddleware(route.endpoint,8192),methods=route.methods))
            elif route.path == '/.well-known/oauth-authorization-server':
                metadata = build_metadata(self.base_url, self.service_documentation_url,
                    self.client_registration_options, self.revocation_options)
                metadata.issuer = self.issuer_url
                metadata.grant_types_supported = ['authorization_code','refresh_token']
                metadata.revocation_endpoint_auth_methods_supported=['none']
                metadata.token_endpoint_auth_methods_supported = ['none']
                metadata.authorization_response_iss_parameter_supported = True
                routes.append(Route(route.path, endpoint=cors_middleware(MetadataHandler(metadata).handle, ['GET', 'OPTIONS']), methods=['GET', 'OPTIONS']))
            else:
                routes.append(route)
        # Same SDK metadata handler/resource at both standard discovery paths.
        # This adds discovery compatibility without changing token/owner policy.
        resource=next((r for r in routes if r.path=='/.well-known/oauth-protected-resource'+(mcp_path or '')),None)
        if resource is not None and resource.path!='/.well-known/oauth-protected-resource':
            routes.append(Route('/.well-known/oauth-protected-resource',endpoint=resource.endpoint,methods=resource.methods))
        routes.append(Route('/approval/{tx}',self.wait_handler))
        for route in routes:route.app=SafeOAuthAudit(route.app,route.path)
        return routes


def build_candidate_server(provider):
    """Actual protected MCP, only harmless ping; no production tools imported."""
    server = FastMCP('T06 single owner authorization candidate',auth=provider)
    @server.tool(annotations={'readOnlyHint':True,'openWorldHint':False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':['test:ping']}]})
    async def auth_test_ping() -> dict:
        token = get_access_token()
        if token is None:
            raise PermissionError('MCP authorization required')
        await provider.resolve_owner(token.token)  # Recheck current lifetime/revocation.
        return {'ok':True,'candidate':'T06','project_reading_enabled':False,'model_request_performed':False}
    return server


_OWNER_JS = """(() => {
  'use strict';
  let token = null;
  const status = document.getElementById('status'), list = document.getElementById('pending');
  async function post(path, body) {
    const response = await fetch('/owner-api/' + path, {method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/json','X-Setup-Token':token},body:JSON.stringify(body)});
    if (!response.ok) throw new Error('本机会话已过期或请求被拒绝，请刷新后重试。');
    return response.json();
  }
  async function refresh() {
    const data = await post('pending', {});
    list.replaceChildren(); status.textContent = data.pending.length ? '请核对每项连接后明确批准或拒绝。' : '没有等待批准的连接。';
    for (const row of data.pending) {
      const card = document.createElement('article'), detail = document.createElement('pre');
      detail.textContent = '配对标签：' + row.pairing_label + '\\nClient ID：' + row.client_id + '\\nIssuer：' + row.issuer +
        '\\nResource：' + row.resource + '\\n完整回调：' + row.redirect_uri + '\\nScope：' + row.scopes.join(', ');
      card.append(detail);
      for (const [text,allow] of [['允许本次连接',true],['拒绝',false]]) {
        const button = document.createElement('button'); button.textContent = text;
        button.addEventListener('click', async () => {
          for (const action of card.querySelectorAll('button')) action.disabled = true;
          try { await post('approve', {transaction_id:row.transaction_id,action_nonce:row.action_nonce,allow}); await refresh(); }
          catch (error) { status.textContent = error.message; }
        }); card.append(button);
      } list.append(card);
    }
  }
  (async () => {
    const response = await fetch('/owner-api/session',{credentials:'same-origin',cache:'no-store'});
    if (!response.ok) throw new Error('无法取得本机会话。');
    token = (await response.json()).setup_token; await refresh();
    setInterval(() => refresh().catch(error => { status.textContent = error.message; }),2000);
  })().catch(error => { status.textContent = error.message; });
})();"""
