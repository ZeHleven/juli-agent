"""Portable durable checkpoint; credentials and providers are never serialized."""
from typing import Literal
from pydantic import Field, model_validator
from .common import Contract
from .models import ModelMessage, ModelToolCall
from .evidence import AnalysisResult


class Clarification(Contract):
    kind: Literal['clarification']
    question: str = Field(min_length=1, max_length=4000)


class ReviewCheckpoint(Contract):
    """Opt-in application workflow state; never a second task-state authority."""
    phase: Literal['draft', 'reading', 'review', 'final'] = 'draft'
    draft: AnalysisResult | None = None
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=8)
    last_decision_json: str | None = Field(default=None, max_length=8000)

    @model_validator(mode='after')
    def valid_stage(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('Review evidence must be unique')
        if self.phase != 'draft' and self.draft is None:
            raise ValueError('A review stage requires its draft')
        if self.phase == 'draft' and (self.draft is not None or self.evidence_ids):
            raise ValueError('Initial review state cannot contain a draft')
        return self


class Checkpoint(Contract):
    messages: tuple[ModelMessage, ...] = Field(max_length=256)
    pending: ModelToolCall | None = None
    pending_queue: tuple[ModelToolCall, ...] = Field(default=(), max_length=7)
    seen_calls: tuple[str, ...] = Field(default=(), max_length=100)
    actions: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    input_units: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    active_seconds: float = Field(default=0, ge=0)
    result: AnalysisResult | None = None
    clarification_draft: AnalysisResult | None = None
    review: ReviewCheckpoint | None = None

    @model_validator(mode='after')
    def queue_requires_head(self):
        if self.clarification_draft is not None and (self.result is not None or self.review is not None or self.pending is not None):
            raise ValueError('A clarification candidate cannot coexist with a result, review or tool proposal')
        if self.pending_queue and self.pending is None:
            raise ValueError('A pending tail requires a current tool')
        calls = ((self.pending,) if self.pending else ()) + self.pending_queue
        if len({c.call_id for c in calls}) != len(calls):
            raise ValueError('Pending tool IDs must be unique')
        return self
