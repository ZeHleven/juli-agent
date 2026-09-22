from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

Reference = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Name = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")]
Version = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class Contract(BaseModel):
    # Do not strip evidence or instruction text. Nested collections are tuples.
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

