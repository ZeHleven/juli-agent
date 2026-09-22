from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import Field, model_validator

from .common import Contract, Digest, DomainError, Name, Reference

MediaType = Annotated[str, Field(min_length=3, max_length=100, pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")]


class Principal(Contract):
    principal_id: Reference
    enabled: bool = True


class Workspace(Contract):
    workspace_id: Reference
    owner_id: Reference
    name: str = Field(min_length=1, max_length=120)


class AccessContext(Contract):
    """Server-created authority; never accept this object from model/user input."""

    principal: Principal
    workspace_ids: tuple[Reference, ...] = Field(max_length=256)

    def require_workspace(self, workspace_id: str) -> None:
        if not self.principal.enabled or workspace_id not in self.workspace_ids:
            raise DomainError("access_denied", "The current principal cannot access this workspace.")


class ResourceStatus(str, Enum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


class ResourceVersion(Contract):
    resource_id: Reference
    version_id: Reference
    workspace_id: Reference
    media_type: MediaType
    source: Name = "file"
    display_name: str = Field(min_length=1, max_length=300)
    content_hash: Digest
    status: ResourceStatus = ResourceStatus.READY


class ResourceBinding(Contract):
    # Roles belong to a scenario binding, not to the underlying resource.
    role: Name
    version_id: Reference


class ResourceRole(Contract):
    name: Name
    label: str = Field(min_length=1, max_length=120)
    min_count: int = Field(default=0, ge=0, le=128)
    max_count: int = Field(default=32, ge=1, le=128)
    media_types: tuple[MediaType, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def consistent_limits(self) -> ResourceRole:
        if self.min_count > self.max_count:
            raise ValueError("min_count cannot exceed max_count")
        if len(set(self.media_types)) != len(self.media_types):
            raise ValueError("media_types must be unique")
        return self
