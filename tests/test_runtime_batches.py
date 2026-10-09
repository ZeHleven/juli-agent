"""Offline execution tests for bounded, durable multi-tool assistant turns."""
import json
import sqlite3

import pytest

from juli.adapters.local_tools import LocalEvidenceTools
from juli.adapters.scripted_model import ScriptedModel
from juli.adapters.sqlite_store import SQLiteStore
from juli.bootstrap import create_durable_runner
from juli.core.common import DomainError
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.runtime import Checkpoint
from juli.core.tools import ToolReference
from test_model_session import change_budget, limitation, model_task


def search(call_id, query='Python'):
    return ModelToolCall(call_id=call_id,
        tool=ToolReference(name='evidence.search', version='1.0.0'),
        arguments_json=json.dumps({'query': query}))


def batch(*calls, text='First I will inspect both relevant passages.'):
    return ModelReply(text=text, tool_calls=calls,
        usage=ModelUsage(input_tokens=10, output_tokens=20))


def runner(setup, steps=()):
    path, _, owner, space, _, task = setup
    return create_durable_runner(SQLiteStore(path), owner.token, space, task,
        ScriptedModel(steps), profile='synthetic-batches-v1')


def checkpoint(setup):
    with sqlite3.connect(setup[0]) as db:
        value = db.execute('SELECT checkpoint_json FROM runtime_runs').fetchone()[0]
    return Checkpoint.model_validate_json(value)


