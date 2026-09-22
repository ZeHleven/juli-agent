"""Synthetic contract validation, deliberately no AI inference or tool execution."""
from juli.core.common import canonical_json, digest
from juli.core.evidence import AnalysisResult, Evidence, EvidenceLocator, EvidenceReadReceipt, ResultBlock, validate_result
from juli.core.resources import AccessContext, Principal, ResourceBinding, ResourceVersion, Workspace
from juli.core.scenarios import ScenarioRegistry
from juli.core.tasks import Conversation, TaskState, TaskStatus, admit_task, transition_task


def contract_demo(registry: ScenarioRegistry) -> dict:
    workspace = Workspace(workspace_id="workspace:demo", owner_id="principal:demo", name="合成资料演示")
    access = AccessContext(principal=Principal(principal_id=workspace.owner_id), workspace_ids=(workspace.workspace_id,))
    conversation = Conversation(conversation_id="conversation:demo", workspace_id=workspace.workspace_id, created_by=workspace.owner_id)
    # All names and facts below are synthetic. Four resources demonstrate the
    # removal of the old two-resource limit; roles come from the package itself.
    results = []
    for index, package in enumerate(registry.list(), start=1):
        resources, bindings, texts = [], [], {}
        for role_index, role in enumerate(package.manifest.resource_roles, start=1):
            count = max(role.min_count, min(2, role.max_count))
            for resource_index in range(1, count+1):
                reference = f"version:{index}:{role_index}:{resource_index}"
                text = f"合成示例资料 {index}-{role_index}-{resource_index}：用于验证资源绑定和原文引用。\n"
                texts[reference] = text
                resources.append(ResourceVersion(resource_id=f"resource:{index}:{role_index}:{resource_index}", version_id=reference, workspace_id=workspace.workspace_id, media_type=role.media_types[0] if role.media_types else "text/plain", display_name=f"示例资料 {role_index}-{resource_index}", content_hash=digest(text.encode("utf-8"))))
                bindings.append(ResourceBinding(role=role.name, version_id=reference))
        task = admit_task(access=access, conversation=conversation, task_id=f"task:demo:{index}", question="验证这些合成资料能按本场景绑定，并且原文引用关系一致。", package=package, resources=tuple(resources), bindings=tuple(bindings))
        first = task.resources[0]
        evidence = Evidence(evidence_id=f"evidence:demo:{index}", workspace_id=workspace.workspace_id, resource_version_id=first.version_id, resource_content_hash=first.content_hash, parser_version="synthetic-fixture:1", locator=EvidenceLocator(paragraph=1), text=texts[first.version_id], content_hash=digest(texts[first.version_id].encode("utf-8")))
        # These receipts are synthetic fixtures, not claims of a running parser.
        receipt = EvidenceReadReceipt(task_id=task.task_id, evidence_id=evidence.evidence_id, content_hash=evidence.content_hash)
        answer = AnalysisResult(task_id=task.task_id, blocks=(ResultBlock(kind="fact", text=evidence.text, evidence_ids=(evidence.evidence_id,)),), scenario_payload_json=canonical_json({"summary":"合成合同校验完成；未进行 AI 分析。"}))
        validate_result(snapshot=task, package=package, result=answer, evidence=(evidence,), receipts=(receipt,), access=access)
        state = TaskState(task_id=task.task_id, workspace_id=task.workspace_id)
        history = [state.status.value]
        for target, question in ((TaskStatus.PENDING,"合成追问：请补充分析目标。"),(TaskStatus.RUNNING,None),(TaskStatus.COMPLETED,None)):
            state = transition_task(state, access=access, expected_version=state.version, target=target, pending_question=question)
            history.append(state.status.value)
        results.append({"scenario":package.manifest.id,"resource_count":len(task.resources),"roles":sorted({b.role for b in task.bindings}),"task_fingerprint":task.fingerprint,"scenario_fingerprint":package.fingerprint,"citation_contract_valid":True,"lifecycle_contract":history})
    return {"mode":"offline_contract_validation","analysis_executed":False,"model_calls":0,"network_calls":0,"persistence_enabled":False,"scenarios":results}

