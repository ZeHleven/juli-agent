import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.api import create_app
from juli.core.common import DomainError


@pytest.fixture
def api_setup(tmp_path,registry):
    path=tmp_path/'juli.sqlite3'
    migrate(path)
    store=SQLiteStore(path)
    alice=store.provision_principal('Synthetic Alice')
    bob=store.provision_principal('Synthetic Bob')
    with TestClient(create_app(registry,database=path)) as client:
        yield path,store,alice,bob,client


def headers(identity):return {'Authorization':'Bearer '+identity.token}


def seeded_api(api_setup):
    _,_,alice,_,client=api_setup
    auth=headers(alice)
    space=client.post('/v1/workspaces',headers=auth,json={'name':'Synthetic'}).json()['workspace_id']
    base='/v1/workspaces/'+space
    resource=client.post(base+'/resources',headers=auth,json={'display_name':'Background','text':'  synthetic background\n'}).json()
    conversation=client.post(base+'/conversations',headers=auth).json()['conversation_id']
    body={'question':'Synthetic question','scenario_id':'minimal','scenario_version':'0.1.0','bindings':[{'role':'subject','version_id':resource['version_id']}]}
    return base,resource,conversation,body


def test_authenticated_api_persists_real_server_owned_state(api_setup,registry):
    path,store,alice,bob,client=api_setup
    auth=headers(alice)
    assert client.get('/v1/workspaces').status_code==401
    assert client.get('/v1/me',headers=auth).json()['principal_id']==alice.principal_id
    base,resource,conversation,body=seeded_api(api_setup)
    url=base+'/conversations/'+conversation+'/tasks'
    assert client.post(url,headers=auth,json=body).status_code==422
    response=client.post(url,headers={**auth,'Idempotency-Key':'request:api'},json=body)
    assert response.status_code==201
    record=response.json()
    assert record['state']['status']=='prepared'
    assert record['snapshot']['principal_id']==alice.principal_id
    assert record['execution_enabled'] is False
    assert client.post(url,headers={**auth,'Idempotency-Key':'request:api'},json=body).json()==record
    assert len(client.get(url,headers=auth).json()['items'])==1
    task_url=base+'/tasks/'+record['snapshot']['task_id']
    assert client.get(task_url,headers=headers(bob)).status_code==403
    with TestClient(create_app(registry,database=path)) as restarted:
        assert restarted.get(task_url,headers=auth).json()==record
        assert restarted.post(task_url+'/cancel',headers=auth,json={'expected_version':1}).json()['state']['status']=='cancelled'
        assert restarted.post(task_url+'/cancel',headers=auth,json={'expected_version':1}).status_code==409
    store.revoke_token(alice.token_id)
    assert client.get(task_url,headers=auth).status_code==401


@pytest.mark.parametrize('injection',[
    {'principal_id':'principal:forged'},
    {'resources':[]},
    {'access':{'workspace_ids':['workspace:any']}},
    {'budget':{'max_actions':99999}},
])
def test_client_cannot_supply_authority_or_resource_metadata(api_setup,injection):
    _,_,alice,_,client=api_setup
    base,_,conversation,body=seeded_api(api_setup)
    response=client.post(base+'/conversations/'+conversation+'/tasks',headers={**headers(alice),'Idempotency-Key':'injected'},json={**body,**injection})
    assert response.status_code==422


def test_member_role_changes_revoke_http_access(api_setup):
    _,_,alice,bob,client=api_setup
    base,resource,_,_=seeded_api(api_setup)
    member=base+'/members/'+bob.principal_id
    auth=headers(alice)
    assert client.put(member,headers=auth,json={'role':'viewer'}).status_code==204
    read=base+'/resource-versions/'+resource['version_id']
    assert client.get(read,headers=headers(bob)).status_code==200
    assert client.post(base+'/resources',headers=headers(bob),json={'display_name':'x','text':'x'}).status_code==403
    assert client.put(base+'/members/'+alice.principal_id,headers=headers(bob),json={'role':'editor'}).status_code==403
    assert client.put(member,headers=auth,json={'role':'owner'}).status_code==422
    assert client.delete(member,headers=auth).status_code==204
    assert client.get(read,headers=headers(bob)).status_code==403


def test_invalid_task_and_withdrawal_are_controlled(api_setup):
    _,_,alice,_,client=api_setup
    base,resource,conversation,body=seeded_api(api_setup)
    auth={**headers(alice),'Idempotency-Key':'invalid'}
    url=base+'/conversations/'+conversation+'/tasks'
    assert client.post(url,headers=auth,json={**body,'question':'   '}).status_code==422
    assert client.post(url,headers=auth,json={**body,'bindings':body['bindings']*2}).status_code==422
    assert client.post(base+'/resources/'+resource['resource_id']+'/withdraw',headers=auth).status_code==204
    assert client.get(base+'/resource-versions/'+resource['version_id'],headers=auth).status_code==404
    assert client.post(url,headers=auth,json=body).status_code==404


def test_no_database_startup_migration_and_no_static_token_fallback(tmp_path,registry):
    path=tmp_path/'absent.sqlite3'
    with pytest.raises(DomainError):create_app(registry,database=path)
    assert not path.exists()
    migrate(path)
    with pytest.raises(ValueError):create_app(registry,database=path,api_token='synthetic-static-token-not-a-pat')
    with TestClient(create_app(registry,database=path)) as client:
        assert client.get('/v1/me',headers={'Authorization':'Bearer synthetic-static-token-not-a-pat'}).status_code==401
        assert client.post('/v1/identity/create',json={}).status_code==404
        assert client.post('/v1/login',json={}).status_code==404


def test_cli_explicit_migration_provision_revoke_and_restart(tmp_path):
    root=Path(__file__).resolve().parents[1]
    environment={**os.environ,'PYTHONPATH':str(root/'backend/src'),'PYTHONDONTWRITEBYTECODE':'1'}
    database=tmp_path/'cli.sqlite3'
    def command(*args):
        result=subprocess.run([sys.executable,'-m','juli',*args],env=environment,cwd=tmp_path,text=True,encoding='utf-8',capture_output=True)
        assert result.returncode==0, result.stderr
        return json.loads(result.stdout)
    assert command('db','migrate','--database',str(database))['changed'] is True
    assert command('db','migrate','--database',str(database))['changed'] is False
    issued=command('identity','create','--database',str(database),'--name','Synthetic CLI')
    assert SQLiteStore(database).authenticate(issued['token']).principal_id==issued['principal_id']
    assert command('identity','revoke','--database',str(database),'--token-id',issued['token_id'])['revoked'] is True
    with pytest.raises(DomainError):SQLiteStore(database).authenticate(issued['token'])
