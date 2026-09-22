from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from juli.adapters import sqlite_migrations as migrations
from juli.adapters.sqlite_store import SQLiteStore
from juli.core.common import DomainError
from juli.core.resources import ResourceBinding
from juli.core.scenarios import ScenarioPackage, package_fingerprint


@pytest.fixture
def persistent(tmp_path, package):
    path = tmp_path / 'juli.sqlite3'
    migrations.migrate(path)
    now = [1000.0]
    store = SQLiteStore(path, clock=lambda:now[0])
    alice = store.provision_principal('Synthetic Alice')
    bob = store.provision_principal('Synthetic Bob')
    workspace = store.create_workspace(alice.token, 'Synthetic workspace')
    resource = store.save_text(alice.token,workspace.workspace_id,display_name='Original',text='  synthetic original\n')
    conversation = store.create_conversation(alice.token,workspace.workspace_id)
    return path,store,alice,bob,workspace,resource,conversation,now


def create_task(persistent,package,key='request:1'):
    _,store,alice,_,space,resource,conversation,_ = persistent
    return store.create_task(alice.token,space.workspace_id,conversation.conversation_id,question='Synthetic question',package=package,bindings=(ResourceBinding(role='subject',version_id=resource.version_id),),request_key=key)


def test_migration_is_explicit_idempotent_and_refuses_foreign_databases(tmp_path):
    path=tmp_path/'new.sqlite3'
    with pytest.raises(DomainError):SQLiteStore(path)
    assert not path.exists()
    assert migrations.migrate(path)=={'version':3,'changed':True}
    assert migrations.migrate(path)=={'version':3,'changed':False}
    other=tmp_path/'foreign.sqlite3'
    with sqlite3.connect(other) as db:db.execute('CREATE TABLE unrelated(value TEXT)')
    before=other.read_bytes()
    with pytest.raises(DomainError):migrations.migrate(other)
    assert other.read_bytes()==before


def test_failed_migration_rolls_back_all_ddl(tmp_path,monkeypatch):
    path=tmp_path/'broken.sqlite3'
    monkeypatch.setattr(migrations,'DDL',migrations.DDL+('NOT A SQL STATEMENT',))
    with pytest.raises(sqlite3.Error):migrations.migrate(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]
        assert db.execute('PRAGMA user_version').fetchone()[0]==0


@pytest.mark.parametrize('tamper',[
    'PRAGMA user_version=99',
    "UPDATE schema_migrations SET checksum='wrong'",
    'PRAGMA application_id=123',
])
def test_unknown_revision_or_receipt_is_rejected(tmp_path,tamper):
    path=tmp_path/'juli.sqlite3'
    migrations.migrate(path)
    with sqlite3.connect(path) as db:db.execute(tamper)
    with pytest.raises(DomainError):SQLiteStore(path)


def test_tokens_are_hashed_expire_and_are_revocable(persistent):
    path,store,alice,bob,_,_,_,now=persistent
    assert alice.token not in repr(alice)
    with sqlite3.connect(path) as db:
        dump='\n'.join(db.iterdump())
        assert alice.token not in dump and bob.token not in dump
        assert len(db.execute('SELECT token_hash FROM tokens LIMIT 1').fetchone()[0])==64
    assert store.authenticate(alice.token).principal_id==alice.principal_id
    now[0]=alice.expires_at
    with pytest.raises(DomainError):store.authenticate(alice.token)
    now[0]=1000
    store.revoke_token(alice.token_id)
    with pytest.raises(DomainError):store.authenticate(alice.token)
    assert store.authenticate(bob.token)


def test_disabling_principal_invalidates_all_tokens_and_enable_does_not_revive(persistent):
    _,store,alice,_,_,_,_,_=persistent
    extra=store.issue_token(alice.principal_id)
    store.set_principal_enabled(alice.principal_id,False)
    for token in (alice.token,extra.token):
        with pytest.raises(DomainError):store.authenticate(token)
    store.set_principal_enabled(alice.principal_id,True)
    with pytest.raises(DomainError):store.authenticate(alice.token)
    assert store.authenticate(store.issue_token(alice.principal_id).token)


def test_workspace_roles_and_revocation_are_live(persistent):
    _,store,alice,bob,space,resource,_,_=persistent
    assert store.list_workspaces(bob.token)==[]
    with pytest.raises(DomainError):store.read_resource(bob.token,space.workspace_id,resource.version_id)
    store.set_member(alice.token,space.workspace_id,bob.principal_id,'viewer')
    assert store.read_resource(bob.token,space.workspace_id,resource.version_id)['text']=='  synthetic original\n'
    with pytest.raises(DomainError):store.save_text(bob.token,space.workspace_id,display_name='x',text='x')
    with pytest.raises(DomainError):store.set_member(bob.token,space.workspace_id,alice.principal_id,'editor')
    store.set_member(alice.token,space.workspace_id,bob.principal_id,'editor')
    store.save_text(bob.token,space.workspace_id,display_name='New',text='synthetic')
    with pytest.raises(DomainError):store.set_member(alice.token,space.workspace_id,alice.principal_id,None)
    store.set_member(alice.token,space.workspace_id,bob.principal_id,None)
    with pytest.raises(DomainError):store.read_resource(bob.token,space.workspace_id,resource.version_id)


