"""Manually advanced, in-memory integration session. M05 owns durable execution."""
from contextlib import contextmanager
import time
from threading import Lock

from pydantic import Field, ValidationError

from juli.core.common import Contract, DomainError
from juli.core.evidence import AnalysisResult
from juli.core.models import ModelMessage, ModelReply, ModelRequest
from juli.core.schemas import decode_json, validate_payload
from .context import conservative_input_units, initial_context


class SessionPolicy(Contract):
    allow_external_egress: bool = Field(default=False, strict=True)
    max_model_calls: int = Field(default=8, ge=1, le=32, strict=True)
    max_output_per_call: int = Field(default=1024, ge=1, le=16000, strict=True)
    max_elapsed_seconds: int = Field(default=120, ge=1, le=300, strict=True)


class ModelSession:
    def __init__(self, store, token, workspace_id, task_id, provider, gateway, *, policy=None, clock=time.monotonic):
        self.store, self._token = store, token
        self.workspace_id, self.task_id = workspace_id, task_id
        self.provider, self.gateway = provider, gateway
        self.policy = policy or SessionPolicy()
        self.clock = clock
        self.deadline = clock() + self.policy.max_elapsed_seconds
        self.snapshot, self.package, self.state_version = store.model_task_context(token, workspace_id, task_id)
        self.tools = gateway.granted(self.package)
        self._messages = list(initial_context(self.snapshot, self.package))
        self._pending = None
        self._seen_calls = set()
        self._lock = Lock()
        self._closed = False
        self.actions = self.model_calls = self.input_units = self.output_tokens = 0
        self.events = []  # Metadata only; never a prompt, original body or credential log.

    @contextmanager
    def _serial(self):
        if not self._lock.acquire(blocking=False):
            raise DomainError('session_busy', 'This session already has an action in progress.')
        try:
            if self._closed:
                raise DomainError('session_closed', 'Create a new authorized session; this one is closed.')
            yield
        except Exception:
            self._closed = True
            self.events.append({'kind': 'stopped', 'actions': self.actions})
            raise
        finally:
            self._lock.release()

    def _gate(self):
        if self.clock() >= self.deadline:
            raise DomainError('operation_timeout', 'The session deadline has expired.')
        snapshot, package, state_version = self.store.model_task_context(self._token, self.workspace_id, self.task_id)
        if snapshot.fingerprint != self.snapshot.fingerprint or package.fingerprint != self.package.fingerprint or state_version != self.state_version:
            raise DomainError('task_changed', 'Reload the task before taking more actions.')

    def _action(self):
        if self.actions >= self.snapshot.budget.max_actions:
            raise DomainError('action_budget_exceeded', 'The task action allowance is exhausted.')
        self.actions += 1

    def usage(self):
        return {'actions': self.actions, 'model_calls': self.model_calls,
                'reserved_input_units': self.input_units, 'charged_output_tokens': self.output_tokens,
                'durable': False}

    def advance(self) -> dict:
        """One model request only. A proposed tool requires a separate execute_pending call."""
        with self._serial():
            self._gate()
            if self._pending is not None:
                raise DomainError('tool_pending', 'Resolve the pending tool before asking the model again.')
            if self.provider.external_egress and not self.policy.allow_external_egress:
                raise DomainError('egress_denied', 'This task has not been approved for external model data egress.')
            if self.model_calls >= self.policy.max_model_calls:
                raise DomainError('model_budget_exceeded', 'The session model call allowance is exhausted.')
            remaining = self.snapshot.budget.max_output_tokens - self.output_tokens
            if remaining <= 0:
                raise DomainError('output_budget_exceeded', 'The task output allowance is exhausted.')
            request = ModelRequest(messages=tuple(self._messages), tools=self.tools,
                                   max_output_tokens=min(remaining, self.policy.max_output_per_call),
                                   timeout_seconds=min(30, self.deadline - self.clock()))
            reserved_input = conservative_input_units(request)
            if self.input_units + reserved_input > self.snapshot.budget.max_input_tokens or reserved_input + request.max_output_tokens > self.provider.context_window:
                raise DomainError('input_budget_exceeded', 'Narrow the task; the bounded context does not fit.')
            self._action()
            self.model_calls += 1
            self.input_units += reserved_input
            self.output_tokens += request.max_output_tokens  # Unknown outcomes retain the reservation.
            try:
                reply = ModelReply.model_validate(self.provider.complete(request))
            except ValidationError:
                raise DomainError('invalid_model_reply', 'The model reply violates its contract.') from None
            self.input_units += max(0, reply.usage.input_tokens - reserved_input)
            self.output_tokens += reply.usage.output_tokens - request.max_output_tokens
            if (self.input_units > self.snapshot.budget.max_input_tokens or
                    self.output_tokens > self.snapshot.budget.max_output_tokens or
                    reply.usage.output_tokens > request.max_output_tokens):
                raise DomainError('reported_budget_exceeded', 'Reported usage exceeded the reserved allowance; response discarded.')
            self._gate()  # Drop late results after cancellation, withdrawal or revocation.
            self.events.append({'kind': 'model_returned', 'call': self.model_calls})
            if reply.tool_call is not None:
                call = reply.tool_call
                if call.call_id in self._seen_calls:
                    raise DomainError('duplicate_call', 'Repeated provider call IDs cannot be executed again.')
                definition = next((t for t in self.tools if t.reference == call.tool), None)
                if definition is None:
                    raise DomainError('tool_not_granted', 'The model requested a tool outside this session.')
                validate_payload(definition.input_schema_json, decode_json(call.arguments_json))
                self._seen_calls.add(call.call_id)
                self._pending = call
                self._messages.append(ModelMessage(role='assistant', tool_call=call))
                return {'kind': 'tool_requested', 'call': call.model_dump(mode='json'), 'usage': self.usage()}
            try:
                result = AnalysisResult.model_validate(decode_json(reply.text))
            except ValidationError:
                raise DomainError('invalid_analysis_result', 'The final answer does not satisfy the analysis contract.') from None
            self.store.validate_task_result(self._token, self.workspace_id, self.task_id, result)
            self._closed = True
            self.events.append({'kind': 'result_validated'})
            return {'kind': 'result_validated', 'result': result.model_dump(mode='json'),
                    'persisted': False, 'usage': self.usage()}

    def execute_pending(self, call_id: str) -> dict:
        with self._serial():
            self._gate()
            if self._pending is None or self._pending.call_id != call_id:
                raise DomainError('no_pending_call', 'Only the exact pending model call may be dispatched.')
            self._action()
            call = self._pending
            output = self.gateway.execute(self.package, call, timeout_seconds=min(30, self.deadline - self.clock()))
            self._gate()
            self._messages.append(ModelMessage(role='tool', content=output, tool_call_id=call.call_id))
            self._pending = None
            self.events.append({'kind': 'tool_returned', 'tool': call.tool.name})
            return {'kind': 'tool_returned', 'call_id': call.call_id, 'usage': self.usage()}
