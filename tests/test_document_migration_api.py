import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from juli.adapters import sqlite_migrations as migrations
from juli.adapters.sqlite_store import SQLiteStore
from juli.api import create_app
from juli.core.common import DomainError, digest
from juli.core.resources import ResourceBinding, ResourceVersion


def legacy_database(path):
    """Construct a real populated revision-1 database, without calling current migrations."""
    token = 'synthetic-local-migration-token-long-enough'
    resource = ResourceVersion(resource_id='resource:old', version_id='version:old', workspace_id='workspace:old', media_type='text/plain', source='manual', display_name='Legacy synthetic', content_hash=digest(b'Legacy Python\r\n'))
    with sqlite3.connect(path) as db:
        for statement in migrations.DDL:
            db.execute(statement)
        db.execute('INSERT INTO schema_migrations VALUES (?,?)', (1, migrations.checksum()))
        db.execute(f'PRAGMA application_id={migrations.APPLICATION_ID}')
        db.execute('PRAGMA user_version=1')
        db.execute("INSERT INTO principals VALUES ('principal:old','Legacy',1,1)")
        db.execute("INSERT INTO tokens VALUES ('token:old','principal:old',?,9999999999,0,1)", (hashlib.sha256(token.encode()).hexdigest(),))
        db.execute("INSERT INTO workspaces VALUES ('workspace:old','principal:old','Legacy',1)")
        db.execute("INSERT INTO memberships VALUES ('workspace:old','principal:old','owner')")
        db.execute("INSERT INTO resources VALUES ('resource:old','workspace:old',1,1)")
        db.execute('INSERT INTO resource_versions VALUES (?,?,?,?,?,?,?)', ('version:old','resource:old','workspace:old',1,resource.model_dump_json(),'Legacy Python\r\n',1))
    return token, resource


def test_explicit_upgrade_preserves_data_and_builds_search_index(tmp_path, package):
    path = tmp_path / 'v1.sqlite3'
    token, resource = legacy_database(path)
    with pytest.raises(DomainError): SQLiteStore(path)
    assert migrations.migrate(path) == {'version': 3, 'changed': True}
    assert migrations.migrate(path) == {'version': 3, 'changed': False}
    store = SQLiteStore(path)
    assert store.read_original(token, resource.workspace_id, resource.version_id) == b'Legacy Python\r\n'
    assert store.read_resource(token, resource.workspace_id, resource.version_id)['resource'] == resource.model_dump(mode='json')
    conversation = store.create_conversation(token, resource.workspace_id)
    task = store.create_task(token, resource.workspace_id, conversation.conversation_id, question='Legacy sample', package=package, bindings=(ResourceBinding(role='subject',version_id=resource.version_id),),request_key='legacy')['snapshot']['task_id']
    assert store.search_evidence(token,resource.workspace_id,task,'Python')['items']


@pytest.mark.parametrize('failure', ['bad_ddl', 'bad_existing_text'])
def test_upgrade_failure_leaves_v1_data_and_schema_intact(tmp_path, monkeypatch, failure):
    path = tmp_path / 'v1.sqlite3'
    legacy_database(path)
    if failure == 'bad_ddl':
        monkeypatch.setattr(migrations, 'DDL_V2', migrations.DDL_V2 + ('NOT SQL',))
    else:
        with sqlite3.connect(path) as db: db.execute("UPDATE resource_versions SET content='changed'")
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    with pytest.raises((DomainError, sqlite3.Error)): migrations.migrate(path)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.fixture
def document_api(tmp_path, registry):
    path = tmp_path / 'api.sqlite3'
    migrations.migrate(path)
    store = SQLiteStore(path)
    identity = store.provision_principal('Synthetic API')
    space = store.create_workspace(identity.token, 'Files').workspace_id
    with TestClient(create_app(registry, database=path)) as client:
        yield store, identity, '/v1/workspaces/' + space, client


def test_binary_upload_scope_search_read_and_download(document_api):
    store, identity, base, client = document_api
    auth = {'Authorization': 'Bearer ' + identity.token}
    binary_auth = {**auth, 'Content-Type': 'application/octet-stream'}
    original = '  Python 原文\r\n'.encode()
    response = client.post(base + '/files?filename=sample.txt', headers=binary_auth, content=original)
    assert response.status_code == 201
    version = response.json()['resource']['version_id']
    prefix = base + '/resource-versions/' + version
    assert client.get(prefix + '/parse', headers=auth).json()['parse']['state'] == 'ready'
    downloaded = client.get(prefix + '/original', headers=auth)
    assert downloaded.content == original
    assert downloaded.headers['content-disposition'].startswith('attachment')
    conversation = client.post(base + '/conversations', headers=auth).json()['conversation_id']
    record = client.post(base + '/conversations/' + conversation + '/tasks', headers={**auth,'Idempotency-Key':'files'}, json={'question':'合成问题','scenario_id':'minimal','scenario_version':'0.1.0','bindings':[{'role':'subject','version_id':version}]}).json()
    task = base + '/tasks/' + record['snapshot']['task_id']
    found = client.post(task + '/evidence/search', headers=auth, json={'query':'Python'}).json()['items'][0]
    read = client.post(task + '/evidence/read', headers=auth, json={'evidence_id':found['evidence_id']})
    assert read.status_code == 200 and read.json()['items'][0]['text'] == original.decode()
    assert client.post(task + '/evidence/read', headers=auth, json={'evidence_id':found['evidence_id'], 'receipts':[]}).status_code == 422
    assert client.post(task + '/evidence/search', headers=auth, json={'query':'   '}).status_code == 422
    store.revoke_token(identity.token_id)
    assert client.get(prefix + '/original', headers=auth).status_code == 401
    assert client.post(task + '/evidence/read', headers=auth, json={'evidence_id':found['evidence_id']}).status_code == 401


def test_upload_limits_content_type_and_filename_checked_before_parser(document_api, monkeypatch):
    store, identity, base, client = document_api
    auth = {'Authorization':'Bearer ' + identity.token, 'Content-Type':'application/octet-stream'}
    def unexpected(*args, **kwargs):
        raise AssertionError('Rejected upload must not invoke parser')
    monkeypatch.setattr(store, 'save_file', unexpected)
    # The app owns a separate store; patching the class covers that instance too.
    monkeypatch.setattr(SQLiteStore, 'save_file', unexpected)
    assert client.post(base + '/files?filename=sample.txt', content=b'x').status_code == 401
    assert client.post(base + '/files?filename=../sample.txt', headers=auth, content=b'x').status_code == 422
    assert client.post(base + '/files?filename=sample.exe', headers=auth, content=b'x').status_code == 415
    assert client.post(base + '/files?filename=sample.txt', headers={**auth,'Content-Type':'multipart/form-data'}, content=b'x').status_code == 415
    assert client.post(base + '/files?filename=large.txt', headers=auth, content=(b'x' * 1024 * 1024 for _ in range(9))).status_code == 413


def test_failed_upload_is_visible_without_ready_task(document_api):
    _, identity, base, client = document_api
    auth = {'Authorization':'Bearer ' + identity.token, 'Content-Type':'application/octet-stream'}
    response = client.post(base + '/files?filename=bad.pdf', headers=auth, content=b'not a pdf')
    assert response.status_code == 201
    assert response.json()['resource']['status'] == 'failed'
    assert response.json()['parse']['error_code'] == 'invalid_document'
