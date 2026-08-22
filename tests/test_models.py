import pytest
from pydantic import ValidationError

from techpack_pdf.models import ReviewStatus, TranslatorInfo
from techpack_pdf.errors import TechpackError


@pytest.mark.parametrize("value", ["approved", "approved_edited", "skipped"])
def test_review_status_accepts_only_the_three_contract_values(value):
    assert ReviewStatus(value).value == value


def test_review_status_rejects_values_outside_the_contract():
    with pytest.raises(ValueError):
        ReviewStatus("pending")


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
