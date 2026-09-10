import pytest
from pydantic import ValidationError

from techpack_pdf.models import (
    PipelineInfo,
    ReviewDocument,
    ReviewItem,
    ReviewStatus,
    TranslatorInfo,
)
from techpack_pdf.errors import TechpackError


@pytest.mark.parametrize("value", ["approved", "approved_edited", "skipped"])
def test_review_status_accepts_only_the_three_contract_values(value):
    assert ReviewStatus(value).value == value


def test_review_status_rejects_values_outside_the_contract():
    with pytest.raises(ValueError):
        ReviewStatus("pending")


def test_review_document_requires_explicit_schema_version():
    with pytest.raises(ValidationError, match="schema_version"):
        ReviewDocument.model_validate({})


@pytest.mark.parametrize("field", ["items", "blocking_issues", "review_completed_at"])
def test_review_document_requires_structural_review_fields(field):
    payload = _valid_review_document_payload()
    payload.pop(field)

    with pytest.raises(ValidationError, match=field):
        ReviewDocument.model_validate(payload)


def test_review_document_accepts_explicit_empty_items_issues_and_null_completion():
    document = ReviewDocument.model_validate(_valid_review_document_payload())

    assert document.items == []
    assert document.blocking_issues == []
    assert document.review_completed_at is None


def test_review_item_requires_structural_translation_agent_role_key():
    payload = _valid_review_item_payload()
    payload.pop("translation_agent_role")

    with pytest.raises(ValidationError, match="translation_agent_role"):
        ReviewItem.model_validate(payload)


def test_review_item_accepts_optional_reviewed_target_rect():
    payload = _valid_review_item_payload()
    payload["reviewed_target_rect"] = [110.0, 20.0, 190.0, 40.0]

    item = ReviewItem.model_validate(payload)

    assert item.reviewed_target_rect == [110.0, 20.0, 190.0, 40.0]


@pytest.mark.parametrize(
    "rectangle",
    ([float("nan"), 10.0, 80.0, 30.0], [40.0, 10.0, 40.0, 30.0]),
)
def test_review_item_rejects_nonfinite_or_empty_reviewed_target_rect(rectangle):
    payload = _valid_review_item_payload()
    payload["reviewed_target_rect"] = rectangle

    with pytest.raises(ValidationError):
        ReviewItem.model_validate(payload)


@pytest.mark.parametrize(
    ("execution_mode", "agent_role"),
    [
        ("main_agent", None),
        ("main_agent", "primary-translator"),
        ("subagent", "techpack-translator"),
        ("mixed", "translation-coordinator"),
    ],
)
def test_review_item_accepts_provenance_allowed_by_translation_contract(
    execution_mode,
    agent_role,
):
    payload = _valid_review_item_payload()
    payload.update(
        translation_execution_mode=execution_mode,
        translation_agent_role=agent_role,
    )

    item = ReviewItem.model_validate(payload)

    assert item.translation_agent_role == agent_role


@pytest.mark.parametrize("execution_mode", ["subagent", "mixed"])
@pytest.mark.parametrize("agent_role", [None, "", "   "])
def test_review_item_rejects_missing_delegated_role(execution_mode, agent_role):
    payload = _valid_review_item_payload()
    payload.update(
        translation_execution_mode=execution_mode,
        translation_agent_role=agent_role,
    )

    with pytest.raises(ValidationError, match="translation_agent_role|delegated"):
        ReviewItem.model_validate(payload)


def test_translator_info_rejects_empty_model_but_accepts_unknown():
    with pytest.raises(ValidationError, match="model"):
        TranslatorInfo(
            host="codex",
            execution_mode="main_agent",
            model="",
            prompt_version="v1",
        )

    info = TranslatorInfo(
        host="codex",
        execution_mode="subagent",
        model="unknown",
        prompt_version="v1",
    )
    assert info.model == "unknown"


def test_review_pipeline_requires_parser_and_host_agent_executor():
    pipeline = PipelineInfo(
        parser="pymupdf+mineru",
        translation_executor="host_agent",
        host="codex",
        execution_mode="mixed",
        model="unknown",
        prompt_version="v1",
    )

    assert pipeline.parser == "pymupdf+mineru"
    assert pipeline.translation_executor == "host_agent"

    with pytest.raises(ValidationError, match="translation_executor"):
        PipelineInfo(
            parser="pymupdf+mineru",
            translation_executor="direct_api",
            host="codex",
            execution_mode="mixed",
            model="unknown",
            prompt_version="v1",
        )


