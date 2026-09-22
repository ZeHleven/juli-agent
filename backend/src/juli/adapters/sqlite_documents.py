"""Document and evidence operations sharing the store's live transaction authority."""
import json
from uuid import uuid4

from pydantic import ValidationError

from juli.core.common import DomainError, digest
from juli.core.documents import ParsedDocument
from juli.core.evidence import AnalysisResult, Evidence, EvidenceReadReceipt, validate_result
from juli.core.resources import ResourceVersion
from juli.core.scenarios import ScenarioPackage
from juli.core.tasks import TaskSnapshot
from .document_index import insert_document, terms
from .document_parsers import media_type
from .document_worker import extract_file
from .sqlite_migrations import transaction


class DocumentStoreMixin:
    def model_task_context(self, token, workspace_id, task_id):
        """Server-owned context for a serial M04 session, never supplied by HTTP clients."""
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id, write=True)
            if db.execute('SELECT 1 FROM runtime_runs WHERE task_id=?', (task_id,)).fetchone():
                raise DomainError('durable_task', 'Use the durable runner for this task.')
            return self._model_context(db, workspace_id, task_id)

    def _model_context(self, db, workspace_id, task_id):
        record = self._task(db, workspace_id, task_id)
        if record['state']['status'] not in {'prepared', 'running'}:
            raise DomainError('task_not_active', 'This task cannot make new model or tool actions.')
        snapshot = TaskSnapshot.model_validate(record['snapshot'])
        for resource in snapshot.resources:
            current, _ = self._resource(db, workspace_id, resource.version_id)
            if current != resource:
                raise DomainError('integrity_error', 'A task resource no longer matches its snapshot.')
        package = ScenarioPackage.model_validate_json(db.execute('SELECT package_json FROM scenario_packages WHERE fingerprint=?', (snapshot.scenario_fingerprint,)).fetchone()[0])
        if package.fingerprint != snapshot.scenario_fingerprint:
            raise DomainError('integrity_error', 'The task scenario no longer matches its snapshot.')
        return snapshot, package, record['state']['version']

    def require_file_write(self, token: str, workspace_id: str, resource_id: str | None = None):
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id, write=True)
            self._file_target(db, workspace_id, resource_id)

    @staticmethod
    def _file_target(db, workspace_id, resource_id):
        if resource_id is not None and db.execute('SELECT id FROM resources WHERE id=? AND workspace_id=? AND active=1', (resource_id, workspace_id)).fetchone() is None:
            raise DomainError('not_found', 'An active resource in this workspace is required.')

    def save_file(self, token: str, workspace_id: str, *, filename: str, data: bytes, resource_id: str | None = None) -> dict:
        self.require_file_write(token, workspace_id, resource_id)
        kind = media_type(filename)
        parsed, error = extract_file(filename, data)  # No DB write lock while parsing.
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, write=True)  # Recheck after parsing.
            self._file_target(db, workspace_id, resource_id)
            if resource_id is None:
                resource_id = 'resource:' + uuid4().hex
                db.execute('INSERT INTO resources VALUES (?,?,1,?)', (resource_id, workspace_id, self.clock()))
            number = db.execute('SELECT COALESCE(MAX(version_number),0)+1 FROM resource_versions WHERE resource_id=?', (resource_id,)).fetchone()[0]
            resource = ResourceVersion(resource_id=resource_id, version_id='version:' + uuid4().hex,
                                       workspace_id=workspace_id, media_type=kind, source='file', display_name=filename,
                                       content_hash=digest(data), status='ready' if parsed else 'failed')
            db.execute('INSERT INTO resource_versions VALUES (?,?,?,?,?,?,?)', (resource.version_id, resource_id, workspace_id, number, resource.model_dump_json(), parsed.text if parsed else '', self.clock()))
            insert_document(db, resource, filename, data, parsed, error)
            self._audit(db, access.principal.principal_id, workspace_id, 'document.' + resource.status.value, resource.version_id)
            return {'resource': resource.model_dump(mode='json'), 'parse': self._parse_info(db, resource.version_id)}

    @staticmethod
    def _parse_info(db, version_id):
        row = db.execute('SELECT parser_version,state,error_code,parsed_json FROM document_parses WHERE version_id=?', (version_id,)).fetchone()
        if row is None:
            raise DomainError('integrity_error', 'The resource parsing record is missing.')
        parsed = ParsedDocument.model_validate_json(row[3]) if row[3] else None
        return {'parser_version': row[0], 'state': row[1], 'error_code': row[2], 'warnings': list(parsed.warnings) if parsed else [],
                'chunk_count': db.execute('SELECT COUNT(*) FROM evidence_chunks WHERE version_id=?', (version_id,)).fetchone()[0]}

    def document_info(self, token, workspace_id, version_id):
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            resource, _ = self._resource(db, workspace_id, version_id)
            return {'resource': resource.model_dump(mode='json'), 'parse': self._parse_info(db, version_id)}

    def read_original(self, token, workspace_id, version_id) -> bytes:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            self._resource(db, workspace_id, version_id)
            return bytes(db.execute('SELECT data FROM document_originals WHERE version_id=?', (version_id,)).fetchone()[0])

    def _evidence(self, db, workspace_id, snapshot, evidence_id) -> Evidence:
        row = db.execute('SELECT version_id,evidence_json FROM evidence_chunks WHERE id=?', (evidence_id,)).fetchone()
        sources = {r.version_id: r for r in snapshot.resources}
        if row is None or row[0] not in sources:
            raise DomainError('not_found', 'Evidence is not bound to this task.')
        resource, text = self._resource(db, workspace_id, row[0])
        try:
            evidence = Evidence.model_validate_json(row[1])
            parsed = ParsedDocument.model_validate_json(db.execute('SELECT parsed_json FROM document_parses WHERE version_id=?', (row[0],)).fetchone()[0])
        except (ValidationError, TypeError) as exc:
            raise DomainError('integrity_error', 'Stored evidence metadata is invalid.') from exc
        position = evidence.locator
        start, end = position.char_start, position.char_end
        matches_section = start is not None and end is not None and any(s.start <= start < end <= s.end and s.page == position.page and s.paragraph == position.paragraph for s in parsed.sections)
        expected_id = 'evidence:' + digest(f'{resource.version_id}|{parsed.parser_version}|{start}|{end}'.encode())
        if (not matches_section or evidence.evidence_id != evidence_id or evidence_id != expected_id or
                evidence.resource_version_id != resource.version_id or evidence.workspace_id != workspace_id or
                evidence.resource_content_hash != resource.content_hash or resource != sources[resource.version_id] or
                evidence.parser_version != parsed.parser_version or parsed.text != text or text[start:end] != evidence.text):
            raise DomainError('integrity_error', 'Evidence no longer matches its pinned source and position.')
        return evidence

    def search_evidence(self, token, workspace_id, task_id, query: str, *, limit: int = 10) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 4000 or type(limit) is not int or not 1 <= limit <= 20:
            raise DomainError('invalid_search', 'Provide a query of 1–4000 characters and a limit of 1–20.')
        query_terms = sorted(terms(query))
        if not query_terms or len(query_terms) > 64:
            raise DomainError('invalid_search', 'Use 1–64 lexical query terms.')
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            snapshot = TaskSnapshot.model_validate(self._task(db, workspace_id, task_id)['snapshot'])
            marks = ','.join('?' for _ in query_terms)
            rows = db.execute(f'''SELECT e.id,COUNT(DISTINCT t.term) AS score
                FROM evidence_terms t JOIN evidence_chunks e ON e.id=t.evidence_id
                JOIN task_resources b ON b.version_id=e.version_id
                JOIN resource_versions v ON v.id=e.version_id JOIN resources r ON r.id=v.resource_id
                WHERE b.task_id=? AND v.workspace_id=? AND r.active=1 AND t.term IN ({marks})
                GROUP BY e.id ORDER BY score DESC,e.id LIMIT ?''', (task_id, workspace_id, *query_terms, limit)).fetchall()
            items = []
            for row in rows:
                evidence = self._evidence(db, workspace_id, snapshot, row[0])
                # Candidate only. No receipt is created by search.
                items.append({'evidence_id': evidence.evidence_id, 'resource_version_id': evidence.resource_version_id,
                              'snippet': evidence.text[:2000]})
            return {'items': items}

    def read_evidence(self, token, workspace_id, task_id, evidence_id) -> dict:
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id)
            snapshot = TaskSnapshot.model_validate(self._task(db, workspace_id, task_id)['snapshot'])
            evidence = self._evidence(db, workspace_id, snapshot, evidence_id)
            db.execute('INSERT INTO evidence_reads VALUES (?,?,?,?,?) ON CONFLICT(task_id,evidence_id,principal_id) DO UPDATE SET content_hash=excluded.content_hash,created_at=excluded.created_at',
                       (task_id, evidence_id, access.principal.principal_id, evidence.content_hash, self.clock()))
            return {'items': [evidence.model_dump(mode='json')]}

    def validate_task_result(self, token, workspace_id, task_id, result: AnalysisResult) -> None:
        """Server-side integration boundary for M04; caller cannot submit receipts or sources."""
        with transaction(self.path) as db:
            access = self._authorize(db, token, workspace_id)
            self._validate_result(db, access, workspace_id, task_id, result)

    def _validate_result(self, db, access, workspace_id, task_id, result):
        snapshot = TaskSnapshot.model_validate(self._task(db, workspace_id, task_id)['snapshot'])
        referenced = sorted({ref for block in result.blocks for ref in block.evidence_ids})
        evidence = tuple(self._evidence(db, workspace_id, snapshot, ref) for ref in referenced)
        receipts = tuple(EvidenceReadReceipt(task_id=task_id, evidence_id=r[0], content_hash=r[1])
                         for r in db.execute('SELECT evidence_id,content_hash FROM evidence_reads WHERE task_id=? AND principal_id=?', (task_id, access.principal.principal_id)))
        package = ScenarioPackage.model_validate_json(db.execute('SELECT package_json FROM scenario_packages WHERE fingerprint=?', (snapshot.scenario_fingerprint,)).fetchone()[0])
        validate_result(snapshot=snapshot, package=package, result=result, evidence=evidence, receipts=receipts, access=access)
