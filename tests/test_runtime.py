from juli.model_demo import scripted_analysis_json
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from juli.adapters.scripted_model import ScriptedModel
from juli.adapters.sqlite_store import SQLiteStore
from juli.adapters import sqlite_migrations as migrations
from juli.application.model_session import SessionPolicy
from juli.bootstrap import create_durable_runner, create_model_session
from juli.core.common import DomainError
from juli.core.models import ModelReply, ModelUsage
from juli.core.resources import ResourceBinding
from juli.model_demo import scripted_retrieval_steps
from test_model_session import model_task, limitation, change_budget
from test_document_migration_api import legacy_database


def runner(setup, steps=(), **options):
    path, _, owner, space, _, task = setup
    return create_durable_runner(SQLiteStore(path), owner.token, space, task, ScriptedModel(steps), profile=options.pop('profile', 'synthetic-v1'), **options)


def test_restart_between_every_action_preserves_history_budget_result(model_task):
    steps = scripted_retrieval_steps(model_task[-1])
    first = runner(model_task, [steps[0]])
    assert first.step()['usage']['actions'] == 1
    assert runner(model_task).step()['usage']['actions'] == 2
    assert runner(model_task, [steps[1]]).step()['usage']['model_calls'] == 2
    assert runner(model_task).step()['usage']['actions'] == 4
    done = runner(model_task, [steps[2]]).run()
    assert done['state']['status'] == 'completed'
    assert done['usage']['actions'] == 5 and done['usage']['model_calls'] == 3
    resumed = runner(model_task)
    assert resumed.run()['result'] == done['result']
    cursor = 0
    kinds = []
    while True:
        page = resumed.view(after=cursor, limit=2)
        if not page['events']: break
        assert page['next_cursor'] > cursor
        cursor = page['next_cursor']
        kinds.extend(e['kind'] for e in page['events'])
    assert kinds.count('model_reserved') == 3 and kinds[-1] == 'completed'
    with sqlite3.connect(model_task[0]) as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_locks').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM runtime_actions WHERE state='committed'").fetchone()[0] == 5
        assert model_task[2].token not in ''.join(str(x) for x in db.iterdump())


def test_clarification_restart_idempotency_and_history(model_task):
    question = ModelReply(text='{"kind":"clarification","question":"What is your goal?"}', usage=ModelUsage(input_tokens=1, output_tokens=10))
    pending = runner(model_task, [question]).run()
    assert pending['state']['status'] == 'pending'
    version = pending['state']['version']
    def complete(request):
        assert json.loads(request.messages[-1].content)['clarification_answer'] == 'Synthetic learning'
        return limitation(model_task[-1])
    resumed = runner(model_task, [complete])
    answered = resumed.answer(expected_version=version, request_key='answer1', text='Synthetic learning')
    assert answered['usage']['actions'] == 1
    assert resumed.answer(expected_version=version, request_key='answer1', text='Synthetic learning')['state'] == answered['state']
    with pytest.raises(DomainError, match='already describes'):
        resumed.answer(expected_version=version, request_key='answer1', text='Changed')
    with pytest.raises(DomainError): resumed.answer(expected_version=version, request_key='answer2', text='Another')
    assert resumed.run()['state']['status'] == 'completed'


def test_concurrent_workers_cannot_double_dispatch(model_task):
    entered, release = Event(), Event()
    def complete(request):
        entered.set()
        assert release.wait(10)
        return limitation(model_task[-1])
    first = runner(model_task, [complete])
    second = runner(model_task, [limitation(model_task[-1])])
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first.step)
        assert entered.wait(10)
        try:
            with pytest.raises(DomainError) as error: second.step()
            assert error.value.code == 'runtime_busy'
            assert second.provider.calls == 0
        finally: release.set()
        assert future.result()['state']['status'] == 'completed'


def test_expired_unknown_action_never_replays_and_fences_late_result(model_task):
    first = runner(model_task, [limitation(model_task[-1])])
    action = first.ledger.reserve(first.profile, first.policy, first._prepare)[0]
    with sqlite3.connect(model_task[0]) as db:
        db.execute('UPDATE runtime_actions SET expires_at=0 WHERE id=?', (action,))
    second = runner(model_task)
    failed = second.run()
    assert failed['state']['status'] == 'failed'
    assert failed['usage']['model_calls'] == 1 and failed['usage']['output_tokens'] == 1024
    assert second.provider.calls == 0
    with pytest.raises(DomainError):
        first.ledger.commit(action, first.profile, first.policy, lambda *_: pytest.fail('fence did not hold'))
    assert any(e['kind'] == 'outcome_unknown' for e in failed['events'])


