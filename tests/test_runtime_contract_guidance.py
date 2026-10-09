"""E06 contract regressions using synthetic workshop data, never live models.

These tests establish schema/runtime behavior and visible guidance. A scripted
provider cannot demonstrate that an LLM follows that guidance.
"""
import json
import sqlite3
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from juli.adapters.scripted_model import ScriptedModel
from juli.adapters.sqlite_store import SQLiteStore
from juli.application.context import initial_context
from juli.application.model_response import (
    EVIDENCE_WORKFLOW, OUTPUT_CONTRACT, RESPONSE_PROTOCOL,
    parse_analysis_response, response_schema,
)
from juli.application.model_session import SessionPolicy
from juli.application.review import REVIEW_PROTOCOL, stage_messages
from juli.bootstrap import create_durable_runner, tool_contracts
from juli.core.common import DomainError, canonical_json, digest
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.tools import ToolReference
from test_runtime_review import review_case


def analysis(task='task:synthetic-workshop'):
    return {'kind': 'analysis', 'task_id': task,
            'blocks': [{'kind': 'inference',
                        'text': 'Based on the learner statement, attendance is provisionally feasible.',
                        'evidence_ids': []}],
            'scenario_payload': {'summary': 'Conditional on the learner self-report, attendance is feasible.'}}


def reply(data):
    return ModelReply(text=canonical_json(data), usage=ModelUsage(input_tokens=10, output_tokens=20))


def call(call_id, name, arguments):
    return ModelToolCall(call_id=call_id, tool=ToolReference(name=name, version='1.0.0'),
                         arguments_json=canonical_json(arguments))


def tool_reply(*calls):
    return ModelReply(tool_calls=calls, usage=ModelUsage(input_tokens=10, output_tokens=20))


def runner(case, steps=(), *, review=False, provider=None):
    return create_durable_runner(SQLiteStore(case.path), case.owner.token, case.space, case.task,
        provider or ScriptedModel(steps), profile='synthetic-e06-contract',
        policy=SessionPolicy(review_before_final=review))


def receipts(case):
    with sqlite3.connect(case.path) as db:
        return db.execute('SELECT evidence_id,content_hash FROM evidence_reads WHERE task_id=? ORDER BY evidence_id',
                          (case.task,)).fetchall()


@pytest.mark.parametrize('allow_clarification', [False, True])
@pytest.mark.parametrize('variant,accepted', [
    ('missing_fact_refs', False), ('empty_fact_refs', False),
    ('duplicate_fact_refs', False), ('duplicate_inference_refs', False),
    ('extra_root_trust', False), ('user_statement_kind', False),
    ('valid_fact', True), ('empty_inference_refs', True), ('omitted_inference_refs', True),
])
def test_model_schema_and_parser_agree_without_repair(allow_clarification, variant, accepted):
    data = analysis()
    if allow_clarification:
        data['critical_question'] = None
    block = data['blocks'][0]
    if variant in {'missing_fact_refs', 'empty_fact_refs', 'duplicate_fact_refs', 'valid_fact'}:
        block.update(kind='fact', text='The workshop requires Tuesday evenings.', evidence_ids=['evidence:synthetic'])
    if variant in {'missing_fact_refs', 'omitted_inference_refs'}:
        del block['evidence_ids']
    elif variant == 'empty_fact_refs':
        block['evidence_ids'] = []
    elif variant.startswith('duplicate_'):
        block['evidence_ids'] = ['evidence:synthetic', 'evidence:synthetic']
    elif variant == 'extra_root_trust':
        data['trust'] = 'untrusted_user_data'
    elif variant == 'user_statement_kind':
        block['kind'] = 'user_statement'
    original = deepcopy(data)
    schema = response_schema(allow_clarification=allow_clarification)
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(data) is accepted
    if accepted:
        result = parse_analysis_response(data)
        assert result.blocks[0].text == block['text']
        assert set(result.model_dump()) == {'task_id', 'blocks', 'scenario_payload_json'}
    else:
        with pytest.raises(ValidationError):
            parse_analysis_response(data)
    assert data == original