def test_review_pipeline_forbids_uncontracted_fields_without_changing_translator_info():
    with pytest.raises(ValidationError, match="extra"):
        PipelineInfo(
            parser="pymupdf",
            translation_executor="host_agent",
            host="codex",
            execution_mode="main_agent",
            model="gpt-5",
            prompt_version="v1",
            agent_role="not-a-pipeline-field",
        )

    translator = TranslatorInfo(
        host="codex",
        execution_mode="subagent",
        model="gpt-5",
        prompt_version="v1",
        agent_role="techpack-translator",
    )
    assert translator.agent_role == "techpack-translator"


@pytest.mark.parametrize("field", ["parser", "host", "model", "prompt_version"])
def test_review_pipeline_rejects_blank_provenance(field):
    values = {
        "parser": "pymupdf",
        "translation_executor": "host_agent",
        "host": "codex",
        "execution_mode": "main_agent",
        "model": "unknown",
        "prompt_version": "v1",
    }
    values[field] = "   "

    with pytest.raises(ValidationError, match=field):
        PipelineInfo(**values)


def test_models_forbid_uncontracted_fields():
    with pytest.raises(ValidationError, match="extra"):
        TranslatorInfo(
            host="codex",
            execution_mode="mixed",
            model="unknown",
            prompt_version="v1",
            credential="must-not-be-accepted",
        )


def test_error_serialization_keeps_operational_details_and_redacts_credentials():
    error = TechpackError(
        "pdf_invalid",
        "Cannot open PDF",
        {
            "item_id": "item-001",
            "page_index": 4,
            "row_number": 7,
            "error_code": "pdf_invalid",
            "api_token": "do-not-leak",
            "description": "full source text is not an operational detail",
        },
    )

    assert error.to_dict() == {
        "code": "pdf_invalid",
        "message": "Cannot open PDF",
        "details": {
            "item_id": "item-001",
            "page_index": 4,
            "row_number": 7,
            "error_code": "pdf_invalid",
        },
    }


def test_error_serialization_rejects_substring_matched_payload_keys():
    error = TechpackError(
        "pdf_invalid",
        "Cannot open PDF",
        {
            "page_source_text": "confidential full text",
            "path_payload": "confidential path payload",
            "item_id": "item-001",
        },
    )

    assert error.to_dict()["details"] == {"item_id": "item-001"}


def _valid_review_document_payload() -> dict:
    return {
        "schema_version": "1.1",
        "job_id": "abc123def456-20260822T000000Z",
        "source": {
            "filename": "techpack.pdf",
            "sha256": "a" * 64,
            "page_count": 1,
        },
        "glossary": {
            "filename": "glossary.csv",
            "sha256": "b" * 64,
        },
        "pipeline": {
            "parser": "pymupdf+mineru",
            "translation_executor": "host_agent",
            "host": "codex",
            "execution_mode": "subagent",
            "model": "unknown",
            "prompt_version": "1.0",
        },
        "items": [],
        "blocking_issues": [],
        "review_completed_at": None,
    }


def _valid_review_item_payload() -> dict:
    return {
        "item_id": "p001-i001",
        "page_index": 0,
        "page_type": "bom",
        "source_text": "Shell 12 mm",
        "normalized_text": "shell 12 mm",
        "source_bbox": [10.0, 10.0, 90.0, 20.0],
        "source_kind": "body",
        "coordinate_confidence": "high",
        "decision_reason": "field_rule",
        "locked_tokens": ["12", "mm"],
        "glossary_hits": [],
        "suggested_translation": "大身 12 mm",
        "reviewed_translation": None,
        "review_status": None,
        "risk_level": "low",
        "translation_host": "codex",
        "translation_execution_mode": "main_agent",
        "translation_model": "unknown",
        "translation_agent_role": None,
        "translation_prompt_version": "1.0",
        "placement_strategy": "same_region",
        "target_rect": [100.0, 10.0, 180.0, 30.0],
        "reviewed_target_rect": None,
        "font_size": 6.0,
        "leader_line": None,
        "warnings": [],
    }
