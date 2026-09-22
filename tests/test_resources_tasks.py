import pytest
from pydantic import ValidationError

from juli.core.common import DomainError, digest
from juli.core.resources import AccessContext, Principal, ResourceBinding, ResourceVersion
from juli.core.tasks import TaskSnapshot, TaskState, TaskStatus, admit_task, transition_task


def test_more_than_two_resources_and_multiple_roles_are_supported(access, conversation, package, resource):
    resources = tuple(ResourceVersion.model_validate({**resource.model_dump(),"version_id":f"version:{i}","resource_id":f"resource:{i}"}) for i in range(5))
    bindings = tuple(ResourceBinding(role="subject" if i<3 else "reference",version_id=r.version_id) for i,r in enumerate(resources))
    extra_role = ResourceBinding(role="reference",version_id=resources[0].version_id)
    first = admit_task(access=access,conversation=conversation,task_id="task:many",question="比较",package=package,resources=resources,bindings=bindings+(extra_role,))
    second = admit_task(access=access,conversation=conversation,task_id="task:many",question="比较",package=package,resources=tuple(reversed(resources)),bindings=tuple(reversed(bindings+(extra_role,))))
    assert len(first.resources)==5 and len(first.bindings)==6
    assert first.fingerprint==second.fingerprint
    assert TaskSnapshot.model_validate_json(first.model_dump_json()).fingerprint==first.fingerprint


def test_background_version_changes_do_not_rewrite_old_task(snapshot,resource,access,conversation,package):
    old = snapshot.fingerprint
    updated = ResourceVersion.model_validate({**resource.model_dump(),"version_id":"version:a:2","content_hash":digest(b"updated synthetic resource")})
    new = admit_task(access=access,conversation=conversation,task_id="task:b",question=snapshot.question,package=package,resources=(updated,),bindings=(ResourceBinding(role="subject",version_id=updated.version_id),))
    assert snapshot.fingerprint==old
    assert snapshot.resources[0].version_id=="version:a:1"
    assert new.resources[0].version_id=="version:a:2"
    with pytest.raises(ValidationError):
        snapshot.resources[0].version_id="version:tampered"


@pytest.mark.parametrize("field,value", [("workspace_id","space:other"),("status","processing"),("status","withdrawn")])
def test_task_rejects_inaccessible_or_unready_resource(field,value,resource,access,conversation,package):
    changed = ResourceVersion.model_validate({**resource.model_dump(),field:value})
    with pytest.raises((DomainError,ValidationError)):
        admit_task(access=access,conversation=conversation,task_id="task:x",question="分析",package=package,resources=(changed,),bindings=(ResourceBinding(role="subject",version_id=changed.version_id),))


@pytest.mark.parametrize("bindings", [(),(ResourceBinding(role="undeclared",version_id="version:a:1"),),(ResourceBinding(role="subject",version_id="version:missing"),),(ResourceBinding(role="subject",version_id="version:a:1"),)*2])
def test_missing_unknown_dangling_and_duplicate_bindings_fail(bindings,resource,access,conversation,package):
    with pytest.raises((DomainError,ValidationError)):
        admit_task(access=access,conversation=conversation,task_id="task:x",question="分析",package=package,resources=(resource,),bindings=bindings)


def test_unbound_resource_cannot_sneak_into_context(snapshot,resource):
    second = ResourceVersion.model_validate({**resource.model_dump(),"version_id":"version:extra"})
    with pytest.raises(ValidationError,match="every resource must be bound"):
        TaskSnapshot.model_validate({**snapshot.model_dump(),"resources":(resource,second)})


def test_disabled_principal_cannot_create_task(resource,conversation,package):
    access = AccessContext(principal=Principal(principal_id="person:a",enabled=False),workspace_ids=("space:a",))
    with pytest.raises(DomainError,match="cannot access"):
        admit_task(access=access,conversation=conversation,task_id="task:x",question="分析",package=package,resources=(resource,),bindings=(ResourceBinding(role="subject",version_id=resource.version_id),))


def test_wait_resume_cancel_and_stale_version(access):
    state=TaskState(task_id="task:a",workspace_id="space:a")
    waiting=transition_task(state,access=access,expected_version=1,target=TaskStatus.PENDING,pending_question="请补充资料")
    assert waiting.version==2 and waiting.pending_question
    with pytest.raises(DomainError) as exc:
        transition_task(waiting,access=access,expected_version=1,target=TaskStatus.RUNNING)
    assert exc.value.code=="stale_task_version"
    resumed=transition_task(waiting,access=access,expected_version=2,target=TaskStatus.RUNNING)
    cancelled=transition_task(resumed,access=access,expected_version=3,target=TaskStatus.CANCELLED)
    with pytest.raises(DomainError) as exc:
        transition_task(cancelled,access=access,expected_version=4,target=TaskStatus.COMPLETED)
    assert exc.value.code=="invalid_transition"


def test_cross_workspace_transition_and_invalid_pending_shape(access):
    state=TaskState(task_id="task:other",workspace_id="space:other")
    with pytest.raises(DomainError):
        transition_task(state,access=access,expected_version=1,target=TaskStatus.CANCELLED)
    with pytest.raises(ValidationError):
        TaskState(task_id="task:x",workspace_id="space:a",status="pending")
