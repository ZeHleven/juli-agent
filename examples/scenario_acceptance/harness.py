"""Trusted fixture setup; all user workflows exercise the public HTTP endpoints."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from juli.adapters.scenario_files import load_scenario
from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.api import create_app
from juli.bootstrap import load_registry
from juli.core.common import canonical_json, digest
from juli.core.tasks import TaskBudget
from juli.runtime_api import RuntimeConfig

from .fixture_model import ScenarioFixtureModel

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EXAMPLE_BUDGET = TaskBudget(max_input_tokens=128000)


def cases():
    return json.loads((HERE/'cases.json').read_text(encoding='utf-8'))


def example_registry():
    registry = load_registry(ROOT/'scenarios')
    registry.register(load_scenario(HERE/'scenarios/equipment-choice'))
    return registry


def body(response, status=200):
    if response.status_code != status:
        raise AssertionError(f'Unexpected HTTP status {response.status_code}: {response.text}')
    return response.json()


class AcceptanceSession:
    def __init__(self, database, case, *, task_budget=None, provider_factory=None, registry=None):
        self.database = Path(database)
        migrate(self.database)  # Explicit, newly supplied synthetic database only.
        owner = SQLiteStore(self.database).provision_principal('Synthetic scenario owner')
        self.token = owner.token
        self.case = case
        self.registry = registry or example_registry()
        self.package = load_scenario(ROOT/case['package'])
        self.task_budget = task_budget
        self.config = RuntimeConfig(provider_factory or (lambda: ScenarioFixtureModel(case)),
            'm08-fixture:' + digest(canonical_json(case).encode()), 'demo', scenario_ids=(case['id'],))
        self.client = self.connect()
        self.space = body(self.client.post('/v1/workspaces', json={'name': 'Synthetic scenario workspace'}), 201)['workspace_id']
        self.base = '/v1/workspaces/' + self.space
        # Save background first, once. Later tasks bind the existing version IDs.
        self.documents = {}
        for doc in sorted(case['documents'], key=lambda doc: not doc['background']):
            self.documents[doc['file']] = self.upload(doc)

    def connect(self):
        options = {'task_budget': self.task_budget} if self.task_budget is not None else {}
        app = create_app(self.registry, database=self.database, runtime=self.config, **options)
        client = TestClient(app)
        client.headers['Authorization'] = 'Bearer ' + self.token
        return client

    def restart(self):
        self.client.close()
        self.client = self.connect()

    def close(self):
        self.client.close()

    def upload(self, document, *, suffix=''):
        text = (HERE/'data'/document['file']).read_text(encoding='utf-8') + suffix
        result = body(self.client.post(self.base+'/files', params={'filename': Path(document['file']).name},
            content=text.encode('utf-8'), headers={'Content-Type': 'application/octet-stream'}), 201)
        assert result['resource']['status'] == 'ready'
        return result['resource']

    def prepare(self, *, missing=False, new_subject=False):
        selected = [doc for doc in self.case['documents'] if not (missing and doc['background'])]
        bindings = []
        for doc in selected:
            resource = self.documents[doc['file']]
            if new_subject and not doc['background']:
                resource = self.upload(doc, suffix='\n这是第二次任务的新合成文件。\n')
            bindings.extend({'role': role, 'version_id': resource['version_id']} for role in doc['roles'])
        conversation = body(self.client.post(self.base+'/conversations'), 201)['conversation_id']
        self.task_route = self.base+'/conversations/'+conversation+'/tasks'
        self.task_input = {'question': self.case['question'], 'scenario_id': self.package.manifest.id,
            'scenario_version': self.package.manifest.version, 'bindings': bindings}
        created = body(self.client.post(self.task_route, json=self.task_input, headers={'Idempotency-Key':'fixture-task'}), 201)
        self.snapshot = created['snapshot']
        self.task_base = self.base+'/tasks/'+self.snapshot['task_id']
        return created

    def step(self):
        before = body(self.client.get(self.task_base+'/runtime'))
        actions = (before['usage'] or {}).get('actions', 0)
        return body(self.client.post(self.task_base+'/step', json={'expected_actions': actions}))

    def finish(self):
        pending = self.step()
        assert pending['state']['status'] == 'pending'
        self.restart()  # Rebuild the app/provider; state and context live in the database.
        resumed = body(self.client.get(self.task_base+'/runtime'))
        assert resumed['state'] == pending['state']
        body(self.client.post(self.task_base+'/answer', json={'expected_version': pending['state']['version'],
            'text': self.case['answer']}, headers={'Idempotency-Key': 'fixture-answer'}))
        for _ in range(24):
            outcome = self.step()
            if outcome['state']['status'] != 'running':
                return outcome
        raise AssertionError('The bounded fixture exceeded its action allowance.')

    def evidence(self, outcome):
        ids = {ref for block in outcome['result']['blocks'] for ref in block['evidence_ids']}
        return [body(self.client.post(self.task_base+'/evidence/read', json={'evidence_id': ref}))['items'][0]
            for ref in sorted(ids)]


def run_case(database, case, *, missing=False, task_budget=EXAMPLE_BUDGET):
    session = AcceptanceSession(database, case, task_budget=task_budget)
    try:
        session.prepare(missing=missing)
        outcome = session.finish()
        assert outcome['state']['status'] == 'completed', outcome['state']
        evidence = session.evidence(outcome)
        expected = {binding['version_id'] for binding in session.snapshot['bindings']}
        assert {item['resource_version_id'] for item in evidence} == expected
        payload = json.loads(outcome['result']['scenario_payload_json'])
        session.package.validate_result(payload)
        assert payload == case['missing_payload' if missing else 'complete_payload']
        return {'scenario': case['id'], 'mode': 'missing_background' if missing else 'complete',
            'task_status': outcome['state']['status'], 'bound_resources': len(expected),
            'cited_resources': len({item['resource_version_id'] for item in evidence}),
            'clarification_resumed_after_app_rebuild': True,
            'actions': outcome['usage']['actions'], 'scripted_model_calls': outcome['usage']['model_calls'],
            'input_units': outcome['usage']['input_units'], 'payload': payload}
    finally:
        session.close()
