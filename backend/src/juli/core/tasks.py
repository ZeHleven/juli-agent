from __future__ import annotations

from collections import Counter
from enum import Enum

from pydantic import Field, model_validator

from .common import Contract, Digest, DomainError, Name, Reference, Version, canonical_json, digest
from .resources import AccessContext, ResourceBinding, ResourceStatus, ResourceVersion
from .scenarios import ScenarioPackage


class Conversation(Contract):
    conversation_id: Reference
    workspace_id: Reference
    created_by: Reference


class TaskBudget(Contract):
    max_actions: int = Field(default=24, ge=1, le=100)
    max_input_tokens: int = Field(default=32000, ge=256, le=1000000)
    max_output_tokens: int = Field(default=4000, ge=64, le=100000)


class TaskSnapshot(Contract):
    task_id: Reference
    conversation_id: Reference
    workspace_id: Reference
    principal_id: Reference
    question: str = Field(min_length=1, max_length=16000)
    scenario_id: Name
    scenario_version: Version
    scenario_fingerprint: Digest
    resources: tuple[ResourceVersion, ...] = Field(max_length=128)
    bindings: tuple[ResourceBinding, ...] = Field(max_length=256)
    budget: TaskBudget = Field(default_factory=TaskBudget)

    @model_validator(mode="after")
    def consistent_scope(self) -> TaskSnapshot:
        versions = {r.version_id for r in self.resources}
        if len(versions) != len(self.resources):
            raise ValueError("resource version IDs must be unique")
        if any(r.workspace_id != self.workspace_id for r in self.resources):
            raise ValueError("all resources must belong to the task workspace")
        if any(r.status != ResourceStatus.READY for r in self.resources):
            raise ValueError("task resources must be ready")
        if versions != {b.version_id for b in self.bindings}:
            raise ValueError("every resource must be bound and every binding must resolve")
        if len({(b.role, b.version_id) for b in self.bindings}) != len(self.bindings):
            raise ValueError("duplicate resource role bindings are forbidden")
        if not self.question.strip():
            raise ValueError("question cannot be blank")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(canonical_json(self.model_dump(mode="json")).encode("utf-8"))


def admit_task(*, access: AccessContext, conversation: Conversation, task_id: str, question: str, package: ScenarioPackage, resources: tuple[ResourceVersion, ...], bindings: tuple[ResourceBinding, ...], budget: TaskBudget | None = None) -> TaskSnapshot:
    """Validate server-resolved resources. This function does not load authority."""
    access.require_workspace(conversation.workspace_id)
    manifest = package.manifest
    if len(resources) > manifest.max_resources:
        raise DomainError("too_many_resources", "The scenario resource limit was exceeded.")
    by_version = {r.version_id: r for r in resources}
    roles = {r.name: r for r in manifest.resource_roles}
    counts = Counter(b.role for b in bindings)
    for binding in bindings:
        if binding.role not in roles:
            raise DomainError("unknown_role", "A binding uses an undeclared role.")
        resource = by_version.get(binding.version_id)
        if resource is None:
            raise DomainError("resource_not_bound", "A binding does not resolve to a supplied resource.")
        access.require_workspace(resource.workspace_id)
        role = roles[binding.role]
        if role.media_types and resource.media_type not in role.media_types:
            raise DomainError("unsupported_media_type", "A resource does not match its role's media types.")
    for role in roles.values():
        if not role.min_count <= counts[role.name] <= role.max_count:
            raise DomainError("role_cardinality", f"The number of resources for role '{role.name}' is outside its declared range.")
    return TaskSnapshot(task_id=task_id, conversation_id=conversation.conversation_id, workspace_id=conversation.workspace_id, principal_id=access.principal.principal_id, question=question, scenario_id=manifest.id, scenario_version=manifest.version, scenario_fingerprint=package.fingerprint, resources=tuple(sorted(resources, key=lambda r:r.version_id)), bindings=tuple(sorted(bindings, key=lambda b:(b.role,b.version_id))), budget=budget or TaskBudget())


class TaskStatus(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(Contract):
    task_id: Reference
    workspace_id: Reference
    version: int = Field(default=1, ge=1)
    status: TaskStatus = TaskStatus.RUNNING
    pending_question: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def pending_consistency(self) -> TaskState:
        if (self.status == TaskStatus.PENDING) != (self.pending_question is not None):
            raise ValueError("only pending tasks must carry a question")
        return self


def transition_task(state: TaskState, *, access: AccessContext, expected_version: int, target: TaskStatus, pending_question: str | None = None) -> TaskState:
    """Pure lifecycle validation, not a durable runtime or concurrency primitive."""
    access.require_workspace(state.workspace_id)
    if expected_version != state.version:
        raise DomainError("stale_task_version", "The task changed; reload its current state.")
    allowed = {
        TaskStatus.PREPARED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
        TaskStatus.RUNNING: {TaskStatus.PENDING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED},
    }
    if target not in allowed.get(state.status, set()):
        raise DomainError("invalid_transition", "This lifecycle transition is not allowed.")
    return TaskState(task_id=state.task_id, workspace_id=state.workspace_id, version=state.version+1, status=target, pending_question=pending_question)