@pytest.mark.parametrize('change', ['revoke', 'withdraw', 'cancel'])
def test_late_result_discarded_atomically(model_task, change):
    path, store, owner, space, resource, task = model_task
    def complete(request):
        if change == 'revoke': store.revoke_token(owner.token_id)
        elif change == 'withdraw': store.withdraw_resource(owner.token, space, resource.resource_id)
        else:
            state = store.get_task(owner.token, space, task)['state']
            store.cancel_task(owner.token, space, task, state['version'])
        return limitation(task)
    run = runner(model_task, [complete])
    with pytest.raises(DomainError): run.step()
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute('SELECT checkpoint_json FROM runtime_runs').fetchone()[0])
        assert saved['result'] is None
        assert json.loads(db.execute('SELECT state_json FROM tasks').fetchone()[0])['status'] == ('cancelled' if change == 'cancel' else 'failed')
        assert db.execute('SELECT COUNT(*) FROM runtime_locks').fetchone()[0] == 0


def test_failure_redaction_and_no_automatic_retry(model_task):
    def fail(request): raise RuntimeError('SENSITIVE_PROVIDER_BODY')
    run = runner(model_task, [fail])
    with pytest.raises(DomainError) as error: run.step()
    assert 'SENSITIVE' not in str(error.value)
    assert runner(model_task).run()['state']['status'] == 'failed'
    with sqlite3.connect(model_task[0]) as db:
        assert 'SENSITIVE' not in ''.join(db.iterdump())


def test_budget_and_config_cannot_reset_on_restart(model_task):
    policy = SessionPolicy(max_model_calls=1)
    steps = scripted_retrieval_steps(model_task[-1])
    run = runner(model_task, [steps[0]], policy=policy)
    run.step()
    with pytest.raises(DomainError): runner(model_task)
    with pytest.raises(DomainError): runner(model_task, policy=policy, profile='different')
    runner(model_task, policy=policy).step()
    stopped = runner(model_task, policy=policy).run()
    assert stopped['state']['status'] == 'failed'
    assert stopped['usage']['model_calls'] == 1 and stopped['usage']['actions'] == 2


def test_old_session_cannot_bypass_durable_budget(model_task):
    _, store, owner, space, _, task = model_task
    old = create_model_session(store, owner.token, space, task, ScriptedModel([limitation(task)]))
    runner(model_task)
    with pytest.raises(DomainError): old.advance()
    assert old.provider.calls == 0
    with pytest.raises(DomainError): create_model_session(store, owner.token, space, task, ScriptedModel([]))


def test_conversation_has_one_active_task_until_cancel(model_task, package):
    _, store, owner, space, resource, task = model_task
    first = runner(model_task)
    snapshot = store.get_task(owner.token, space, task)['snapshot']
    second = store.create_task(owner.token, space, snapshot['conversation_id'], question='Second', package=package, bindings=(ResourceBinding(role='subject',version_id=resource.version_id),), request_key='second')['snapshot']['task_id']
    args = (store, owner.token, space, second, ScriptedModel([]))
    with pytest.raises(DomainError) as error: create_durable_runner(*args, profile='synthetic-v1')
    assert error.value.code == 'conversation_busy'
    first.cancel(expected_version=first.view()['state']['version'])
    create_durable_runner(*args, profile='synthetic-v1')


def test_fact_without_receipt_cannot_complete(model_task):
    _, store, owner, space, _, task = model_task
    evidence = store.search_evidence(owner.token, space, task, 'Python')['items'][0]['evidence_id']
    from juli.core.evidence import AnalysisResult, ResultBlock
    result = AnalysisResult(task_id=task, blocks=(ResultBlock(kind='fact',text='Synthetic',evidence_ids=(evidence,)),),scenario_payload_json='{"summary":"synthetic"}')
    run = runner(model_task, [ModelReply(text=scripted_analysis_json(result),usage=ModelUsage(input_tokens=1,output_tokens=1))])
    with pytest.raises(DomainError) as error: run.step()
    assert error.value.code == 'evidence_not_read'
    assert run.view()['result'] is None and run.view()['state']['status'] == 'failed'


