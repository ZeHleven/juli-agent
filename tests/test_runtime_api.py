import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi.testclient import TestClient
from juli.api import create_app
from juli.adapters.sqlite_store import SQLiteStore
from juli.adapters.workbench_demo import demo_config, WorkbenchDemoModel
from juli.runtime_api import RuntimeConfig
from juli.core.models import ModelReply, ModelUsage
from test_model_session import model_task, limitation


def client_for(setup, registry, runtime='demo'):
    path,store,owner,space,resource,task=setup
    app=create_app(registry,database=path,runtime=demo_config() if runtime=='demo' else runtime)
    client=TestClient(app)
    client.headers['Authorization']='Bearer '+owner.token
    return client, f'/v1/workspaces/{space}/tasks/{task}'


def test_default_disabled_static_packaged_and_authenticated_reads(model_task,registry):
    client,base=client_for(model_task,registry,None)
    with client:
        assert client.get('/').status_code==200
        assert '据理' in client.get('/').text
        for name in ['app.js','style.css']:
            response=client.get('/workbench/'+name)
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
            assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        assert client.get('/workbench/missing').status_code==404
        assert client.get('/v1/runtime').json()['mode']=='disabled'
        assert client.post(base+'/step',json={'expected_actions':0}).status_code==503
        assert client.get(base+'/runtime').json()['state']['status']=='prepared'
        client.headers.clear()
        assert client.get('/v1/runtime').status_code==401
        assert client.get(base+'/runtime').status_code==401
        assert client.get('/').status_code==200  # Static shell contains no identity or data.


def test_full_http_clarification_restart_continue_and_citation(model_task,registry):
    client,base=client_for(model_task,registry)
    with client:
        pending=client.post(base+'/step',json={'expected_actions':0}).json()
        assert pending['state']['status']=='pending'
        answer={'expected_version':pending['state']['version'],'text':'Python'}
        first=client.post(base+'/answer',json=answer,headers={'Idempotency-Key':'answer-one'})
        assert first.status_code==200
        assert client.post(base+'/answer',json=answer,headers={'Idempotency-Key':'answer-one'}).json()['state']==first.json()['state']
    client,base=client_for(model_task,registry)
    with client:
        for count in range(1,6):
            response=client.post(base+'/step',json={'expected_actions':count})
            assert response.status_code==200, response.text
        done=response.json()
        assert done['state']['status']=='completed' and done['usage']['model_calls']==4
        ref=done['result']['blocks'][1]['evidence_ids'][0]
        read=client.post(base+'/evidence/read',json={'evidence_id':ref})
        assert read.status_code==200 and read.json()['items'][0]['locator']['char_start']==0
        assert client.get(base+'/runtime?after='+str(done['next_cursor'])).json()['events']==[]


@pytest.mark.parametrize('body',[{}, {'expected_actions':True}, {'expected_actions':-1}, {'expected_actions':0,'api_key':'fake'}, {'expected_actions':0,'provider':'arbitrary'}, {'expected_actions':0,'allow_external_egress':True}])
def test_execution_input_rejects_forged_authority(model_task,registry,body):
    client,base=client_for(model_task,registry)
    with client:
        assert client.post(base+'/step',json=body).status_code==422
        assert client.get(base+'/runtime').json()['usage'] is None


def test_repeated_step_does_not_dispatch_again(model_task,registry):
    client,base=client_for(model_task,registry)
    with client:
        assert client.post(base+'/step',json={'expected_actions':0}).status_code==200
        assert client.post(base+'/step',json={'expected_actions':0}).status_code==409
        assert client.get(base+'/runtime').json()['usage']['model_calls']==1


def test_concurrent_http_dispatch_and_cancel(model_task,registry):
    entered,release=Event(),Event()
    class BlockingModel(WorkbenchDemoModel):
        def complete(self,request):
            entered.set()
            assert release.wait(10)
            return limitation(model_task[-1])
    config=RuntimeConfig(BlockingModel,'blocked-test','local')
    client,base=client_for(model_task,registry,config)
    with client, ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(client.post,base+'/step',json={'expected_actions':0})
        assert entered.wait(10)
        try:
            assert client.post(base+'/step',json={'expected_actions':0}).status_code==409
            current=client.get(base+'/runtime').json()
            assert client.post(base+'/cancel',json={'expected_version':current['state']['version']}).status_code==200
        finally: release.set()
        assert future.result().status_code==409
        cancelled=client.get(base+'/runtime').json()
        assert cancelled['state']['status']=='cancelled' and cancelled['result'] is None


def test_nonowner_and_wrong_workspace_cannot_dispatch(model_task,registry):
    _,store,owner,space,_,task=model_task
    other=store.provision_principal('Other')
    store.set_member(owner.token,space,other.principal_id,'editor')
    client,base=client_for(model_task,registry)
    with client:
        client.headers['Authorization']='Bearer '+other.token
        assert client.post(base+'/step',json={'expected_actions':0}).status_code==403
        assert client.get(base+'/runtime').status_code==403
        assert client.post('/v1/workspaces/workspace:foreign/tasks/'+task+'/step',json={'expected_actions':0}).status_code==403


def test_no_match_demo_returns_limitation_not_fake_fact(model_task,registry):
    client,base=client_for(model_task,registry)
    with client:
        pending=client.post(base+'/step',json={'expected_actions':0}).json()
        client.post(base+'/answer',json={'expected_version':pending['state']['version'],'text':'ZZZUNMATCHABLE'},headers={'Idempotency-Key':'no-match'})
        for n in range(1,4): out=client.post(base+'/step',json={'expected_actions':n}).json()
        assert out['state']['status']=='completed'
        assert all(b['kind']=='limitation' for b in out['result']['blocks'])


def test_runtime_config_cannot_use_catalog_or_hide_external_egress(registry):
    with pytest.raises(ValueError): create_app(registry,api_token='a'*30,runtime=demo_config())
    with pytest.raises(ValueError): RuntimeConfig(WorkbenchDemoModel,'test','external')


def test_demo_scenario_restriction_and_cursor_validation(model_task,registry):
    config=RuntimeConfig(WorkbenchDemoModel,'restricted','demo',scenario_ids=('uninstalled',))
    client,base=client_for(model_task,registry,config)
    with client:
        assert client.post(base+'/step',json={'expected_actions':0}).status_code==403
        assert client.get(base+'/runtime?after=-1').status_code==422
        assert client.get(base+'/runtime?limit=101').status_code==422
