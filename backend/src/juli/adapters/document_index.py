"""Deterministic positions and a small, portable lexical index."""
import json
import re
import unicodedata

from juli.core.common import digest, DomainError
from juli.core.documents import ParsedDocument
from juli.core.evidence import Evidence, EvidenceLocator
from juli.core.resources import ResourceVersion
from .document_parsers import assemble

CHUNK_CHARS = 1600
OVERLAP = 160


def terms(text: str) -> set[str]:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    found = set()
    for part in re.findall(r'[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+', normalized):
        if '\u3400' <= part[0] <= '\u9fff':
            found.update(part)
            found.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            found.add(part[:80])
    return found


def evidence_chunks(resource: ResourceVersion, parsed: ParsedDocument):
    for section in parsed.sections:
        start = section.start
        while start < section.end:
            end = min(start + CHUNK_CHARS, section.end)
            text = parsed.text[start:end]
            if text.strip():
                evidence_id = 'evidence:' + digest(f'{resource.version_id}|{parsed.parser_version}|{start}|{end}'.encode())
                yield Evidence(evidence_id=evidence_id, workspace_id=resource.workspace_id,
                               resource_version_id=resource.version_id, resource_content_hash=resource.content_hash,
                               parser_version=parsed.parser_version,
                               locator=EvidenceLocator(page=section.page, paragraph=section.paragraph, char_start=start, char_end=end),
                               text=text, content_hash=digest(text.encode('utf-8')))
            if end == section.end:
                break
            start = end - OVERLAP


def insert_document(db, resource: ResourceVersion, filename: str, original: bytes,
                    parsed: ParsedDocument | None, error: str | None) -> None:
    db.execute('INSERT INTO document_originals VALUES (?,?,?)', (resource.version_id, filename, original))
    text = parsed.text if parsed else ''
    db.execute('INSERT INTO document_parses VALUES (?,?,?,?,?,?)',
               (resource.version_id, parsed.parser_version if parsed else 'unparsed-v1',
                'ready' if parsed else 'failed', error, digest(text.encode()), parsed.model_dump_json() if parsed else None))
    if parsed:
        for evidence in evidence_chunks(resource, parsed):
            db.execute('INSERT INTO evidence_chunks VALUES (?,?,?)', (evidence.evidence_id, resource.version_id, evidence.model_dump_json()))
            db.executemany('INSERT INTO evidence_terms VALUES (?,?)', [(term, evidence.evidence_id) for term in sorted(terms(evidence.text))])


def backfill_v1(db):
    # M02 resources are UTF-8 manual text. Abort rather than repair inconsistent records.
    for row in db.execute('SELECT id,descriptor_json,content FROM resource_versions').fetchall():
        resource = ResourceVersion.model_validate_json(row[1])
        data = row[2].encode('utf-8')
        if resource.version_id != row[0] or resource.content_hash != digest(data):
            raise DomainError('integrity_error', 'Existing text does not match its M02 version; migration stopped.')
        parsed = assemble([(row[2], None, None)], 'manual-text-v1')
        insert_document(db, resource, resource.display_name, data, parsed, None)
