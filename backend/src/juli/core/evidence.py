from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from .common import Contract, Digest, DomainError, Reference, digest
from .resources import AccessContext
from .scenarios import ScenarioPackage
from .schemas import decode_json
from .tasks import TaskSnapshot


class EvidenceLocator(Contract):
    # Structured positions, never client-supplied filesystem/network URLs.
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)
    sheet: str | None = Field(default=None, min_length=1, max_length=200)
    row: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_position(self) -> EvidenceLocator:
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("character offsets must be supplied together")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("character range must be nonempty")
        if all(v is None for v in (self.page,self.paragraph,self.row,self.char_start)):
            raise ValueError("at least one original content position is required")
        return self


class Evidence(Contract):
    evidence_id: Reference
    workspace_id: Reference
    resource_version_id: Reference
    resource_content_hash: Digest
    parser_version: Reference
    locator: EvidenceLocator
    text: str = Field(min_length=1, max_length=64000)
    content_hash: Digest

    @model_validator(mode="after")
    def exact_content_hash(self) -> Evidence:
        if digest(self.text.encode("utf-8")) != self.content_hash:
            raise ValueError("evidence hash does not match its original text")
        return self


class EvidenceReadReceipt(Contract):
    """Created by an authorized reader; durable receipts live in the store, never model input."""
    task_id: Reference
    evidence_id: Reference
    content_hash: Digest


class ClaimKind(str, Enum):
    FACT = "fact"
    INFERENCE = "inference"
    ADVICE = "advice"
    LIMITATION = "limitation"


class ResultBlock(Contract):
    kind: ClaimKind
    text: str = Field(min_length=1, max_length=16000)
    evidence_ids: tuple[Reference, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def fact_requires_evidence(self) -> ResultBlock:
        if self.kind == ClaimKind.FACT and not self.evidence_ids:
            raise ValueError("a factual block requires evidence")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence IDs must be unique within a block")
        return self


class AnalysisResult(Contract):
    task_id: Reference
    blocks: tuple[ResultBlock, ...] = Field(min_length=1, max_length=100)
    scenario_payload_json: str = Field(default="{}", max_length=65536)


def validate_result(*, snapshot: TaskSnapshot, package: ScenarioPackage, result: AnalysisResult, evidence: tuple[Evidence, ...], receipts: tuple[EvidenceReadReceipt, ...], access: AccessContext) -> None:
    """Validate associations, not semantic truth. Live revocation is a later adapter responsibility."""
    access.require_workspace(snapshot.workspace_id)
    if result.task_id != snapshot.task_id:
        raise DomainError("task_mismatch", "The answer belongs to a different task.")
    if package.fingerprint != snapshot.scenario_fingerprint:
        raise DomainError("scenario_version_changed", "Use the exact scenario package pinned to this task.")
    sources = {r.version_id:r for r in snapshot.resources}
    items = {e.evidence_id:e for e in evidence}
    if len(items) != len(evidence):
        raise DomainError("duplicate_evidence", "Ambiguous evidence IDs are not allowed.")
    reads = {(r.task_id,r.evidence_id,r.content_hash) for r in receipts}
    for evidence_id in {ref for block in result.blocks for ref in block.evidence_ids}:
        item = items.get(evidence_id)
        if item is None:
            raise DomainError("evidence_not_found", "A citation does not resolve to supplied evidence.")
        source = sources.get(item.resource_version_id)
        if item.workspace_id != snapshot.workspace_id or source is None or source.content_hash != item.resource_content_hash:
            raise DomainError("evidence_out_of_scope", "A citation does not match this task's resource snapshot.")
        if (snapshot.task_id,item.evidence_id,item.content_hash) not in reads:
            raise DomainError("evidence_not_read", "Cited content must have been read for this task.")
    payload = decode_json(result.scenario_payload_json)
    package.validate_result(payload)
