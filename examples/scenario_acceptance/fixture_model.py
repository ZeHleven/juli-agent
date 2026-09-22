"""A transcript-driven fixture. Scenario-specific content lives only in cases.json."""
import json

from juli.core.common import canonical_json, digest
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.tools import ToolReference


class ScenarioFixtureModel:
    external_egress = False
    context_window = 1000000

    def __init__(self, case):
        self.case = case
        self.runtime_identity = 'scenario-fixture:' + digest(canonical_json(case).encode())

    def complete(self, request):
        users = [json.loads(message.content) for message in request.messages if message.role == 'user']
        task = next(item for item in users if 'task_id' in item)
        usage = ModelUsage(input_tokens=100, output_tokens=100)
        if not any('clarification_answer' in item for item in users):
            return ModelReply(text=canonical_json({'kind': 'clarification', 'question': self.case['clarification']}), usage=usage)
        outputs = [json.loads(message.content) for message in request.messages if message.role == 'tool']

        def call(name, arguments, number):
            return ModelReply(tool_call=ModelToolCall(call_id=f'fixture:{number}',
                tool=ToolReference(name=name, version='1.0.0'), arguments_json=canonical_json(arguments)), usage=usage)

        if not outputs:
            return call('evidence.search', {'query': self.case['query']}, 0)
        candidates = outputs[0]['items']
        read = [item for output in outputs[1:] for item in output['items']]
        seen = {item['evidence_id'] for item in read}
        unread = next((item for item in candidates if item['evidence_id'] not in seen), None)
        if unread:
            return call('evidence.read', {'evidence_id': unread['evidence_id']}, len(outputs))

        present_roles = {role for item in task['bound_resource_catalog'] for role in item['roles']}
        missing = not set(self.case['context_roles']) <= present_roles
        payload = self.case['missing_payload'] if missing else self.case['complete_payload']
        blocks = [ResultBlock(kind='limitation', text='离线脚本流程验收；下面只引用合成原文，结构结论为预设值，不是 AI 分析。')]
        blocks.extend(ResultBlock(kind='fact', text=item['text'], evidence_ids=(item['evidence_id'],)) for item in read)
        if missing:
            blocks.append(ResultBlock(kind='limitation', text='背景资料未绑定，资料不足；未提供证明不等于不具备能力，不据此作适配判断。'))
        result = AnalysisResult(task_id=task['task_id'], blocks=tuple(blocks), scenario_payload_json=canonical_json(payload))
        return ModelReply(text=result.model_dump_json(), usage=usage)
