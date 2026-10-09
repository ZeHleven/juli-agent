from juli.model_demo import scripted_analysis_json
import copy
import ipaddress
import json
import shutil
import socket

import pytest
from pydantic import ValidationError

from juli.adapters.scenario_files import load_scenario
from juli.api import create_app
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelReply, ModelUsage
from juli.core.tasks import TaskBudget
from examples.scenario_acceptance.fixture_model import ScenarioFixtureModel
from examples.scenario_acceptance.harness import (
    AcceptanceSession, EXAMPLE_BUDGET, HERE, body, cases, example_registry, run_case,
)

CASES = cases()


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    attempts = []
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    create_connection = socket.create_connection
    def allowed(address):
        # Windows asyncio creates a socket pair using a loopback connection.
        try:
            return isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            return False
    def check(address):
        if allowed(address):
            return
        attempts.append(True)
        raise AssertionError('Scenario acceptance must not connect to external network services.')
    def guarded_connect(sock, address):
        check(address)
        return connect(sock, address)
    def guarded_connect_ex(sock, address):
        check(address)
        return connect_ex(sock, address)
    def guarded_create(address, *args, **kwargs):
        check(address)
        return create_connection(address, *args, **kwargs)
    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
    monkeypatch.setattr(socket, 'create_connection', guarded_create)
    yield
    assert not attempts


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
@pytest.mark.parametrize('missing', [False, True], ids=['complete', 'missing-background'])
def test_scenarios_share_upload_question_resume_read_and_result_api(tmp_path, case, missing):
    result = run_case(tmp_path/'new.sqlite3', case, missing=missing)
    assert result['cited_resources'] == (1 if missing else 3)
    assert result['actions'] == (6 if missing else 10)
    assert result['scripted_model_calls'] == (4 if missing else 6)


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_saved_background_is_reused_with_a_new_document_without_reupload(tmp_path, case):
    session = AcceptanceSession(tmp_path/'new.sqlite3', case, task_budget=EXAMPLE_BUDGET)
    try:
        session.prepare()
        old_snapshot = session.snapshot
        old_task = session.task_base
        first = session.finish()
        assert first['state']['status'] == 'completed'
        background_versions = {session.documents[doc['file']]['version_id'] for doc in case['documents'] if doc['background']}
        # Updating a saved resource creates a new version, leaving the old task intact.
        doc = next(doc for doc in case['documents'] if doc['background'])
        resource = session.documents[doc['file']]
        updated = body(session.client.post(session.base+'/resources/'+resource['resource_id']+'/versions',
            json={'display_name':'Updated synthetic background', 'text':case['query']+' 更新的合成背景。'}), 201)
        assert updated['version_id'] != resource['version_id']
        assert body(session.client.get(old_task))['snapshot'] == old_snapshot
        session.prepare(new_subject=True)
        new_versions = {r['version_id'] for r in session.snapshot['resources']}
        assert background_versions <= new_versions
        assert updated['version_id'] not in new_versions  # Selection remains explicit, not latest-by-magic.
        assert new_versions != {r['version_id'] for r in old_snapshot['resources']}
        second = session.finish()
        assert second['state']['status'] == 'completed'
        evidence = session.evidence(second)
        assert {e['resource_version_id'] for e in evidence} == new_versions
        for item in evidence:
            original = body(session.client.get(session.base+'/resource-versions/'+item['resource_version_id']))
            # Both uploaded Markdown and saved text are normalized to the indexed text.
            start, end = item['locator']['char_start'], item['locator']['char_end']
            assert original['text'][start:end] == item['text']
        assert body(session.client.get(old_task+'/runtime'))['result'] == first['result']
        # Three original resources plus one new subject; background update is a version.
        listed = body(session.client.get(session.base+'/resources'))['items']
        assert len({item['resource']['resource_id'] for item in listed}) == 4
        assert len(listed) == 5  # The API lists immutable versions, including the update.
    finally:
        session.close()


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_unbound_background_and_foreign_resources_never_enter_citations(tmp_path, case):
    session = AcceptanceSession(tmp_path/'scope.sqlite3', case, task_budget=EXAMPLE_BUDGET)
    try:
        foreign_space = body(session.client.post('/v1/workspaces', json={'name':'Other synthetic workspace'}), 201)['workspace_id']
        for base in (session.base, '/v1/workspaces/'+foreign_space):
            body(session.client.post(base+'/resources', json={'display_name':'Unbound synthetic decoy',
                'text':case['query']+' UNBOUND_SYNTHETIC_SECRET'}), 201)
        session.prepare(missing=True)
        outcome = session.finish()
        assert outcome['state']['status'] == 'completed'
        assert 'UNBOUND_SYNTHETIC_SECRET' not in json.dumps(outcome)
        citations = session.evidence(outcome)
        assert len(citations) == 1
        assert citations[0]['resource_version_id'] == session.snapshot['resources'][0]['version_id']
        assert any('资料不足' in b['text'] for b in outcome['result']['blocks'])
    finally:
        session.close()


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
@pytest.mark.parametrize('fault', ['wrong-type', 'extra-property'])
def test_scenario_specific_schema_is_enforced_when_result_is_committed(tmp_path, case, fault):
    payload = copy.deepcopy(case['complete_payload'])
    if fault == 'extra-property':
        payload['unregistered_field'] = 'invalid'
    else:
        payload[next(key for key in payload if key != 'summary')] = 'not-an-array'
    class InvalidPayload(ScenarioFixtureModel):
        def complete(self, request):
            task = next(json.loads(m.content) for m in request.messages if m.role == 'user')
            result = AnalysisResult(task_id=task['task_id'], blocks=(ResultBlock(kind='limitation',text='Invalid fixture'),),
                scenario_payload_json=json.dumps(payload))
            return ModelReply(text=scripted_analysis_json(result), usage=ModelUsage(input_tokens=1, output_tokens=1))
    session = AcceptanceSession(tmp_path/'schema.sqlite3',case,task_budget=EXAMPLE_BUDGET,
        provider_factory=lambda: InvalidPayload(case))
    try:
        session.prepare()
        response = session.client.post(session.task_base+'/step',json={'expected_actions':0})
        assert response.status_code == 422 and response.json()['error'] == 'result_schema_mismatch'
        saved = body(session.client.get(session.task_base+'/runtime'))
        assert saved['state']['status'] == 'failed' and saved['result'] is None
    finally:
        session.close()


