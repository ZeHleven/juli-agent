from pathlib import Path

import pytest

from juli.bootstrap import load_registry
from juli.core.common import digest
from juli.core.resources import AccessContext, Principal, ResourceBinding, ResourceVersion
from juli.core.tasks import Conversation, admit_task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry():
    return load_registry(ROOT / "scenarios")


@pytest.fixture
def package(registry):
    return registry.get("minimal", "0.1.0")


@pytest.fixture
def access():
    return AccessContext(principal=Principal(principal_id="person:a"), workspace_ids=("space:a",))


@pytest.fixture
def conversation():
    return Conversation(conversation_id="conversation:a", workspace_id="space:a", created_by="person:a")


@pytest.fixture
def resource():
    return ResourceVersion(resource_id="resource:a", version_id="version:a:1", workspace_id="space:a", media_type="text/plain", display_name="合成资料", content_hash=digest(b"synthetic resource"))


@pytest.fixture
def snapshot(access, conversation, package, resource):
    return admit_task(access=access, conversation=conversation, task_id="task:a", question="合成资料中有什么依据？", package=package, resources=(resource,), bindings=(ResourceBinding(role="subject", version_id=resource.version_id),))
