"""Synthetic offline protocol fixtures, never product inference evidence."""
import copy
import json
from pathlib import Path

import pytest

from local_agents.backends.codex import (
    CLI_SHA256, CLI_VERSION, CodexProtocolError, execution_parameters,
    parse_events, require_project_execution,
)


def selection(effort='', tier='standard'):
    cases = json.loads((Path(__file__).parents[2] / 'contracts/agent/cases.json').read_text('utf-8'))
    option = next(case['value'] for case in cases['cases'] if case['schema_def'] == 'model_option' and case['valid'])
    option = copy.deepcopy(option)
    option.update(reasoning_efforts=['low', 'medium'], service_tiers=['standard', 'priority'])
    return dict(model_id=option['model_id'], reasoning_effort=effort, service_tier=tier, option=option)


def parameters(value=None, **kwargs):
    return execution_parameters(value or selection(), cli_version=CLI_VERSION, cli_sha256=CLI_SHA256, **kwargs)


def event(kind, **fields):
    return json.dumps(dict(type=kind, **fields))


def stream(*middle):
    return [event('thread.started', thread_id='private-native-id'), event('turn.started'), *middle,
            event('turn.completed', usage=dict(input_tokens=10, cached_input_tokens=2,
                  output_tokens=3, reasoning_output_tokens=1))]


def message(text='fixture answer', identifier='item-1'):
    return event('item.completed', item=dict(id=identifier, type='agent_message', text=text))


def test_default_effort_uses_disclosed_default_and_standard_clears_inheritance():
    result = parameters()
    assert result['config_overrides'] == ('model_reasoning_effort="low"', 'service_tier="default"')
    assert result['requested']['reasoning_effort'] == ''
    assert result['candidate']['reasoning_effort'] == 'low'
    assert result['candidate']['source'] == 'cli_catalog'
    assert result['actual'] == dict(model=None, reasoning_effort=None, service_tier=None)


def test_explicit_medium_priority_are_not_inherited_or_silently_reset():
    result = parameters(selection('medium', 'priority'))
    assert result['config_overrides'] == ('model_reasoning_effort="medium"', 'service_tier="priority"')
    assert result['candidate']['reasoning_effort'] == 'medium'


@pytest.mark.parametrize('version,digest', [('future', CLI_SHA256), (CLI_VERSION, '0'*64), (None, CLI_SHA256)])
def test_unknown_installed_mapping_rejects(version, digest):
    with pytest.raises(CodexProtocolError, match='CAPABILITY_UNAVAILABLE'):
        execution_parameters(selection(), cli_version=version, cli_sha256=digest)


@pytest.mark.parametrize('mutation', [
    lambda x: x.update(reasoning_effort='ultra'),
    lambda x: x.update(service_tier='flex'),
    lambda x: x['option'].update(default_reasoning_effort=None),
    lambda x: x['option'].update(compatibility='unknown'),
    lambda x: x.update(option=None),
])
def test_unsupported_or_unknown_selection_rejected(mutation):
    value=selection(); mutation(value)
    with pytest.raises((CodexProtocolError, ValueError)):
        parameters(value)


def test_execution_is_closed_even_for_pinned_supported_parameters():
    parameters()
    with pytest.raises(CodexProtocolError, match='CAPABILITY_UNAVAILABLE'):
        require_project_execution()


def test_valid_stream_has_only_report_text_and_cumulative_usage():
    result = parse_events(stream(message()), exit_code=0)
    assert result['succeeded'] is True
    assert result['text'] == 'fixture answer'
    assert result['usage'] == dict(input_tokens=10, cached_input_tokens=2, cache_write_input_tokens=0,
                                  output_tokens=3, reasoning_output_tokens=1)
    assert result['usage_basis'] == 'thread_cumulative'
    assert result['actual'] == dict(model=None, reasoning_effort=None, service_tier=None, cost=None)
    assert 'private-native-id' not in json.dumps(result)


