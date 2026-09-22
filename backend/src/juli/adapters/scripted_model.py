"""Offline deterministic provider for contract tests; never presented as model intelligence."""
from juli.core.common import DomainError
from juli.core.models import ModelReply


class ScriptedModel:
    external_egress = False
    context_window = 1000000

    def __init__(self, steps):
        self._steps = iter(steps)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        try:
            step = next(self._steps)
        except StopIteration:
            raise DomainError('script_exhausted', 'The offline model script is exhausted.') from None
        return ModelReply.model_validate(step(request) if callable(step) else step)
