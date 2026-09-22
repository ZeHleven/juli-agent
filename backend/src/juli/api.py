"""Foundation API with an optional, explicitly initialized local store."""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse

from juli import __version__
from juli.core.common import DomainError
from juli.core.evidence import AnalysisResult, Evidence
from juli.core.resources import ResourceBinding, ResourceVersion, Workspace
from juli.core.scenarios import ScenarioManifest, ScenarioRegistry
from juli.core.tasks import Conversation, TaskBudget, TaskSnapshot, TaskState
from juli.core.tools import ToolDefinition

CONTRACTS = {model.__name__:model for model in (Workspace,ResourceVersion,ResourceBinding,Conversation,TaskSnapshot,TaskState,Evidence,ToolDefinition,ScenarioManifest,AnalysisResult)}


def create_app(registry: ScenarioRegistry, *, api_token: str | None = None, database: Path | None = None, runtime=None, task_budget: TaskBudget | None = None) -> FastAPI:
    if runtime is not None and database is None:
        raise ValueError('Runtime execution requires an explicitly initialized database.')
    if task_budget is not None:
        if database is None:
            raise ValueError('Task budget configuration requires a database deployment.')
        task_budget = TaskBudget.model_validate(task_budget)
    if database is not None and api_token is not None:
        raise ValueError("Choose database authentication or read-only catalog authentication, never both.")
    store = None
    if database is not None:
        from juli.adapters.sqlite_store import SQLiteStore
        store = SQLiteStore(database)
    elif not api_token or len(api_token) < 24 or not api_token.isascii() or any(c.isspace() for c in api_token):
        raise ValueError("Set a non-whitespace ASCII API token of at least 24 characters.")
    app = FastAPI(title="据理基础合同 API", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    @app.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response
    bearer = HTTPBearer(auto_error=False)

    def credential(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        supplied = credentials.credentials if credentials else ""
        if store is not None:
            store.authenticate(supplied)
        elif not hmac.compare_digest(supplied.encode("utf-8"), api_token.encode("ascii")):
            raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate":"Bearer"})
        return supplied

    @app.exception_handler(DomainError)
    async def domain_error(_request, error: DomainError):
        status = {"unauthenticated": 401, "access_denied": 403, "not_found": 404,
                  "scenario_not_found": 404, "stale_task_version": 409, "invalid_transition": 409,
                  "idempotency_conflict": 409, "scenario_version_conflict": 409,
                  "storage_conflict": 409, "storage_unavailable": 503,
                  "runtime_owner_required": 403, "runtime_scenario_disabled": 403,
                  "runtime_busy": 409, "conversation_busy": 409, "stale_runtime_version": 409,
                  "runtime_config_changed": 409, "action_fenced": 409, "runtime_disabled": 503,
                  "database_not_ready": 503, "migration_mismatch": 503, "integrity_error": 503,
                  "parser_busy": 503, "file_size": 413, "unsupported_file": 415, "upload_content_type": 415}.get(error.code, 422)
        return JSONResponse(status_code=status, content={"error": error.code, "message": str(error)},
                            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None)

    @app.get("/health")
    def health() -> dict:
        if store is not None:
            from juli.adapters.sqlite_migrations import transaction
            with transaction(store.path):
                pass
        return {"status":"ok","version":__version__,"stage":"local_persistence" if store else "contracts_and_registry",
                "analysis_enabled":runtime is not None and runtime.mode != 'demo',
                "execution_enabled":runtime is not None, "runtime_mode":runtime.mode if runtime else 'disabled'}

    @app.get("/v1/scenarios", dependencies=[Depends(credential)])
    def scenarios() -> dict:
        return {"items":[{"manifest":p.manifest.model_dump(mode="json"),"fingerprint":p.fingerprint} for p in registry.list()],"execution_enabled":False}

    @app.get("/v1/contracts/{name}", dependencies=[Depends(credential)])
    def schema(name: str) -> dict:
        if name not in CONTRACTS:
            raise HTTPException(status_code=404, detail="Contract not found")
        return CONTRACTS[name].model_json_schema()

    if store is not None:
        from juli.persistence_api import attach_persistence_routes
        attach_persistence_routes(app, store, registry, credential, task_budget=task_budget)
        from juli.document_api import attach_document_routes
        attach_document_routes(app, store, credential)
        from juli.runtime_api import attach_runtime_routes
        attach_runtime_routes(app, store, credential, runtime)
        from juli.workbench import attach_workbench
        attach_workbench(app)
    return app
