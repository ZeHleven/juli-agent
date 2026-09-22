"""Portable durable checkpoint; credentials and providers are never serialized."""
from typing import Literal
from pydantic import Field
from .common import Contract
from .models import ModelMessage, ModelToolCall
from .evidence import AnalysisResult


class Clarification(Contract):
    kind: Literal['clarification']
    question: str = Field(min_length=1, max_length=4000)


class Checkpoint(Contract):
    messages: tuple[ModelMessage, ...] = Field(max_length=256)
    pending: ModelToolCall | None = None
    seen_calls: tuple[str, ...] = Field(default=(), max_length=100)
    actions: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    input_units: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    active_seconds: float = Field(default=0, ge=0)
    result: AnalysisResult | None = None
