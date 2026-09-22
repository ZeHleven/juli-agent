from __future__ import annotations

from enum import Enum
import json

from pydantic import Field, field_validator

from .common import Contract, DomainError, Name, Version
from .schemas import normalize_schema


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"


class ToolReference(Contract):
    name: Name
    version: Version


class ToolPolicy(Contract):
    effect: ToolEffect = ToolEffect.READ_ONLY
    external_egress: bool = False
    requires_confirmation: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class ToolDefinition(Contract):
    reference: ToolReference
    description: str = Field(min_length=1, max_length=1000)
    policy: ToolPolicy
    input_schema_json: str = Field(max_length=65536)
    output_schema_json: str = Field(max_length=65536)

    @field_validator("input_schema_json", "output_schema_json")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        return normalize_schema(json.loads(value))


class ToolRegistry:
    """Definitions only. Registering a tool does not execute or authorize it."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        definition = ToolDefinition.model_validate(definition)
        key = (definition.reference.name, definition.reference.version)
        if key in self._definitions:
            raise DomainError("duplicate_tool", "A tool with this name and version is already registered.")
        self._definitions[key] = definition

    def get(self, reference: ToolReference) -> ToolDefinition:
        try:
            return self._definitions[(reference.name, reference.version)]
        except KeyError as exc:
            raise DomainError("tool_not_found", f"Required tool is not registered: {reference.name}@{reference.version}") from exc

    def list(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))


def require_tool_grant(tool: ToolDefinition, allowed: tuple[ToolReference, ...], *, allow_mutation: bool, allow_external_egress: bool, confirmed: bool) -> None:
    if tool.reference not in allowed:
        raise DomainError("tool_not_granted", "This tool is not granted to the current task.")
    if tool.policy.effect == ToolEffect.MUTATING and not allow_mutation:
        raise DomainError("mutation_denied", "This task does not permit mutations.")
    if tool.policy.external_egress and not allow_external_egress:
        raise DomainError("egress_denied", "This task does not permit external data egress.")
    if tool.policy.requires_confirmation and not confirmed:
        raise DomainError("confirmation_required", "This action requires confirmation.")

