"""Local JSON schemas only: validation must never fetch a remote reference."""
from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from .common import DomainError, canonical_json


def decode_json(value: str) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("Non-finite numbers are not JSON values.")
    try:
        return json.loads(value, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise DomainError("invalid_json", "A valid finite JSON value is required.") from exc


def _reject_external_references(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise DomainError("schema_too_deep", "Schema nesting exceeds the allowed depth.")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                raise DomainError("external_schema_reference", "Only same-document schema references are allowed.")
            if key == "$id":
                raise DomainError("schema_id_not_supported", "Schema identifiers that change resolution scope are unsupported.")
            _reject_external_references(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_external_references(item, depth + 1)


def normalize_schema(schema: Any) -> str:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise DomainError("invalid_schema", "A root object JSON Schema is required.")
    if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") != "https://json-schema.org/draft/2020-12/schema":
        raise DomainError("unsupported_schema_draft", "Use JSON Schema draft 2020-12.")
    _reject_external_references(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise DomainError("invalid_schema", exc.message) from exc
    return canonical_json(schema)


def validate_payload(schema_json: str, payload: Any) -> None:
    schema = decode_json(normalize_schema(decode_json(schema_json)))
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        raise DomainError("result_schema_mismatch", exc.message) from exc
    except (Unresolvable, RecursionError) as exc:
        raise DomainError("invalid_schema_reference", "A local schema reference cannot be resolved safely.") from exc
