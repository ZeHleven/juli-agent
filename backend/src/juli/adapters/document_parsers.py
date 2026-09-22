"""Local extraction only: no links, macros, OCR, model calls, or shell programs."""
from io import BytesIO
import json
from pathlib import Path
import sys
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from juli.core.common import DomainError
from juli.core.documents import MAX_TEXT_CHARS, ParsedDocument, TextSection

MAX_FILE_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {
    '.txt': 'text/plain', '.md': 'text/markdown', '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}


def media_type(filename: str) -> str:
    if not filename or len(filename) > 120 or any(ord(c) < 32 or c in '/\\:' for c in filename):
        raise DomainError('invalid_filename', 'Use a display filename without a path.')
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise DomainError('unsupported_file', 'Supported extensions: .txt, .md, .docx, .pdf.')
    return MEDIA_TYPES[suffix]


def assemble(parts, parser_version: str, warnings=()) -> ParsedDocument:
    texts, sections, offset = [], [], 0
    for text, page, paragraph in parts:
        if not text:
            continue
        if offset + len(text) > MAX_TEXT_CHARS or len(sections) >= 10000:
            raise DomainError('extraction_limit', 'Extracted text exceeds local limits.')
        sections.append(TextSection(start=offset, end=offset + len(text), page=page, paragraph=paragraph))
        texts.append(text)
        offset += len(text)
    if not ''.join(texts).strip():
        raise DomainError('no_text', 'No extractable text was found.')
    return ParsedDocument(parser_version=parser_version, text=''.join(texts), sections=tuple(sections), warnings=tuple(warnings))


def parse_text(data: bytes) -> ParsedDocument:
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError as exc:
        raise DomainError('invalid_encoding', 'Text files must use UTF-8.') from exc
    if '\x00' in text:
        raise DomainError('invalid_text', 'NUL characters are not accepted.')
    return assemble([(text, None, None)], 'utf8-text-v1')


def parse_docx(data: bytes) -> ParsedDocument:
    namespace = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 2000 or len(set(names)) != len(names) or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                raise DomainError('archive_limit', 'The document archive exceeds local limits.')
            if any(e.flag_bits & 1 or e.file_size > 1000 * max(e.compress_size, 1) for e in entries):
                raise DomainError('archive_limit', 'Encrypted or excessively compressed documents are not accepted.')
            if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                raise DomainError('invalid_document', 'A DOCX document body is required.')
            if any('vbaproject' in name.lower() for name in names):
                raise DomainError('unsupported_document', 'Macro-bearing documents are not accepted.')
            if archive.getinfo('word/document.xml').file_size > 8 * 1024 * 1024:
                raise DomainError('archive_limit', 'The document XML exceeds local limits.')
            xml = archive.read('word/document.xml')
        # Reject declarations, including UTF-16 representations, before XML parsing.
        scan = xml.replace(b'\x00', b'').upper()
        if b'<!DOCTYPE' in scan or b'<!ENTITY' in scan:
            raise DomainError('unsafe_xml', 'Document type and entity declarations are not accepted.')
        tree = ElementTree.fromstring(xml)
        body = tree.find(namespace + 'body')
        if body is None:
            raise DomainError('invalid_document', 'A DOCX document body is required.')
        parts = []
        for index, paragraph in enumerate(body.iter(namespace + 'p'), 1):
            fragments = []
            for node in paragraph.iter():
                if node.tag == namespace + 't':
                    fragments.append(node.text or '')
                elif node.tag == namespace + 'tab':
                    fragments.append('\t')
                elif node.tag in {namespace + 'br', namespace + 'cr'}:
                    fragments.append('\n')
            parts.append((''.join(fragments) + '\n', None, index))
        return assemble(parts, 'docx-body-v1', ['Body paragraphs and table-cell paragraphs only; headers, footnotes, images and layout are not extracted.'])
    except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError) as exc:
        raise DomainError('invalid_document', 'The DOCX document cannot be parsed.') from exc


def parse_pdf(data: bytes) -> ParsedDocument:
    if not data.startswith(b'%PDF-'):
        raise DomainError('invalid_document', 'A PDF signature is required.')
    try:
        import pypdf
    except ImportError as exc:
        raise DomainError('parser_unavailable', 'Install the documents extra to parse PDF files.') from exc
    reader = pypdf.PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise DomainError('encrypted_pdf', 'Encrypted PDFs are not supported.')
    if len(reader.pages) > 200:
        raise DomainError('page_limit', 'PDF files are limited to 200 pages.')
    parts, warnings, count = [], [], 0
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ''
        count += len(text) + 1
        if count > MAX_TEXT_CHARS:
            raise DomainError('extraction_limit', 'Extracted text exceeds local limits.')
        if not text.strip():
            warnings.append(f'Page {index} has no extracted text; image content was not analyzed.')
        parts.append((text + '\n', index, None))
    if not any(text.strip() for text, _, _ in parts):
        raise DomainError('ocr_required', 'This PDF has no extractable text; OCR is not implemented.')
    return assemble(parts, 'pypdf-' + pypdf.__version__ + '-text-v1', warnings)


def parse_document(filename: str, data: bytes) -> ParsedDocument:
    kind = media_type(filename)
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise DomainError('file_size', 'Provide a nonempty file of at most 8 MiB.')
    if kind == 'application/pdf':
        return parse_pdf(data)
    if filename.lower().endswith('.docx'):
        return parse_docx(data)
    return parse_text(data)


def worker() -> None:
    # Fixed local temporary paths provided by the parent, never user-selected paths.
    source, output, filename = sys.argv[2:5]
    try:
        parsed = parse_document(filename, Path(source).read_bytes())
        result = {'parsed': parsed.model_dump(mode='json')}
    except DomainError as error:
        result = {'error': error.code}
    except Exception:
        result = {'error': 'invalid_document'}
    Path(output).write_text(json.dumps(result, ensure_ascii=True), encoding='utf-8')