def test_restart_preserves_original_versions_tasks_and_pinned_scenario(persistent,package):
    path,store,alice,_,space,resource,_,_=persistent
    old=create_task(persistent,package)
    assert old['state']['status']=='prepared' and old['execution_enabled'] is False
    newer=store.save_text(alice.token,space.workspace_id,resource_id=resource.resource_id,display_name='Updated',text='updated synthetic text')
    assert newer.version_id!=resource.version_id
    restarted=SQLiteStore(path,clock=lambda:1000)
    reread=restarted.get_task(alice.token,space.workspace_id,old['snapshot']['task_id'])
    assert reread==old
    assert reread['snapshot']['resources'][0]['version_id']==resource.version_id
    assert restarted.read_resource(alice.token,space.workspace_id,resource.version_id)['text']=='  synthetic original\n'
    assert restarted.task_package(alice.token,space.workspace_id,old['snapshot']['task_id'])==package


def test_idempotency_replay_and_conflict(persistent,package):
    first=create_task(persistent,package)
    assert create_task(persistent,package)==first
    _,store,alice,_,space,resource,conv,_=persistent
    with pytest.raises(DomainError) as exc:
        store.create_task(alice.token,space.workspace_id,conv.conversation_id,question='different',package=package,bindings=(ResourceBinding(role='subject',version_id=resource.version_id),),request_key='request:1')
    assert exc.value.code=='idempotency_conflict'
    store.revoke_token(alice.token_id)
    with pytest.raises(DomainError):create_task(persistent,package)


def test_parallel_duplicate_requests_and_cancellation_use_one_record(persistent,package):
    with ThreadPoolExecutor(max_workers=2) as executor:
        records=list(executor.map(lambda _:create_task(persistent,package),range(2)))
    assert records[0]==records[1]
    _,store,alice,_,space,_,_,_=persistent
    task_id=records[0]['snapshot']['task_id']
    def cancel(_):
        try:return store.cancel_task(alice.token,space.workspace_id,task_id,1)['state']['status']
        except DomainError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes=list(executor.map(cancel,range(2)))
    assert sorted(outcomes)==['cancelled','stale_task_version']
    assert store.get_task(alice.token,space.workspace_id,task_id)['state']['version']==2


def test_cross_workspace_references_never_resolve(persistent,package):
    _,store,alice,bob,space,resource,conv,_=persistent
    other=store.create_workspace(bob.token,'Other')
    other_conv=store.create_conversation(bob.token,other.workspace_id)
    with pytest.raises(DomainError):store.read_resource(bob.token,other.workspace_id,resource.version_id)
    with pytest.raises(DomainError):store.create_task(bob.token,other.workspace_id,other_conv.conversation_id,question='x',package=package,bindings=(ResourceBinding(role='subject',version_id=resource.version_id),),request_key='cross')
    with pytest.raises(DomainError):store.create_task(alice.token,space.workspace_id,other_conv.conversation_id,question='x',package=package,bindings=(),request_key='cross')
    record=create_task(persistent,package)
    with pytest.raises(DomainError):store.get_task(bob.token,other.workspace_id,record['snapshot']['task_id'])


def test_withdrawn_resource_blocks_content_and_new_tasks_but_keeps_history(persistent,package):
    _,store,alice,_,space,resource,_,_=persistent
    old=create_task(persistent,package)
    store.withdraw_resource(alice.token,space.workspace_id,resource.resource_id)
    with pytest.raises(DomainError):store.read_resource(alice.token,space.workspace_id,resource.version_id)
    with pytest.raises(DomainError):create_task(persistent,package,'new-request')
    assert store.get_task(alice.token,space.workspace_id,old['snapshot']['task_id'])==old


def test_changed_scenario_requires_new_version_and_failed_admission_rolls_back(persistent,package):
    path,store,alice,_,space,_,conv,_=persistent
    create_task(persistent,package)
    changed=ScenarioPackage(manifest=package.manifest,instructions_text='changed',result_schema_json=package.result_schema_json,fingerprint=package_fingerprint(package.manifest,'changed',package.result_schema_json))
    with pytest.raises(DomainError) as exc:create_task(persistent,changed,'new-request')
    assert exc.value.code=='scenario_version_conflict'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM idempotency').fetchone()[0]==1


def test_database_content_tampering_is_detected(persistent,package):
    path,store,alice,_,space,resource,_,_=persistent
    with sqlite3.connect(path) as db:db.execute('UPDATE resource_versions SET content=? WHERE id=?',('changed',resource.version_id))
    with pytest.raises(DomainError) as exc:store.read_resource(alice.token,space.workspace_id,resource.version_id)
    assert exc.value.code=='integrity_error'


def test_invalid_token_lifetime_rolls_back_principal(tmp_path):
    path=tmp_path/'juli.sqlite3'
    migrations.migrate(path)
    store=SQLiteStore(path)
    with pytest.raises(DomainError):store.provision_principal('Invalid',ttl_days=0)
    with sqlite3.connect(path) as db:assert db.execute('SELECT COUNT(*) FROM principals').fetchone()[0]==0