@pytest.mark.parametrize('lines', [[], stream()[:-1], stream()[1:], stream()[::2],
    stream('bad json'), stream('{"type":"turn.completed","usage":NaN}'),
    stream('{"type":"item.completed","type":"turn.started"}'),
    stream(event('future.event', secret='never-publish-me')),
    stream(event('item.completed', item=dict(id='x', type='future_item', secret='never-publish-me'))),
    stream(message()) + [message('after terminal')], stream(message(), message()),
    stream(event('turn.failed', error=dict(message='never-publish-me'))),
    stream(event('error', message='never-publish-me')),
    stream(event('item.completed', item=dict(id='x', type='agent_message', text=123))),
])
def test_malformed_missing_unknown_and_contradictory_events_cannot_succeed(lines):
    result = parse_events(lines, exit_code=0)
    assert not result['succeeded']
    assert result['diagnostics']
    assert 'never-publish-me' not in json.dumps(result)


@pytest.mark.parametrize('code', [1, -1, None, True])
def test_exit_status_is_independent_required_evidence(code):
    assert not parse_events(stream(message()), exit_code=code)['succeeded']


def test_reasoning_summary_and_raw_command_output_are_never_report_text():
    lines=stream(event('item.completed', item=dict(id='r', type='reasoning', text='hidden fixture')),
                 event('item.completed', item=dict(id='c', type='command_execution', command='fixture',
                       aggregated_output='raw fixture', exit_code=0, status='completed')), message())
    result=parse_events(lines, exit_code=0)
    assert result['succeeded']
    assert 'hidden fixture' not in json.dumps(result)
    assert 'raw fixture' not in json.dumps(result)


def test_nonfatal_error_item_retained_as_diagnostic_without_faking_failure():
    result=parse_events(stream(event('item.completed', item=dict(id='w', type='error', message='warning fixture')), message()), exit_code=0)
    assert result['succeeded']
    assert result['diagnostics'] == ('ITEM_WARNING',)


@pytest.mark.parametrize('usage', [dict(input_tokens=True, cached_input_tokens=0, output_tokens=1, reasoning_output_tokens=0),
    dict(input_tokens=-1, cached_input_tokens=0, output_tokens=1, reasoning_output_tokens=0),
    dict(input_tokens=1, cached_input_tokens=2, output_tokens=0, reasoning_output_tokens=0), {}])
def test_invalid_usage_does_not_become_reported_zero_or_success(usage):
    lines=stream(message()); lines[-1]=event('turn.completed',usage=usage)
    result=parse_events(lines, exit_code=0)
    assert not result['succeeded'] and result['usage'] is None


def test_limits_reject_without_silently_truncating_success():
    assert not parse_events(stream(message('a'*20)), exit_code=0, max_text_chars=5)['succeeded']
    assert not parse_events(stream(message()), exit_code=0, max_events=2)['succeeded']
    assert not parse_events(stream(message()), exit_code=0, max_line_chars=10)['succeeded']


def test_multiple_turn_totals_are_not_added_or_accepted_as_single_execution():
    lines=stream(message())+[event('turn.started'), event('turn.completed', usage=dict(input_tokens=20,
        cached_input_tokens=2, output_tokens=4, reasoning_output_tokens=1))]
    assert not parse_events(lines, exit_code=0)['succeeded']


def test_unknown_default_reasoning_status_cannot_supply_an_execution_default():
    value=selection(); value['option']['reasoning_effort_status']='unknown'
    with pytest.raises((CodexProtocolError, ValueError)):
        parameters(value)


@pytest.mark.parametrize('item', [
    dict(id='s', type='agent_message', text='\ud800'),
    dict(id='c', type='command_execution', command='fixture', aggregated_output='',
         exit_code=2**40, status='completed'),
])
def test_invalid_wire_strings_and_integer_ranges_fail_closed(item):
    assert not parse_events(stream(event('item.completed', item=item)), exit_code=0)['succeeded']


def test_unfinished_tool_lifecycle_prevents_completion():
    lines=stream(event('item.started', item=dict(id='c', type='command_execution', command='fixture',
          aggregated_output='', exit_code=None, status='in_progress')), message())
    assert not parse_events(lines, exit_code=0)['succeeded']
