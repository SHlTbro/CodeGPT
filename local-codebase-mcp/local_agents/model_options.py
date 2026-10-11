"""Pure model selection. Canonical TOML (T17/ConfigStore) owns saved choices.

Context has EXACT required nonsecret keys: auth_mode, billing_source, provider,
endpoint, credential_context_id, credential_revision, config_revision,
cli_version, cli_sha256, executable. Unknown identity/CLI values may be null;
manual drafts do not require a credential. No aliases, secrets or discovery.
"""
from copy import deepcopy
import hashlib
import json
import re

from local_agents.contracts import _endpoint_allowed, validation_errors

CONTEXT_KEYS = frozenset(('auth_mode', 'billing_source', 'provider', 'endpoint',
    'credential_context_id', 'credential_revision', 'config_revision', 'cli_version', 'cli_sha256', 'executable'))
_HEX = re.compile(r'^[a-f0-9]{64}$')
EFFORT_LABELS = {'': '模型默认', 'none': '无', 'minimal': '最低', 'low': '低',
                 'medium': '中', 'high': '高', 'xhigh': '更高', 'max': '最高', 'ultra': '超高'}
TIER_LABELS = {'standard': '标准', 'priority': '优先'}


class ModelOptionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # Never echo supplied input/provider exceptions.


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_wire(value).encode('utf-8')).hexdigest()


def _text(value, limit=128):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def validate_context(context):
    if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
        raise ModelOptionError('INVALID_ARGUMENT')
    mode, provider = context['auth_mode'], context['provider']
    if mode not in ('unselected', 'chatgpt', 'api_key') or provider not in ('openai', 'custom_responses'):
        raise ModelOptionError('INVALID_ARGUMENT')
    billing = {'chatgpt': 'chatgpt_plan', 'api_key': 'openai_api' if provider == 'openai' else 'custom_api', 'unselected': None}[mode]
    if context['billing_source'] != billing or not isinstance(context['config_revision'], str) or not _HEX.fullmatch(context['config_revision']):
        raise ModelOptionError('INVALID_ARGUMENT')
    if mode == 'chatgpt' and (provider != 'openai' or context['endpoint'] is not None):
        raise ModelOptionError('INVALID_ARGUMENT')
    if mode == 'api_key' and not _endpoint_allowed(context['endpoint'], provider):
        raise ModelOptionError('INVALID_ARGUMENT')
    if mode == 'unselected' and context['endpoint'] is not None:
        raise ModelOptionError('INVALID_ARGUMENT')
    for name in ('credential_context_id', 'credential_revision', 'cli_version', 'executable'):
        if context[name] is not None and not _text(context[name], 4096 if name == 'executable' else 128):
            raise ModelOptionError('INVALID_ARGUMENT')
    if context['cli_sha256'] is not None and (not isinstance(context['cli_sha256'], str) or not _HEX.fullmatch(context['cli_sha256'])):
        raise ModelOptionError('INVALID_ARGUMENT')
    try:
        _wire(context).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise ModelOptionError('INVALID_ARGUMENT') from None
    return deepcopy(context)


def validate_selection(model_id, *, reasoning_effort='', service_tier='standard', option=None,
                       allowed_models=None, forced_model=None):
    """No parameter fallback: empty effort and standard explicitly clear inheritance."""
    if not _text(model_id) or not isinstance(reasoning_effort, str) or not _text(service_tier, 64):
        raise ModelOptionError('INVALID_ARGUMENT')
    if allowed_models is not None:
        if not isinstance(allowed_models, (list, tuple)) or any(not _text(model) for model in allowed_models):
            raise ModelOptionError('INVALID_ARGUMENT')
        if model_id not in allowed_models:
            raise ModelOptionError('POLICY_DENIED')
    if forced_model is not None:
        if not _text(forced_model):
            raise ModelOptionError('INVALID_ARGUMENT')
        if model_id != forced_model:
            raise ModelOptionError('POLICY_DENIED')
    selection = {'model_id': model_id, 'reasoning_effort': reasoning_effort,
                 'service_tier': service_tier, 'option': deepcopy(option)}
    if validation_errors('model_selection', selection):
        raise ModelOptionError('UNSUPPORTED_MODEL_PARAMETER')
    return selection


def selection_fingerprint(context, selection, *, profile, scope_fingerprint):
    context = validate_context(context)
    if not _text(profile, 64) or not isinstance(scope_fingerprint, str) or not _HEX.fullmatch(scope_fingerprint):
        raise ModelOptionError('INVALID_ARGUMENT')
    if validation_errors('model_selection', selection) or not _text(selection['model_id']):
        raise ModelOptionError('INVALID_ARGUMENT')
    # Catalog availability/revision is diagnostic, not a different user choice.
    return _hash({'context': context, 'selection': {name: selection[name] for name in
        ('model_id', 'reasoning_effort', 'service_tier')}, 'profile': profile, 'scope_fingerprint': scope_fingerprint})


def require_execution_capability(selection):
    """Return explicit adapter intent; never constructs/runs a CLI command.

    T11 must enforce both inherit=False flags using its verified CLI controls.
    Candidate compatibility is not current identity permission verification.
    """
    if validation_errors('model_selection', selection):
        raise ModelOptionError('UNSUPPORTED_MODEL_PARAMETER')
    option = selection['option']
    if option is None or option['compatibility'] != 'supported' or option['availability'] == 'unavailable':
        raise ModelOptionError('CAPABILITY_UNAVAILABLE')
    return {'model': selection['model_id'], 'reasoning_effort': selection['reasoning_effort'] or None,
        'service_tier': None if selection['service_tier'] == 'standard' else selection['service_tier'],
        'inherit_reasoning_effort': False, 'inherit_service_tier': False}
