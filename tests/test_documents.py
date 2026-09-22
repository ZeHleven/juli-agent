from io import BytesIO
import json
import sqlite3
import subprocess
from zipfile import ZipFile

import pytest

from juli.adapters import document_worker, sqlite_documents
from juli.adapters.document_index import evidence_chunks
from juli.adapters.document_parsers import assemble, parse_document
from juli.adapters.sqlite_migrations import migrate
from juli.adapters.sqlite_store import SQLiteStore
from juli.core.common import DomainError, digest
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.resources import ResourceBinding, ResourceVersion


def docx_bytes(body: str, prefix=''):
    output = BytesIO()
    xml = prefix + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>'
    with ZipFile(output, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', xml)
    return output.getvalue()


def pdf_bytes(texts, password=None):
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = pypdf.PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 40 720 Td (' + text + ') Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def document_store(tmp_path):
    path = tmp_path / 'documents.sqlite3'
    migrate(path)
    store = SQLiteStore(path)
    alice = store.provision_principal('Synthetic author')
    bob = store.provision_principal('Synthetic reader')
    space = store.create_workspace(alice.token, 'Synthetic documents').workspace_id
    conversation = store.create_conversation(alice.token, space).conversation_id
    return path, store, alice, bob, space, conversation


def task_for(setup, package, versions, key='documents:1'):
    _, store, alice, _, space, conversation = setup
    return store.create_task(alice.token, space, conversation, question='合成资料分析', package=package,
                             bindings=tuple(ResourceBinding(role='subject', version_id=version) for version in versions), request_key=key)['snapshot']['task_id']


def test_utf8_markdown_positions_and_overlap_preserve_extracted_content():
    text = '  开发经验 Python\r\n' + '中' * 3500 + '\n尾部  '
    parsed = parse_document('profile.MD', b'\xef\xbb\xbf' + text.encode())
    assert parsed.text == text
    resource = ResourceVersion(resource_id='r:1', version_id='v:1', workspace_id='w:1', media_type='text/markdown', display_name='Profile', content_hash=digest(text.encode()))
    chunks = list(evidence_chunks(resource, parsed))
    assert len(chunks) == 3
    assert chunks[0].locator.char_end - chunks[1].locator.char_start == 160
    for chunk in chunks:
        assert text[chunk.locator.char_start:chunk.locator.char_end] == chunk.text
    assert chunks[-1].locator.char_end == len(text)


def test_docx_body_table_paragraphs_and_tabs_have_positions():
    original = docx_bytes('<w:p><w:r><w:t>  Python</w:t><w:tab/><w:t>开发</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>SQL</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    parsed = parse_document('synthetic.docx', original)
    assert parsed.text == '  Python\t开发\nSQL\n'
    assert [s.paragraph for s in parsed.sections] == [1, 2]
    assert parsed.warnings


def test_pdf_pages_and_image_only_limitations_are_explicit():
    parsed = parse_document('synthetic.pdf', pdf_bytes(['Python experience', '', 'SQL experience']))
    assert [s.page for s in parsed.sections] == [1, 2, 3]
    assert 'Page 2' in parsed.warnings[0]
    with pytest.raises(DomainError) as error:
        parse_document('blank.pdf', pdf_bytes(['']))
    assert error.value.code == 'ocr_required'
    with pytest.raises(DomainError) as error:
        parse_document('secret.pdf', pdf_bytes(['test'], password='synthetic'))
    assert error.value.code == 'encrypted_pdf'


@pytest.mark.parametrize('filename,data,code', [
    ('../escape.txt', b'x', 'invalid_filename'), ('a.exe', b'x', 'unsupported_file'),
    ('a.txt', b'', 'file_size'), ('a.txt', b'\xff', 'invalid_encoding'),
    ('a.txt', b'a\0b', 'invalid_text'), ('a.pdf', b'not pdf', 'invalid_document'),
    ('a.docx', b'not zip', 'invalid_document'),
    ('a.txt', b'x' * 250001, 'extraction_limit'),
], ids=['path', 'extension', 'empty', 'encoding', 'nul', 'pdf-signature', 'docx-signature', 'text-limit'])
def test_invalid_documents_are_not_silently_accepted(filename, data, code):
    with pytest.raises(DomainError) as error:
        parse_document(filename, data)
    assert error.value.code == code


def test_docx_entities_and_duplicate_archive_members_are_rejected():
    malicious = docx_bytes('<w:p><w:r><w:t>&x;</w:t></w:r></w:p>', '<!DOCTYPE doc [<!ENTITY x SYSTEM "file:///not-read">]>')
    with pytest.raises(DomainError) as error:
        parse_document('entity.docx', malicious)
    assert error.value.code == 'unsafe_xml'
    output = BytesIO(docx_bytes('<w:p/>'))
    with pytest.warns(UserWarning):
        with ZipFile(output, 'a') as archive:
            archive.writestr('word/document.xml', 'duplicate')
    with pytest.raises(DomainError) as error:
        parse_document('duplicate.docx', output.getvalue())
    assert error.value.code == 'archive_limit'


def test_parser_subprocess_real_success_and_timeout_do_not_expose_environment(monkeypatch):
    parsed, error = document_worker.extract_file('real.txt', '  原文\n'.encode())
    assert error is None and parsed.text == '  原文\n'
    monkeypatch.setenv('SYNTHETIC_MODEL_KEY', 'synthetic-not-a-real-secret')
    def timeout(args, **kwargs):
        assert 'SYNTHETIC_MODEL_KEY' not in kwargs['env']
        assert kwargs['timeout'] == 15 and kwargs['stdin'] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(args, 15)
    monkeypatch.setattr(document_worker.subprocess, 'run', timeout)
    assert document_worker.extract_file('slow.txt', b'test') == (None, 'parser_timeout')


def test_upload_search_read_receipt_and_restart(document_store, package):
    path, store, alice, _, space, _ = document_store
    original = b'\xef\xbb\xbf' + '  Python 开发经验，SQL 数据分析。\r\n'.encode()
    saved = store.save_file(alice.token, space, filename='profile.md', data=original)
    version = saved['resource']['version_id']
    assert saved['parse']['state'] == 'ready'
    assert saved['resource']['content_hash'] == digest(original)
    assert store.read_original(alice.token, space, version) == original
    task = task_for(document_store, package, [version])
    result = store.search_evidence(alice.token, space, task, '开发 SQL')
    evidence_id = result['items'][0]['evidence_id']
    answer = AnalysisResult(task_id=task, blocks=(ResultBlock(kind='fact', text='资料记录了开发经验。', evidence_ids=(evidence_id,)),), scenario_payload_json='{"summary":"合成总结"}')
    with pytest.raises(DomainError) as error:
        store.validate_task_result(alice.token, space, task, answer)
    assert error.value.code == 'evidence_not_read'
    evidence = store.read_evidence(alice.token, space, task, evidence_id)['items'][0]
    assert evidence['text'] == original.decode('utf-8-sig')
    store.validate_task_result(alice.token, space, task, answer)
    restarted = SQLiteStore(path)
    assert restarted.search_evidence(alice.token, space, task, '开发 SQL') == result
    restarted.validate_task_result(alice.token, space, task, answer)


def test_search_excludes_unbound_other_workspace_and_new_versions(document_store, package):
    _, store, alice, bob, space, _ = document_store
    bound = store.save_text(alice.token, space, display_name='v1', text='Python original')
    hidden = store.save_text(alice.token, space, display_name='Unbound', text='Secretunique')
    task = task_for(document_store, package, [bound.version_id])
    store.save_text(alice.token, space, resource_id=bound.resource_id, display_name='v2', text='Newunique')
    assert store.search_evidence(alice.token, space, task, 'Secretunique')['items'] == []
    assert store.search_evidence(alice.token, space, task, 'Newunique')['items'] == []
    another_task = task_for(document_store, package, [hidden.version_id], 'other')
    hidden_evidence = store.search_evidence(alice.token, space, another_task, 'Secretunique')['items'][0]['evidence_id']
    with pytest.raises(DomainError): store.read_evidence(alice.token, space, task, hidden_evidence)
    other_space = store.create_workspace(bob.token, 'Other space').workspace_id
    with pytest.raises(DomainError): store.search_evidence(bob.token, other_space, task, 'Python')
    assert store.search_evidence(alice.token, space, task, 'Python')['items']


def test_receipts_are_principal_scoped_and_rechecked_after_withdrawal(document_store, package):
    _, store, alice, bob, space, _ = document_store
    version = store.save_text(alice.token, space, display_name='Manual', text='Python')
    task = task_for(document_store, package, [version.version_id])
    eid = store.search_evidence(alice.token, space, task, 'Python')['items'][0]['evidence_id']
    store.read_evidence(alice.token, space, task, eid)
    result = AnalysisResult(task_id=task, blocks=(ResultBlock(kind='fact', text='Synthetic fact', evidence_ids=(eid,)),), scenario_payload_json='{"summary":"test"}')
    store.set_member(alice.token, space, bob.principal_id, 'viewer')
    with pytest.raises(DomainError) as error: store.validate_task_result(bob.token, space, task, result)
    assert error.value.code == 'evidence_not_read'
    store.read_evidence(bob.token, space, task, eid)
    store.validate_task_result(bob.token, space, task, result)
    store.set_member(alice.token, space, bob.principal_id, None)
    with pytest.raises(DomainError): store.read_evidence(bob.token, space, task, eid)
    store.withdraw_resource(alice.token, space, version.resource_id)
    assert store.search_evidence(alice.token, space, task, 'Python')['items'] == []
    for operation in [lambda: store.read_evidence(alice.token, space, task, eid), lambda: store.validate_task_result(alice.token, space, task, result), lambda: store.read_original(alice.token, space, version.version_id)]:
        with pytest.raises(DomainError): operation()


def test_failed_parse_keeps_original_but_cannot_enter_task(document_store, package):
    _, store, alice, _, space, _ = document_store
    saved = store.save_file(alice.token, space, filename='bad.docx', data=b'broken archive')
    version = saved['resource']['version_id']
    assert saved['resource']['status'] == 'failed'
    assert saved['parse']['error_code'] == 'invalid_document'
    assert saved['parse']['chunk_count'] == 0
    assert store.read_original(alice.token, space, version) == b'broken archive'
    with pytest.raises(DomainError): task_for(document_store, package, [version])


def test_authorization_rechecked_after_parser_returns(document_store, monkeypatch):
    path, store, alice, bob, space, _ = document_store
    store.set_member(alice.token, space, bob.principal_id, 'editor')
    def revoke_during_parse(filename, data):
        store.set_member(alice.token, space, bob.principal_id, None)
        return parse_document(filename, data), None
    monkeypatch.setattr(sqlite_documents, 'extract_file', revoke_during_parse)
    with pytest.raises(DomainError): store.save_file(bob.token, space, filename='test.txt', data=b'not saved')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM document_originals').fetchone()[0] == 0


def test_evidence_position_tampering_does_not_create_a_receipt(document_store, package):
    path, store, alice, _, space, _ = document_store
    version = store.save_text(alice.token, space, display_name='Original', text='Python actual source')
    task = task_for(document_store, package, [version.version_id])
    eid = store.search_evidence(alice.token, space, task, 'Python')['items'][0]['evidence_id']
    with sqlite3.connect(path) as db:
        data = json.loads(db.execute('SELECT evidence_json FROM evidence_chunks WHERE id=?', (eid,)).fetchone()[0])
        data['locator']['page'] = 99
        db.execute('UPDATE evidence_chunks SET evidence_json=? WHERE id=?', (json.dumps(data), eid))
    with pytest.raises(DomainError) as error: store.read_evidence(alice.token, space, task, eid)
    assert error.value.code == 'integrity_error'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM evidence_reads').fetchone()[0] == 0
