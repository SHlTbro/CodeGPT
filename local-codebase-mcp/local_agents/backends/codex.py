"""Pure, pinned Codex exec protocol slice. No subprocess, auth or model IO.

Wire source: openai/codex 36650394c5b38c2990ccf2a3457165ca3e9d9726.
Exec is not an approved project sandbox. Parameters are candidate intent only.
"""
import json
from copy import deepcopy

from local_agents.model_options import require_execution_capability

CLI_VERSION = '0.157.1'
CLI_SHA256 = '8cb0e69e99ff2a158c54815db82d0f2e524d8f301bc30184722cfd1ae5973574'


class CodexProtocolError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def execution_parameters(selection, *, cli_version, cli_sha256):
    """Produce two explicit -c values, never a launch command or permission.

    Empty effort means the disclosed model default, not a global medium guess.
    Standard is the pinned CLI's `default`, removing inherited priority/flex.
    Neither candidate setting proves what a provider ultimately resolved.
    """
    if (cli_version, cli_sha256) != (CLI_VERSION, CLI_SHA256):
        raise CodexProtocolError('CAPABILITY_UNAVAILABLE')
    intent = require_execution_capability(selection)
    option = selection['option']
    effort = intent['reasoning_effort'] or option['default_reasoning_effort']
    if (option['reasoning_effort_status'] != 'known' or effort is None or
            effort not in option['reasoning_efforts']):
        raise CodexProtocolError('UNSUPPORTED_MODEL_PARAMETER')
    return {'model': intent['model'],
            'config_overrides': ('model_reasoning_effort=' + json.dumps(effort),
                                 'service_tier=' + json.dumps(intent['service_tier'] or 'default')),
            'requested': {key: deepcopy(selection[key]) for key in
                          ('model_id', 'reasoning_effort', 'service_tier')},
            'candidate': {'reasoning_effort': effort, 'service_tier': selection['service_tier'],
                          'source': option['parameter_source']},
            'actual': {'model': None, 'reasoning_effort': None, 'service_tier': None}}


def require_project_execution():
    """Fail closed until identity, isolation and tool permissions are verified."""
    raise CodexProtocolError('CAPABILITY_UNAVAILABLE')


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _string(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode('utf-8')
        return True
    except UnicodeError:
        return False


def _integer(value):
    return type(value) is int and 0 <= value <= 2**63-1


def _usage(value):
    keys = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
            'output_tokens', 'reasoning_output_tokens')
    if not isinstance(value, dict):
        raise ValueError('usage')
    result = {key: value.get(key, 0 if key == 'cache_write_input_tokens' else None) for key in keys}
    if not all(_integer(item) for item in result.values()):
        raise ValueError('usage')
    if (result['cached_input_tokens'] > result['input_tokens'] or
            result['reasoning_output_tokens'] > result['output_tokens']):
        raise ValueError('usage')
    return result


def _item(value):
    """Validate the supported item subset; never retain raw tool/reasoning data."""
    if not isinstance(value, dict) or not _string(value.get('id')) or not value['id']:
        raise ValueError('item')
    kind = value.get('type')
    if kind in ('agent_message', 'reasoning', 'error'):
        valid = _string(value.get('message' if kind == 'error' else 'text'))
    elif kind == 'command_execution':
        valid = (_string(value.get('command')) and _string(value.get('aggregated_output')) and
                 (value.get('exit_code') is None or type(value['exit_code']) is int and
                  -2**31 <= value['exit_code'] <= 2**31-1) and
                 value.get('status') in ('in_progress', 'completed', 'failed', 'declined'))
    elif kind == 'file_change':
        valid = (isinstance(value.get('changes'), list) and
                 all(isinstance(change, dict) and _string(change.get('path')) and
                     change.get('kind') in ('add', 'delete', 'update') for change in value['changes']) and
                 value.get('status') in ('in_progress', 'completed', 'failed'))
    elif kind == 'todo_list':
        valid = (isinstance(value.get('items'), list) and
                 all(isinstance(item, dict) and _string(item.get('text')) and
                     type(item.get('completed')) is bool for item in value['items']))
    elif kind == 'mcp_tool_call':
        result, error = value.get('result'), value.get('error')
        valid = (_string(value.get('server')) and _string(value.get('tool')) and
                 value.get('status') in ('in_progress', 'completed', 'failed') and
                 (result is None or isinstance(result, dict) and isinstance(result.get('content'), list)) and
                 (error is None or isinstance(error, dict) and _string(error.get('message'))))
    else:
        raise CodexProtocolError('UNKNOWN_ITEM')
    if not valid:
        raise ValueError('item')
    return kind


