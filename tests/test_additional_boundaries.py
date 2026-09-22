import json

import pytest
from pydantic import ValidationError

from juli.core.common import DomainError
from juli.core.evidence import AnalysisResult,ResultBlock,validate_result
from juli.core.scenarios import ScenarioManifest,ScenarioPackage,package_fingerprint
from juli.core.schemas import normalize_schema,validate_payload
from juli.core.resources import ResourceBinding
from juli.core.tasks import admit_task


def revised_package(package, **manifest_changes):
    manifest=ScenarioManifest.model_validate({**package.manifest.model_dump(),**manifest_changes})
    return ScenarioPackage(manifest=manifest,instructions_text=package.instructions_text,result_schema_json=package.result_schema_json,fingerprint=package_fingerprint(manifest,package.instructions_text,package.result_schema_json))


def test_scenario_fingerprint_cannot_be_supplied_for_changed_contents(package):
    with pytest.raises(ValidationError,match='fingerprint'):
        ScenarioPackage.model_validate({**package.model_dump(),'instructions_text':'偷偷替换指导'})


def test_output_cannot_use_new_scenario_for_old_task(snapshot,package,access):
    new=revised_package(package,version='0.2.0')
    result=AnalysisResult(task_id=snapshot.task_id,blocks=(ResultBlock(kind='limitation',text='资料不足'),),scenario_payload_json='{"summary":"资料不足"}')
    with pytest.raises(DomainError) as exc:
        validate_result(snapshot=snapshot,package=new,result=result,evidence=(),receipts=(),access=access)
    assert exc.value.code=='scenario_version_changed'


@pytest.mark.parametrize('payload',['{bad json}','{"summary":NaN}','{"summary":Infinity}'])
def test_non_json_output_cannot_pass_contracts(snapshot,package,access,payload):
    result=AnalysisResult(task_id=snapshot.task_id,blocks=(ResultBlock(kind='limitation',text='资料不足'),),scenario_payload_json=payload)
    with pytest.raises(DomainError) as exc:
        validate_result(snapshot=snapshot,package=package,result=result,evidence=(),receipts=(),access=access)
    assert exc.value.code=='invalid_json'


def test_unresolved_local_schema_has_controlled_error():
    schema=normalize_schema({'type':'object','properties':{'summary':{'$ref':'#/$defs/missing'}}})
    with pytest.raises(DomainError) as exc:validate_payload(schema,{'summary':'hello'})
    assert exc.value.code=='invalid_schema_reference'


def test_schema_cannot_change_resolution_scope():
    with pytest.raises(DomainError):
        normalize_schema({'type':'object','$defs':{'x':{'$id':'https://example.invalid/schema','type':'string'}}})


def test_role_media_type_is_enforced(package,resource,access,conversation):
    package=revised_package(package,resource_roles=[{'name':'image-only','label':'图片','min_count':1,'max_count':1,'media_types':['image/png']}])
    with pytest.raises(DomainError) as exc:
        admit_task(access=access,conversation=conversation,task_id='task:media',question='检查',package=package,resources=(resource,),bindings=(ResourceBinding(role='image-only',version_id=resource.version_id),))
    assert exc.value.code=='unsupported_media_type'


def test_invalid_cardinality_cannot_be_registered(package):
    with pytest.raises(ValidationError):
        revised_package(package,max_resources=1,resource_roles=[{'name':'many','label':'太多','min_count':2,'max_count':4}])


def test_global_resource_limit_is_enforced(package,resource,access,conversation):
    from juli.core.resources import ResourceVersion
    package=revised_package(package,max_resources=1)
    second=ResourceVersion.model_validate({**resource.model_dump(),'version_id':'version:second'})
    with pytest.raises(DomainError) as exc:
        admit_task(access=access,conversation=conversation,task_id='task:limit',question='检查',package=package,resources=(resource,second),bindings=(ResourceBinding(role='subject',version_id=resource.version_id),ResourceBinding(role='reference',version_id=second.version_id)))
    assert exc.value.code=='too_many_resources'
