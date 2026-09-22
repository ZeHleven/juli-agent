"""New synthetic fixtures; no imports from the original quotation project."""
from pathlib import Path
from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.adapters.workbench_demo import WorkbenchDemoModel
from juli.bootstrap import create_durable_runner, load_registry
from juli.core.resources import ResourceBinding

ROOT = Path(__file__).resolve().parents[2]


class CountingModel(WorkbenchDemoModel):
    runtime_identity = 'm07-synthetic-v1'

    def __init__(self, before=None):
        self.calls = 0
        self.before = before
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request.model_dump(mode='json'))
        if self.before:
            self.before()
        return super().complete(request)


def setup(path):
    migrate(path)
    store = SQLiteStore(path)
    owner = store.provision_principal('Synthetic M07 owner')
    space = store.create_workspace(owner.token, 'Synthetic M07').workspace_id
    resource = store.save_text(owner.token, space, display_name='Synthetic background', text='Synthetic Python development experience.')
    conversation = store.create_conversation(owner.token, space)
    package = load_registry(ROOT/'scenarios').get('minimal', '0.1.0')
    task = store.create_task(owner.token, space, conversation.conversation_id,
        question='Find synthetic Python experience.', package=package,
        bindings=(ResourceBinding(role='subject', version_id=resource.version_id),), request_key='m07')['snapshot']['task_id']
    return {'path':str(path), 'token':owner.token, 'token_id':owner.token_id, 'space':space,
            'task':task, 'resource':resource.resource_id, 'version':resource.version_id}


def make_runner(config, *, provider=None, policy=None, clock=None):
    store = SQLiteStore(Path(config['path']), **({'clock':clock} if clock else {}))
    return create_durable_runner(store, config['token'], config['space'], config['task'],
        provider or CountingModel(), profile='m07-controlled-comparison', policy=policy)


def comparable(outcome):
    return {'state':outcome['state'], 'result':outcome['result'],
            'usage':{k:v for k,v in outcome['usage'].items() if k != 'active_seconds'},
            'events':[e['kind'] for e in outcome['events']]}
