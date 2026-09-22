from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

import json

from .common import Contract, Digest, DomainError, Name, Version, canonical_json, digest
from .resources import ResourceRole
from .schemas import normalize_schema, validate_payload
from .tools import ToolReference, ToolRegistry


class ScenarioManifest(Contract):
    api_version: Literal["juli.scenario.v1"] = "juli.scenario.v1"
    id: Name
    version: Version
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    resource_roles: tuple[ResourceRole, ...] = Field(min_length=1, max_length=32)
    max_resources: int = Field(default=128, ge=1, le=128)
    instructions: str = Field(min_length=1, max_length=180)
    result_schema: str = Field(min_length=1, max_length=180)
    tools: tuple[ToolReference, ...] = Field(default=(), max_length=64)

    @field_validator("instructions", "result_schema")
    @classmethod
    def relative_package_path(cls, value: str) -> str:
        # Require one canonical path spelling across Windows and POSIX.
        if "\\" in value or ":" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("package paths must be relative canonical paths")
        return value

    @model_validator(mode="after")
    def unique_declarations(self) -> ScenarioManifest:
        if len({r.name for r in self.resource_roles}) != len(self.resource_roles):
            raise ValueError("resource role names must be unique")
        if len({(t.name, t.version) for t in self.tools}) != len(self.tools):
            raise ValueError("tool references must be unique")
        if any(r.min_count > self.max_resources for r in self.resource_roles) or sum(r.min_count for r in self.resource_roles) > 256:
            raise ValueError("role minimums cannot exceed global resource/binding limits")
        return self


class ScenarioPackage(Contract):
    manifest: ScenarioManifest
    instructions_text: str = Field(min_length=1, max_length=32768)
    result_schema_json: str = Field(min_length=2, max_length=65536)
    fingerprint: Digest

    @field_validator("result_schema_json")
    @classmethod
    def normalized_result_schema(cls, value: str) -> str:
        return normalize_schema(json.loads(value))

    @model_validator(mode="after")
    def check_fingerprint(self) -> ScenarioPackage:
        expected = package_fingerprint(self.manifest, self.instructions_text, self.result_schema_json)
        if self.fingerprint != expected:
            raise ValueError("scenario fingerprint does not match package contents")
        return self

    def validate_result(self, payload: object) -> None:
        validate_payload(self.result_schema_json, payload)


def package_fingerprint(manifest: ScenarioManifest, instructions: str, result_schema_json: str) -> str:
    return digest(canonical_json({"manifest": manifest.model_dump(mode="json"), "instructions": instructions, "result_schema": json.loads(result_schema_json)}).encode("utf-8"))


class ScenarioRegistry:
    def __init__(self, tools: ToolRegistry):
        self._tools = tools
        self._packages: dict[tuple[str, str], ScenarioPackage] = {}

    def register(self, package: ScenarioPackage) -> None:
        package = ScenarioPackage.model_validate(package)
        key = (package.manifest.id, package.manifest.version)
        if key in self._packages:
            raise DomainError("duplicate_scenario", "This scenario version is already registered.")
        for reference in package.manifest.tools:
            self._tools.get(reference)
        self._packages[key] = package

    def get(self, scenario_id: str, version: str) -> ScenarioPackage:
        try:
            return self._packages[(scenario_id, version)]
        except KeyError as exc:
            raise DomainError("scenario_not_found", "The requested scenario version is not registered.") from exc

    def list(self) -> tuple[ScenarioPackage, ...]:
        return tuple(self._packages[key] for key in sorted(self._packages))
