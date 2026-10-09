"""Opt-in transcript-driven UI fixture, not a reasoning model. No network or credentials."""
import json
from juli.core.common import canonical_json
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.tools import ToolReference
from juli.model_demo import scripted_analysis_json


class WorkbenchDemoModel:
    external_egress = False
    context_window = 1000000
    runtime_identity = 'workbench-fixture-v1'

    def complete(self, request):
        task = next(json.loads(m.content) for m in request.messages if m.role == 'user' and 'task_id' in json.loads(m.content))
        outputs = [json.loads(m.content) for m in request.messages if m.role == 'tool']
        answered = any(m.role == 'user' and 'clarification_answer' in json.loads(m.content) for m in request.messages)
        usage = ModelUsage(input_tokens=100, output_tokens=100)
        if not answered:
            return ModelReply(text=canonical_json({'kind':'clarification','question':'这是离线流程演示。请输入一个资料中的关键词，用来演示检索与引用（不进行真实 AI 分析）。'}), usage=usage)
        if not outputs:
            answer = next(json.loads(m.content)['clarification_answer'] for m in reversed(request.messages) if m.role == 'user' and 'clarification_answer' in json.loads(m.content))
            call = ModelToolCall(call_id='demo:search',tool=ToolReference(name='evidence.search',version='1.0.0'),arguments_json=canonical_json({'query':answer[:4000]}))
            return ModelReply(tool_call=call,usage=usage)
        if len(outputs) == 1 and outputs[0]['items']:
            call = ModelToolCall(call_id='demo:read',tool=ToolReference(name='evidence.read',version='1.0.0'),arguments_json=canonical_json({'evidence_id':outputs[0]['items'][0]['evidence_id']}))
            return ModelReply(tool_call=call,usage=usage)
        blocks = [ResultBlock(kind='limitation',text='离线演示已完成。这是预设流程，不代表对资料的真实 AI 分析或判断。')]
        if len(outputs) > 1:
            evidence = outputs[-1]['items'][0]
            blocks.append(ResultBlock(kind='fact',text=evidence['text'],evidence_ids=(evidence['evidence_id'],)))
        else:
            blocks.append(ResultBlock(kind='limitation',text='未找到匹配关键词的资料片段，没有生成引用。'))
        result = AnalysisResult(task_id=task['task_id'],blocks=tuple(blocks),scenario_payload_json=canonical_json({'summary':'仅演示工作台与持久运行流程。'}))
        return ModelReply(text=scripted_analysis_json(result),usage=usage)


def demo_config():
    from juli.runtime_api import RuntimeConfig
    return RuntimeConfig(WorkbenchDemoModel,'workbench-offline-v1','demo',scenario_ids=('minimal',))
