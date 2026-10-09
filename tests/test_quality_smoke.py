"""Offline evaluation harness checks; no provider credentials or network."""
import json
from juli.model_demo import scripted_analysis_json
from pathlib import Path

import pytest

from examples.quality_eval.smoke import preflight, run_case, wait_for_answer
from juli.adapters.scripted_model import ScriptedModel
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.tools import ToolReference

USAGE = ModelUsage(input_tokens=20, output_tokens=20)


def final(request):
    task_id = json.loads(next(m.content for m in request.messages if m.role == 'user'))['task_id']
    result = AnalysisResult(task_id=task_id, blocks=(ResultBlock(kind='limitation', text='离线测试，不是实际分析'),),
                            scenario_payload_json='{"summary":"离线合同测试"}')
    return ModelReply(text=scripted_analysis_json(result), usage=USAGE)


def test_preflight_allows_only_approved_cases_and_input_fields():
    for case_id in ('career-01', 'career-02', 'tender-03'):
        case = preflight(case_id=case_id)
        assert set(case) == {'id', 'question', 'as_of_date', 'documents', 'scenario_path'}
        assert all(doc['path'].startswith('inputs/'+case_id+'/') for doc in case['documents'])
    with pytest.raises(ValueError, match='three approved'):
        preflight(case_id='course-01')


def test_harness_uploads_documents_and_completes_without_claiming_quality(tmp_path):
    path = tmp_path/'trial'
    model = ScriptedModel([final])
    outcome, error = run_case(preflight(), path, model)
    assert error is None and outcome['state']['status'] == 'completed'
    record = json.loads((path/'run.json').read_text(encoding='utf-8'))
    assert all(d['parse']['state'] == 'ready' for d in record['documents'])
    assert all(d['display_name'].endswith('.md') for d in record['documents'])
    assert record['mode'] == 'offline_harness_test' and record['quality_verdict'] is None
    assert json.loads((path/'outcome.json').read_text(encoding='utf-8'))['quality_verdict'] is None
    with pytest.raises(FileExistsError):
        run_case(preflight(), path, model)


def test_clarification_requires_actual_question_and_records_operator_answer(tmp_path):
    model = ScriptedModel([ModelReply(text='{"kind":"clarification","question":"能否每周到岗？"}', usage=USAGE), final])
    def answer(view, directory, number):
        assert view['state']['pending_question'] == '能否每周到岗？' and number == 1
        return '这是离线操作者回答'
    outcome, error = run_case(preflight(case_id='career-02'), tmp_path/'trial', model, answer_callback=answer)
    assert error is None and outcome['state']['status'] == 'completed'
    assert (tmp_path/'trial/answer-001-submitted.json').exists()


def test_unanswered_case_stays_pending_and_does_not_invent_an_answer(tmp_path):
    model = ScriptedModel([ModelReply(text='{"kind":"clarification","question":"时间是否合适？"}', usage=USAGE)])
    outcome, error = run_case(preflight(case_id='career-02'), tmp_path/'trial', model)
    assert error is None and outcome['state']['status'] == 'pending' and model.calls == 1
    assert not list((tmp_path/'trial').glob('answer-*-submitted.json'))


def test_invalid_model_output_stops_without_implicit_retry(tmp_path):
    model = ScriptedModel([ModelReply(text='not json', usage=USAGE), final])
    outcome, error = run_case(preflight(), tmp_path/'trial', model)
    assert error and outcome['state']['status'] == 'failed' and model.calls == 1


def test_real_gateway_search_and_read_are_recorded(tmp_path):
    search = ModelReply(tool_call=ModelToolCall(call_id='eval-search',
        tool=ToolReference(name='evidence.search', version='1.0.0'), arguments_json='{"query":"Python"}'), usage=USAGE)
    def read(request):
        item = json.loads(request.messages[-1].content)['items'][0]
        return ModelReply(tool_call=ModelToolCall(call_id='eval-read',
            tool=ToolReference(name='evidence.read', version='1.0.0'),
            arguments_json=json.dumps({'evidence_id': item['evidence_id']})), usage=USAGE)
    outcome, error = run_case(preflight(), tmp_path/'trial', ScriptedModel([search, read, final]))
    assert error is None and outcome['state']['status'] == 'completed'
    assert len(list((tmp_path/'trial').glob('tool-*-response.json'))) == 2


def test_wait_accepts_only_operator_text_file(tmp_path):
    (tmp_path/'answer-001.json').write_text('{"text":"合成回答"}', encoding='utf-8')
    assert wait_for_answer({'state': {'status': 'pending'}}, tmp_path, 1, timeout=1) == '合成回答'
    (tmp_path/'answer-002.json').write_text('{"gold":"不应上传"}', encoding='utf-8')
    with pytest.raises(ValueError, match='only text'):
        wait_for_answer({'state': {'status': 'pending'}}, tmp_path, 2, timeout=1)
