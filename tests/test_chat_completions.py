import json

import httpx
import pytest

from juli.adapters.chat_completions import ChatCompletionsProvider, ChatModelConfig, tool_alias
from juli.application.model_session import SessionPolicy
from juli.bootstrap import create_model_session, tool_contracts
from juli.core.common import DomainError
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelMessage, ModelRequest, ModelToolCall
from juli.core.tools import ToolReference

# Reuse only synthetic fixture creation; no application credentials or external services.
from test_model_session import model_task

ENDPOINT = 'https://model.example.invalid/v1/chat/completions'
SYNTHETIC_KEY = 'synthetic-test-credential-not-real'


def request():
    return ModelRequest(messages=(ModelMessage(role='user', content='Synthetic input'),), tools=tool_contracts().list(), max_output_tokens=100)


def response(message=None, reason='stop', usage=None):
    return {'choices': [{'finish_reason': reason, 'message': message or {'role':'assistant','content':'{"synthetic":true}'}}],
            'usage': usage or {'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}


def provider(handler, *, approved=True):
    config = ChatModelConfig(endpoint=ENDPOINT, model='synthetic-model', context_window=64000)
    return ChatCompletionsProvider(config, api_key=SYNTHETIC_KEY, approved_endpoint=ENDPOINT if approved else None, transport=httpx.MockTransport(handler))


def test_wire_mapping_limits_credentials_and_usage():
    seen = []
    def handler(wire):
        seen.append(wire)
        body = json.loads(wire.content)
        assert body['model'] == 'synthetic-model'
        assert body['max_completion_tokens'] == 100
        assert body['parallel_tool_calls'] is False and body['stream'] is False and body['store'] is False
        assert body['n'] == 1
        assert all('.' not in t['function']['name'] and len(t['function']['name']) <= 64 for t in body['tools'])
        assert SYNTHETIC_KEY.encode() not in wire.content
        assert wire.headers['authorization'] == 'Bearer ' + SYNTHETIC_KEY
        return httpx.Response(200, json=response())
    adapter = provider(handler)
    result = adapter.complete(request())
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 5
    assert len(seen) == 1 and SYNTHETIC_KEY not in repr(adapter)


def test_tool_response_maps_alias_back_to_versioned_juli_tool():
    tool = ToolReference(name='evidence.search', version='1.0.0')
    message = {'role':'assistant','content':None,'tool_calls':[{'id':'call:1','type':'function','function':{'name':tool_alias(tool),'arguments':'{"query":"Python"}'}}]}
    result = provider(lambda _: httpx.Response(200,json=response(message,'tool_calls'))).complete(request())
    assert result.tool_call.tool == tool
    assert result.tool_call.call_id == 'call:1'


def wire_tool(call_id, *, arguments='{"query":"Python"}', name=None, kind='function'):
    reference = ToolReference(name='evidence.search', version='1.0.0')
    return {'id': call_id, 'type': kind, 'function': {'name': name or tool_alias(reference), 'arguments': arguments}}


@pytest.mark.parametrize('count', [1, 2, 8])
def test_tool_batch_and_explanation_are_preserved_as_one_reply_and_message(count):
    explanation = '  先分别检索岗位要求与已有能力。\n保留原始说明。  '
    calls = [wire_tool(f'batch:{i}', arguments=json.dumps({'query': f'query-{i}'})) for i in range(count)]
    message = {'role': 'assistant', 'content': explanation, 'tool_calls': calls}
    reply = provider(lambda _: httpx.Response(200, json=response(message, 'tool_calls'))).complete(request())
    assert reply.text == explanation
    assert [call.call_id for call in reply.calls] == [item['id'] for item in calls]
    assert [call.arguments_json for call in reply.calls] == [item['function']['arguments'] for item in calls]
    assert (reply.tool_call is not None) == (count == 1)
    history = ModelMessage(role='assistant', content=reply.text, tool_call=reply.tool_call, tool_calls=reply.tool_calls)
    model_request = ModelRequest(messages=(ModelMessage(role='user', content='Question'), history,
                                          *(ModelMessage(role='tool', tool_call_id=call.call_id, content='{"items":[]}') for call in reply.calls)),
                                 tools=request().tools, max_output_tokens=100)
    captured = []
    def handler(wire):
        captured.append(json.loads(wire.content))
        return httpx.Response(200, json=response())
    provider(handler).complete(model_request)
    messages = captured[0]['messages']
    assert len(messages) == 2 + count
    assert messages[1] == message
    assert [item['tool_call_id'] for item in messages[2:]] == [item['id'] for item in calls]


@pytest.mark.parametrize('malformation', [
    'duplicate_id', 'too_many', 'late_unknown_tool', 'late_non_function', 'late_bad_json',
    'late_array_arguments', 'late_nonfinite_arguments', 'late_nonstring_arguments',
    'late_invalid_id', 'nontext_explanation', 'truncated', 'refusal', 'usage_missing',
    'calls_not_array', 'calls_false',
])
def test_entire_batch_is_rejected_if_any_call_or_envelope_is_invalid(malformation):
    calls = [wire_tool('batch:1'), wire_tool('batch:2')]
    message = {'role': 'assistant', 'content': 'Both searches are needed.', 'tool_calls': calls}
    body = response(message, 'tool_calls')
    if malformation == 'duplicate_id': calls[1]['id'] = calls[0]['id']
    elif malformation == 'too_many': message['tool_calls'] = [wire_tool(f'batch:{i}') for i in range(9)]
    elif malformation == 'late_unknown_tool': calls[1]['function']['name'] = 'unregistered'
    elif malformation == 'late_non_function': calls[1]['type'] = 'custom'
    elif malformation == 'late_bad_json': calls[1]['function']['arguments'] = '{bad JSON'
    elif malformation == 'late_array_arguments': calls[1]['function']['arguments'] = '[]'
    elif malformation == 'late_nonfinite_arguments': calls[1]['function']['arguments'] = '{"number":NaN}'
    elif malformation == 'late_nonstring_arguments': calls[1]['function']['arguments'] = {'query': 'Python'}
    elif malformation == 'late_invalid_id': calls[1]['id'] = 'invalid id with spaces'
    elif malformation == 'nontext_explanation': message['content'] = [{'type': 'text', 'text': 'unsupported'}]
    elif malformation == 'truncated': body['choices'][0]['finish_reason'] = 'length'
    elif malformation == 'refusal': message['refusal'] = 'Refused'
    elif malformation == 'usage_missing': del body['usage']
    elif malformation == 'calls_not_array': message['tool_calls'] = {'id': 'batch:1'}
    elif malformation == 'calls_false': message['tool_calls'] = False
    attempts = []
    def handler(wire):
        attempts.append(wire)
        return httpx.Response(200, json=body)
    with pytest.raises(DomainError) as error:
        provider(handler).complete(request())
    assert error.value.code == 'invalid_model_response'
    assert len(attempts) == 1


def test_unavailable_tool_late_in_history_batch_fails_before_transport():
    calls = (
        ModelToolCall(call_id='history:1', tool=ToolReference(name='evidence.search', version='1.0.0'), arguments_json='{}'),
        ModelToolCall(call_id='history:2', tool=ToolReference(name='unavailable.tool', version='1.0.0'), arguments_json='{}'),
    )
    model_request = ModelRequest(messages=(ModelMessage(role='assistant', content='Both searches', tool_calls=calls),),
                                 tools=request().tools, max_output_tokens=100)
    def forbidden(_):
        raise AssertionError('Invalid history must not trigger transport')
    with pytest.raises(DomainError) as error:
        provider(forbidden).complete(model_request)
    assert error.value.code == 'tool_not_granted'


def test_assistant_tool_and_tool_result_history_have_native_roles():
    call = ModelToolCall(call_id='call:history',tool=ToolReference(name='evidence.search',version='1.0.0'),arguments_json='{"query":"Python"}')
    model_request = ModelRequest(messages=(ModelMessage(role='user',content='Question'),ModelMessage(role='assistant',tool_call=call),ModelMessage(role='tool',tool_call_id=call.call_id,content='{"items":[]}')), tools=tool_contracts().list(),max_output_tokens=100)
    def handler(wire):
        messages = json.loads(wire.content)['messages']
        assert messages[1]['tool_calls'][0]['function']['name'] == tool_alias(call.tool)
        assert messages[2]['role'] == 'tool' and messages[2]['tool_call_id'] == call.call_id
        return httpx.Response(200,json=response())
    provider(handler).complete(model_request)


@pytest.mark.parametrize('status', [302,401,429,500])
def test_http_failures_and_redirects_are_not_retried_or_echoed(status):
    attempts = []
    def handler(wire):
        attempts.append(wire)
        return httpx.Response(status,text='Do not echo ' + SYNTHETIC_KEY,headers={'Location':'https://other.example.invalid/'})
    with pytest.raises(DomainError) as error: provider(handler).complete(request())
    assert error.value.code == 'model_http_error'
    assert SYNTHETIC_KEY not in str(error.value) and len(attempts) == 1


def test_transport_timeout_is_unknown_and_not_retried():
    attempts = []
    def handler(wire):
        attempts.append(wire)
        raise httpx.ReadTimeout('Do not echo ' + SYNTHETIC_KEY, request=wire)
    with pytest.raises(DomainError) as error: provider(handler).complete(request())
    assert error.value.code == 'model_timeout' and SYNTHETIC_KEY not in str(error.value)
    assert len(attempts) == 1


@pytest.mark.parametrize('malformation', ['usage_missing','usage_boolean','usage_inconsistent','length','refusal','unknown_tool','bad_json'])
def test_malformed_or_incomplete_responses_are_rejected(malformation):
    body = response()
    if malformation == 'usage_missing': del body['usage']
    elif malformation == 'usage_boolean': body['usage']['prompt_tokens'] = True
    elif malformation == 'usage_inconsistent': body['usage']['total_tokens'] = 99
    elif malformation == 'length': body['choices'][0]['finish_reason'] = 'length'
    elif malformation == 'refusal': body['choices'][0]['message']['refusal'] = 'Refused'
    elif malformation == 'unknown_tool':
        call = {'id':'a','type':'function','function':{'name':'unregistered','arguments':'{}'}}
        body['choices'][0] = {'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[call]}}
    def handler(_):
        return httpx.Response(200, content=b'not json' if malformation == 'bad_json' else json.dumps(body).encode())
    with pytest.raises(DomainError) as error: provider(handler).complete(request())
    assert error.value.code == 'invalid_model_response'


def test_response_size_limit_and_explicit_egress_approval():
    with pytest.raises(DomainError) as error:
        provider(lambda _:httpx.Response(200,content=b'x' * (1024 * 1024 + 1))).complete(request())
    assert error.value.code == 'model_response_too_large'
    def forbidden(_): raise AssertionError('No approval, no transport')
    with pytest.raises(DomainError) as error: provider(forbidden,approved=False).complete(request())
    assert error.value.code == 'egress_denied'


@pytest.mark.parametrize('endpoint', ['http://model.example.invalid/v1/chat/completions', 'https://user:pass@model.example.invalid/v1/chat/completions', ENDPOINT+'?key=secret', ENDPOINT+'#fragment', 'https://model.example.invalid:8443/v1/chat/completions'])
def test_only_explicit_https_destination_configuration_is_accepted(endpoint):
    with pytest.raises(DomainError): ChatModelConfig(endpoint=endpoint,model='synthetic',context_window=64000)


def test_wire_adapter_and_gateway_complete_synthetic_retrieval_cycle(model_task):
    _, store, owner, space, _, task = model_task
    calls = []
    def handler(wire):
        body = json.loads(wire.content)
        calls.append(body)
        if len(calls) == 1:
            reference, args = ToolReference(name='evidence.search',version='1.0.0'), {'query':'Python'}
        elif len(calls) == 2:
            candidate = json.loads(body['messages'][-1]['content'])['items'][0]
            reference, args = ToolReference(name='evidence.read',version='1.0.0'), {'evidence_id':candidate['evidence_id']}
        else:
            evidence = json.loads(body['messages'][-1]['content'])['items'][0]
            result = AnalysisResult(task_id=task,blocks=(ResultBlock(kind='fact',text='Synthetic evidence-backed statement.',evidence_ids=(evidence['evidence_id'],)),),scenario_payload_json='{"summary":"Synthetic"}')
            return httpx.Response(200,json=response({'role':'assistant','content':result.model_dump_json()}))
        tool = {'id':'wire:'+str(len(calls)),'type':'function','function':{'name':tool_alias(reference),'arguments':json.dumps(args)}}
        return httpx.Response(200,json=response({'role':'assistant','content':None,'tool_calls':[tool]},'tool_calls'))
    adapter = provider(handler)
    session = create_model_session(store,owner.token,space,task,adapter,policy=SessionPolicy(allow_external_egress=True))
    for _ in range(2):
        pending = session.advance()
        session.execute_pending(pending['call']['call_id'])
    assert session.advance()['kind'] == 'result_validated'
    assert len(calls) == 3
    assert all(owner.token not in json.dumps(c) and SYNTHETIC_KEY not in json.dumps(c) for c in calls)
