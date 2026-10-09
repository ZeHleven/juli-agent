"""E07 synthetic decisions test the state boundary, not model reasoning ability."""
import json
import sqlite3
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from juli.application.model_response import parse_interactive_analysis, parse_analysis_response, response_schema
from juli.core.common import DomainError, canonical_json, digest
from juli.core.scenarios import ScenarioPackage, package_fingerprint
from juli.core.schemas import normalize_schema
from juli.model_demo import scripted_retrieval_steps
from test_model_session import model_task
from test_runtime import runner
from test_runtime_responses import native_result, reply, checkpoint


@pytest.fixture
def package(registry):
    original = registry.get('minimal', '0.1.0')
    schema = json.loads(original.result_schema_json)
    schema['properties']['questions'] = {'type': 'array', 'items': {'type': 'string'}}
    encoded = normalize_schema(schema)
    return ScenarioPackage(manifest=original.manifest, instructions_text=original.instructions_text,
        result_schema_json=encoded,
        fingerprint=package_fingerprint(original.manifest, original.instructions_text, encoded))


QUESTION = 'Can you attend the required weekly practical session?'


def candidate(task, question=QUESTION):
    data = native_result(task)
    data['critical_question'] = question
    data['blocks'][0]['text'] = 'Attendance is not recorded. A decision requires this condition.'
    data['scenario_payload'] = {'summary': 'Synthetic candidate; attendance unresolved.',
                                'questions': ['Optional: which learning format do you prefer?']}
    return data


@pytest.mark.parametrize('answer', ['I can attend.', 'I do not know yet.'])
def test_candidate_waits_then_user_answer_survives_restart_and_idempotency(model_task, answer):
    path, _, _, _, _, task = model_task
    raw = reply(candidate(task))
    pending = runner(model_task, [raw]).run()
    assert pending['state']['status'] == 'pending'
    assert pending['state']['pending_question'] == QUESTION and pending['result'] is None
    saved = checkpoint(path)
    assert saved['messages'][-1]['content'] == raw.text
    assert saved['clarification_draft']['task_id'] == task and saved['result'] is None
    idle = runner(model_task)
    assert idle.run()['state'] == pending['state'] and idle.provider.calls == 0
    args = {'expected_version': pending['state']['version'], 'request_key': 'synthetic-attendance', 'text': answer}
    resumed = idle.answer(**args)
    assert idle.answer(**args) == resumed
    assert checkpoint(path)['clarification_draft'] is None
    with pytest.raises(DomainError):
        idle.answer(**{**args, 'text': 'Changed answer'})

    def complete(request):
        assert request.messages[-2].content == raw.text
        assert request.messages[-1].role == 'user'
        assert json.loads(request.messages[-1].content) == {'clarification_answer': answer}
        data = candidate(task, None)
        data['blocks'] = [{'kind': 'inference', 'text': 'If attendance is feasible, the course may suit the learner.'},
                          {'kind': 'limitation', 'text': 'User availability is self-reported and unverified.'}]
        data['scenario_payload']['summary'] = 'Conditional analysis; confirm attendance before committing.'
        return reply(data)

    completed = runner(model_task, [complete]).run()
    assert completed['state']['status'] == 'completed'
    assert completed['usage']['model_calls'] == completed['usage']['actions'] == 2
    assert completed['usage']['input_units'] <= 32000
    assert len(json.loads(completed['result']['scenario_payload_json'])['questions']) == 1
    assert checkpoint(path)['clarification_draft'] is None
    assert sum(event['kind'] == 'pending' for event in completed['events']) == 1
    assert set(completed['result']) == {'task_id', 'blocks', 'scenario_payload_json'}


def test_optional_report_questions_do_not_force_pending(model_task):
    data = candidate(model_task[-1], None)
    data['scenario_payload']['summary'] = 'Enough information for a conditional answer; optional suggestions remain.'
    done = runner(model_task, [reply(data)]).run()
    assert done['state']['status'] == 'completed'
    assert not any(event['kind'] == 'pending' for event in done['events'])


@pytest.mark.parametrize('question', ['', '   ', '\n\t', 3, True, [], {}, 'x' * 4001])
def test_bad_declaration_rejected_by_advertised_schema_and_runtime(model_task, question):
    data = candidate(model_task[-1], question)
    assert not Draft202012Validator(response_schema(allow_clarification=True)).is_valid(data)
    with pytest.raises(ValidationError):
        parse_interactive_analysis(data)
    run = runner(model_task, [reply(data)])
    with pytest.raises(DomainError) as error:
        run.step()
    assert error.value.code == 'invalid_model_reply'
    assert run.view()['result'] is None and run.view()['state']['status'] == 'failed'
    assert run.run()['usage']['model_calls'] == 1 and run.provider.calls == 1


@pytest.mark.parametrize('shape', ['old-native', 'legacy'])
def test_missing_declaration_cannot_silently_complete_durable_task(model_task, shape):
    data = candidate(model_task[-1], None)
    del data['critical_question']
    if shape == 'legacy':
        data = parse_analysis_response(data).model_dump(mode='json')
    # Noninteractive compatibility does not authorize ordinary durable completion.
    assert parse_analysis_response(data).task_id == model_task[-1]
    assert not Draft202012Validator(response_schema(allow_clarification=True)).is_valid(data)
    run = runner(model_task, [reply(data)])
    with pytest.raises(DomainError) as error:
        run.run()
    assert error.value.code == 'invalid_model_reply'
    assert run.view()['state']['status'] == 'failed' and run.view()['result'] is None


