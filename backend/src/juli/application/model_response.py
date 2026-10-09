"""Model-facing decisions translated into the unchanged stored result contract."""
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, TypeAdapter

from juli.core.common import Contract, Reference, canonical_json
from juli.core.evidence import AnalysisResult, ResultBlock
from juli.core.runtime import Clarification


RESPONSE_PROTOCOL = 'native-analysis-v3'

# Shared by ordinary and opt-in review paths; keep one model-facing meaning for
# the unchanged public result contract. These are instructions, not output repair.
OUTPUT_CONTRACT = '''Analysis: kind="analysis",task_id,blocks,scenario_payload (OBJECT per scenario_result_schema).
Copy task_id, never input fields. fact requires original citations; evidence_ids unique.
User answers: self-reported inference with evidence_ids=[], never document facts.
State verification limits; do not repeat resolved questions.
'''

EVIDENCE_WORKFLOW = '''Read relevant originals before analysis/clarification; snippets are not reads.
evidence.read uses earlier search evidence_id, never version IDs/guesses.
Wait for search results; batch independent calls only.
'''


class AnalysisResponse(Contract):
    kind: Literal['analysis']
    task_id: Reference
    blocks: tuple[ResultBlock, ...] = Field(min_length=1, max_length=100)
    scenario_payload: dict[str, JsonValue]


class InteractiveAnalysisResponse(AnalysisResponse):
    # Required nullable declaration, never synthesized from prose or defaulted.
    critical_question: Annotated[str, Field(min_length=1, max_length=4000, pattern=r'\S')] | None


def parse_interactive_analysis(decoded: Any) -> tuple[AnalysisResult, str | None]:
    """Ordinary durable entry: a candidate and its explicit readiness declaration."""
    response = InteractiveAnalysisResponse.model_validate(decoded)
    return _result(response), response.critical_question


def _result(response: AnalysisResponse) -> AnalysisResult:
    return AnalysisResult(task_id=response.task_id, blocks=response.blocks,
                          scenario_payload_json=canonical_json(response.scenario_payload))


def parse_analysis_response(decoded: Any) -> AnalysisResult:
    """Accept the explicit envelope or an untagged legacy result, without repair.

    Scenario schema and evidence authorization still belong to the original
    result validation boundary. An explicit discriminator never falls back to
    legacy parsing, including when the new envelope is malformed.
    """
    if isinstance(decoded, dict) and 'kind' in decoded:
        # Connection/review compatibility may consume an explicitly ready v3
        # result, but cannot discard an unresolved question. Durable ordinary
        # execution must use parse_interactive_analysis, never this fallback.
        if 'critical_question' in decoded:
            result, question = parse_interactive_analysis(decoded)
            if question is not None:
                raise ValueError('Interactive clarification requires the durable decision path')
            return result
        response = AnalysisResponse.model_validate(decoded)
        return _result(response)
    return AnalysisResult.model_validate(decoded)


def response_schema(*, allow_clarification: bool = False) -> dict:
    """Advertise only current model output, not the legacy compatibility path.

    The installed scenario schema is supplied separately; embedding it here
    would change the scope of its existing local JSON Schema references.
    """
    if not allow_clarification:
        schema = AnalysisResponse.model_json_schema()
    else:
        decision = Annotated[InteractiveAnalysisResponse | Clarification, Field(discriminator='kind')]
        schema = TypeAdapter(decision).json_schema()
        # OpenAPI's mapping duplicates oneOf + each kind const; JSON Schema
        # validation does not use it. Retain every validation constraint.
        schema['discriminator'].pop('mapping', None)
    # Mirror the existing Python cross-field validator. Pydantic does not infer
    # these constraints from fact_requires_evidence, so advertising its default
    # schema alone promised an output the runtime would reject (E05).
    block = schema['$defs']['ResultBlock']
    block['properties']['evidence_ids']['uniqueItems'] = True
    block['allOf'] = [{
        'if': {'properties': {'kind': {'const': 'fact'}}, 'required': ['kind']},
        'then': {'required': ['evidence_ids'], 'properties': {'evidence_ids': {'minItems': 1}}},
    }]
    # Pydantic's display titles repeat field names in every model request. Keep
    # validation keywords and named properties intact; never transform data literals.
    def compact(node):
        if isinstance(node, list):
            return [compact(item) for item in node]
        if not isinstance(node, dict):
            return node
        return {key: (value if key in {'const', 'enum', 'default', 'examples'} else
                      {name: compact(item) for name, item in value.items()}
                      if key in {'properties', '$defs'} else compact(value))
                for key, value in node.items() if key != 'title'}
    return compact(schema)
