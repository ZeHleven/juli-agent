"""Offline durable lifecycle demonstration, using only disposable synthetic data."""
from pathlib import Path
import tempfile
from juli.adapters.scripted_model import ScriptedModel
from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.bootstrap import create_durable_runner
from juli.core.models import ModelReply, ModelUsage
from juli.core.resources import ResourceBinding
from juli.model_demo import scripted_retrieval_steps


def runtime_demo(registry):
    with tempfile.TemporaryDirectory(prefix='juli-runtime-demo-') as folder:
        path = Path(folder)/'synthetic.sqlite3'
        migrate(path)
        store = SQLiteStore(path)
        owner = store.provision_principal('Synthetic runtime owner')
        space = store.create_workspace(owner.token, 'Synthetic runtime').workspace_id
        resource = store.save_text(owner.token, space, display_name='Synthetic', text='Python synthetic experience.')
        conversation = store.create_conversation(owner.token, space)
        task = store.create_task(owner.token, space, conversation.conversation_id, question='Review synthetic experience', package=registry.get('minimal','0.1.0'), bindings=(ResourceBinding(role='subject',version_id=resource.version_id),),request_key='runtime-demo')['snapshot']['task_id']
        def resume(steps):
            return create_durable_runner(SQLiteStore(path), owner.token, space, task, ScriptedModel(steps), profile='offline-synthetic-v1')
        question = ModelReply(text='{"kind":"clarification","question":"Which experience should I review?"}',usage=ModelUsage(input_tokens=10,output_tokens=10))
        pending = resume([question]).run()
        resume([]).answer(expected_version=pending['state']['version'],request_key='synthetic-answer',text='Python')
        search, read, final = scripted_retrieval_steps(task)
        resume([search]).step()
        resume([]).step()
        resume([read]).step()
        resume([]).step()
        done = resume([final]).run()
        return {'mode':'offline_durable_scripted', 'real_model_calls':0, 'network_calls':0,
                'analysis_quality_evaluated':False, 'checkpoint_reloaded_between_actions':True,
                'clarification_status':pending['state']['status'], 'task_status':done['state']['status'],
                'usage':done['usage'], 'result_persisted':bool(resume([]).view()['result']),
                'event_kinds':[e['kind'] for e in done['events']]}