def test_conversion_preserves_candidate_and_does_not_discard_question():
    data = candidate('task:synthetic')
    before = deepcopy(data)
    assert Draft202012Validator(response_schema(allow_clarification=True)).is_valid(data)
    result, question = parse_interactive_analysis(data)
    assert question == QUESTION and json.loads(result.scenario_payload_json) == data['scenario_payload']
    assert data == before
    with pytest.raises(ValueError):
        parse_analysis_response(data)


@pytest.mark.parametrize('fault,code', [('task', 'task_mismatch'), ('schema', 'result_schema_mismatch'),
                                     ('unread', 'evidence_not_read'), ('missing', 'not_found'),
                                     ('unbound', 'not_found')])
def test_candidate_checks_same_task_scenario_and_originals_before_pending(model_task, fault, code):
    path, store, owner, space, _, task = model_task
    data = candidate(task)
    if fault == 'task':
        data['task_id'] = 'task:foreign'
    elif fault == 'schema':
        data['scenario_payload'] = {'questions': []}
    else:
        ref = 'evidence:nonexistent'
        if fault == 'unread':
            ref = store.search_evidence(owner.token, space, task, 'Python')['items'][0]['evidence_id']
        elif fault == 'unbound':
            other = store.save_text(owner.token, space, display_name='Unbound synthetic', text='Unbound data')
            with sqlite3.connect(path) as db:
                ref = db.execute('SELECT id FROM evidence_chunks WHERE version_id=?', (other.version_id,)).fetchone()[0]
        data['blocks'] = [{'kind': 'fact', 'text': 'Synthetic claim', 'evidence_ids': [ref]}]
    run = runner(model_task, [reply(data)])
    with pytest.raises(DomainError) as error:
        run.step()
    assert error.value.code == code
    saved = checkpoint(path)
    assert saved['clarification_draft'] is None and saved['result'] is None
    assert not any(event['kind'] == 'pending' for event in run.view()['events'])


def test_candidate_with_real_original_receipt_enters_pending(model_task):
    search, read, final = scripted_retrieval_steps(model_task[-1])
    def ask(request):
        data = json.loads(final(request).text)
        data['critical_question'] = QUESTION
        return reply(data)
    done = runner(model_task, [search, read, ask]).run()
    assert done['state']['status'] == 'pending' and done['result'] is None
    with sqlite3.connect(model_task[0]) as db:
        assert db.execute('SELECT COUNT(*) FROM evidence_reads').fetchone()[0] == 1
    assert checkpoint(model_task[0])['clarification_draft']['blocks'][0]['kind'] == 'fact'


@pytest.mark.parametrize('change', ['revoke', 'withdraw', 'cancel'])
@pytest.mark.parametrize('timing', ['during-call', 'while-pending'])
def test_authority_changes_fence_candidate_and_answer(model_task, change, timing):
    path, store, owner, space, resource, task = model_task
    def alter():
        if change == 'revoke':
            store.revoke_token(owner.token_id)
        elif change == 'withdraw':
            store.withdraw_resource(owner.token, space, resource.resource_id)
        else:
            state = store.get_task(owner.token, space, task)['state']
            store.cancel_task(owner.token, space, task, state['version'])
    def respond(request):
        if timing == 'during-call':
            alter()
        return reply(candidate(task))
    run = runner(model_task, [respond])
    if timing == 'during-call':
        with pytest.raises(DomainError):
            run.step()
        assert checkpoint(path)['clarification_draft'] is None
    else:
        pending = run.step()
        alter()
        with pytest.raises(DomainError):
            run.answer(expected_version=pending['state']['version'], request_key='late', text='I can attend.')
        assert not any(m['role'] == 'user' and 'clarification_answer' in m['content'] for m in checkpoint(path)['messages'])
    assert checkpoint(path)['result'] is None and run.provider.calls == 1


def test_answer_does_not_reset_model_budget_or_add_automatic_retry(model_task):
    from juli.application.model_session import SessionPolicy
    policy = SessionPolicy(max_model_calls=1)
    run = runner(model_task, [reply(candidate(model_task[-1]))], policy=policy)
    pending = run.run()
    run.answer(expected_version=pending['state']['version'], request_key='answer', text='I can attend.')
    failed = run.run()
    assert failed['state']['status'] == 'failed' and failed['result'] is None
    assert failed['usage']['model_calls'] == 1 and run.provider.calls == 1


@pytest.mark.parametrize('old', ['native-analysis-v1', 'native-analysis-v2'])
def test_old_model_protocol_cannot_resume_new_runner(model_task, old):
    run = runner(model_task)
    profile = digest(canonical_json({'deployment_profile': 'synthetic-v1',
        'provider_type': 'juli.adapters.scripted_model.ScriptedModel', 'provider_identity': None,
        'external_egress': False, 'context_window': 1000000,
        'message_protocol': 'tool-batches-v1', 'response_protocol': old}).encode())
    with sqlite3.connect(model_task[0]) as db:
        db.execute('UPDATE runtime_runs SET profile=?', (profile,))
    with pytest.raises(DomainError) as error:
        runner(model_task)
    assert error.value.code == 'runtime_config_changed' and run.provider.calls == 0
