"""Single-node local store. Every operation resolves current authority in its transaction."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Callable
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from juli.core.common import DomainError, Reference, canonical_json, digest
from juli.core.resources import AccessContext, Principal, ResourceBinding, ResourceVersion, Workspace
from juli.core.scenarios import ScenarioPackage
from juli.core.tasks import Conversation, TaskBudget, TaskSnapshot, TaskState, TaskStatus, admit_task, transition_task
from .sqlite_migrations import transaction
from .sqlite_documents import DocumentStoreMixin
from .document_index import insert_document
from .document_parsers import assemble


def new_id(kind: str) -> str:
    return f"{kind}:{uuid4().hex}"


def require_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise DomainError("invalid_name", "Names must contain 1–120 characters.")
    return value.strip()


@dataclass(frozen=True)
class IssuedToken:
    principal_id: str
    token_id: str
    expires_at: float
    token: str = field(repr=False)


class SQLiteStore(DocumentStoreMixin):
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path).resolve()
        self.clock = clock
        with transaction(self.path):
            pass  # Verify only. Never create or migrate here.

    def _audit(self, db, principal_id, workspace_id, action, target_id):
        db.execute("INSERT INTO audit_events(principal_id,workspace_id,action,target_id,created_at) VALUES (?,?,?,?,?)", (principal_id, workspace_id, action, target_id, self.clock()))

    def _issue(self, db, principal_id: str, ttl_days: int) -> IssuedToken:
        if type(ttl_days) is not int or not 1 <= ttl_days <= 90:
            raise DomainError("invalid_token_lifetime", "Token lifetime must be 1–90 days.")
        row = db.execute("SELECT enabled FROM principals WHERE id=?", (principal_id,)).fetchone()
        if row is None or not row[0]:
            raise DomainError("principal_unavailable", "An enabled local principal is required.")
        token = "juli_" + secrets.token_urlsafe(32)
        token_id = new_id("token")
        expires = self.clock() + ttl_days * 86400
        db.execute("INSERT INTO tokens(id,principal_id,token_hash,expires_at,created_at) VALUES (?,?,?,?,?)", (token_id, principal_id, hashlib.sha256(token.encode()).hexdigest(), expires, self.clock()))
        self._audit(db, principal_id, None, "token.issued", token_id)
        return IssuedToken(principal_id, token_id, expires, token)

    # Local administrative methods: exposed by CLI only, not HTTP endpoints.
    def provision_principal(self, name: str, *, ttl_days: int = 30) -> IssuedToken:
        name = require_name(name)
        with transaction(self.path, write=True) as db:
            principal_id = new_id("principal")
            db.execute("INSERT INTO principals VALUES (?,?,1,?)", (principal_id, name, self.clock()))
            return self._issue(db, principal_id, ttl_days)

    def issue_token(self, principal_id: str, *, ttl_days: int = 30) -> IssuedToken:
        with transaction(self.path, write=True) as db:
            return self._issue(db, principal_id, ttl_days)

    def revoke_token(self, token_id: str) -> None:
        with transaction(self.path, write=True) as db:
            row = db.execute("SELECT principal_id FROM tokens WHERE id=?", (token_id,)).fetchone()
            if row is None:
                raise DomainError("not_found", "Token not found.")
            db.execute("UPDATE tokens SET revoked=1 WHERE id=?", (token_id,))
            self._audit(db, row[0], None, "token.revoked", token_id)

    def set_principal_enabled(self, principal_id: str, enabled: bool) -> None:
        with transaction(self.path, write=True) as db:
            if not db.execute("UPDATE principals SET enabled=? WHERE id=?", (int(enabled), principal_id)).rowcount:
                raise DomainError("not_found", "Principal not found.")
            if not enabled:
                db.execute("UPDATE tokens SET revoked=1 WHERE principal_id=?", (principal_id,))
            self._audit(db, principal_id, None, "principal.enabled" if enabled else "principal.disabled", principal_id)

    def _identity(self, db, token: str) -> Principal:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise DomainError("unauthenticated", "A valid personal access token is required.")
        row = db.execute("SELECT p.id FROM tokens t JOIN principals p ON p.id=t.principal_id WHERE t.token_hash=? AND t.revoked=0 AND t.expires_at>? AND p.enabled=1", (hashlib.sha256(token.encode()).hexdigest(), self.clock())).fetchone()
        if row is None:
            raise DomainError("unauthenticated", "A valid personal access token is required.")
        return Principal(principal_id=row[0])

    def authenticate(self, token: str) -> Principal:
        with transaction(self.path) as db:
            return self._identity(db, token)

    def _authorize(self, db, token: str, workspace_id: str, *, write=False, owner=False) -> AccessContext:
        principal = self._identity(db, token)
        row = db.execute("SELECT role FROM memberships WHERE workspace_id=? AND principal_id=?", (workspace_id, principal.principal_id)).fetchone()
        if row is None or (write and row[0] == "viewer") or (owner and row[0] != "owner"):
            raise DomainError("access_denied", "Workspace permission is required for this operation.")
        return AccessContext(principal=principal, workspace_ids=(workspace_id,))

    def create_workspace(self, token: str, name: str) -> Workspace:
        name = require_name(name)
        with transaction(self.path, write=True) as db:
            principal = self._identity(db, token)
            workspace = Workspace(workspace_id=new_id("workspace"), owner_id=principal.principal_id, name=name)
            db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", (workspace.workspace_id, workspace.owner_id, workspace.name, self.clock()))
            db.execute("INSERT INTO memberships VALUES (?,?,'owner')", (workspace.workspace_id, workspace.owner_id))
            self._audit(db, workspace.owner_id, workspace.workspace_id, "workspace.created", workspace.workspace_id)
            return workspace

    def list_workspaces(self, token: str) -> list[dict]:
        with transaction(self.path) as db:
            principal = self._identity(db, token)
            rows = db.execute("SELECT w.id AS workspace_id,w.owner_id,w.name,m.role FROM workspaces w JOIN memberships m ON m.workspace_id=w.id WHERE m.principal_id=? ORDER BY w.created_at,w.id LIMIT 1000", (principal.principal_id,)).fetchall()
            return [dict(row) for row in rows]

    def set_member(self, token: str, workspace_id: str, principal_id: str, role: str | None) -> None:
        if role not in {None, "editor", "viewer"}:
            raise DomainError("invalid_role", "Members may be editors or viewers.")
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, owner=True)
            owner = db.execute("SELECT owner_id FROM workspaces WHERE id=?", (workspace_id,)).fetchone()[0]
            if principal_id == owner:
                raise DomainError("owner_protected", "Ownership cannot be changed through member editing.")
            target = db.execute("SELECT enabled FROM principals WHERE id=?", (principal_id,)).fetchone()
            if target is None or (role is not None and not target[0]):
                raise DomainError("principal_unavailable", "An enabled local principal is required.")
            if role is None:
                db.execute("DELETE FROM memberships WHERE workspace_id=? AND principal_id=?", (workspace_id, principal_id))
            else:
                db.execute("INSERT INTO memberships VALUES (?,?,?) ON CONFLICT(workspace_id,principal_id) DO UPDATE SET role=excluded.role", (workspace_id, principal_id, role))
            self._audit(db, access.principal.principal_id, workspace_id, "member." + (role or "removed"), principal_id)

    def list_members(self, token: str, workspace_id: str) -> list[dict]:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id, owner=True)
            return [dict(r) for r in db.execute("SELECT principal_id,role FROM memberships WHERE workspace_id=? ORDER BY principal_id", (workspace_id,))]

    def save_text(self, token: str, workspace_id: str, *, display_name: str, text: str, resource_id: str | None = None) -> ResourceVersion:
        display_name = require_name(display_name)
        try:
            encoded = text.encode("utf-8") if isinstance(text, str) else b""
        except UnicodeError as exc:
            raise DomainError("invalid_text", "Text must be valid UTF-8.") from exc
        if not isinstance(text, str) or not text.strip() or '\x00' in text or len(text) > 65536 or len(encoded) > 262144:
            raise DomainError("invalid_text", "Provide nonempty text of at most 64K characters / 256 KiB.")
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, write=True)
            if resource_id is None:
                resource_id = new_id("resource")
                db.execute("INSERT INTO resources VALUES (?,?,1,?)", (resource_id, workspace_id, self.clock()))
            elif db.execute("SELECT id FROM resources WHERE id=? AND workspace_id=? AND active=1", (resource_id, workspace_id)).fetchone() is None:
                raise DomainError("not_found", "An active resource in this workspace is required.")
            version_number = db.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM resource_versions WHERE resource_id=?", (resource_id,)).fetchone()[0]
            resource = ResourceVersion(resource_id=resource_id, version_id=new_id("version"), workspace_id=workspace_id, media_type="text/plain", source="manual", display_name=display_name, content_hash=digest(text.encode("utf-8")))
            db.execute("INSERT INTO resource_versions VALUES (?,?,?,?,?,?,?)", (resource.version_id, resource_id, workspace_id, version_number, resource.model_dump_json(), text, self.clock()))
            parsed = assemble([(text, None, None)], 'manual-text-v1')
            insert_document(db, resource, display_name, encoded, parsed, None)
            self._audit(db, access.principal.principal_id, workspace_id, "resource.version_created", resource.version_id)
            return resource

    def _resource(self, db, workspace_id: str, version_id: str):
        row = db.execute("SELECT v.descriptor_json,v.content FROM resource_versions v JOIN resources r ON r.id=v.resource_id WHERE v.id=? AND v.workspace_id=? AND r.active=1", (version_id, workspace_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "An active resource version in this workspace is required.")
        try:
            descriptor = ResourceVersion.model_validate_json(row[0])
        except ValidationError as exc:
            raise DomainError("integrity_error", "Stored resource metadata is invalid.") from exc
        if descriptor.version_id != version_id or descriptor.workspace_id != workspace_id:
            raise DomainError("integrity_error", "Stored resource scope is inconsistent.")
        document = db.execute('SELECT o.data,p.extracted_hash,p.state FROM document_originals o JOIN document_parses p ON p.version_id=o.version_id WHERE o.version_id=?', (version_id,)).fetchone()
        if document is None or descriptor.content_hash != digest(bytes(document[0])) or digest(row[1].encode('utf-8')) != document[1] or descriptor.status.value != document[2]:
            raise DomainError("integrity_error", "Stored original, text or parsing status no longer matches its version.")
        return descriptor, row[1]

    def read_resource(self, token: str, workspace_id: str, version_id: str) -> dict:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            descriptor, text = self._resource(db, workspace_id, version_id)
            return {"resource": descriptor.model_dump(mode="json"), "text": text}

    def list_resources(self, token: str, workspace_id: str) -> list[dict]:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            rows = db.execute("SELECT v.descriptor_json,v.version_number FROM resource_versions v JOIN resources r ON r.id=v.resource_id WHERE v.workspace_id=? AND r.active=1 ORDER BY v.created_at,v.id LIMIT 1000", (workspace_id,))
            return [{"resource": ResourceVersion.model_validate_json(r[0]).model_dump(mode="json"), "version_number": r[1]} for r in rows]

    def withdraw_resource(self, token: str, workspace_id: str, resource_id: str) -> None:
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, write=True)
            if not db.execute("UPDATE resources SET active=0 WHERE id=? AND workspace_id=?", (resource_id, workspace_id)).rowcount:
                raise DomainError("not_found", "Resource not found.")
            self._audit(db, access.principal.principal_id, workspace_id, "resource.withdrawn", resource_id)

    def create_conversation(self, token: str, workspace_id: str) -> Conversation:
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, write=True)
            conversation = Conversation(conversation_id=new_id("conversation"), workspace_id=workspace_id, created_by=access.principal.principal_id)
            db.execute("INSERT INTO conversations VALUES (?,?,?,?)", (conversation.conversation_id, workspace_id, conversation.created_by, self.clock()))
            self._audit(db, access.principal.principal_id, workspace_id, "conversation.created", conversation.conversation_id)
            return conversation

    def list_conversations(self, token: str, workspace_id: str) -> list[dict]:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            return [dict(r) for r in db.execute("SELECT id AS conversation_id,workspace_id,created_by FROM conversations WHERE workspace_id=? ORDER BY created_at,id LIMIT 1000", (workspace_id,))]

    def _task(self, db, workspace_id, task_id):
        row = db.execute("SELECT snapshot_json,snapshot_hash,state_json,state_version FROM tasks WHERE id=? AND workspace_id=?", (task_id, workspace_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Task not found in this workspace.")
        try:
            snapshot = TaskSnapshot.model_validate_json(row[0])
            state = TaskState.model_validate_json(row[2])
        except ValidationError as exc:
            raise DomainError("integrity_error", "Stored task records are invalid.") from exc
        if snapshot.fingerprint != row[1] or state.version != row[3] or snapshot.task_id != state.task_id or snapshot.task_id != task_id or snapshot.workspace_id != workspace_id or state.workspace_id != workspace_id:
            raise DomainError("integrity_error", "Stored task records are inconsistent.")
        return {"snapshot": snapshot.model_dump(mode="json"), "state": state.model_dump(mode="json"), "execution_enabled": False}

    def create_task(self, token: str, workspace_id: str, conversation_id: str, *, question: str, package: ScenarioPackage, bindings: tuple[ResourceBinding, ...], request_key: str, budget: TaskBudget | None = None) -> dict:
        TypeAdapter(Reference).validate_python(request_key)
        # A deployment-owned budget is frozen only on first creation. Retrying the same
        # user request after a config change must return its original task and budget.
        request_hash = digest(canonical_json({"question": question, "scenario": package.fingerprint, "bindings": sorted((b.role, b.version_id) for b in bindings)}).encode())
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, write=True)
            row = db.execute("SELECT created_by FROM conversations WHERE id=? AND workspace_id=?", (conversation_id, workspace_id)).fetchone()
            if row is None:
                raise DomainError("not_found", "Conversation not found in this workspace.")
            scope = f"task:{workspace_id}:{conversation_id}"
            replay = db.execute("SELECT request_hash,task_id FROM idempotency WHERE principal_id=? AND scope=? AND request_key=?", (access.principal.principal_id, scope, request_key)).fetchone()
            if replay:
                if replay[0] != request_hash:
                    raise DomainError("idempotency_conflict", "This request key was already used with different input.")
                return self._task(db, workspace_id, replay[1])
            conversation = Conversation(conversation_id=conversation_id, workspace_id=workspace_id, created_by=row[0])
            resources = tuple(self._resource(db, workspace_id, version_id)[0] for version_id in sorted({b.version_id for b in bindings}))
            try:
                snapshot = admit_task(access=access, conversation=conversation, task_id=new_id("task"), question=question, package=package, resources=resources, bindings=bindings, budget=budget)
            except ValidationError as exc:
                raise DomainError("invalid_task_input", "The task input does not satisfy the resource and question contracts.") from exc
            existing = db.execute("SELECT fingerprint FROM scenario_packages WHERE scenario_id=? AND version=?", (package.manifest.id, package.manifest.version)).fetchone()
            if existing and existing[0] != package.fingerprint:
                raise DomainError("scenario_version_conflict", "Scenario contents changed without a version increment.")
            db.execute("INSERT OR IGNORE INTO scenario_packages VALUES (?,?,?,?)", (package.fingerprint, package.manifest.id, package.manifest.version, package.model_dump_json()))
            state = TaskState(task_id=snapshot.task_id, workspace_id=workspace_id, status=TaskStatus.PREPARED)
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", (snapshot.task_id, workspace_id, conversation_id, access.principal.principal_id, package.fingerprint, snapshot.model_dump_json(), snapshot.fingerprint, state.model_dump_json(), state.version, self.clock()))
            db.executemany("INSERT INTO task_resources VALUES (?,?,?)", [(snapshot.task_id, b.version_id, b.role) for b in snapshot.bindings])
            db.execute("INSERT INTO idempotency VALUES (?,?,?,?,?)", (access.principal.principal_id, scope, request_key, request_hash, snapshot.task_id))
            self._audit(db, access.principal.principal_id, workspace_id, "task.prepared", snapshot.task_id)
            return self._task(db, workspace_id, snapshot.task_id)

    def get_task(self, token: str, workspace_id: str, task_id: str) -> dict:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            return self._task(db, workspace_id, task_id)

    def list_tasks(self, token: str, workspace_id: str, conversation_id: str) -> list[dict]:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            if not db.execute("SELECT id FROM conversations WHERE id=? AND workspace_id=?", (conversation_id, workspace_id)).fetchone():
                raise DomainError("not_found", "Conversation not found in this workspace.")
            return [self._task(db, workspace_id, r[0]) for r in db.execute("SELECT id FROM tasks WHERE workspace_id=? AND conversation_id=? ORDER BY created_at,id LIMIT 1000", (workspace_id, conversation_id))]

    def task_package(self, token: str, workspace_id: str, task_id: str) -> ScenarioPackage:
        with transaction(self.path) as db:
            self._authorize(db, token, workspace_id)
            task = self._task(db, workspace_id, task_id)
            row = db.execute("SELECT package_json FROM scenario_packages WHERE fingerprint=?", (task["snapshot"]["scenario_fingerprint"],)).fetchone()
            if row is None:
                raise DomainError("integrity_error", "The task's pinned scenario is missing.")
            return ScenarioPackage.model_validate_json(row[0])

    def cancel_task(self, token: str, workspace_id: str, task_id: str, expected_version: int) -> dict:
        with transaction(self.path, write=True) as db:
            access = self._authorize(db, token, workspace_id, write=True)
            record = self._task(db, workspace_id, task_id)
            current = TaskState.model_validate(record["state"])
            next_state = transition_task(current, access=access, expected_version=expected_version, target=TaskStatus.CANCELLED)
            updated = db.execute("UPDATE tasks SET state_json=?,state_version=? WHERE id=? AND workspace_id=? AND state_version=?", (next_state.model_dump_json(), next_state.version, task_id, workspace_id, expected_version))
            if updated.rowcount != 1:
                raise DomainError("stale_task_version", "Task state changed concurrently.")
            if db.execute('SELECT 1 FROM runtime_runs WHERE task_id=?', (task_id,)).fetchone():
                db.execute("UPDATE runtime_actions SET state='cancelled' WHERE task_id=? AND state='reserved'", (task_id,))
                db.execute('DELETE FROM runtime_locks WHERE task_id=?', (task_id,))
                db.execute('INSERT INTO runtime_events(task_id,kind,created_at) VALUES (?,?,?)', (task_id, 'cancelled', self.clock()))
            self._audit(db, access.principal.principal_id, workspace_id, "task.cancelled", task_id)
            return self._task(db, workspace_id, task_id)
