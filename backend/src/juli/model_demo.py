"""An offline, scripted integration proof using a disposable synthetic database."""
import json
from pathlib import Path
import tempfile

from juli.adapters.scripted_model import ScriptedModel
from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.bootstrap import create_model_session
from juli.core.common import canonical_json
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.resources import ResourceBinding
from juli.core.tools import ToolReference


def scripted_analysis_json(result: AnalysisResult, *, critical_question=None) -> str:
    """Construct an explicit synthetic v3 decision; never process provider output."""
    return canonical_json({'kind': 'analysis', 'task_id': result.task_id,
        'blocks': [block.model_dump(mode='json') for block in result.blocks],
        'scenario_payload': json.loads(result.scenario_payload_json),
        'critical_question': critical_question})


def scripted_retrieval_steps(task_id: str):
    usage = ModelUsage(input_tokens=100, output_tokens=80)
    search = ModelReply(tool_call=ModelToolCall(call_id='demo:search', tool=ToolReference(name='evidence.search', version='1.0.0'), arguments_json='{"query":"Python"}'), usage=usage)

    def read(request):
        candidate = json.loads(request.messages[-1].content)['items'][0]
        return ModelReply(tool_call=ModelToolCall(call_id='demo:read', tool=ToolReference(name='evidence.read', version='1.0.0'), arguments_json=canonical_json({'evidence_id': candidate['evidence_id']})), usage=usage)

    def answer(request):
        original = json.loads(request.messages[-1].content)['items'][0]
        result = AnalysisResult(task_id=task_id, blocks=(ResultBlock(kind='fact', text='合成资料记录了 Python 开发经验。', evidence_ids=(original['evidence_id'],)),), scenario_payload_json='{"summary":"仅验证连接和引用，不代表真实模型分析。"}')
        return ModelReply(text=scripted_analysis_json(result), usage=usage)
    return [search, read, answer]


def model_demo(registry):
    package = registry.get('minimal', '0.1.0')  # A demo fixture, never a core scenario branch.
    with tempfile.TemporaryDirectory(prefix='juli-model-demo-') as directory:
        path = Path(directory) / 'synthetic.sqlite3'
        migrate(path)
        store = SQLiteStore(path)
        identity = store.provision_principal('Synthetic demo')
        space = store.create_workspace(identity.token, 'Synthetic model wiring').workspace_id
        resource = store.save_text(identity.token, space, display_name='合成背景', text='  合成资料：具备 Python 开发经验。\n')
        conversation = store.create_conversation(identity.token, space)
        task = store.create_task(identity.token, space, conversation.conversation_id, question='合成资料记录了什么经验？', package=package, bindings=(ResourceBinding(role='subject', version_id=resource.version_id),), request_key='model-demo')['snapshot']['task_id']
        provider = ScriptedModel(scripted_retrieval_steps(task))
        session = create_model_session(store, identity.token, space, task, provider)
        for _ in range(2):
            proposal = session.advance()
            session.execute_pending(proposal['call']['call_id'])
        outcome = session.advance()
        return {'mode': 'offline_scripted_integration', 'analysis_executed': False, 'real_model_calls': 0,
                'network_calls': 0, 'scripted_model_calls': provider.calls,
                'result_contract_valid': outcome['kind'] == 'result_validated',
                'task_status': store.get_task(identity.token, space, task)['state']['status'],
                'usage': session.usage(), 'events': session.events}