@pytest.fixture
def tool_trace(monkeypatch):
    calls = []
    original = LocalEvidenceTools.execute

    def observe(self, reference, arguments, *, timeout_seconds):
        calls.append((reference.name, arguments))
        return original(self, reference, arguments, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(LocalEvidenceTools, 'execute', observe)
    return calls


def test_batch_survives_every_restart_and_preserves_one_assistant_turn(model_task, tool_trace):
    proposal = batch(search('first', 'Python'), search('second', 'synthetic'))
    first = runner(model_task, [proposal])
    assert first.step()['usage']['actions'] == 1
    assert first.provider.calls == 1 and tool_trace == []
    saved = checkpoint(model_task)
    assert saved.pending.call_id == 'first'
    assert [call.call_id for call in saved.pending_queue] == ['second']

    second = runner(model_task)
    assert second.step()['usage']['actions'] == 2
    assert second.provider.calls == 0
    saved = checkpoint(model_task)
    assert saved.pending.call_id == 'second' and saved.pending_queue == ()

    third = runner(model_task)
    assert third.step()['usage']['actions'] == 3
    assert third.provider.calls == 0
    saved = checkpoint(model_task)
    assert saved.pending is None and saved.pending_queue == ()
    assert tool_trace == [('evidence.search', {'query': 'Python'}),
                          ('evidence.search', {'query': 'synthetic'})]

    inspected = []
    def finish(request):
        inspected.append(request)
        assistant, tool_one, tool_two = request.messages[-3:]
        assert assistant.role == 'assistant' and assistant.content == proposal.text
        assert [call.call_id for call in assistant.calls] == ['first', 'second']
        assert [tool_one.role, tool_two.role] == ['tool', 'tool']
        assert [tool_one.tool_call_id, tool_two.tool_call_id] == ['first', 'second']
        assert all(json.loads(message.content)['items'] for message in (tool_one, tool_two))
        assert sum(bool(message.calls) for message in request.messages) == 1
        return limitation(model_task[-1])

    final = runner(model_task, [finish])
    done = final.run()
    assert done['state']['status'] == 'completed'
    assert done['usage']['actions'] == 4 and done['usage']['model_calls'] == 2
    assert len(inspected) == final.provider.calls == 1
    resumed = runner(model_task)
    assert resumed.run()['result'] == done['result']
    assert resumed.provider.calls == 0 and len(tool_trace) == 2
    with sqlite3.connect(model_task[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_actions WHERE kind='tool' AND state='committed'").fetchone()[0] == 2


@pytest.mark.parametrize('fault', ['unauthorized', 'schema', 'duplicate'])
def test_invalid_second_call_rejects_entire_batch_before_any_tool(model_task, tool_trace, fault):
    first, second = search('first'), search('second')
    if fault == 'unauthorized':
        second = ModelToolCall(call_id='second',
            tool=ToolReference(name='shell.execute', version='1.0.0'), arguments_json='{}')
    elif fault == 'schema':
        second = second.model_copy(update={'arguments_json': '{"query":"Python","workspace_id":"foreign"}'})
    else:
        second = search('first', 'synthetic')
    # Dict input intentionally allows malformed provider replies to reach validation.
    reply = {'text': 'I intend to make both calls.',
        'tool_calls': [first.model_dump(), second.model_dump()],
        'usage': {'input_tokens': 10, 'output_tokens': 20}}
    run = runner(model_task, [reply])
    with pytest.raises(DomainError):
        run.step()
    assert run.provider.calls == 1 and tool_trace == []
    saved = checkpoint(model_task)
    assert saved.pending is None and saved.pending_queue == ()
    assert not any(message.calls for message in saved.messages)
    stopped = runner(model_task)
    assert stopped.run()['state']['status'] == 'failed'
    assert stopped.provider.calls == 0 and tool_trace == []


def test_second_call_reusing_an_earlier_turn_id_rejects_whole_new_batch(model_task, tool_trace):
    prior = ModelReply(tool_call=search('already-used'),
        usage=ModelUsage(input_tokens=10, output_tokens=20))
    first = runner(model_task, [prior])
    first.step()
    first.step()
    run = runner(model_task, [batch(search('new-first'), search('already-used'))])
    with pytest.raises(DomainError) as error:
        run.step()
    assert error.value.code == 'duplicate_call'
    assert len(tool_trace) == 1
    assert run.view()['state']['status'] == 'failed'
    assert run.view()['usage']['actions'] == 3


@pytest.mark.parametrize('change', ['revoke', 'withdraw', 'cancel', 'tool_grant'])
def test_queued_tool_rechecks_current_authority_resources_and_cancellation(model_task, tool_trace, change):
    path, store, owner, space, resource, task = model_task
    run = runner(model_task, [batch(search('first'), search('second', 'synthetic'))])
    run.step()
    run.step()
    assert len(tool_trace) == 1
    if change == 'revoke':
        store.revoke_token(owner.token_id)
    elif change == 'withdraw':
        store.withdraw_resource(owner.token, space, resource.resource_id)
    elif change == 'cancel':
        state = store.get_task(owner.token, space, task)['state']
        store.cancel_task(owner.token, space, task, state['version'])
    else:
        run.gateway.platform_tools = ()

    # Some revocations return a failed/cancelled view; revoked credentials deny
    # even reading that view. Neither outcome may dispatch the queued operation.
    for _ in range(2):
        try:
            result = run.step()
        except DomainError:
            pass
        else:
            assert result['state']['status'] in {'failed', 'cancelled'}
    assert len(tool_trace) == 1 and run.provider.calls == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_actions WHERE kind='tool' AND state='committed'").fetchone()[0] == 1
        if change == 'cancel':
            assert json.loads(db.execute('SELECT state_json FROM tasks').fetchone()[0])['status'] == 'cancelled'


def test_remaining_budget_must_cover_whole_batch_before_first_tool(model_task, tool_trace):
    change_budget(model_task, max_actions=2)
    run = runner(model_task, [batch(search('first'), search('second'))])
    with pytest.raises(DomainError) as error:
        run.step()
    assert error.value.code == 'action_budget_exceeded'
    assert run.provider.calls == 1 and tool_trace == []
    assert run.view()['state']['status'] == 'failed'
    assert run.view()['usage']['actions'] == 1
    assert checkpoint(model_task).pending is None


def test_middle_tool_failure_stops_remaining_queue_without_retry(model_task, monkeypatch):
    executed = []
    original = LocalEvidenceTools.execute

    def fail_second(self, reference, arguments, *, timeout_seconds):
        executed.append(arguments['query'])
        if arguments['query'] == 'FAIL':
            raise RuntimeError('synthetic execution failed')
        return original(self, reference, arguments, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(LocalEvidenceTools, 'execute', fail_second)
    first = runner(model_task, [batch(search('first'), search('failing', 'FAIL'), search('last', 'synthetic'))])
    first.step()
    first.step()
    resumed = runner(model_task)
    with pytest.raises(DomainError) as error:
        resumed.step()
    assert error.value.code == 'action_failed'
    assert executed == ['Python', 'FAIL']
    again = runner(model_task)
    stopped = again.run()
    assert stopped['state']['status'] == 'failed'
    assert stopped['usage']['actions'] == 3
    assert stopped['usage']['model_calls'] == 1
    assert resumed.provider.calls == again.provider.calls == 0
    assert executed == ['Python', 'FAIL']
    assert [message.tool_call_id for message in checkpoint(model_task).messages if message.role == 'tool'] == ['first']


def test_legacy_single_pending_checkpoint_defaults_to_empty_queue(model_task, tool_trace):
    reply = ModelReply(tool_call=search('legacy-single'),
        usage=ModelUsage(input_tokens=10, output_tokens=20))
    runner(model_task, [reply]).step()
    # Exercise old checkpoint JSON under the same current deployment profile;
    # changing a provider/runtime profile remains independently prohibited.
    with sqlite3.connect(model_task[0]) as db:
        value = json.loads(db.execute('SELECT checkpoint_json FROM runtime_runs').fetchone()[0])
        value.pop('pending_queue')
        db.execute('UPDATE runtime_runs SET checkpoint_json=?', (json.dumps(value),))
    assert checkpoint(model_task).pending_queue == ()
    assert checkpoint(model_task).pending.call_id == 'legacy-single'
    resumed = runner(model_task)
    assert resumed.step()['usage']['actions'] == 2
    assert resumed.provider.calls == 0 and len(tool_trace) == 1
    assert checkpoint(model_task).pending is None
    final = runner(model_task, [limitation(model_task[-1])]).run()
    assert final['state']['status'] == 'completed'
    assert final['usage']['actions'] == 3 and len(tool_trace) == 1
