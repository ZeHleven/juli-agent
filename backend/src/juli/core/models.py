"""Provider-independent messages and bounded tool batches; no runtime state or secrets."""
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .common import Contract, Reference
from .tools import ToolDefinition, ToolReference

MAX_MODEL_TOOL_CALLS = 8


class ModelToolCall(Contract):
    call_id: Reference
    tool: ToolReference
    arguments_json: str = Field(min_length=2, max_length=16384)


class ModelMessage(Contract):
    role: Literal['system', 'user', 'assistant', 'tool']
    content: str = Field(default='', max_length=200000)
    tool_call: ModelToolCall | None = None
    tool_calls: tuple[ModelToolCall, ...] = Field(default=(), max_length=MAX_MODEL_TOOL_CALLS)
    tool_call_id: Reference | None = None

    @property
    def calls(self) -> tuple[ModelToolCall, ...]:
        return (self.tool_call,) if self.tool_call is not None else self.tool_calls

    @model_validator(mode='after')
    def valid_role_fields(self):
        _validate_call_fields(self.tool_call, self.tool_calls)
        if (self.role == 'tool') != (self.tool_call_id is not None):
            raise ValueError('only tool messages have a tool call ID')
        if self.calls and self.role != 'assistant':
            raise ValueError('tool requests belong to assistant messages')
        if not self.calls and not self.content:
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
    tool_calls: tuple[ModelToolCall, ...] = Field(default=(), max_length=MAX_MODEL_TOOL_CALLS)
    usage: ModelUsage

    @property
    def calls(self) -> tuple[ModelToolCall, ...]:
        return (self.tool_call,) if self.tool_call is not None else self.tool_calls

    @model_validator(mode='after')
    def one_decision(self):
        _validate_call_fields(self.tool_call, self.tool_calls)
        if not self.calls and (self.text is None or not self.text.strip()):
            raise ValueError('return a tool batch or nonempty final text')
        return self


def _validate_call_fields(single: ModelToolCall | None, batch: tuple[ModelToolCall, ...]) -> None:
    if single is not None and batch:
        raise ValueError('legacy tool_call and tool_calls are mutually exclusive')
    if len({call.call_id for call in batch}) != len(batch):
        raise ValueError('tool call IDs must be unique within an assistant message')


class ModelProvider(Protocol):
    external_egress: bool
    context_window: int

    def complete(self, request: ModelRequest) -> ModelReply: ...


class ToolExecutor(Protocol):
    def execute(self, reference: ToolReference, arguments: dict, *, timeout_seconds: float) -> dict: ...
