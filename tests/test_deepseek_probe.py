"""Offline transport and durable-budget checks. Never reads credentials."""
import json
from decimal import Decimal
import subprocess
import sys

import httpx
import pytest

from examples.quality_eval.deepseek_probe import BatchBudget, DeepSeekProbeProvider, MAX_BODY
from juli.bootstrap import tool_contracts
from juli.core.common import DomainError
from juli.core.models import ModelMessage, ModelRequest
from juli.core.models import ModelReply, ModelUsage


SECRET = 'synthetic-evaluation-credential-not-real'


def test_five_yuan_batch_stops_before_a_reservation_exceeds_remaining_balance(tmp_path):
    budget = BatchBudget(tmp_path/'budget', max_cny='5')
    reply = ModelReply(text='Synthetic worst-case usage',
        usage=ModelUsage(input_tokens=64000, output_tokens=4096))
    for _ in range(31):
        budget.settle(budget.reserve(4096), reply, 4096)
    resumed = BatchBudget(tmp_path/'budget', max_cny='5')
    assert resumed.snapshot()['committed_cny'] == '4.983808'
    with pytest.raises(DomainError) as caught:
        resumed.reserve(4096)
    assert caught.value.code == 'probe_budget_exhausted'
    assert resumed.snapshot()['calls'] == 31


def request(**changes):
    fields = dict(messages=(ModelMessage(role='user', content='Synthetic evaluation input'),),
                  tools=tool_contracts().list(), max_output_tokens=100)
    return ModelRequest(**(fields | changes))


def body():
    return {'model': 'reported-synthetic-model', 'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': 'Synthetic answer'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}


def make_provider(tmp_path, handler, budget=None):
    return DeepSeekProbeProvider(api_key=SECRET, trace_directory=tmp_path / 'traces',
                                budget=budget or BatchBudget(tmp_path / 'budget'),
                                transport=httpx.MockTransport(handler))


def assert_no_secret(tmp_path):
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_payload_credentials_raw_body_and_settlement(tmp_path):
    expected = json.dumps(body()).encode()
    seen = []
    def handler(wire):
        seen.append(wire)
        payload = json.loads(wire.content)
        assert wire.headers['Authorization'] == 'Bearer ' + SECRET
        assert wire.headers['Accept-Encoding'] == 'identity'
        assert payload['model'] == 'deepseek-v4-flash'
        assert payload['max_tokens'] == 100 and payload['thinking'] == {'type': 'disabled'}
        assert payload['parallel_tool_calls'] is False and payload['tool_choice'] == 'auto'
        assert not {'n', 'store', 'max_completion_tokens'} & payload.keys()
        assert SECRET.encode() not in wire.content
        return httpx.Response(200, content=expected)
    adapter = make_provider(tmp_path, handler)
    assert adapter.complete(request()).text == 'Synthetic answer'
    assert len(seen) == 1
    trace = tmp_path / 'traces/call-0001'
    assert (trace / 'response.raw.json').read_bytes() == expected
    assert json.loads((trace / 'request.json').read_text())['max_output_tokens'] == 100
    assert json.loads((trace / 'reply.json').read_text())['usage']['input_tokens'] == 10
    snapshot = BatchBudget(tmp_path / 'budget').snapshot()
    assert snapshot['calls'] == 1 and snapshot['stopped'] is False
    assert snapshot['committed_cny'] == '0.000060' and snapshot['reserved_cny'] == '0.000000'
    assert_no_secret(tmp_path)


@pytest.mark.parametrize('status', [401, 429, 500])
def test_non_200_error_body_is_never_saved_or_retried(tmp_path, status):
    seen = []
    def handler(wire):
        seen.append(wire)
        return httpx.Response(status, text='private error text ' + SECRET)
    adapter = make_provider(tmp_path, handler)
    with pytest.raises(DomainError, match='HTTP'):
        adapter.complete(request())
    with pytest.raises(DomainError) as stopped:
        make_provider(tmp_path, handler).complete(request())
    assert stopped.value.code == 'probe_batch_stopped' and len(seen) == 1
    assert not list(tmp_path.rglob('response.raw.json'))
    assert all(b'private error text' not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())
    assert BatchBudget(tmp_path / 'budget').snapshot()['entries'][0]['status'] == 'unknown'
    assert_no_secret(tmp_path)


def test_parse_failure_preserves_bounded_raw_body_and_reservation(tmp_path):
    adapter = make_provider(tmp_path, lambda _: httpx.Response(200, content=b'not JSON'))
    with pytest.raises(DomainError) as error:
        adapter.complete(request())
    assert error.value.code == 'invalid_model_response'
    assert (tmp_path / 'traces/call-0001/response.raw.json').read_bytes() == b'not JSON'
    assert adapter.budget.snapshot()['reserved_cny'] == '0.128800'


def test_timeout_has_no_retry_and_retains_unknown_cost(tmp_path):
    seen = []
    def handler(wire):
        seen.append(wire)
        raise httpx.ReadTimeout(SECRET, request=wire)
    adapter = make_provider(tmp_path, handler)
    with pytest.raises(DomainError) as error:
        adapter.complete(request())
    assert error.value.code == 'model_timeout' and SECRET not in str(error.value)
    assert len(seen) == 1 and adapter.budget.snapshot()['stopped'] is True
    assert_no_secret(tmp_path)


def test_raw_secret_across_chunks_is_redacted_before_write(tmp_path):
    class Split(httpx.SyncByteStream):
        def __iter__(self):
            raw = json.dumps(body() | {'extra': SECRET}).encode()
            middle = raw.index(SECRET.encode()) + 8
            yield raw[:middle]
            yield raw[middle:]
    adapter = make_provider(tmp_path, lambda _: httpx.Response(200, stream=Split()))
    adapter.complete(request())
    assert b'[HIDDEN]' in (tmp_path / 'traces/call-0001/response.raw.json').read_bytes()
    assert_no_secret(tmp_path)


