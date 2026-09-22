"""Serial, fail-closed dispatch; registry visibility never implies permission."""
import time

from juli.core.common import DomainError, canonical_json
from juli.core.schemas import decode_json, validate_payload
from juli.core.tools import ToolReference, require_tool_grant


class ToolGateway:
    def __init__(self, registry, executor, *, platform_tools: tuple[ToolReference, ...], clock=time.monotonic):
        self.registry = registry
        self.executor = executor
        self.platform_tools = platform_tools
        self.clock = clock

    def granted(self, package):
        # Model-visible tools are the intersection, not all registered capabilities.
        result = []
        for reference in package.manifest.tools:
            if reference in self.platform_tools:
                definition = self.registry.get(reference)
                require_tool_grant(definition, self.platform_tools, allow_mutation=False,
                                   allow_external_egress=False, confirmed=False)
                result.append(definition)
        return tuple(result)

    def execute(self, package, call, *, timeout_seconds: float):
        allowed = tuple(d.reference for d in self.granted(package))
        definition = self.registry.get(call.tool)
        require_tool_grant(definition, allowed, allow_mutation=False, allow_external_egress=False, confirmed=False)
        arguments = decode_json(call.arguments_json)
        if not isinstance(arguments, dict):
            raise DomainError('invalid_tool_arguments', 'Tool arguments must be an object.')
        validate_payload(definition.input_schema_json, arguments)
        timeout = min(timeout_seconds, definition.policy.timeout_seconds)
        if timeout <= 0:
            raise DomainError('operation_timeout', 'The tool deadline has expired.')
        started = self.clock()
        output = self.executor.execute(call.tool, arguments, timeout_seconds=timeout)
        if self.clock() - started >= timeout:
            raise DomainError('operation_timeout', 'The tool response arrived after its deadline.')
        validate_payload(definition.output_schema_json, output)
        encoded = canonical_json(output)
        if len(encoded.encode('utf-8')) > 64 * 1024:
            raise DomainError('tool_output_too_large', 'The tool output exceeds 64 KiB.')
        return encoded
