"""Bounded binary upload and task-scoped evidence access, without an Agent runtime."""
from fastapi import Depends, FastAPI, Query, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from juli.core.common import Contract, DomainError, Reference
from juli.adapters.document_parsers import MAX_FILE_BYTES, media_type


class SearchInput(Contract):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=10, ge=1, le=20, strict=True)


class ReadInput(Contract):
    evidence_id: Reference


def attach_document_routes(app: FastAPI, store, credential):
    @app.post('/v1/workspaces/{workspace_id}/files', status_code=201)
    async def upload(workspace_id: str, request: Request,
                     filename: str = Query(min_length=1, max_length=120),
                     resource_id: str | None = Query(default=None, max_length=160),
                     token: str = Depends(credential)):
        await run_in_threadpool(store.require_file_write, token, workspace_id, resource_id)
        media_type(filename)
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/octet-stream':
            raise DomainError('upload_content_type', 'Send raw bytes with application/octet-stream, not multipart or a URL.')
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_FILE_BYTES:
                raise DomainError('file_size', 'Files are limited to 8 MiB.')
            data.extend(chunk)
        return await run_in_threadpool(store.save_file, token, workspace_id, filename=filename, data=bytes(data), resource_id=resource_id)

    @app.get('/v1/workspaces/{workspace_id}/resource-versions/{version_id}/parse')
    def parse_info(workspace_id: str, version_id: str, token: str = Depends(credential)):
        return store.document_info(token, workspace_id, version_id)

    @app.get('/v1/workspaces/{workspace_id}/resource-versions/{version_id}/original')
    def original(workspace_id: str, version_id: str, token: str = Depends(credential)):
        return Response(content=store.read_original(token, workspace_id, version_id), media_type='application/octet-stream',
                        headers={'Content-Disposition': 'attachment; filename="original.bin"', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})

    @app.post('/v1/workspaces/{workspace_id}/tasks/{task_id}/evidence/search')
    def search(workspace_id: str, task_id: str, body: SearchInput, token: str = Depends(credential)):
        return store.search_evidence(token, workspace_id, task_id, body.query, limit=body.limit)

    @app.post('/v1/workspaces/{workspace_id}/tasks/{task_id}/evidence/read')
    def read(workspace_id: str, task_id: str, body: ReadInput, token: str = Depends(credential)):
        return store.read_evidence(token, workspace_id, task_id, body.evidence_id)
