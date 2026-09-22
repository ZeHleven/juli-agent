import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from candidate import GraphDriver
from fixtures import ROOT, CountingModel, comparable, make_runner, setup
from juli.adapters.sqlite_store import SQLiteStore
from juli.application.model_session import SessionPolicy
from juli.core.common import DomainError
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.models import ModelReply, ModelToolCall, ModelUsage
from juli.core.tools import ToolReference


def driver(config, backend, **kwargs):
    runner = make_runner(config, **kwargs)
    return GraphDriver(runner) if backend == 'graph' else runner


def clone(config, path):
    shutil.copyfile(config['path'], path)
    return {**config, 'path':str(path)}


def raw(config):
    with sqlite3.connect(config['path']) as db:
        state = json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(config['task'],)).fetchone()[0])
        checkpoint = json.loads(db.execute('SELECT checkpoint_json FROM runtime_runs WHERE task_id=?',(config['task'],)).fetchone()[0])
        return state, checkpoint


def test_same_snapshot_model_tools_context_and_result_in_both_loops(tmp_path):
    original = setup(tmp_path/'seed.sqlite3')
    outcomes = []
    requests = []
    for backend in ('native','graph'):
        config = clone(original,tmp_path/(backend+'.sqlite3'))
        provider = CountingModel()
        run = driver(config,backend,provider=provider)
        pending = run.run()
        assert pending['state']['status']=='pending'
        run.answer(expected_version=pending['state']['version'],request_key='answer',text='Python')
        complete = run.run()
        assert complete['state']['status']=='completed'
        assert complete['usage']['actions']==6 and complete['usage']['model_calls']==4
        outcomes.append(comparable(complete))
        requests.append(provider.requests)
    assert outcomes[0] == outcomes[1]
    assert len(requests[0]) == 4 and requests[0] == requests[1]


@pytest.mark.parametrize('backend',['native','graph'])
def test_restart_between_committed_actions_and_answer_does_not_reset_budget(tmp_path,backend):
    config=setup(tmp_path/'restart.sqlite3')
    first=driver(config,backend)
    pending=first.step(expected_actions=0)
    driver(config,backend).answer(expected_version=pending['state']['version'],request_key='answer',text='Python')
    for count in range(1,6):
        outcome=driver(config,backend).step(expected_actions=count)
    assert outcome['state']['status']=='completed'
    assert outcome['usage']['actions']==6 and outcome['usage']['model_calls']==4


@pytest.mark.parametrize('backend',['native','graph'])
def test_stale_dispatch_does_not_become_new_action(tmp_path,backend):
    config=setup(tmp_path/'stale.sqlite3')
    run=driver(config,backend)
    first=run.step(expected_actions=0)
    with pytest.raises(DomainError) as error: driver(config,backend).step(expected_actions=0)
    assert error.value.code=='stale_runtime_version'
    assert driver(config,backend).view()['usage']==first['usage']


@pytest.mark.parametrize('backend',['native','graph'])
@pytest.mark.parametrize('mutation',['cancel','revoke','withdraw'])
def test_permission_and_cancellation_after_call_are_still_enforced(tmp_path,backend,mutation):
    config=setup(tmp_path/'fence.sqlite3')
    store=SQLiteStore(Path(config['path']))
    def mutate():
        if mutation=='cancel':
            state=store.get_task(config['token'],config['space'],config['task'])['state']
            store.cancel_task(config['token'],config['space'],config['task'],state['version'])
        elif mutation=='revoke': store.revoke_token(config['token_id'])
        else: store.withdraw_resource(config['token'],config['space'],config['resource'])
    provider=CountingModel(mutate)
    run=driver(config,backend,provider=provider)
    with pytest.raises(DomainError):run.step(expected_actions=0)
    state,checkpoint=raw(config)
    assert provider.calls==1 and checkpoint['result'] is None
    assert state['status']==('cancelled' if mutation=='cancel' else 'failed')


@pytest.mark.parametrize('backend',['native','graph'])
def test_persisted_budget_applies_after_clarification(tmp_path,backend):
    config=setup(tmp_path/'budget.sqlite3')
    policy=SessionPolicy(max_model_calls=1)
    run=driver(config,backend,policy=policy)
    pending=run.run()
    run.answer(expected_version=pending['state']['version'],request_key='answer',text='Python')
    stopped=driver(config,backend,policy=policy).run()
    assert stopped['state']['status']=='failed' and stopped['usage']['model_calls']==1


@pytest.mark.parametrize('backend',['native','graph'])
def test_expired_unknown_reservation_is_not_replayed(tmp_path,backend):
    config=setup(tmp_path/'unknown.sqlite3')
    first=make_runner(config)
    first.ledger.reserve(first.profile,first.policy,first._prepare,expected_actions=0)
    with sqlite3.connect(config['path']) as db: db.execute('UPDATE runtime_actions SET expires_at=0')
    provider=CountingModel()
    stopped=driver(config,backend,provider=provider).run()
    assert stopped['state']['status']=='failed' and provider.calls==0
    assert stopped['usage']['actions']==1 and stopped['usage']['output_tokens']==1024


