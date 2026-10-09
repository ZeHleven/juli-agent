"""Strict native decisions and lossless translation; no provider or network."""
import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from juli.application.model_response import (
    RESPONSE_PROTOCOL, AnalysisResponse, parse_analysis_response, response_schema,
)
from juli.core.common import DomainError, canonical_json
from juli.core.evidence import AnalysisResult
from juli.core.runtime import Clarification
from juli.core.schemas import validate_payload


def native():
    return {
        'kind': 'analysis',
        'task_id': 'task:synthetic',
        'blocks': [{'kind': 'fact', 'text': '  原文事实。\n', 'evidence_ids': ['ev:one']}],
        'scenario_payload': {
            'summary': '合成说明',
            'nested': {'values': [True, None, 3, 2.5, '保留文字']},
        },
    }


def test_native_result_preserves_content_and_json_values_without_mutating_input():
    data = native()
    before = deepcopy(data)
    result = parse_analysis_response(data)
    assert RESPONSE_PROTOCOL == 'native-analysis-v3'
    assert result.task_id == data['task_id']
    assert result.blocks[0].text == data['blocks'][0]['text']
    assert result.blocks[0].evidence_ids == ('ev:one',)
    assert json.loads(result.scenario_payload_json) == data['scenario_payload']
    assert result.scenario_payload_json == canonical_json(data['scenario_payload'])
    assert '保留文字' in result.scenario_payload_json
    assert data == before
    assert set(result.model_dump()) == {'task_id', 'blocks', 'scenario_payload_json'}


def test_legacy_result_preserves_original_encoded_payload_and_defaults():
    data = native()
    legacy = {key: value for key, value in data.items() if key not in ('kind', 'scenario_payload')}
    original = '{ "summary" : "原始编码", "values": [1, true, null] }'
    legacy['scenario_payload_json'] = original
    assert parse_analysis_response(legacy) == AnalysisResult.model_validate(legacy)
    assert parse_analysis_response(legacy).scenario_payload_json == original
    del legacy['scenario_payload_json']
    assert parse_analysis_response(legacy).scenario_payload_json == '{}'


@pytest.mark.parametrize('change', [
    {'kind': 'unknown'},
    {'kind': None},
    {'kind': 'clarification', 'question': 'A question?'},
    {'question': 'Mixed output is invalid.'},
    {'scenario_payload_json': '{"summary":"mixed"}'},
    {'scenario_payload': '{"summary":"encoded"}'},
    {'scenario_payload': None},
    {'scenario_payload': []},
    {'scenario_payload': 1},
    {'blocks': []},
    {'task_id': 'invalid task id'},
    {'extra': 'must not be silently removed'},
])
def test_native_malformations_are_rejected_without_legacy_fallback(change):
    with pytest.raises(ValidationError):
        parse_analysis_response({**native(), **change})


@pytest.mark.parametrize('missing', ['kind', 'task_id', 'blocks', 'scenario_payload'])
def test_native_required_fields_are_not_invented(missing):
    data = native()
    del data[missing]
    with pytest.raises(ValidationError):
        parse_analysis_response(data)


@pytest.mark.parametrize('data', [None, [], 'plain text', 1, True])
def test_nonobjects_are_not_repaired(data):
    with pytest.raises(ValidationError):
        parse_analysis_response(data)


def test_fact_without_evidence_is_rejected_before_result_conversion():
    data = native()
    data['blocks'][0]['evidence_ids'] = []
    with pytest.raises(ValidationError):
        parse_analysis_response(data)


@pytest.mark.parametrize('allow_clarification', [False, True])
def test_schema_stands_alone_and_accepts_current_analysis(allow_clarification):
    schema = response_schema(allow_clarification=allow_clarification)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    advertised = native()
    if allow_clarification:
        advertised['critical_question'] = None
    assert validator.is_valid(advertised)
    assert parse_analysis_response(native()).task_id == 'task:synthetic'
    assert 'scenario_payload_json' not in canonical_json(schema)
    assert 'scenario_payload' in canonical_json(schema)
    for change in ({'question': 'mixed'}, {'kind': 'unknown'}, {'scenario_payload': '{}'}):
        invalid = {**native(), **change}
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            parse_analysis_response(invalid)


def test_clarification_schema_is_explicit_mutually_exclusive_and_opt_in():
    question = {'kind': 'clarification', 'question': 'Which synthetic option applies?'}
    durable_schema = response_schema(allow_clarification=True)
    durable = Draft202012Validator(durable_schema)
    assert durable_schema['discriminator']['propertyName'] == 'kind'
    assert durable.is_valid(question)
    assert Clarification.model_validate(question).question == question['question']
    assert not Draft202012Validator(response_schema()).is_valid(question)
    with pytest.raises(ValidationError):
        parse_analysis_response(question)
    assert not durable.is_valid({**question, 'blocks': native()['blocks']})
    assert not durable.is_valid({**native(), 'question': question['question']})
    assert not durable.is_valid({'kind': 'clarification'})
    # Compatibility parsing is deliberately not an advertised model branch.
    legacy = parse_analysis_response(native()).model_dump(mode='json')
    assert not durable.is_valid(legacy)
    assert parse_analysis_response(legacy) == parse_analysis_response(native())


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_native_json_values_are_rejected(value):
    data = native()
    data['scenario_payload']['nested']['values'].append(value)
    with pytest.raises(ValueError):
        parse_analysis_response(data)


def test_existing_payload_length_limit_applies_after_canonical_encoding():
    data = native()
    # {"v":"..."} uses eight characters around this unescaped JSON string.
    data['scenario_payload'] = {'v': '中' * (65536 - 8)}
    assert len(parse_analysis_response(data).scenario_payload_json) == 65536
    data['scenario_payload']['v'] += '中'
    with pytest.raises(ValidationError):
        parse_analysis_response(data)
    data['scenario_payload'] = {'v': '\n' * 32765}
    with pytest.raises(ValidationError):
        parse_analysis_response(data)


def test_block_count_and_original_text_limits_remain_enforced():
    data = native()
    data['blocks'] = [data['blocks'][0]] * 100
    assert len(parse_analysis_response(data).blocks) == 100
    data['blocks'].append(data['blocks'][0])
    with pytest.raises(ValidationError):
        parse_analysis_response(data)
    data = native()
    data['blocks'][0]['text'] = 'x' * 16001
    with pytest.raises(ValidationError):
        parse_analysis_response(data)


@pytest.mark.parametrize('legacy', [False, True])
def test_e03_catalog_mistake_still_fails_separate_scenario_validation(legacy):
    # Synthetic regression of the recorded failure shape, not rewritten evidence.
    schema = canonical_json({
        'type': 'object', 'properties': {'summary': {'type': 'string', 'minLength': 1}},
        'required': ['summary'], 'additionalProperties': False,
    })
    data = native()
    data['scenario_payload'] = {'question': 'Synthetic question', 'bound_resource_catalog': []}
    if legacy:
        data = {'task_id': data['task_id'], 'blocks': data['blocks'],
                'scenario_payload_json': canonical_json(data['scenario_payload'])}
    result = parse_analysis_response(data)
    assert 'summary' not in json.loads(result.scenario_payload_json)
    with pytest.raises(DomainError) as error:
        validate_payload(schema, json.loads(result.scenario_payload_json))
    assert error.value.code == 'result_schema_mismatch'


def test_native_model_rejects_non_json_python_values():
    data = native()
    data['scenario_payload']['not_json'] = object()
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(data)
