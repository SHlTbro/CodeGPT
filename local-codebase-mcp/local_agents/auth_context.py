"""Nonsecret frozen identity descriptors and task-local environment construction.

Only trusted local services call this seam. Login observations are T11 inputs,
not user/MCP claims. No token files are opened, copied or probed here. Metadata
does not assert actual CLI identity/model verification or tool-child isolation.
"""
from dataclasses import dataclass, field
from copy import deepcopy
import re
import secrets
from pathlib import Path

from local_agents.config import normalize_agent_config
from local_agents.credentials import CredentialError, protect_directory


@dataclass(frozen=True)
class AuthContext:
    auth_mode: str
    billing_source: str
    provider: str
    endpoint: str | None
    credential_context_id: str
    credential_revision: str
    effective_auth_verified: bool = field(default=False, init=False)


def resolve_auth_context(agent, credential_store, *, login_method=None, login_context_id=None, login_revision=None):
    try:
        # Validate an enabled unselected draft structurally without confusing
        # its missing identity with a malformed object. Full validation follows.
        draft = deepcopy(agent)
        if draft.get('enabled') is True and draft.get('codex', {}).get('enabled') is True and draft['codex'].get('auth_mode', 'unselected') == 'unselected':
            draft['enabled'] = draft['codex']['enabled'] = False
            normalize_agent_config(draft)
            raise CredentialError('AUTH_MODE_UNSELECTED')
        config = normalize_agent_config(agent)
    except CredentialError:
        raise
    except Exception:
        raise CredentialError('INVALID_ARGUMENT') from None
    codex = config['codex']
    if not config['enabled'] or not codex['enabled']:
        raise CredentialError('CAPABILITY_DISABLED')
    if codex['auth_mode'] == 'chatgpt':
        if login_method not in (None, 'chatgpt'):
            raise CredentialError('AUTH_MODE_MISMATCH')
        if login_method is None or any(type(v) is not str or re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', v) is None for v in (login_context_id, login_revision)):
            raise CredentialError('AUTH_REQUIRED')
        return AuthContext('chatgpt', 'chatgpt_plan', 'openai', None, login_context_id, login_revision)
    if codex['auth_mode'] != 'api_key':
        raise CredentialError('AUTH_MODE_UNSELECTED')
    binding = credential_store.binding()
    if not binding['configured']:
        raise CredentialError('AUTH_REQUIRED')
    if binding['endpoint'] != codex['api_base_url'] or binding['provider'] != codex['api_provider']:
        raise CredentialError('CREDENTIAL_CONFLICT')
    return AuthContext('api_key', 'openai_api' if binding['provider'] == 'openai' else 'custom_api',
        binding['provider'], binding['endpoint'], binding['credential_context_id'], binding['credential_revision'])


def child_environment(context, *, parent_env, managed_home_root=None, credential_store=None, shared_home=None):
    """Build a new env; never mutate parent env or shared CLI configuration.

    Positive allowlist excludes arbitrary Key/provider/plugin environment vars.
    API home derives from service-owned root and opaque frozen context/version.
    Subscription authentication stays in its supplied trusted shared home;
    T11 must separately suppress config/extension inheritance with verified flags.
    """
    if not isinstance(context, AuthContext) or not isinstance(parent_env, dict):
        raise CredentialError('INVALID_ARGUMENT')
    allowed = {'path', 'systemroot', 'windir', 'comspec', 'pathext', 'temp', 'tmp',
        'userprofile', 'appdata', 'localappdata', 'programfiles', 'programfiles(x86)', 'programdata'}
    env = {key: value for key, value in parent_env.items() if key.casefold() in allowed}
    if context.auth_mode == 'chatgpt':
        if shared_home is None or not Path(shared_home).is_dir():
            raise CredentialError('AUTH_REQUIRED')
        env['CODEX_HOME'] = str(Path(shared_home))
    elif context.auth_mode == 'api_key':
        if managed_home_root is None or credential_store is None or any(re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', value) is None for value in (context.credential_context_id, context.credential_revision)):
            raise CredentialError('INVALID_ARGUMENT')
        home = protect_directory(Path(managed_home_root) / context.credential_context_id / context.credential_revision / secrets.token_hex(16))
        # An API-only home must never reuse an OAuth cache or inherited config.
        if any(home.iterdir()):
            raise CredentialError('AUTH_MODE_MISMATCH')
        env['CODEX_HOME'] = str(home)
        env['OPENAI_API_KEY'] = credential_store.resolve(endpoint=context.endpoint,
            provider=context.provider, credential_revision=context.credential_revision)
    else:
        raise CredentialError('AUTH_REQUIRED')
    return env
