import pytest
from pydantic import ValidationError

from techpack_pdf.models import PipelineInfo, ReviewDocument, ReviewStatus, TranslatorInfo
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
