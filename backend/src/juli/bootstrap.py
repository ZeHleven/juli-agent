"""Explicit application composition; the core knows no built-in scenario IDs."""
from pathlib import Path

from pydantic import Field

from juli.adapters.scenario_files import load_scenario
from juli.core.common import Contract, DomainError, Reference, canonical_json
from juli.core.evidence import Evidence
from juli.core.scenarios import ScenarioRegistry
from juli.core.tools import ToolDefinition, ToolPolicy, ToolReference, ToolRegistry


class SearchCandidate(Contract):
    evidence_id: Reference
    resource_version_id: Reference
    snippet: str = Field(max_length=2000)


class SearchOutput(Contract):
    items: tuple[SearchCandidate, ...] = Field(max_length=128)


class ReadOutput(Contract):
    items: tuple[Evidence, ...] = Field(max_length=128)


def tool_contracts() -> ToolRegistry:
    """Tool contracts shared by scenario registration, local retrieval and the model gateway."""
    registry = ToolRegistry()
    specs = (
        ("evidence.search", "Find candidates; wait for returned evidence_id before reads.", "query"),
        ("evidence.read", "Read original by earlier search evidence_id, not file version ID or same-batch search.", "evidence_id"),
    )
    for name, description, argument in specs:
        output_model = SearchOutput if name == "evidence.search" else ReadOutput
        argument_schema = {"type": "string", "minLength": 1, "maxLength": 4000}
        if argument == 'evidence_id':
            argument_schema['description'] = 'Earlier search evidence_id; NOT resource_version_id/version_id.'
        registry.register(ToolDefinition(reference=ToolReference(name=name, version="1.0.0"), description=description, policy=ToolPolicy(), input_schema_json=canonical_json({"type":"object","properties":{argument:argument_schema},"required":[argument],"additionalProperties":False}), output_schema_json=canonical_json(output_model.model_json_schema())))
    return registry


def create_model_session(store, token, workspace_id, task_id, provider, *, policy=None):
    """Trusted library composition. Never accept a provider/configuration from model arguments."""
    from juli.adapters.local_tools import LocalEvidenceTools
    from juli.application.gateway import ToolGateway
    from juli.application.model_session import ModelSession
    registry = tool_contracts()
    executor = LocalEvidenceTools(store, token, workspace_id, task_id)
    gateway = ToolGateway(registry, executor, platform_tools=tuple(t.reference for t in registry.list()))
    return ModelSession(store, token, workspace_id, task_id, provider, gateway, policy=policy)


def load_registry(directory: Path) -> ScenarioRegistry:
    if not directory.is_dir():
        raise DomainError("scenario_directory_missing", "Specify a directory containing installed scenario packages.")
    registry = ScenarioRegistry(tool_contracts())
    for manifest in sorted(directory.glob("*/scenario.json")):
        registry.register(load_scenario(manifest.parent))
    if not registry.list():
        raise DomainError("no_scenarios", "No scenario.json packages were found in the selected directory.")
    return registry


def create_durable_runner(store, token, workspace_id, task_id, provider, *, profile, policy=None):
    """Trusted composition only: profile identifies model, endpoint and deployment grants.

    Reuse the same immutable profile on restart. Never take it or a provider from
    an uploaded document or an HTTP request. Credentials remain process-local.
    """
    from juli.adapters.local_tools import LocalEvidenceTools
    from juli.application.gateway import ToolGateway
    from juli.application.runtime import DurableRunner
    registry = tool_contracts()
    executor = LocalEvidenceTools(store, token, workspace_id, task_id)
    gateway = ToolGateway(registry, executor, platform_tools=tuple(t.reference for t in registry.list()))
    return DurableRunner(store, token, workspace_id, task_id, provider, gateway, profile=profile, policy=policy)