def test_default_budget_stops_multi_document_fixture_without_raising_defaults(tmp_path):
    session = AcceptanceSession(tmp_path/'limited.sqlite3', CASES[0])
    try:
        session.prepare()
        assert session.snapshot['budget'] == TaskBudget().model_dump(mode='json')
        stopped = session.finish()
        assert stopped['state']['status'] == 'failed' and stopped['result'] is None
        assert stopped['usage']['actions'] == 5 and stopped['usage']['model_calls'] == 3
        assert any(e['kind'] == 'admission_rejected' for e in stopped['events'])
    finally:
        session.close()


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_role_requirements_are_enforced_before_starting_a_model(tmp_path, case):
    session = AcceptanceSession(tmp_path/'roles.sqlite3',case,task_budget=EXAMPLE_BUDGET)
    try:
        session.prepare()
        required = {role.name for role in session.package.manifest.resource_roles if role.min_count}
        invalid = copy.deepcopy(session.task_input)
        invalid['bindings'] = [b for b in invalid['bindings'] if b['role'] not in required]
        response = session.client.post(session.task_route,json=invalid,headers={'Idempotency-Key':'missing-required'})
        assert response.status_code == 422 and response.json()['error'] == 'role_cardinality'
        invalid = copy.deepcopy(session.task_input)
        invalid['bindings'][0]['role'] = 'undeclared-role'
        response = session.client.post(session.task_route,json=invalid,headers={'Idempotency-Key':'unknown-role'})
        assert response.status_code == 422 and response.json()['error'] == 'unknown_role'
        assert body(session.client.get(session.task_base+'/runtime'))['usage'] is None
    finally:
        session.close()


@pytest.mark.parametrize('budget_field', ['budget', 'task_budget'])
def test_clients_cannot_set_task_budget_and_config_changes_do_not_rewrite_retries(tmp_path, budget_field):
    session = AcceptanceSession(tmp_path/'policy.sqlite3', CASES[0],task_budget=EXAMPLE_BUDGET)
    try:
        session.prepare()
        saved = session.snapshot
        forged = session.client.post(session.task_route,json={**session.task_input,budget_field:{'max_input_tokens':999999}},
            headers={'Idempotency-Key':'forged'})
        assert forged.status_code == 422
        session.task_budget = TaskBudget(max_input_tokens=64000)
        session.restart()
        retried = body(session.client.post(session.task_route,json=session.task_input,headers={'Idempotency-Key':'fixture-task'}),201)
        assert retried['snapshot'] == saved
        new = body(session.client.post(session.task_route,json=session.task_input,headers={'Idempotency-Key':'new-policy-task'}),201)
        assert new['snapshot']['budget']['max_input_tokens'] == 64000
        assert session.finish()['state']['status'] == 'completed'  # Original task keeps its frozen budget.
    finally:
        session.close()


def test_budget_configuration_is_validated_and_not_available_in_catalog_mode(tmp_path):
    registry = example_registry()
    with pytest.raises(ValueError):
        create_app(registry, api_token='x'*32, task_budget=EXAMPLE_BUDGET)
    with pytest.raises(ValidationError):
        create_app(registry, database=tmp_path/'not-created.sqlite3',task_budget={'max_input_tokens':-1})
    assert not (tmp_path/'not-created.sqlite3').exists()


def test_renamed_package_and_three_new_roles_run_without_scenario_name_branches(tmp_path):
    custom = tmp_path/'custom'
    shutil.copytree(HERE/'scenarios/equipment-choice',custom)
    manifest_path = custom/'scenario.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['id'] = 'portable-review'
    replacements = {role['name']:f'input-{index}' for index,role in enumerate(manifest['resource_roles'])}
    for role in manifest['resource_roles']: role['name'] = replacements[role['name']]
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
    case = copy.deepcopy(CASES[2])
    case.update(id='portable-review', package=str(custom))
    case['context_roles'] = [replacements[role] for role in case['context_roles']]
    for doc in case['documents']: doc['roles'] = [replacements[role] for role in doc['roles']]
    registry = example_registry()
    registry.register(load_scenario(custom))
    session = AcceptanceSession(tmp_path/'custom.sqlite3',case,task_budget=EXAMPLE_BUDGET,registry=registry)
    try:
        session.prepare()
        assert {b['role'] for b in session.snapshot['bindings']} == set(replacements.values())
        assert session.finish()['state']['status'] == 'completed'
    finally:
        session.close()
