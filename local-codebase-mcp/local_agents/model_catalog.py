"""Nonsecret metadata cache; no subprocess, network, auth or inference.

view is cache-only; refresh calls an explicitly injected metadata-only fetcher
only when enabled. Context shape is documented in model_options. save_option
is PURE: T17 -> ConfigStore canonical TOML remains the sole choice authority.
verification_reader(task_id) is trusted owner-bound T11/Store integration;
metadata and HTTP user inputs must never supply runtime verification evidence.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from config import ConfigError, _atomic_write, _file_lock
from local_agents.contracts import validation_errors
from local_agents.model_options import (ModelOptionError, _HEX, _hash, _text, _wire,
    require_execution_capability, selection_fingerprint, validate_context, validate_selection)


def _manual(model_id, mode):
    return {'model_id': model_id, 'display_name': model_id, 'source': 'manual', 'auth_mode': mode,
        'availability': 'unknown', 'compatibility': 'unknown', 'reasoning_efforts': [],
        'reasoning_effort_status': 'unknown', 'default_reasoning_effort': None, 'service_tiers': [],
        'parameter_source': 'unknown', 'verified_selection_fingerprint': None, 'checked_at': None,
        'reason': '手工模型尚未核实能力或当前身份权限。', 'cost_hint': None, 'cost_hint_basis': 'unknown'}


def historical_bundled(payload, auth_mode, *, observed_cli_version):
    """Convert explicit historical debug-models projection; never auto-discover it."""
    if auth_mode not in ('chatgpt', 'api_key') or not _text(observed_cli_version) or not isinstance(payload, dict) or not isinstance(payload.get('models'), list):
        raise ModelOptionError('INVALID_ARGUMENT')
    options = []
    for model in payload['models']:
        item = _manual(model['slug'], auth_mode)
        efforts = [level['effort'] for level in model.get('supported_reasoning_levels', [])]
        item.update(display_name=model.get('display_name', model['slug']), source='cli_bundled',
            availability='unverified', reasoning_efforts=efforts,
            reasoning_effort_status='known' if efforts else 'unknown',
            default_reasoning_effort=model.get('default_reasoning_level') if efforts else None,
            service_tiers=['standard', *[tier['id'] for tier in model.get('service_tiers', [])]],
            parameter_source='cli_catalog', reason=f'历史 CLI {observed_cli_version} bundled 候选；当前兼容和账户权限未验证。')
        if validation_errors('model_option', item):
            raise ModelOptionError('INVALID_ARGUMENT')
        options.append(item)
    return {'source': 'cli_bundled', 'models': options, 'fetched_at': None}


class ModelCatalog:
    def __init__(self, cache_path, *, metadata_fetcher=None, verification_reader=None, clock=time.time):
        self.path = Path(cache_path).absolute()
        if self.path.suffix.lower() != '.json' or (metadata_fetcher is not None and not callable(metadata_fetcher)) or (verification_reader is not None and not callable(verification_reader)):
            raise ModelOptionError('INVALID_ARGUMENT')
        self.metadata_fetcher, self.verification_reader, self.clock = metadata_fetcher, verification_reader, clock

    def _read(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 16 * 1024 * 1024:
                raise ModelOptionError('STORAGE_UNAVAILABLE')
            data = json.loads(self.path.read_bytes())
            if set(data) != {'entries'} or not isinstance(data['entries'], dict) or len(data['entries']) > 64:
                raise ValueError
            return data
        except FileNotFoundError:
            return {'entries': {}}
        except (OSError, ValueError, TypeError):
            raise ModelOptionError('STORAGE_UNAVAILABLE') from None

    def _write(self, data):
        encoded = _wire(data).encode('utf-8')
        if len(data['entries']) > 64 or len(encoded) > 16 * 1024 * 1024:
            raise ModelOptionError('STORAGE_UNAVAILABLE')
        try:
            _atomic_write(self.path, encoded)
        except (OSError, ConfigError):
            raise ModelOptionError('STORAGE_UNAVAILABLE') from None

    def _catalog(self, context, entry, *, state, saved_model_id, fingerprint=None, warning=None):
        if entry is not None and (not isinstance(entry, dict) or set(entry) != {'models','source','fetched_at','proofs'} or
                not isinstance(entry['models'], list) or len(entry['models']) > 200 or
                not isinstance(entry['proofs'], dict) or len(entry['proofs']) > 200):
            raise ModelOptionError('STORAGE_UNAVAILABLE')
        models = deepcopy(entry['models']) if entry else []
        for item in models:
            if validation_errors('model_option', item) or item['auth_mode'] != context['auth_mode']:
                raise ModelOptionError('STORAGE_UNAVAILABLE')
            item.update(availability='unverified', verified_selection_fingerprint=None, checked_at=None)
            proof = entry.get('proofs', {}).get(fingerprint) if fingerprint else None
            if proof and proof['model_id'] == item['model_id'] and item['compatibility'] == 'supported':
                item.update(availability='verified', verified_selection_fingerprint=fingerprint, checked_at=proof['checked_at'])
        if saved_model_id is not None:
            if not _text(saved_model_id):
                raise ModelOptionError('INVALID_ARGUMENT')
            if context['auth_mode'] != 'unselected' and not any(m['model_id'] == saved_model_id for m in models):
                if len(models) >= 200:
                    models = models[:199]
                models.append(_manual(saved_model_id, context['auth_mode']))
        source = entry['source'] if entry and entry['models'] else ('manual_only' if models else 'none')
        if models and any(m['source'] == 'manual' for m in models) and source not in ('none', 'manual_only'):
            source = 'mixed'
        result = {'catalog_revision': _hash({'context': context, 'models': models}),
            'config_revision': context['config_revision'], 'auth_mode': context['auth_mode'],
            'provider': context['provider'], 'credential_context_id': context['credential_context_id'],
            'source': source, 'state': state, 'models': models,
            'fetched_at': entry['fetched_at'] if entry else None, 'manual_entry_allowed': True,
            'model_request_performed': False, 'warning': warning}
        if validation_errors('model_catalog', result):
            raise ModelOptionError('STORAGE_UNAVAILABLE')
        return result

    def view(self, context, *, enabled, saved_model_id=None, selection_fingerprint=None):
        context = validate_context(context)
        if type(enabled) is not bool:
            raise ModelOptionError('INVALID_ARGUMENT')
        if selection_fingerprint is not None and (not isinstance(selection_fingerprint, str) or not _HEX.fullmatch(selection_fingerprint)):
            raise ModelOptionError('INVALID_ARGUMENT')
        if not enabled or context['auth_mode'] == 'unselected':
            return self._catalog(context, None, state='disabled' if not enabled else 'unselected',
                saved_model_id=saved_model_id, warning='关闭或未选择身份；不探测 CLI 或账户。')
        entry = self._read()['entries'].get(_hash(context))
        return self._catalog(context, entry, state='cached' if entry else 'unknown',
            saved_model_id=saved_model_id, fingerprint=selection_fingerprint,
            warning=None if entry else '无可靠目录；保留手工模型，未验证权限。')

    def refresh(self, context, *, enabled, saved_model_id=None):
        context = validate_context(context)
        if type(enabled) is not bool:
            raise ModelOptionError('INVALID_ARGUMENT')
        if not enabled or context['auth_mode'] == 'unselected':
            return self.view(context, enabled=enabled, saved_model_id=saved_model_id)
        if self.metadata_fetcher is None:
            return self.view(context, enabled=True, saved_model_id=saved_model_id)
        try:
            raw = self.metadata_fetcher(deepcopy(context))
            if not isinstance(raw, dict) or set(raw) != {'source', 'models', 'fetched_at'} or raw['source'] not in ('cli_catalog', 'cli_bundled', 'official_catalog', 'api_models'):
                raise ValueError
            if raw['source'] == 'api_models' and context['auth_mode'] != 'api_key':
                raise ValueError
            if not isinstance(raw['models'], list) or len(raw['models']) > 200:
                raise ValueError
            models = []
            for proposed in raw['models']:
                if validation_errors('model_option', proposed) or proposed['auth_mode'] != context['auth_mode'] or not _text(proposed['model_id']):
                    raise ValueError
                item = deepcopy(proposed)
                item.update(source=raw['source'], availability='unverified', verified_selection_fingerprint=None,
                    checked_at=None, cost_hint=None, cost_hint_basis='unknown', reason='元数据候选；未验证当前身份可调用性。')
                if raw['source'] in ('api_models', 'cli_bundled'):
                    item['compatibility'] = 'unknown'
                if raw['source'] == 'api_models':
                    item.update(reasoning_efforts=[], reasoning_effort_status='unknown',
                                default_reasoning_effort=None, service_tiers=[], parameter_source='unknown')
                models.append(item)
            entry = {'models': models, 'source': raw['source'], 'fetched_at': raw['fetched_at'], 'proofs': {}}
            self._catalog(context, entry, state='refreshed', saved_model_id=saved_model_id)
        except Exception:
            previous = self.view(context, enabled=True, saved_model_id=saved_model_id)
            previous.update(state='unavailable', warning='目录刷新失败；保留已保存模型及旧候选，权限未重新验证。')
            return previous
        with _file_lock(self.path.with_suffix('.json.lock')):
            data = self._read()
            # ponytail: fixed small map; refuse capacity instead of implicit
            # eviction of another identity's diagnostics/verification evidence.
            data['entries'][_hash(context)] = entry
            self._write(data)
        return self._catalog(context, entry, state='refreshed', saved_model_id=saved_model_id)

    def save_option(self, context, model_id, *, reasoning_effort='', service_tier='standard',
                    profile, scope_fingerprint, allowed_models=None, forced_model=None):
        """Pure validation result for T17 ConfigStore CAS; DOES NOT save a choice."""
        context = validate_context(context)
        catalog = self.view(context, enabled=True)
        option = next((item for item in catalog['models'] if item['model_id'] == model_id), None)
        selected = validate_selection(model_id, reasoning_effort=reasoning_effort,
            service_tier=service_tier, option=option, allowed_models=allowed_models, forced_model=forced_model)
        return {'selection': selected, 'selection_fingerprint': selection_fingerprint(context, selected,
            profile=profile, scope_fingerprint=scope_fingerprint)}

    def record_verification(self, context, selection, *, profile, scope_fingerprint, task_id):
        """Only trusted owner-bound runtime reader can promote an exact selection."""
        context = validate_context(context)
        require_execution_capability(selection)
        fingerprint = selection_fingerprint(context, selection, profile=profile, scope_fingerprint=scope_fingerprint)
        if self.verification_reader is None:
            raise ModelOptionError('VERIFICATION_REQUIRED')
        try:
            proof = self.verification_reader(task_id)
            if set(proof) != {'task', 'test'}:
                raise ValueError
            task, test = proof['task'], proof['test']
            if validation_errors('task', task) or validation_errors('model_test_status', test):
                raise ValueError
            identity = task['execution_context']
            if (task['task_id'] != task_id or test['task_id'] != task_id or task['status'] != 'succeeded' or
                task['verification'] != 'passed' or task['purpose'] != 'model_test' or
                test['status'] != 'succeeded' or test['verification'] != 'passed' or not test['model_request_performed'] or
                task['requested_model'] != selection['model_id'] or task['actual_models'] != [selection['model_id']] or
                identity['selection_fingerprint'] != fingerprint or test['selection_fingerprint'] != fingerprint or
                not identity['effective_auth_verified'] or test['model_id'] != selection['model_id'] or
                task['profile'] != profile or test['profile'] != profile or test['billing_source'] != context['billing_source'] or
                any(identity[key] != context[key] for key in ('auth_mode','billing_source','provider','endpoint','credential_context_id','credential_revision')) or
                task['config_revision'] != context['config_revision'] or task['backend_version'] != context['cli_version'] or
                identity['requested_reasoning_effort'] != (selection['reasoning_effort'] or None) or
                identity['requested_service_tier'] != selection['service_tier']):
                raise ValueError
        except Exception:
            raise ModelOptionError('VERIFICATION_REQUIRED') from None
        with _file_lock(self.path.with_suffix('.json.lock')):
            data = self._read()
            entry = data['entries'].get(_hash(context))
            if entry is None or not any(item['model_id'] == selection['model_id'] for item in entry['models']):
                raise ModelOptionError('CAPABILITY_UNAVAILABLE')
            if len(entry['proofs']) >= 200 and fingerprint not in entry['proofs']:
                raise ModelOptionError('STORAGE_UNAVAILABLE')
            entry['proofs'][fingerprint] = {'model_id': selection['model_id'], 'checked_at': test['updated_at']}
            self._write(data)
        return self.view(context, enabled=True, selection_fingerprint=fingerprint)
