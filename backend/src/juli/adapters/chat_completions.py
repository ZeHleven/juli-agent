"""Explicit opt-in Chat Completions transport. No environment lookup or automatic retry."""
from dataclasses import dataclass
import hashlib
import json
import time
from urllib.parse import urlsplit

from pydantic import ValidationError

from juli.core.common import DomainError, canonical_json
from juli.core.models import ModelReply, ModelRequest, ModelToolCall, ModelUsage
from juli.core.schemas import decode_json


@dataclass(frozen=True)
class ChatModelConfig:
    endpoint: str
    model: str
    context_window: int
    timeout_seconds: float = 30

    def __post_init__(self):
        try:
            url = urlsplit(self.endpoint)
            valid_url = (url.scheme == 'https' and url.hostname and url.port in {None, 443}
                         and not url.username and not url.password and not url.query and not url.fragment
                         and url.path.endswith('/chat/completions') and not any(c.isspace() for c in self.endpoint))
        except ValueError:
            valid_url = False
        if not valid_url:
            raise DomainError('invalid_model_config', 'Use an explicit HTTPS chat/completions endpoint on port 443 without credentials, query or fragment.')
        if not isinstance(self.model, str) or not self.model or len(self.model) > 120 or any(c.isspace() for c in self.model):
            raise DomainError('invalid_model_config', 'An explicit model identifier is required.')
        if type(self.context_window) is not int or not 256 <= self.context_window <= 1000000:
            raise DomainError('invalid_model_config', 'Declare a context window of 256–1000000 tokens.')
        if type(self.timeout_seconds) not in {int, float} or not 0 < self.timeout_seconds <= 120:
            raise DomainError('invalid_model_config', 'Declare a finite timeout of at most 120 seconds.')


def tool_alias(reference) -> str:
    # Provider names cannot contain the dots in the public Juli tool names.
    return 'juli_' + hashlib.sha256(f'{reference.name}@{reference.version}'.encode()).hexdigest()[:32]


class ChatCompletionsProvider:
    external_egress = True

    @property
    def runtime_identity(self):
        """Non-secret model/config identity for durable resume, excluding rotating keys."""
        from dataclasses import asdict
        return hashlib.sha256(canonical_json(asdict(self.config)).encode()).hexdigest()

    def __init__(self, config: ChatModelConfig, *, api_key: str, approved_endpoint: str | None = None, transport=None):
        if not isinstance(api_key, str) or not 8 <= len(api_key) <= 4096 or not api_key.isascii() or any(c.isspace() for c in api_key):
            raise DomainError('invalid_model_credentials', 'Supply an explicit nonempty API credential.')
        self.config = config
        self.context_window = config.context_window
        self._api_key = api_key
        self._approved_endpoint = approved_endpoint
        self._transport = transport  # Dependency injection for offline HTTP fixtures.

    def _payload(self, request: ModelRequest):
        aliases = {tool_alias(t.reference): t.reference for t in request.tools}
        if len(aliases) != len(request.tools):
            raise DomainError('duplicate_tool', 'The provider request contains duplicate tools.')
        messages = []
        for message in request.messages:
            item = {'role': message.role, 'content': message.content}
            if message.tool_call is not None:
                call = message.tool_call
                alias = tool_alias(call.tool)
                if alias not in aliases:
                    raise DomainError('tool_not_granted', 'History includes an unavailable tool.')
                item = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': call.call_id, 'type': 'function', 'function': {'name': alias, 'arguments': call.arguments_json}}]}
            if message.tool_call_id:
                item['tool_call_id'] = message.tool_call_id
            messages.append(item)
        payload = {'model': self.config.model, 'messages': messages, 'max_completion_tokens': request.max_output_tokens,
                   'stream': False, 'n': 1, 'store': False}
        if request.tools:
            payload.update({'tools': [{'type': 'function', 'function': {'name': tool_alias(t.reference), 'description': t.description,
                                                                     'parameters': decode_json(t.input_schema_json), 'strict': False}} for t in request.tools],
                            'parallel_tool_calls': False, 'tool_choice': 'auto'})
        return payload, aliases

    def complete(self, request: ModelRequest) -> ModelReply:
        if self._approved_endpoint != self.config.endpoint:
            raise DomainError('egress_denied', 'Explicit approval for this exact model endpoint is required.')
        request = ModelRequest.model_validate(request)
        payload, aliases = self._payload(request)
        encoded = canonical_json(payload).encode('utf-8')
        if len(encoded) > 256 * 1024:
            raise DomainError('context_too_large', 'Provider request exceeds 256 KiB.')
        try:
            import httpx
        except ImportError:
            raise DomainError('model_adapter_unavailable', 'Install the model extra for the HTTP adapter.') from None
        timeout = min(request.timeout_seconds, self.config.timeout_seconds)
        started = time.monotonic()
        response_bytes = bytearray()
        try:
            # No environment proxies, redirect following, debug output, or automatic retries.
            with httpx.Client(transport=self._transport, timeout=timeout, trust_env=False, follow_redirects=False) as client:
                with client.stream('POST', self.config.endpoint, content=encoded,
                                   headers={'Authorization': 'Bearer ' + self._api_key, 'Content-Type': 'application/json'}) as response:
                    if response.status_code != 200:
                        raise DomainError('model_http_error', f'Model endpoint returned HTTP {response.status_code}; no retry was made.')
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started >= timeout:
                            raise DomainError('model_timeout', 'Model response timed out; outcome is unknown and was not retried.')
                        if len(response_bytes) + len(chunk) > 1024 * 1024:
                            raise DomainError('model_response_too_large', 'Model response exceeds 1 MiB.')
                        response_bytes.extend(chunk)
        except httpx.TimeoutException:
            raise DomainError('model_timeout', 'Model request timed out; outcome is unknown and was not retried.') from None
        except httpx.HTTPError:
            raise DomainError('model_transport_error', 'Model transport failed; outcome is unknown and was not retried.') from None
        if time.monotonic() - started >= timeout:
            raise DomainError('model_timeout', 'Model response arrived after its deadline.')
        try:
            body = decode_json(response_bytes.decode('utf-8'))
            choices = body['choices']
            if len(choices) != 1:
                raise ValueError('one choice required')
            choice, usage = choices[0], body['usage']
            stats = ModelUsage(input_tokens=usage['prompt_tokens'], output_tokens=usage['completion_tokens'])
            if type(usage['total_tokens']) is not int or usage['total_tokens'] != stats.input_tokens + stats.output_tokens:
                raise ValueError('invalid total usage')
            message = choice['message']
            if message['role'] != 'assistant' or message.get('refusal'):
                raise ValueError('refusal or non-assistant message')
            calls = message.get('tool_calls') or []
            if calls:
                if len(calls) != 1 or choice['finish_reason'] != 'tool_calls' or message.get('content'):
                    raise ValueError('ambiguous tool response')
                call = calls[0]
                if call['type'] != 'function':
                    raise ValueError('unsupported tool type')
                reference = aliases[call['function']['name']]
                if not isinstance(call['function']['arguments'], str):
                    raise ValueError('arguments must be JSON text')
                return ModelReply(tool_call=ModelToolCall(call_id=call['id'], tool=reference, arguments_json=call['function']['arguments']), usage=stats)
            if choice['finish_reason'] != 'stop' or not isinstance(message.get('content'), str):
                raise ValueError('incomplete model response')
            return ModelReply(text=message['content'], usage=stats)
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            raise DomainError('invalid_model_response', 'The endpoint returned an incomplete, unsupported or invalid model response.') from None