def test_plain_and_review_analysis_prompts_expose_one_contract(snapshot, package):
    messages = initial_context(snapshot, package, max_tool_calls=8, allow_clarification=True)
    original = tuple(message.model_dump_json() for message in messages)
    assert OUTPUT_CONTRACT in messages[0].content
    assert EVIDENCE_WORKFLOW in messages[0].content
    assert json.loads(messages[2].content)['trust'] == 'untrusted_user_data'
    for phase in ('draft', 'final'):
        projected = stage_messages(messages, phase)
        assert OUTPUT_CONTRACT in projected[0].content
        assert (EVIDENCE_WORKFLOW in projected[0].content) is (phase == 'draft')
        assert projected[1:] == messages[1:]
        schema = json.loads(projected[0].content.split('Response JSON schema:\n', 1)[1])
        assert Draft202012Validator(schema).is_valid(analysis())
        assert not Draft202012Validator(schema).is_valid({**analysis(), 'trust': 'untrusted_user_data'})
    review = stage_messages(messages, 'review')
    assert OUTPUT_CONTRACT not in review[0].content
    schema = json.loads(review[0].content.split('Response JSON schema:\n', 1)[1])
    assert Draft202012Validator(schema).is_valid({'kind': 'ready', 'reason': 'No decisive unknown remains.'})
    assert not Draft202012Validator(schema).is_valid(analysis())
    assert tuple(message.model_dump_json() for message in messages) == original


def test_tool_guidance_distinguishes_ids_without_narrowing_parameter_contract():
    definitions = {tool.reference.name: tool for tool in tool_contracts().list()}
    search, read = definitions['evidence.search'], definitions['evidence.read']
    assert 'returned evidence_id before reads' in search.description
    assert 'earlier search evidence_id' in read.description
    assert 'not file version ID or same-batch search' in read.description
    schema = json.loads(read.input_schema_json)
    assert 'NOT resource_version_id/version_id' in schema['properties']['evidence_id']['description']
    # Guidance is not a new lexical validator. Authorization/storage must still
    # reject a wrong identifier, including one whose string schema is valid.
    assert Draft202012Validator(schema).is_valid({'evidence_id': 'version:synthetic'})
    assert not Draft202012Validator(schema).is_valid({'evidence_id': ''})


@pytest.mark.parametrize('review', [False, True], ids=['ordinary', 'review-final'])
@pytest.mark.parametrize('claim_kind', ['inference', 'fact'])
def test_answer_can_support_conditional_inference_but_never_an_uncited_fact(review_case, review, claim_kind):
    case = review_case()
    question = 'Can you attend the workshop on Tuesday evenings?'
    answer = 'I can attend on Tuesday evenings; this is my current availability.'

    if review:
        draft = analysis(case.task)
        draft['blocks'][0]['evidence_ids'] = list(case.refs)
        question_reply = reply({'kind': 'clarify', 'question': question,
                                'decision_impact': 'The workshop requires Tuesday evening attendance.'})
        run = runner(case, [reply(draft), question_reply], review=True)
    else:
        def search(request):
            assert not any(message.role == 'tool' for message in request.messages)
            return tool_reply(call('workshop-search', 'evidence.search', {'query': 'Workshop'}))

        def reads(request):
            messages = [message for message in request.messages if message.role == 'tool']
            assert len(messages) == 1 and messages[0].tool_call_id == 'workshop-search'
            items = json.loads(messages[0].content)['items']
            assert {item['evidence_id'] for item in items} == set(case.refs)
            return tool_reply(*(call(f'workshop-read-{index}', 'evidence.read',
                {'evidence_id': item['evidence_id']}) for index, item in enumerate(items)))

        def clarify(request):
            outputs = [message for message in request.messages if message.role == 'tool']
            assert len(outputs) == 3
            assert all('content_hash' in json.loads(message.content)['items'][0] for message in outputs[1:])
            return reply({'kind': 'clarification', 'question': question})

        run = runner(case, [search, reads, clarify])
    pending = run.run()
    assert pending['state']['status'] == 'pending' and pending['result'] is None
    original_receipts = receipts(case)
    assert {row[0] for row in original_receipts} == set(case.refs)
    run.answer(expected_version=pending['state']['version'], request_key='availability', text=answer)
    assert receipts(case) == original_receipts

    def final(request):
        matches = [message for message in request.messages if answer in message.content]
        assert len(matches) == 1 and matches[0].role == 'user'
        assert json.loads(matches[0].content) == {'clarification_answer': answer}
        assert OUTPUT_CONTRACT in request.messages[0].content
        data = analysis(case.task)
        if not review:
            data['critical_question'] = None
        data['blocks'][0]['kind'] = claim_kind
        data['blocks'].append({'kind': 'limitation', 'text': 'Availability is self-reported, not independently verified.',
                               'evidence_ids': []})
        return reply(data)

    steps = [final]
    if review:
        steps.insert(0, reply({'kind': 'ready', 'reason': 'Apply the answered attendance condition as a user statement.'}))
    resumed = runner(case, steps, review=review)
    if claim_kind == 'fact':
        with pytest.raises(DomainError) as error:
            resumed.run()
        assert error.value.code == 'invalid_model_reply'
        done = resumed.view()
        assert done['state']['status'] == 'failed' and done['result'] is None
    else:
        done = resumed.run()
        assert done['state']['status'] == 'completed'
        assert done['result']['blocks'][0]['kind'] == 'inference'
        assert all(block['evidence_ids'] == [] for block in done['result']['blocks'])
    assert receipts(case) == original_receipts
    assert sum(event['kind'] == 'pending' for event in done['events']) == 1


