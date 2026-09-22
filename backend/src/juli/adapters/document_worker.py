"""Bounded parser child process. This is resource isolation, not an OS security sandbox."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore

from juli.core.common import DomainError
from juli.core.documents import ParsedDocument
from .document_parsers import MAX_FILE_BYTES, media_type

_slots = BoundedSemaphore(2)


def extract_file(filename: str, data: bytes) -> tuple[ParsedDocument | None, str | None]:
    media_type(filename)
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise DomainError('file_size', 'Provide a nonempty file of at most 8 MiB.')
    if not _slots.acquire(blocking=False):
        raise DomainError('parser_busy', 'The local parsers are busy; retry later.')
    try:
        with tempfile.TemporaryDirectory(prefix='juli-parse-') as temporary:
            source, output = Path(temporary) / 'input', Path(temporary) / 'result.json'
            source.write_bytes(data)
            root = str(Path(__file__).resolve().parents[2])
            code = 'import sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); from juli.adapters.document_parsers import worker; worker()'
            environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG'}}
            try:
                result = subprocess.run([sys.executable, '-I', '-c', code, root, str(source), str(output), filename], cwd=temporary, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                return None, 'parser_timeout'
            except OSError:
                return None, 'parser_unavailable'
            if result.returncode or not output.exists() or output.stat().st_size > 4 * 1024 * 1024:
                return None, 'parser_failed'
            try:
                payload = json.loads(output.read_text(encoding='utf-8'))
                if 'parsed' in payload:
                    return ParsedDocument.model_validate(payload['parsed']), None
                return None, str(payload.get('error', 'parser_failed'))[:80]
            except (ValueError, TypeError):
                return None, 'parser_failed'
    finally:
        _slots.release()
