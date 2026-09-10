"""Strict version 1.1 payload contracts shared by the TechPack workflow."""

import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageType(StrEnum):
    GENERAL_INFO = "general_info"
    BOM = "bom"
    MEASUREMENT = "measurement"
    TECHNICAL_DRAWING = "technical_drawing"
    LABEL_PACK = "label_pack"
    SAMPLE_REVIEW = "sample_review"
    STYLE_SAMPLE = "style_sample"
    HOW_TO_MEASURE = "how_to_measure"
    CONSTRUCTION_DETAIL = "construction_detail"
    CATEGORY_FIELDS = "category_fields"
    UNKNOWN = "unknown"


class DecisionReason(StrEnum):
    PAGE_RULE = "page_rule"
    FIELD_RULE = "field_rule"
    GLOSSARY_HIT = "glossary_hit"
    ACTIONABLE_REVIEW = "actionable_review"
    MIXED_TEXT = "mixed_text"
    MANUAL_CANDIDATE = "manual_candidate"
    SKIPPED_ADMIN = "skipped_admin"
    SKIPPED_CODE = "skipped_code"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    LOW_CONFIDENCE = "low_confidence"


class CoordinateConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(StrEnum):
    APPROVED = "approved"
    APPROVED_EDITED = "approved_edited"
    SKIPPED = "skipped"


class ExecutionMode(StrEnum):
    MAIN_AGENT = "main_agent"
    SUBAGENT = "subagent"
    MIXED = "mixed"


class TranslatorInfo(StrictModel):
    host: str = Field(min_length=1)
    execution_mode: ExecutionMode
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    agent_role: str | None = None


class PipelineInfo(StrictModel):
    parser: str = Field(min_length=1)
    translation_executor: Literal["host_agent"]
    host: str = Field(min_length=1)
    execution_mode: ExecutionMode
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)

    @field_validator("parser", "host", "model", "prompt_version")
    @classmethod
    def provenance_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pipeline provenance must not be blank")
        return value


class FileArtifact(StrictModel):
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: Path | None = None
    page_count: int | None = Field(default=None, ge=0)


class TranslationRequestItem(StrictModel):
    item_id: str = Field(min_length=1)
    page_index: int = Field(ge=0)
    source_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    locked_tokens: list[str] = Field(default_factory=list)
    glossary_hits: list[dict[str, Any]] = Field(default_factory=list)
    translator: TranslatorInfo


class TranslationResponseItem(StrictModel):
    item_id: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    translator: TranslatorInfo


class ReviewItem(StrictModel):
    item_id: str = Field(min_length=1)
    page_index: int = Field(ge=0)
    page_type: PageType
    source_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    source_bbox: list[float] = Field(min_length=4, max_length=4)
    source_kind: str = Field(min_length=1)
    coordinate_confidence: CoordinateConfidence
    decision_reason: DecisionReason
    locked_tokens: list[str] = Field(default_factory=list)
    glossary_hits: list[dict[str, Any]] = Field(default_factory=list)
    suggested_translation: str | None = None
    reviewed_translation: str | None = None
    review_status: ReviewStatus | None = None
    risk_level: str = Field(default="medium", min_length=1)
    translation_host: str = Field(min_length=1)
    translation_execution_mode: ExecutionMode
    translation_model: str = Field(min_length=1)
    translation_agent_role: str | None
    translation_prompt_version: str = Field(min_length=1)
    placement_strategy: str | None = None
    target_rect: list[float] | None = Field(default=None, min_length=4, max_length=4)
    reviewed_target_rect: list[float] | None = Field(
        default=None, min_length=4, max_length=4
    )
    font_size: float | None = Field(default=None, gt=0)
    reviewed_source_bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    reviewed_font_size: float | None = Field(default=None, ge=5, le=24, allow_inf_nan=False)
    leader_line: list[float] | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("suggested_translation", "reviewed_translation")
    @classmethod
    def translations_are_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("translation must not be blank")
        return value

    @field_validator("translation_agent_role")
    @classmethod
    def translation_agent_role_is_normalized(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError(
                "translation_agent_role must be nonblank and have no surrounding whitespace"
            )
        return value

    @field_validator("reviewed_target_rect", "reviewed_source_bbox")
    @classmethod
    def reviewed_target_rect_is_finite_and_nonempty(
        cls, value: list[float] | None
    ) -> list[float] | None:
        if value is None:
            return None
        if not all(math.isfinite(number) for number in value):
            raise ValueError("reviewed_target_rect coordinates must be finite")
        if value[0] >= value[2] or value[1] >= value[3]:
            raise ValueError("reviewed_target_rect must have positive width and height")
        return value

    @model_validator(mode="after")
    def delegated_translation_has_role(self) -> "ReviewItem":
        if (
            self.translation_execution_mode is not ExecutionMode.MAIN_AGENT
            and self.translation_agent_role is None
        ):
            raise ValueError("delegated translation requires translation_agent_role")
        return self


class ReviewDocument(StrictModel):
    schema_version: Literal["1.1"]
    job_id: str = Field(min_length=1)
    source: FileArtifact
    glossary: FileArtifact
    pipeline: PipelineInfo
    items: list[ReviewItem]
    blocking_issues: list[dict[str, Any]]
    review_completed_at: datetime | None


class JobManifest(StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    job_id: str = Field(min_length=1)
    source: FileArtifact
    glossary: FileArtifact
    job_dir: Path
    created_at: datetime
