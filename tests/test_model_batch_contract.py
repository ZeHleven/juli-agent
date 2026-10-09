"""Provider-independent preservation and bounds for one assistant tool batch."""
import pytest
from pydantic import ValidationError

from juli.core.models import MAX_MODEL_TOOL_CALLS, ModelMessage, ModelReply, ModelToolCall, ModelUsage
from juli.core.tools import ToolReference


USAGE = ModelUsage(input_tokens=10, output_tokens=5)


def call(index=1):
    return ModelToolCall(call_id=f'batch:{index}', tool=ToolReference(name='evidence.search', version='1.0.0'),
                         arguments_json='{"query":"synthetic"}')


@pytest.mark.parametrize('count', [1, 2, MAX_MODEL_TOOL_CALLS])
def test_batch_round_trip_preserves_explanation_order_and_ids(count):
    calls = tuple(call(i) for i in range(count))
    explanation = '  分别查证两份材料。\n'
    reply = ModelReply(text=explanation, tool_calls=calls, usage=USAGE)
    message = ModelMessage(role='assistant', content=explanation, tool_calls=calls)
    assert ModelReply.model_validate_json(reply.model_dump_json()) == reply
    assert ModelMessage.model_validate_json(message.model_dump_json()) == message
    assert reply.calls == message.calls == calls
    assert reply.text == message.content == explanation
    assert reply.tool_call is None and message.tool_call is None


def test_legacy_single_call_remains_readable_with_optional_explanation():
    one = call()
    for explanation in (None, 'First read the material.'):
        reply = ModelReply(tool_call=one, text=explanation, usage=USAGE)
        message = ModelMessage(role='assistant', tool_call=one, content=explanation or '')
        assert reply.calls == message.calls == (one,)
        assert reply.tool_calls == message.tool_calls == ()
        assert reply.tool_call == message.tool_call == one


@pytest.mark.parametrize('contract', ['reply', 'message'])
@pytest.mark.parametrize('malformation', ['both_fields', 'duplicate_id', 'oversized'])
def test_ambiguous_or_unbounded_batches_are_rejected(contract, malformation):
    fields = {'tool_calls': (call(1), call(2))}
    if malformation == 'both_fields': fields['tool_call'] = call(3)
    elif malformation == 'duplicate_id': fields['tool_calls'] = (call(1), call(1))
    elif malformation == 'oversized': fields['tool_calls'] = tuple(call(i) for i in range(MAX_MODEL_TOOL_CALLS + 1))
    with pytest.raises(ValidationError):
        if contract == 'reply': ModelReply(usage=USAGE, **fields)
        else: ModelMessage(role='assistant', **fields)


@pytest.mark.parametrize('role', ['system', 'user', 'tool'])
@pytest.mark.parametrize('field', ['tool_call', 'tool_calls'])
def test_non_assistant_messages_cannot_request_tools(role, field):
    fields = {field: call() if field == 'tool_call' else (call(),)}
    if role == 'tool': fields['tool_call_id'] = 'result:1'
    with pytest.raises(ValidationError):
        ModelMessage(role=role, content='content', **fields)


@pytest.mark.parametrize('role', ['system', 'user', 'assistant'])
def test_only_tool_result_messages_may_have_a_tool_call_id(role):
    with pytest.raises(ValidationError):
        ModelMessage(role=role, content='content', tool_call_id='result:1')


def test_tool_result_requires_its_original_call_id():
    with pytest.raises(ValidationError):
        ModelMessage(role='tool', content='{}')
    assert ModelMessage(role='tool', content='{}', tool_call_id='batch:1').calls == ()


@pytest.mark.parametrize('text', [None, '', ' \n\t'])
def test_reply_without_tools_requires_nonempty_final_text(text):
    with pytest.raises(ValidationError):
        ModelReply(text=text, usage=USAGE)


def test_tool_only_reply_and_final_only_reply_remain_valid():
    assert ModelReply(tool_calls=(call(), call(2)), usage=USAGE).text is None
    final = ModelReply(text='{"summary":"synthetic"}', usage=USAGE)
    assert final.calls == ()
    with pytest.raises(ValidationError):
        ModelMessage(role='assistant')
