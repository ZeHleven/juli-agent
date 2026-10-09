"""A serial durable agent loop; each external action runs outside the DB transaction."""
import time
from pydantic import ValidationError
from juli.adapters.runtime_store import RuntimeStore
from juli.core.common import DomainError, canonical_json, digest
from juli.core.models import MAX_MODEL_TOOL_CALLS, ModelMessage, ModelReply, ModelRequest
from juli.core.runtime import Checkpoint, Clarification, ReviewCheckpoint
from juli.core.schemas import decode_json, validate_payload
from .context import conservative_input_units, initial_context
from .model_session import SessionPolicy
from .model_response import RESPONSE_PROTOCOL, parse_analysis_response, parse_interactive_analysis
from .review import REVIEW_PROTOCOL, REVIEW_ADAPTER, schedule_reads, stage_messages


class DurableRunner:
    def __init__(self, store, token, workspace_id, task_id, provider, gateway, *, profile, policy=None):
        if not isinstance(profile, str) or not 1 <= len(profile) <= 160:
            raise DomainError('invalid_profile', 'Specify a stable deployment-owned provider profile.')
        self.ledger = RuntimeStore(store, token, workspace_id, task_id)
        self.provider, self.gateway = provider, gateway
        self.policy = SessionPolicy.model_validate(policy or SessionPolicy())
        identity = {'deployment_profile': profile,
            'provider_type': type(provider).__module__ + '.' + type(provider).__qualname__,
            'provider_identity': getattr(provider, 'runtime_identity', None),
            'external_egress': provider.external_egress, 'context_window': provider.context_window,
            'message_protocol': 'tool-batches-v1', 'response_protocol': RESPONSE_PROTOCOL}
        if self.policy.review_before_final:
            identity['review_protocol'] = REVIEW_PROTOCOL
        self.profile = digest(canonical_json(identity).encode())
        if provider.external_egress and not self.policy.allow_external_egress:
            raise DomainError('egress_denied', 'External model data egress is not enabled in this deployment policy.')
        self.ledger.start(self.profile, self.policy, self._initial)

    def _initial(self, snapshot, package):
        messages = initial_context(snapshot, package,
            max_tool_calls=MAX_MODEL_TOOL_CALLS, allow_clarification=True)
        if self.policy.review_before_final:
            return Checkpoint(messages=stage_messages(messages, 'draft'), review=ReviewCheckpoint())
        return Checkpoint(messages=messages)

    def _prepare(self, checkpoint, snapshot, package):
        if self.policy.review_before_final != (checkpoint.review is not None):
            raise DomainError('runtime_config_changed', 'The checkpoint does not match the trusted review policy.')
        if checkpoint.actions >= snapshot.budget.max_actions:
            raise DomainError('action_budget_exceeded', 'The task action budget is exhausted.')
        seconds = min(30, self.policy.max_elapsed_seconds - checkpoint.active_seconds)
        if seconds <= 0:
            raise DomainError('operation_timeout', 'The active execution allowance is exhausted.')
        changes = {'actions': checkpoint.actions+1, 'active_seconds': checkpoint.active_seconds+seconds}
        if checkpoint.pending:
            return self._update(checkpoint, **changes), 'tool', checkpoint.pending, seconds
        if checkpoint.model_calls >= self.policy.max_model_calls:
            raise DomainError('model_budget_exceeded', 'The durable model budget is exhausted.')
        remaining = snapshot.budget.max_output_tokens - checkpoint.output_tokens
        if remaining <= 0:
            raise DomainError('output_budget_exceeded', 'The durable output budget is exhausted.')
        messages = stage_messages(checkpoint.messages, checkpoint.review.phase) if checkpoint.review else checkpoint.messages
        # Definitions remain available to serialize historical tool messages. Review/final
        # stages explicitly reject new proposals; retaining history never grants dispatch.
        request = ModelRequest(messages=messages, tools=self.gateway.granted(package),
                               max_output_tokens=min(remaining, self.policy.max_output_per_call), timeout_seconds=seconds)
        units = conservative_input_units(request)
        if checkpoint.input_units+units > snapshot.budget.max_input_tokens or units+request.max_output_tokens > self.provider.context_window:
            raise DomainError('input_budget_exceeded', 'The bounded task context does not fit.')
        changes.update(model_calls=checkpoint.model_calls+1, input_units=checkpoint.input_units+units,
                       output_tokens=checkpoint.output_tokens+request.max_output_tokens)
        return self._update(checkpoint, **changes), 'model', request, seconds

    @staticmethod
    def _update(checkpoint, **changes):
        return Checkpoint.model_validate({**checkpoint.model_dump(), **changes})

    def step(self, *, expected_actions=None):
        reservation = self.ledger.reserve(self.profile, self.policy, self._prepare, expected_actions=expected_actions)
        if reservation is None:
            return self.view()
        action, reserved, kind, payload, seconds, package = reservation
        started = time.monotonic()
        try:
            if kind == 'model':
                output = ModelReply.model_validate(self.provider.complete(payload))
            else:
                output = self.gateway.execute(package, payload, timeout_seconds=seconds)
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                raise DomainError('operation_timeout', 'Discarded a response past the reserved deadline.')

            def finish(checkpoint, snapshot, package):
                checkpoint = self._update(checkpoint, active_seconds=max(0, checkpoint.active_seconds-seconds+elapsed))
                if kind == 'tool':
                    message = ModelMessage(role='tool', content=output, tool_call_id=payload.call_id)
                    queue = checkpoint.pending_queue
                    if checkpoint.review and checkpoint.review.phase == 'reading' and not queue:
                        checkpoint = self._update(checkpoint, review=ReviewCheckpoint.model_validate({
                            **checkpoint.review.model_dump(), 'phase': 'review'}))
                    return self._update(checkpoint, pending=queue[0] if queue else None,
                        pending_queue=queue[1:], messages=(*checkpoint.messages, message)), None, None
                units = conservative_input_units(payload)
                input_units = checkpoint.input_units + max(0, output.usage.input_tokens-units)
                output_tokens = checkpoint.output_tokens + output.usage.output_tokens-payload.max_output_tokens
                if input_units > snapshot.budget.max_input_tokens or output_tokens > snapshot.budget.max_output_tokens or output.usage.output_tokens > payload.max_output_tokens:
                    raise DomainError('reported_budget_exceeded', 'Discarded usage exceeding the reservation.')
                checkpoint = self._update(checkpoint, input_units=input_units, output_tokens=output_tokens)
                calls = output.calls
                if calls:
                    if checkpoint.review and checkpoint.review.phase != 'draft':
                        raise DomainError('review_tools_forbidden', 'Readiness review and final generation cannot dispatch tools.')
                    # Validate the entire proposal before executing any member.
                    if len(calls) > 1 and checkpoint.actions + len(calls) > snapshot.budget.max_actions:
                        raise DomainError('action_budget_exceeded', 'The complete tool batch does not fit the remaining action budget.')
                    for call in calls:
                        if call.call_id in checkpoint.seen_calls:
                            raise DomainError('duplicate_call', 'A provider call ID cannot execute twice.')
                        definition = next((t for t in payload.tools if t.reference == call.tool), None)
                        if definition is None:
                            raise DomainError('tool_not_granted', 'Requested tool is outside the installed grants.')
                        validate_payload(definition.input_schema_json, decode_json(call.arguments_json))
                    message = ModelMessage(role='assistant', content=output.text or '',
                        tool_call=output.tool_call, tool_calls=output.tool_calls)
                    return self._update(checkpoint, pending=calls[0], pending_queue=calls[1:],
                        seen_calls=(*checkpoint.seen_calls, *(c.call_id for c in calls)),
                        messages=(*checkpoint.messages, message)), None, None
                decoded = decode_json(output.text)
                message = ModelMessage(role='assistant', content=output.text)
                if checkpoint.review:
                    return self._finish_review(checkpoint, snapshot, package, payload.tools, decoded, message)
                if isinstance(decoded, dict) and decoded.get('kind') == 'clarification':
                    question = Clarification.model_validate(decoded)
                    if not question.question.strip():
                        raise DomainError('invalid_question', 'A clarification must not be blank.')
                    return self._update(checkpoint, messages=(*checkpoint.messages, message)), 'pending', question.question
                result, question = parse_interactive_analysis(decoded)
                if question is not None:
                    return self._update(checkpoint, clarification_draft=result,
                        messages=(*checkpoint.messages, message)), 'pending', question
                return self._update(checkpoint, result=result, clarification_draft=None,
                    messages=(*checkpoint.messages, message)), 'completed', None

            self.ledger.commit(action, self.profile, self.policy, finish)
        except BaseException as exc:
            self.ledger.abandon(action)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Do not persist or expose provider error bodies / credentials.
            if isinstance(exc, DomainError):
                raise DomainError(exc.code, 'The action stopped; inspect the durable task state.') from None
            if isinstance(exc, ValidationError):
                raise DomainError('invalid_model_reply', 'The reply failed validation; the task stopped.') from None
            raise DomainError('action_failed', 'The action outcome is unknown; it will not be replayed.') from None
        return self.view()

    def _finish_review(self, checkpoint, snapshot, package, tools, decoded, message):
        review = checkpoint.review
        messages = (*checkpoint.messages, message)
        if review.phase == 'draft':
            result = parse_analysis_response(decoded)
            review, calls = schedule_reads(result, checkpoint, snapshot, package, tools)
            for call in calls:
                definition = next(t for t in tools if t.reference == call.tool)
                validate_payload(definition.input_schema_json, decode_json(call.arguments_json))
            if calls:
                messages = (*messages, ModelMessage(role='assistant',
                    content='Framework-scheduled original reads for draft validation; not model-proposed tool calls.',
                    tool_calls=calls))
            return self._update(checkpoint, review=review, messages=messages,
                pending=calls[0] if calls else None, pending_queue=calls[1:],
                seen_calls=(*checkpoint.seen_calls, *(c.call_id for c in calls))), None, None
        if review.phase == 'review':
            decision = REVIEW_ADAPTER.validate_python(decoded)
            if any(not value.strip() for value in decision.model_dump().values()):
                raise DomainError('invalid_question', 'A readiness decision must contain nonempty text.')
            updated = ReviewCheckpoint.model_validate({**review.model_dump(),
                'phase': 'final' if decision.kind == 'ready' else 'review',
                'last_decision_json': canonical_json(decision.model_dump())})
            return (self._update(checkpoint, review=updated, messages=messages),
                    'pending' if decision.kind == 'clarify' else None,
                    decision.question if decision.kind == 'clarify' else None)
        if review.phase == 'final':
            result = parse_analysis_response(decoded)
            refs = {ref for block in result.blocks for ref in block.evidence_ids}
            if not refs <= set(review.evidence_ids):
                raise DomainError('review_evidence_changed', 'A final answer cannot introduce evidence outside its reviewed source set.')
            return self._update(checkpoint, result=result, messages=messages), 'completed', None
        raise DomainError('invalid_review_phase', 'A model response arrived during original reading.')

    def run(self):
        """Bounded serial execution until terminal state or a user clarification."""
        while True:
            result = self.step()
            if result['state']['status'] != 'running':
                return result

    def answer(self, *, expected_version, request_key, text):
        self.ledger.answer(self.profile, self.policy, expected_version=expected_version, request_key=request_key, text=text)
        return self.view()

    def cancel(self, *, expected_version):
        self.ledger.store.cancel_task(self.ledger._token, self.ledger.workspace_id, self.ledger.task_id, expected_version)
        return self.view()

    def view(self, *, after=0, limit=100):
        return self.ledger.view(after=after, limit=limit)
