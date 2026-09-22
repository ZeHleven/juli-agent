import ast
import json
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from juli.adapters.scenario_files import load_scenario
from juli.bootstrap import tool_contracts
from juli.core.common import DomainError
from juli.core.resources import ResourceBinding
from juli.core.scenarios import ScenarioRegistry
from juli.core.tasks import admit_task

ROOT=Path(__file__).resolve().parents[1]


def copied_package(tmp_path):
    destination=tmp_path/'custom'
    shutil.copytree(ROOT/'scenarios/minimal',destination)
    return destination


def update_manifest(path, **values):
    file=path/'scenario.json'
    data=json.loads(file.read_text(encoding='utf-8'))
    data.update(values)
    file.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')


def test_builtin_examples_and_version_pins(registry):
    assert {p.manifest.id for p in registry.list()}=={'minimal','career-fit','tender-review'}
    with pytest.raises(DomainError) as exc:
        registry.get('minimal','9.0.0')
    assert exc.value.code=='scenario_not_found'


def test_new_scenario_and_new_role_need_no_core_changes(tmp_path,access,conversation,resource):
    path=copied_package(tmp_path)
    update_manifest(path,id='equipment-choice',resource_roles=[{'name':'available-options','label':'候选设备','min_count':1,'max_count':20}])
    package=load_scenario(path)
    registry=ScenarioRegistry(tool_contracts())
    registry.register(package)
    task=admit_task(access=access,conversation=conversation,task_id='task:new-scenario',question='比较设备',package=registry.get('equipment-choice','0.1.0'),resources=(resource,),bindings=(ResourceBinding(role='available-options',version_id=resource.version_id),))
    assert task.scenario_id=='equipment-choice'
    assert task.bindings[0].role=='available-options'


def test_duplicate_registration_and_unregistered_tools_fail(tmp_path,package):
    registry=ScenarioRegistry(tool_contracts())
    registry.register(package)
    with pytest.raises(DomainError) as exc:
        registry.register(package)
    assert exc.value.code=='duplicate_scenario'
    path=copied_package(tmp_path)
    update_manifest(path,id='requires-new-tool',tools=[{'name':'uninstalled.tool','version':'1.0.0'}])
    with pytest.raises(DomainError) as exc:
        registry.register(load_scenario(path))
    assert exc.value.code=='tool_not_found'


def test_loaded_package_is_immutable_and_new_contents_change_fingerprint(tmp_path):
    path=copied_package(tmp_path)
    before=load_scenario(path)
    (path/'instructions.md').write_text('新的分析方法，不能作为事实证据。',encoding='utf-8')
    after=load_scenario(path)
    assert before.fingerprint!=after.fingerprint
    assert before.instructions_text!=after.instructions_text
    registry=ScenarioRegistry(tool_contracts())
    registry.register(before)
    with pytest.raises(DomainError):registry.register(after)


@pytest.mark.parametrize('relative',['../outside.md','/outside.md','C:/outside.md','dir\\file.md','dir//file.md'])
def test_package_paths_cannot_escape(tmp_path,relative):
    path=copied_package(tmp_path)
    update_manifest(path,instructions=relative)
    with pytest.raises(ValidationError):load_scenario(path)


def test_package_size_and_api_version_are_checked(tmp_path):
    path=copied_package(tmp_path)
    update_manifest(path,api_version='juli.scenario.v9')
    with pytest.raises(ValidationError):load_scenario(path)
    update_manifest(path,api_version='juli.scenario.v1')
    (path/'instructions.md').write_text('x'*65537,encoding='utf-8')
    with pytest.raises(DomainError) as exc:load_scenario(path)
    assert exc.value.code=='package_file_too_large'


@pytest.mark.parametrize('reference',['https://example.invalid/schema.json','file:///private/schema.json','other.json'])
def test_schemas_do_not_fetch_external_references(tmp_path,reference):
    path=copied_package(tmp_path)
    (path/'result.schema.json').write_text(json.dumps({'type':'object','properties':{'x':{'$ref':reference}}}),encoding='utf-8')
    with pytest.raises(DomainError) as exc:load_scenario(path)
    assert exc.value.code=='external_schema_reference'


def test_local_schema_reference_and_output_validation(tmp_path):
    path=copied_package(tmp_path)
    schema={'type':'object','properties':{'summary':{'$ref':'#/$defs/text'}},'required':['summary'],'additionalProperties':False,'$defs':{'text':{'type':'string','minLength':1}}}
    (path/'result.schema.json').write_text(json.dumps(schema),encoding='utf-8')
    package=load_scenario(path)
    package.validate_result({'summary':'可校验'})
    for value in ({},{'summary':10},{'summary':'ok','extra':True}):
        with pytest.raises(DomainError):package.validate_result(value)


def test_core_has_no_old_project_framework_or_scenario_dependency():
    forbidden={'fastapi','uvicorn','langchain','langgraph','sqlalchemy','bid_agent_service','app','juli.adapters','juli.api','juli.bootstrap','juli.demo'}
    for path in (ROOT/'backend/src/juli/core').glob('*.py'):
        text=path.read_text(encoding='utf-8')
        assert all(name not in text for name in ('tender-review','career-fit','equipment-choice','enterprise_knowledge','bid_document'))
        for node in ast.walk(ast.parse(text)):
            modules=[]
            if isinstance(node,ast.ImportFrom) and node.level==0:modules=[node.module or '']
            if isinstance(node,ast.Import):modules=[n.name for n in node.names]
            assert not any(m==bad or m.startswith(bad+'.') for m in modules for bad in forbidden)