@pytest.mark.parametrize('backend',['native','graph'])
def test_fact_without_read_receipt_is_rejected(tmp_path,backend):
    config=setup(tmp_path/'citation.sqlite3')
    store=SQLiteStore(Path(config['path']))
    ref=store.search_evidence(config['token'],config['space'],config['task'],'Python')['items'][0]['evidence_id']
    class UnreadFact(CountingModel):
        def complete(self,request):
            result=AnalysisResult(task_id=config['task'],blocks=(ResultBlock(kind='fact',text='Synthetic assertion',evidence_ids=(ref,)),),scenario_payload_json='{"summary":"synthetic"}')
            return ModelReply(text=result.model_dump_json(),usage=ModelUsage(input_tokens=1,output_tokens=1))
    run=driver(config,backend,provider=UnreadFact())
    with pytest.raises(DomainError) as error:run.run()
    assert error.value.code=='evidence_not_read'
    assert raw(config)[0]['status']=='failed'


@pytest.mark.parametrize('backend',['native','graph'])
def test_model_cannot_choose_a_tool_outside_the_gateway(tmp_path,backend):
    config=setup(tmp_path/'tool.sqlite3')
    class BadTool(CountingModel):
        def complete(self,request):
            return ModelReply(tool_call=ModelToolCall(call_id='bad',tool=ToolReference(name='shell.execute',version='1.0.0'),arguments_json='{}'),usage=ModelUsage(input_tokens=1,output_tokens=1))
    run=driver(config,backend,provider=BadTool())
    with pytest.raises(DomainError) as error:run.run()
    assert error.value.code=='tool_not_granted'
    assert raw(config)[1]['actions']==1


@pytest.mark.parametrize('backend',['native','graph'])
def test_concurrent_drivers_do_not_double_call(tmp_path,backend):
    config=setup(tmp_path/'concurrency.sqlite3')
    entered,release=Event(),Event()
    def wait():
        entered.set()
        assert release.wait(10)
    first=driver(config,backend,provider=CountingModel(wait))
    second_provider=CountingModel()
    second=driver(config,backend,provider=second_provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(first.step,expected_actions=0)
        assert entered.wait(10)
        try:
            with pytest.raises(DomainError):second.step(expected_actions=0)
            assert second_provider.calls==0
        finally:release.set()
        assert future.result()['state']['status']=='pending'


def probe(config,mode,*,expected_code=0):
    paths=[str(ROOT/'backend/src'),str(Path(__file__).parent)]
    # Current evaluator uses a separate target directory; clean venvs need no extra path.
    if (ROOT/'.local/m07-deps').is_dir(): paths.append(str(ROOT/'.local/m07-deps'))
    payload={**config,'mode':mode,'import_paths':paths}
    result=subprocess.run([sys.executable,'-I',str(Path(__file__).with_name('process_probe.py'))],
        input=json.dumps(payload),text=True,capture_output=True,timeout=30,
        cwd=str(Path(config['checkpoint']).parent),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==expected_code, result.stderr
    return json.loads(result.stdout) if result.stdout else None


def test_sqlite_interrupt_survives_process_restart_and_reenters_node(tmp_path):
    config={'checkpoint':str(tmp_path/'graph.sqlite3'),'effects':str(tmp_path/'effects.sqlite3')}
    with sqlite3.connect(config['effects']) as db: db.execute('CREATE TABLE visits(id INTEGER PRIMARY KEY)')
    first=probe(config,'interrupt-start')
    assert first=={'interrupted':True,'answer':'','node_entries':1}
    resumed=probe(config,'interrupt-resume')
    assert resumed=={'interrupted':False,'answer':'synthetic answer','node_entries':2}


def test_commit_gap_replay_is_fenced_but_requires_explicit_reconciliation(tmp_path):
    task_config=setup(tmp_path/'juli.sqlite3')
    config={'task_config':task_config,'checkpoint':str(tmp_path/'graph.sqlite3')}
    probe(config,'gap-crash',expected_code=23)
    assert raw(task_config)[1]['actions']==1
    resumed=probe(config,'gap-resume')
    assert resumed['error']=='stale_runtime_version'
    assert resumed['graph_state']['expected_actions']==0
    assert resumed['outcome']['usage']['actions']==1
    assert resumed['outcome']['state']['status']=='pending'
    # Application explicitly reads current state and accepts an answer; no silent replay.
    run=driver(task_config,'graph')
    run.answer(expected_version=resumed['outcome']['state']['version'],request_key='reconciled-answer',text='Python')
    final=run.run()
    assert final['state']['status']=='completed' and final['usage']['actions']==6
    from langgraph.checkpoint.sqlite import SqliteSaver
    with sqlite3.connect(config['checkpoint'],check_same_thread=False) as db:
        # Decode through the saver: raw SQL may represent serialized data as hex blobs.
        checkpoints = list(SqliteSaver(db).list(None))
    assert checkpoints
    serialized = repr(checkpoints)
    assert task_config['token'] not in serialized
    assert 'Synthetic Python development experience' not in serialized


def test_native_commit_gap_uses_the_one_business_state_after_restart(tmp_path):
    task_config=setup(tmp_path/'juli.sqlite3')
    config={'task_config':task_config,'checkpoint':str(tmp_path/'unused.sqlite3')}
    probe(config,'native-crash',expected_code=23)
    resumed=probe(config,'native-read')['outcome']
    assert resumed['state']['status']=='pending' and resumed['usage']['actions']==1
    assert not Path(config['checkpoint']).exists()
