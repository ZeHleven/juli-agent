import json

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from juli.api import create_app
from juli.bootstrap import tool_contracts
from juli.core.common import DomainError,digest
from juli.core.evidence import AnalysisResult,Evidence,EvidenceLocator,EvidenceReadReceipt,ResultBlock,validate_result
from juli.core.resources import AccessContext,Principal
from juli.core.tools import ToolDefinition,ToolPolicy,ToolReference,require_tool_grant
from juli.demo import contract_demo


def evidence_for(snapshot, **overrides):
    resource=snapshot.resources[0]
    original='  合成原文：金额为 12 万元，而非 120 万元。\n'
    body=dict(evidence_id='evidence:a',workspace_id=snapshot.workspace_id,resource_version_id=resource.version_id,resource_content_hash=resource.content_hash,parser_version='synthetic:1',locator=EvidenceLocator(paragraph=1),text=original,content_hash=digest(original.encode('utf-8')))
    return Evidence(**{**body,**overrides})


def answer_for(snapshot, **overrides):
    return AnalysisResult(**{'task_id':snapshot.task_id,'blocks':(ResultBlock(kind='fact',text='合成资料金额为 12 万元。',evidence_ids=('evidence:a',)),),'scenario_payload_json':json.dumps({'summary':'这是合成合同用例。'}),**overrides})


def test_exact_evidence_original_and_read_receipt(snapshot,package,access):
    evidence=evidence_for(snapshot)
    assert evidence.text.startswith('  ') and evidence.text.endswith('\n')
    receipt=EvidenceReadReceipt(task_id=snapshot.task_id,evidence_id=evidence.evidence_id,content_hash=evidence.content_hash)
    validate_result(snapshot=snapshot,package=package,result=answer_for(snapshot),evidence=(evidence,),receipts=(receipt,),access=access)
    with pytest.raises(DomainError) as exc:
        validate_result(snapshot=snapshot,package=package,result=answer_for(snapshot),evidence=(evidence,),receipts=(),access=access)
    assert exc.value.code=='evidence_not_read'
    with pytest.raises(ValidationError):evidence_for(snapshot,content_hash=digest(b'tampered'))


@pytest.mark.parametrize('overrides',[{'workspace_id':'space:other'},{'resource_version_id':'version:other'},{'resource_content_hash':digest(b'new source')}])
def test_citation_must_match_task_source_snapshot(snapshot,package,access,overrides):
    evidence=evidence_for(snapshot,**overrides)
    receipt=EvidenceReadReceipt(task_id=snapshot.task_id,evidence_id=evidence.evidence_id,content_hash=evidence.content_hash)
    with pytest.raises(DomainError) as exc:
        validate_result(snapshot=snapshot,package=package,result=answer_for(snapshot),evidence=(evidence,),receipts=(receipt,),access=access)
    assert exc.value.code=='evidence_out_of_scope'


def test_read_receipt_cannot_be_reused_from_another_task(snapshot,package,access):
    evidence=evidence_for(snapshot)
    receipt=EvidenceReadReceipt(task_id='task:other',evidence_id=evidence.evidence_id,content_hash=evidence.content_hash)
    with pytest.raises(DomainError) as exc:
        validate_result(snapshot=snapshot,package=package,result=answer_for(snapshot),evidence=(evidence,),receipts=(receipt,),access=access)
    assert exc.value.code=='evidence_not_read'


def test_insufficient_information_is_valid_without_fabricated_fact(snapshot,package,access):
    result=answer_for(snapshot,blocks=(ResultBlock(kind='limitation',text='现有资料不足以判断。'),))
    validate_result(snapshot=snapshot,package=package,result=result,evidence=(),receipts=(),access=access)
    with pytest.raises(ValidationError):ResultBlock(kind='fact',text='没有依据的事实')
    revoked=AccessContext(principal=Principal(principal_id=access.principal.principal_id),workspace_ids=())
    with pytest.raises(DomainError):validate_result(snapshot=snapshot,package=package,result=result,evidence=(),receipts=(),access=revoked)


@pytest.mark.parametrize('field,value,code',[('allow_mutation',False,'mutation_denied'),('allow_external_egress',False,'egress_denied'),('confirmed',False,'confirmation_required')])
def test_tool_registration_does_not_grant_effect_permissions(field,value,code):
    definition=tool_contracts().get(ToolReference(name='evidence.read',version='1.0.0'))
    tool=ToolDefinition.model_validate({**definition.model_dump(),'policy':ToolPolicy(effect='mutating',external_egress=True,requires_confirmation=True)})
    grants={'allowed':(tool.reference,),'allow_mutation':True,'allow_external_egress':True,'confirmed':True,field:value}
    with pytest.raises(DomainError) as exc:require_tool_grant(tool,**grants)
    assert exc.value.code==code


def test_ungranted_tool_and_duplicate_tool_are_rejected():
    registry=tool_contracts()
    tool=registry.list()[0]
    with pytest.raises(DomainError) as exc:
        require_tool_grant(tool,(),allow_mutation=True,allow_external_egress=True,confirmed=True)
    assert exc.value.code=='tool_not_granted'
    with pytest.raises(DomainError):registry.register(tool)


def test_read_only_api_authentication_and_public_contracts(registry):
    token='synthetic-test-token-no-real-secret'
    with TestClient(create_app(registry,api_token=token)) as client:
        assert client.get('/health').json()['analysis_enabled'] is False
        assert client.get('/v1/scenarios').status_code==401
        assert client.get('/v1/scenarios',headers={'Authorization':'Bearer wrong'}).status_code==401
        headers={'Authorization':f'Bearer {token}'}
        listing=client.get('/v1/scenarios',headers=headers)
        assert listing.status_code==200 and len(listing.json()['items'])==3
        assert listing.json()['execution_enabled'] is False
        assert client.get('/v1/contracts/TaskSnapshot',headers=headers).json()['type']=='object'
        assert client.get('/v1/contracts/not-found',headers=headers).status_code==404
        assert client.post('/v1/tasks',headers=headers,json={}).status_code==404
        assert client.get('/openapi.json').status_code==404
    with pytest.raises(ValueError):create_app(registry,api_token='')


def test_offline_demo_does_not_claim_model_or_runtime_execution(registry):
    result=contract_demo(registry)
    assert result['analysis_executed'] is False and result['model_calls']==0 and result['network_calls']==0
    assert len(result['scenarios'])==3
    assert all(s['resource_count']==4 and s['citation_contract_valid'] for s in result['scenarios'])
