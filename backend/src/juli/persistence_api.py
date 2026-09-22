"""Authenticated application endpoints; client payloads never contain authority objects."""
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Response
from pydantic import Field

from juli.adapters.sqlite_store import SQLiteStore
from juli.core.common import Contract, Name, Reference, Version
from juli.core.resources import ResourceBinding
from juli.core.scenarios import ScenarioRegistry


class WorkspaceInput(Contract):
    name: str = Field(min_length=1, max_length=120)


class MemberInput(Contract):
    role: Literal["editor", "viewer"]


class TextInput(Contract):
    display_name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=65536)


class TaskInput(Contract):
    question: str = Field(min_length=1, max_length=16000)
    scenario_id: Name
    scenario_version: Version
    bindings: tuple[ResourceBinding, ...] = Field(max_length=256)


class CancelInput(Contract):
    expected_version: int = Field(ge=1, strict=True)


def attach_persistence_routes(app: FastAPI, store: SQLiteStore, registry: ScenarioRegistry, credential, *, task_budget=None) -> None:
    @app.get("/v1/me")
    def me(token: str = Depends(credential)):
        return store.authenticate(token)

    @app.post("/v1/workspaces", status_code=201)
    def create_workspace(body: WorkspaceInput, token: str = Depends(credential)):
        return store.create_workspace(token, body.name)

    @app.get("/v1/workspaces")
    def workspaces(token: str = Depends(credential)):
        return {"items": store.list_workspaces(token), "limit": 1000}

    @app.put("/v1/workspaces/{workspace_id}/members/{principal_id}", status_code=204)
    def set_member(workspace_id: str, principal_id: str, body: MemberInput, token: str = Depends(credential)):
        store.set_member(token, workspace_id, principal_id, body.role)
        return Response(status_code=204)

    @app.delete("/v1/workspaces/{workspace_id}/members/{principal_id}", status_code=204)
    def remove_member(workspace_id: str, principal_id: str, token: str = Depends(credential)):
        store.set_member(token, workspace_id, principal_id, None)
        return Response(status_code=204)

    @app.get("/v1/workspaces/{workspace_id}/members")
    def members(workspace_id: str, token: str = Depends(credential)):
        return {"items": store.list_members(token, workspace_id)}

    @app.post("/v1/workspaces/{workspace_id}/resources", status_code=201)
    def create_resource(workspace_id: str, body: TextInput, token: str = Depends(credential)):
        return store.save_text(token, workspace_id, **body.model_dump())

    @app.post("/v1/workspaces/{workspace_id}/resources/{resource_id}/versions", status_code=201)
    def update_resource(workspace_id: str, resource_id: str, body: TextInput, token: str = Depends(credential)):
        return store.save_text(token, workspace_id, resource_id=resource_id, **body.model_dump())

    @app.get("/v1/workspaces/{workspace_id}/resources")
    def resources(workspace_id: str, token: str = Depends(credential)):
        return {"items": store.list_resources(token, workspace_id), "limit": 1000}

    @app.get("/v1/workspaces/{workspace_id}/resource-versions/{version_id}")
    def resource(workspace_id: str, version_id: str, token: str = Depends(credential)):
        return store.read_resource(token, workspace_id, version_id)

    @app.post("/v1/workspaces/{workspace_id}/resources/{resource_id}/withdraw", status_code=204)
    def withdraw(workspace_id: str, resource_id: str, token: str = Depends(credential)):
        store.withdraw_resource(token, workspace_id, resource_id)
        return Response(status_code=204)

    @app.post("/v1/workspaces/{workspace_id}/conversations", status_code=201)
    def create_conversation(workspace_id: str, token: str = Depends(credential)):
        return store.create_conversation(token, workspace_id)

    @app.get("/v1/workspaces/{workspace_id}/conversations")
    def conversations(workspace_id: str, token: str = Depends(credential)):
        return {"items": store.list_conversations(token, workspace_id), "limit": 1000}

    @app.post("/v1/workspaces/{workspace_id}/conversations/{conversation_id}/tasks", status_code=201)
    def create_task(workspace_id: str, conversation_id: str, body: TaskInput,
                    request_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")],
                    token: str = Depends(credential)):
        # Never accept resource metadata, scenario contents, principal or ACL from the caller.
        package = registry.get(body.scenario_id, body.scenario_version)
        return store.create_task(token, workspace_id, conversation_id, question=body.question, package=package, bindings=body.bindings, request_key=request_key, budget=task_budget)

    @app.get("/v1/workspaces/{workspace_id}/conversations/{conversation_id}/tasks")
    def tasks(workspace_id: str, conversation_id: str, token: str = Depends(credential)):
        return {"items": store.list_tasks(token, workspace_id, conversation_id), "limit": 1000}

    @app.get("/v1/workspaces/{workspace_id}/tasks/{task_id}")
    def task(workspace_id: str, task_id: str, token: str = Depends(credential)):
        return store.get_task(token, workspace_id, task_id)

    @app.post("/v1/workspaces/{workspace_id}/tasks/{task_id}/cancel")
    def cancel(workspace_id: str, task_id: str, body: CancelInput, token: str = Depends(credential)):
        return store.cancel_task(token, workspace_id, task_id, body.expected_version)
