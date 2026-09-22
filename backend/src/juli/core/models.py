"""Provider-independent messages and one-call model port; no runtime state or secrets."""
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .common import Contract, Reference
from .tools import ToolDefinition, ToolReference


class ModelToolCall(Contract):
    call_id: Reference
    tool: ToolReference
    arguments_json: str = Field(min_length=2, max_length=16384)


class ModelMessage(Contract):
    role: Literal['system', 'user', 'assistant', 'tool']
    content: str = Field(default='', max_length=200000)
    tool_call: ModelToolCall | None = None
    tool_call_id: Reference | None = None

    @model_validator(mode='after')
    def valid_role_fields(self):
        if (self.role == 'tool') != (self.tool_call_id is not None):
            raise ValueError('only tool messages have a tool call ID')
        if self.tool_call is not None and (self.role != 'assistant' or self.content):
            raise ValueError('tool requests are assistant messages without text')
        if self.tool_call is None and not self.content:
            raise ValueError('a message needs content or a tool request')
        return self


class ModelUsage(Contract):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


class ModelRequest(Contract):
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=256)
    tools: tuple[ToolDefinition, ...] = Field(default=(), max_length=64)
    max_output_tokens: int = Field(ge=1, le=100000, strict=True)
    timeout_seconds: float = Field(default=30, gt=0, le=300)


class ModelReply(Contract):
    text: str | None = Field(default=None, min_length=1, max_length=200000)
    tool_call: ModelToolCall | None = None
    usage: ModelUsage

    @model_validator(mode='after')
    def one_decision(self):
        if (self.text is None) == (self.tool_call is None):
            raise ValueError('return exactly one tool request or final text')
        return self


class ModelProvider(Protocol):
    external_egress: bool
    context_window: int

    def complete(self, request: ModelRequest) -> ModelReply: ...


class ToolExecutor(Protocol):
    def execute(self, reference: ToolReference, arguments: dict, *, timeout_seconds: float) -> dict: ...
