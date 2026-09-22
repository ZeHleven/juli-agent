from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from juli.core.common import DomainError
from juli.core.scenarios import ScenarioManifest, ScenarioPackage, package_fingerprint
from juli.core.schemas import normalize_schema

MAX_PACKAGE_FILE_BYTES = 65536


def _reject_link(path: Path) -> None:
    junction = getattr(path, "is_junction", lambda: False)
    # Python 3.11 lacks Path.is_junction; reject Windows reparse points too.
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        attributes = 0
    reparse = attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if path.is_symlink() or junction() or reparse:
        raise DomainError("package_link_forbidden", "Scenario packages must use regular directories and files.")


def _read(root: Path, relative: str) -> str:
    candidate = root / relative
    for part in (candidate, *candidate.parents):
        _reject_link(part)
        if part == root:
            break
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise DomainError("invalid_package_path", "The scenario file is outside the package or missing.")
    with resolved.open("rb") as stream:
        data = stream.read(MAX_PACKAGE_FILE_BYTES + 1)
    if len(data) > MAX_PACKAGE_FILE_BYTES:
        raise DomainError("package_file_too_large", "A scenario package file exceeds 64 KiB.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError("invalid_package_encoding", "Scenario files must use UTF-8.") from exc


def load_scenario(directory: Path) -> ScenarioPackage:
    """Load a deployment-owned data package; never import Python or run hooks."""
    directory = directory.absolute()
    for part in (directory, *directory.parents):
        _reject_link(part)
    root = directory.resolve()
    manifest = ScenarioManifest.model_validate_json(_read(root, "scenario.json"))
    instructions = _read(root, manifest.instructions)
    schema = normalize_schema(json.loads(_read(root, manifest.result_schema)))
    # Capture contents, not paths: a running task will pin this exact fingerprint.
    return ScenarioPackage(manifest=manifest, instructions_text=instructions, result_schema_json=schema, fingerprint=package_fingerprint(manifest, instructions, schema))