def test_result_read_rechecks_source_withdrawal(model_task):
    run = runner(model_task, scripted_retrieval_steps(model_task[-1]))
    assert run.run()['result']
    _, store, owner, space, resource, _ = model_task
    store.withdraw_resource(owner.token, space, resource.resource_id)
    with pytest.raises(DomainError): run.view()


@pytest.mark.parametrize('failure', [False, True])
def test_populated_v2_upgrade_is_explicit_and_atomic(tmp_path, monkeypatch, failure):
    path = tmp_path/'v2.sqlite3'
    token, resource = legacy_database(path)
    with sqlite3.connect(path) as db:
        for ddl in migrations.DDL_V2: db.execute(ddl)
        from juli.adapters.document_index import backfill_v1
        db.row_factory = sqlite3.Row
        backfill_v1(db)
        db.execute('INSERT INTO schema_migrations VALUES (?,?)',(2,migrations.checksum(2)))
        db.execute('PRAGMA user_version=2')
        before = list(db.iterdump())
    with pytest.raises(DomainError): SQLiteStore(path)
    if failure:
        monkeypatch.setattr(migrations,'DDL_V3',migrations.DDL_V3+('INVALID SQL',))
        with pytest.raises(sqlite3.Error): migrations.migrate(path)
        with sqlite3.connect(path) as db: assert list(db.iterdump()) == before
    else:
        assert migrations.migrate(path) == {'version':3,'changed':True}
        assert SQLiteStore(path).read_original(token,resource.workspace_id,resource.version_id) == b'Legacy Python\r\n'


@pytest.mark.parametrize('budget', [{'max_actions':1}, {'max_input_tokens':256}])
def test_admission_exhaustion_is_terminal_without_extra_calls(model_task, budget):
    change_budget(model_task, **budget)
    run = runner(model_task, scripted_retrieval_steps(model_task[-1]))
    result = run.run()
    assert result['state']['status'] == 'failed'
    assert run.provider.calls == (1 if 'max_actions' in budget else 0)


def test_duplicate_tool_call_not_dispatched_again(model_task):
    search = scripted_retrieval_steps(model_task[-1])[0]
    run = runner(model_task, [search, search])
    run.step()
    run.step()
    with pytest.raises(DomainError) as error: run.step()
    assert error.value.code == 'duplicate_call'
    assert run.view()['usage']['actions'] == 3


def test_model_profile_pins_endpoint_and_model_without_persisting_secret(model_task):
    from juli.adapters.chat_completions import ChatCompletionsProvider, ChatModelConfig
    _, store, owner, space, _, task = model_task
    policy = SessionPolicy(allow_external_egress=True)
    def provider(model):
        return ChatCompletionsProvider(ChatModelConfig('https://example.invalid/v1/chat/completions',model,100000),api_key='synthetic-key-not-a-real-secret')
    create_durable_runner(store,owner.token,space,task,provider('synthetic-one'),profile='fixed',policy=policy)
    with pytest.raises(DomainError) as error:
        create_durable_runner(store,owner.token,space,task,provider('synthetic-two'),profile='fixed',policy=policy)
    assert error.value.code == 'runtime_config_changed'
    with sqlite3.connect(model_task[0]) as db:
        assert 'synthetic-key-not-a-real-secret' not in ''.join(db.iterdump())


def test_external_provider_denied_before_start(model_task):
    _,store,owner,space,_,task = model_task
    provider = ScriptedModel([])
    provider.external_egress = True
    with pytest.raises(DomainError) as error:
        create_durable_runner(store,owner.token,space,task,provider,profile='external')
    assert error.value.code == 'egress_denied'
    assert store.get_task(owner.token,space,task)['state']['status'] == 'prepared'


def test_other_editor_cannot_take_over_checkpoint(model_task):
    _,store,owner,space,_,task = model_task
    runner(model_task)
    other = store.provision_principal('Other editor')
    store.set_member(owner.token,space,other.principal_id,'editor')
    with pytest.raises(DomainError) as error:
        create_durable_runner(store,other.token,space,task,ScriptedModel([]),profile='synthetic-v1')
    assert error.value.code == 'runtime_owner_required'


def test_runtime_demo_is_offline_and_durable(registry):
    from juli.runtime_demo import runtime_demo
    result = runtime_demo(registry)
    assert result['task_status'] == 'completed' and result['clarification_status'] == 'pending'
    assert result['usage']['actions'] == 6 and result['usage']['model_calls'] == 4
    assert result['network_calls'] == result['real_model_calls'] == 0
    assert result['result_persisted']