def test_credential_in_model_reply_is_not_returned(tmp_path):
    data = body()
    data['choices'][0]['message']['content'] = SECRET
    adapter = make_provider(tmp_path, lambda _: httpx.Response(200, json=data))
    with pytest.raises(DomainError) as error:
        adapter.complete(request())
    assert error.value.code == 'probe_secret_in_reply'
    assert not list(tmp_path.rglob('reply.json'))
    assert_no_secret(tmp_path)


def test_response_limit_does_not_read_ahead(tmp_path):
    chunks = []
    class Large(httpx.SyncByteStream):
        def __iter__(self):
            chunks.append(1)
            yield b'x' * (MAX_BODY + 1)
            chunks.append(2)
            raise AssertionError('Must not consume another chunk')
    adapter = make_provider(tmp_path, lambda _: httpx.Response(200, stream=Large()))
    with pytest.raises(DomainError) as error:
        adapter.complete(request())
    assert error.value.code == 'model_response_too_large' and chunks == [1]
    assert (tmp_path / 'traces/call-0001/response.raw.json').stat().st_size == MAX_BODY
    assert json.loads((tmp_path / 'traces/call-0001/failure.json').read_text())['raw_body_truncated'] is True


def test_budget_reopen_does_not_reset_pending_and_settings_cannot_change(tmp_path):
    first = BatchBudget(tmp_path)
    assert first.reserve(4096) == 1
    reopened = BatchBudget(tmp_path)
    assert reopened.snapshot()['reserved_cny'] == '0.160768'
    with pytest.raises(DomainError) as blocked:
        reopened.reserve(4096)
    assert blocked.value.code == 'probe_batch_stopped'
    with pytest.raises(DomainError) as mismatch:
        BatchBudget(tmp_path, max_cny='9')
    assert mismatch.value.code == 'probe_budget_mismatch'


def test_cny_and_call_ceiling_block_before_network(tmp_path):
    def forbidden(_):
        raise AssertionError('Budget rejected before network')
    budget = BatchBudget(tmp_path / 'tiny', max_cny='0.1')
    with pytest.raises(DomainError) as error:
        make_provider(tmp_path, forbidden, budget).complete(request())
    assert error.value.code == 'probe_budget_exhausted' and budget.snapshot()['calls'] == 0
    budget = BatchBudget(tmp_path / 'one', max_calls=1)
    make_provider(tmp_path, lambda _: httpx.Response(200, json=body()), budget).complete(request())
    with pytest.raises(DomainError) as error:
        make_provider(tmp_path, forbidden, budget).complete(request())
    assert error.value.code == 'probe_budget_exhausted'


@pytest.mark.parametrize('change,code', [({'max_output_tokens': 4097}, 'probe_output_limit'),
    ({'messages': (ModelMessage(role='user', content='x' * 64001),)}, 'probe_input_limit'),
    ({'messages': (ModelMessage(role='user', content=SECRET),)}, 'probe_secret_in_input')])
def test_limits_and_secret_input_never_send(tmp_path, change, code):
    def forbidden(_):
        raise AssertionError('Validation must reject before network')
    adapter = make_provider(tmp_path, forbidden)
    with pytest.raises(DomainError) as error:
        adapter.complete(request(**change))
    assert error.value.code == code and adapter.budget.snapshot()['calls'] == 0


def test_fully_reserved_36_call_bound_is_below_authorized_ceiling():
    assert Decimal(36 * (64000 * 2 + 4096 * 8)) / 1000000 == Decimal('5.787648')


def test_other_process_sees_pending_reservation(tmp_path):
    budget = BatchBudget(tmp_path)
    budget.reserve(4096)
    script = '''
import sys
from pathlib import Path
from examples.quality_eval.deepseek_probe import BatchBudget
from juli.core.common import DomainError
try:
    BatchBudget(Path(sys.argv[1])).reserve(4096)
except DomainError as error:
    print(error.code)
else:
    raise AssertionError('Other process reset a pending call')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(tmp_path)],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == 'probe_batch_stopped'
    assert budget.snapshot()['calls'] == 1


def test_settlement_cannot_repeat_and_usage_excess_stops_batch(tmp_path):
    adapter = make_provider(tmp_path, lambda _: httpx.Response(200, json=body()))
    reply = adapter.complete(request())
    with pytest.raises(DomainError) as repeated:
        adapter.budget.settle(1, reply, 100)
    assert repeated.value.code == 'probe_budget_state'
    over = body()
    over['usage'] = {'prompt_tokens': 10, 'completion_tokens': 101, 'total_tokens': 111}
    other = make_provider(tmp_path, lambda _: httpx.Response(200, json=over))
    with pytest.raises(DomainError) as error:
        other.complete(request())
    assert error.value.code == 'probe_usage_exceeded'
    assert other.budget.snapshot()['entries'][-1]['status'] == 'unknown'


def test_parallel_tool_responses_remain_rejected(tmp_path):
    data = body()
    call = {'id': 'synthetic:1', 'type': 'function', 'function': {'name': 'unknown', 'arguments': '{}'}}
    data['choices'][0] = {'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [call, call]}}
    adapter = make_provider(tmp_path, lambda _: httpx.Response(200, json=data))
    with pytest.raises(DomainError) as error:
        adapter.complete(request())
    assert error.value.code == 'invalid_model_response'
    assert adapter.budget.snapshot()['stopped'] is True
