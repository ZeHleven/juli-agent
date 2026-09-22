"""One-action HTTP dispatch with durable compare-and-set; no client model configuration."""
from dataclasses import dataclass, field
from typing import Callable, Literal
from fastapi import Depends, Header, Query
from pydantic import Field
from juli.adapters.runtime_store import RuntimeStore
from juli.application.model_session import SessionPolicy
from juli.bootstrap import create_durable_runner
from juli.core.common import Contract, DomainError


@dataclass(frozen=True)
class RuntimeConfig:
    provider_factory: Callable
    profile: str
    mode: Literal['demo', 'local', 'external']
    policy: SessionPolicy = field(default_factory=SessionPolicy)
    scenario_ids: tuple[str, ...] = ()  # Empty permits all installed scenarios.

    def __post_init__(self):
        if not callable(self.provider_factory) or not self.profile or self.mode not in {'demo','local','external'}:
            raise ValueError('An explicit server-owned provider configuration is required.')
        if (self.mode == 'external') != self.policy.allow_external_egress:
            raise ValueError('Only an external deployment mode may approve model data egress.')


class StepInput(Contract):
    expected_actions: int = Field(ge=0, le=100, strict=True)


class AnswerInput(Contract):
    expected_version: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=16000)


def attach_runtime_routes(app, store, credential, config):
    @app.get('/v1/runtime', dependencies=[Depends(credential)])
    def capabilities():
        return {'execution_enabled': config is not None, 'mode': config.mode if config else 'disabled',
                'scenario_ids': list(config.scenario_ids) if config else [], 'dispatch': 'one_action',
                'external_egress': config.policy.allow_external_egress if config else False}

    def runner(token, space, task):
        # Resolve owner/scope before constructing a model or performing initialization.
        RuntimeStore(store, token, space, task).view()
        if config is None:
            raise DomainError('runtime_disabled', 'The administrator has not enabled a model runtime.')
        snapshot = store.get_task(token, space, task)['snapshot']
        if config.scenario_ids and snapshot['scenario_id'] not in config.scenario_ids:
            raise DomainError('runtime_scenario_disabled', 'This deployment does not execute the selected scenario.')
        provider = config.provider_factory()
        if bool(provider.external_egress) != (config.mode == 'external'):
            raise DomainError('runtime_config_changed', 'Provider egress does not match the deployment mode.')
        return create_durable_runner(store, token, space, task, provider, profile=config.profile, policy=config.policy)

    @app.get('/v1/workspaces/{space}/tasks/{task}/runtime')
    def view(space: str, task: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100), token: str = Depends(credential)):
        return RuntimeStore(store, token, space, task).view(after=after, limit=limit)

    @app.post('/v1/workspaces/{space}/tasks/{task}/step')
    def step(space: str, task: str, body: StepInput, token: str = Depends(credential)):
        return runner(token, space, task).step(expected_actions=body.expected_actions)

    @app.post('/v1/workspaces/{space}/tasks/{task}/answer')
    def answer(space: str, task: str, body: AnswerInput,
               request_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=160, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$'),
               token: str = Depends(credential)):
        return runner(token, space, task).answer(expected_version=body.expected_version, request_key=request_key, text=body.text)