@pytest.mark.parametrize('review', [False, True])
def test_old_protocol_identity_cannot_silently_resume_new_contract(review_case, review):
    case = review_case()
    run = runner(case, review=review)
    assert RESPONSE_PROTOCOL == 'native-analysis-v3'
    identity = {'deployment_profile': 'synthetic-e06-contract',
                'provider_type': 'juli.adapters.scripted_model.ScriptedModel',
                'provider_identity': None, 'external_egress': False, 'context_window': 1000000,
                'message_protocol': 'tool-batches-v1', 'response_protocol': 'native-analysis-v1'}
    if review:
        identity['review_protocol'] = REVIEW_PROTOCOL
    old_profile = digest(canonical_json(identity).encode())
    assert old_profile != run.profile
    with sqlite3.connect(case.path) as db:
        before = db.execute('SELECT checkpoint_json FROM runtime_runs WHERE task_id=?', (case.task,)).fetchone()[0]
        db.execute('UPDATE runtime_runs SET profile=? WHERE task_id=?', (old_profile, case.task))
    provider = ScriptedModel([reply(analysis(case.task))])
    with pytest.raises(DomainError) as error:
        runner(case, review=review, provider=provider)
    assert error.value.code == 'runtime_config_changed'
    assert provider.calls == 0
    with sqlite3.connect(case.path) as db:
        assert db.execute('SELECT checkpoint_json FROM runtime_runs WHERE task_id=?', (case.task,)).fetchone()[0] == before
        assert db.execute('SELECT COUNT(*) FROM runtime_actions WHERE task_id=?', (case.task,)).fetchone()[0] == 0


def test_wrong_version_is_not_replaced_after_search_returns_and_failure_is_not_retried(review_case):
    case = review_case()
    version_id = case.resources[0].version_id
    proposal = tool_reply(call('search-first', 'evidence.search', {'query': 'Workshop'}),
                          call('wrong-read', 'evidence.read', {'evidence_id': version_id}))
    run = runner(case, [proposal])
    run.step()
    run.step()
    assert receipts(case) == []
    with pytest.raises(DomainError) as error:
        run.step()
    assert error.value.code == 'not_found'
    failed = run.view()
    assert failed['state']['status'] == 'failed' and failed['result'] is None
    assert failed['usage']['actions'] == 3 and failed['usage']['model_calls'] == 1
    assert run.provider.calls == 1 and receipts(case) == []
    with sqlite3.connect(case.path) as db:
        saved = json.loads(db.execute('SELECT checkpoint_json FROM runtime_runs WHERE task_id=?', (case.task,)).fetchone()[0])
    assert json.loads(saved['pending']['arguments_json']) == {'evidence_id': version_id}
    assert any(message['role'] == 'tool' and message['tool_call_id'] == 'search-first' for message in saved['messages'])
    stopped = runner(case)
    assert stopped.run()['state']['status'] == 'failed'
    assert stopped.provider.calls == 0 and receipts(case) == []