def parse_events(lines, *, exit_code, max_events=10000, max_line_chars=1048576,
                 max_text_chars=262144):
    """Parse one fresh single-turn exec JSONL stream into a bounded projection.

    Diagnostics preserve unknown/error occurrences without echoing their secret-
    bearing payloads. Web/collab items are outside this slice and fail closed.
    Native thread ids stay private; actual identity and cost remain unknown.
    Usage is CLI-emitted *thread total*, not a per-turn delta or a cost quote.
    Caller must drain transport and provide the real process exit status.
    """
    if any(type(limit) is not int or limit < 1 for limit in
           (max_events, max_line_chars, max_text_chars)):
        raise CodexProtocolError('INVALID_ARGUMENT')
    phase, text, usage = 'new', '', None
    diagnostics, completed, items = [], set(), {}
    invalid = False
    try:
        for count, line in enumerate(lines, 1):
            if count > max_events:
                diagnostics.append('EVENT_LIMIT'); invalid = True; break
            if not isinstance(line, str) or len(line) > max_line_chars:
                diagnostics.append('LINE_LIMIT'); invalid = True; continue
            try:
                event = json.loads(line, object_pairs_hook=_object,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
                if not isinstance(event, dict):
                    raise ValueError('event')
                kind = event.get('type')
                if kind == 'thread.started':
                    if phase != 'new' or not _string(event.get('thread_id')) or not event['thread_id']:
                        raise ValueError('order')
                    phase = 'thread'
                elif kind == 'turn.started':
                    if phase != 'thread':
                        raise ValueError('order')
                    phase = 'turn'
                elif kind in ('turn.completed', 'turn.failed'):
                    if phase != 'turn' or any(item['open'] for item in items.values()):
                        raise ValueError('order')
                    if kind == 'turn.completed':
                        usage = _usage(event.get('usage'))
                        phase = 'completed'
                    else:
                        if not isinstance(event.get('error'), dict) or not _string(event['error'].get('message')):
                            raise ValueError('error')
                        phase = 'failed'; diagnostics.append('TURN_FAILED'); invalid = True
                elif kind == 'error':
                    if not _string(event.get('message')):
                        raise ValueError('error')
                    diagnostics.append('STREAM_ERROR'); invalid = True
                elif kind in ('item.started', 'item.updated', 'item.completed'):
                    if phase != 'turn':
                        raise ValueError('order')
                    item = event.get('item'); item_kind = _item(item); identifier = item['id']
                    if identifier in completed or (identifier in items and items[identifier]['kind'] != item_kind):
                        raise ValueError('order')
                    if kind == 'item.started' and identifier in items or kind == 'item.updated' and identifier not in items:
                        raise ValueError('order')
                    is_completed = kind == 'item.completed'
                    if 'status' in item and ((item['status'] == 'in_progress') == is_completed):
                        raise ValueError('status')
                    items[identifier] = {'kind': item_kind, 'open': not is_completed}
                    if is_completed:
                        completed.add(identifier)
                        if item_kind == 'agent_message':
                            if len(item['text']) > max_text_chars:
                                raise CodexProtocolError('TEXT_LIMIT')
                            text = item['text']
                        elif item_kind == 'error':
                            diagnostics.append('ITEM_WARNING')
                else:
                    raise CodexProtocolError('UNKNOWN_EVENT')
            except (ValueError, TypeError, RecursionError) as exc:
                diagnostics.append(exc.code if isinstance(exc, CodexProtocolError) else 'MALFORMED_EVENT')
                invalid = True
    except (OSError, UnicodeError):
        diagnostics.append('STREAM_READ_FAILED'); invalid = True
    if phase != 'completed':
        diagnostics.append('MISSING_COMPLETION'); invalid = True
    if type(exit_code) is not int or exit_code != 0:
        diagnostics.append('PROCESS_EXIT_FAILED'); invalid = True
    return {'succeeded': not invalid, 'text': text, 'usage': usage,
            'usage_basis': 'thread_cumulative' if usage is not None else 'unknown',
            'diagnostics': tuple(diagnostics),
            'actual': {'model': None, 'reasoning_effort': None, 'service_tier': None, 'cost': None}}
