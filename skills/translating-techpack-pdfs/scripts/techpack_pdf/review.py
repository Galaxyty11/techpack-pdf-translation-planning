"""Build a self-contained review page and validate completed review documents."""

from __future__ import annotations

import base64
import binascii
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf
from pydantic import ValidationError

from .errors import TechpackError
from .glossary import normalize_term
from .inputs import sha256_file
from .models import JobManifest, PipelineInfo, ReviewDocument, ReviewItem, ReviewStatus
from .selection import LockedText, LockedToken, validate_locked_tokens


_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "review-template.html"
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "assets" / "review-ui.js"
_DATA_PLACEHOLDER = "__TECHPACK_REVIEW_DATA__"
_SCRIPT_PLACEHOLDER = "__TECHPACK_REVIEW_SCRIPT__"
_PNG_PREFIX = "data:image/png;base64,"
_MUTABLE_REVIEW_FIELDS = frozenset(
    {"review_status", "reviewed_translation", "reviewed_target_rect", "reviewed_source_bbox", "reviewed_font_size"}
)
_DECISION_EXPLANATIONS = {
    "page_rule": "这段文字属于本页需要翻译的内容。",
    "field_rule": "这段文字属于需要翻译的业务字段。",
    "glossary_hit": "这段文字命中了术语表。",
    "actionable_review": "这段文字包含需要执行或确认的意见。",
    "mixed_text": "文字中包含需要原样保留的数字、单位或代码，其余内容需要翻译。",
    "manual_candidate": "系统建议人工确认这段文字是否需要翻译。",
    "skipped_admin": "这是管理信息，通常不需要翻译。",
    "skipped_code": "主要内容是编号或代码，通常不需要翻译。",
    "skipped_duplicate": "这段文字与其他内容重复，通常不需要再次翻译。",
    "low_confidence": "系统判断不够确定，请人工确认是否需要翻译。",
}
_COORDINATE_EXPLANATIONS = {
    "high": "定位准确",
    "medium": "位置基本可靠，请看一眼红框是否正确",
    "low": "位置可能不准，请重点检查红框",
}
_RISK_EXPLANATIONS = {
    "low": ("低风险", "未发现明显排版问题，通常可以直接审核。"),
    "medium": ("需要留意", "发现少量可能影响排版的情况，请检查后再审核。"),
    "high": ("请重点检查", "发现可能影响审核或排版的问题，请逐项确认。"),
}
_WARNING_EXPLANATIONS = {
    "native_only_degradation": "本页使用 PDF 原生文字解析，请检查原文是否完整。",
    "coordinate_confidence": "原文位置可能不够准确，请检查左侧红框。",
    "unknown_model": "翻译模型信息不完整，请人工核对译文。",
    "manual_placement_required": "系统没有找到完全安全的位置，请把红色译文拖到页面空白处。",
}
_PAGE_TYPE_LABELS = {
    "general_info": "基本信息",
    "bom": "物料表",
    "measurement": "尺寸表",
    "technical_drawing": "技术图",
    "label_pack": "标签与包装",
    "sample_review": "样衣评审",
    "style_sample": "款式样衣",
    "how_to_measure": "测量方法",
    "category_fields": "分类字段",
    "construction_detail": "结构细节",
    "unknown": "未分类",
}
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2}


def build_review_html(job: JobManifest, output: Any) -> str:
    """Return a self-contained review page bound to ``job`` and ``output`` data."""
    try:
        manifest = JobManifest.model_validate(job)
        raw_output = _mapping(output)
        pages = _pages(raw_output)
        items = _items(raw_output)
        pipeline = _pipeline(raw_output, items)
        page_count = _manifest_page_count(manifest, pages)
        blocking_issues = _blocking_issues(raw_output)
        payload = {
            "schema_version": "1.1",
            "job_id": manifest.job_id,
            "source": {
                "filename": manifest.source.filename,
                "sha256": manifest.source.sha256,
                "page_count": page_count,
            },
            "glossary": {
                "filename": manifest.glossary.filename,
                "sha256": manifest.glossary.sha256,
            },
            "pipeline": pipeline,
            "items": items,
            "business_explanations": _business_explanations(items),
            "review_navigation": _review_navigation(items),
            "blocking_issues": blocking_issues,
            "review_completed_at": None,
            "pages": pages,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encoded = (
            encoded.replace("<", "\\u003c")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        controller = _SCRIPT_PATH.read_text(encoding="utf-8")
        if template.count(_DATA_PLACEHOLDER) != 1:
            raise ValueError("review template data placeholder is invalid")
        if template.count(_SCRIPT_PLACEHOLDER) != 1:
            raise ValueError("review template script placeholder is invalid")
        replacements = [
            (template.index(_DATA_PLACEHOLDER), _DATA_PLACEHOLDER, encoded),
            (template.index(_SCRIPT_PLACEHOLDER), _SCRIPT_PLACEHOLDER, controller),
        ]
        rendered = template
        for offset, placeholder, value in sorted(replacements, reverse=True):
            rendered = rendered[:offset] + value + rendered[offset + len(placeholder) :]
        return rendered
    except TechpackError:
        raise
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        raise TechpackError(
            "review_page_invalid",
            "Offline review page data is invalid",
            {"error_code": "review_page_invalid"},
        ) from None


def _review_navigation(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[int, list[tuple[int, Mapping[str, Any]]]] = {}
    for original_index, item in enumerate(items):
        page_index = int(item.get("page_index", 0))
        grouped.setdefault(page_index, []).append((original_index, item))

    groups: list[dict[str, Any]] = []
    for page_index, indexed_items in grouped.items():
        ordered = sorted(
            indexed_items,
            key=lambda value: (
                1 if value[1].get("review_status") else 0,
                _RISK_ORDER.get(str(value[1].get("risk_level", "")), 3),
                value[0],
            ),
        )
        page_items = [item for _index, item in ordered]
        risk_counts = {
            risk: sum(item.get("risk_level") == risk for item in page_items)
            for risk in ("high", "medium", "low")
        }
        highest_risk = min(
            (str(item.get("risk_level", "")) for item in page_items),
            key=lambda risk: _RISK_ORDER.get(risk, 3),
            default="low",
        )
        page_type = str(page_items[0].get("page_type", "unknown"))
        groups.append(
            {
                "page_index": page_index,
                "page_number": page_index + 1,
                "page_type": page_type,
                "page_type_label": _PAGE_TYPE_LABELS.get(page_type, page_type),
                "item_ids": [str(item.get("item_id", "")) for item in page_items],
                "unreviewed_count": sum(
                    not item.get("review_status") for item in page_items
                ),
                "risk_counts": risk_counts,
                "highest_risk": highest_risk,
            }
        )

    groups.sort(
        key=lambda group: (
            _RISK_ORDER.get(str(group["highest_risk"]), 3),
            group["page_index"],
        )
    )
    return {
        "default_open_page_index": groups[0]["page_index"] if groups else None,
        "page_type_labels": _PAGE_TYPE_LABELS,
        "groups": groups,
    }


def _business_explanations(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    explanations: dict[str, Any] = {}
    for item in items:
        item_id = str(item.get("item_id", ""))
        decision = str(item.get("decision_reason", ""))
        confidence = str(item.get("coordinate_confidence", ""))
        risk_level = str(item.get("risk_level", ""))
        source_bbox = item.get("source_bbox")
        target_rect = item.get("target_rect")
        warnings = item.get("warnings", [])

        if isinstance(source_bbox, list) and len(source_bbox) == 4:
            confidence_text = _COORDINATE_EXPLANATIONS.get(
                confidence, "请人工检查红框是否正确"
            )
            source_position = (
                f"原文位置：已在左侧预览中用红框标出（{confidence_text}）。"
            )
        else:
            source_position = "原文位置：未能准确标出，请人工核对页面内容。"
        target_position = (
            "译文位置：已在左侧预览中用蓝框标出。"
            if isinstance(target_rect, list) and len(target_rect) == 4
            else "译文位置：写入 PDF 时自动安排。"
        )
        risk_label, risk_summary = _RISK_EXPLANATIONS.get(
            risk_level,
            ("请人工检查", "系统没有给出明确的排版风险等级，请人工确认。"),
        )
        warning_values = warnings if isinstance(warnings, list) else []
        warning_explanations = list(
            dict.fromkeys(
                _WARNING_EXPLANATIONS.get(
                    str(warning), "翻译过程提示需要人工检查。"
                )
                for warning in warning_values
            )
        )
        explanations[item_id] = {
            "decision_reason": _DECISION_EXPLANATIONS.get(
                decision, "系统已选中这段文字，请人工确认。"
            ),
            "coordinates": [source_position, target_position],
            "layout_risk": {
                "level": risk_level,
                "label": risk_label,
                "summary": risk_summary,
                "warnings": warning_explanations,
            },
        }
    return explanations


def load_review(
    path: str | Path,
    job: JobManifest,
    expected_output: Any,
) -> ReviewDocument:
    """Load a completed review bound to the job and trusted generated output."""
    review_path = Path(path)
    try:
        manifest = JobManifest.model_validate(job)
        if manifest.source.path is None or manifest.glossary.path is None:
            raise ValueError("job input paths are required")
        source_path = Path(manifest.source.path)
        glossary_path = Path(manifest.glossary.path)
        raw_expected_output = _mapping(expected_output)
        trusted_item_values = _items(raw_expected_output)
        trusted_items = [ReviewItem.model_validate(item) for item in trusted_item_values]
        trusted_pipeline = PipelineInfo.model_validate(
            _pipeline(raw_expected_output, trusted_item_values)
        )
        trusted_blocking_issues = _blocking_issues(raw_expected_output)
    except (TypeError, ValueError, ValidationError):
        _fail("review_job_invalid", "Review job binding is invalid")
    try:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        _validate_review_timestamp(payload)
        review = ReviewDocument.model_validate(payload)
    except ValidationError as error:
        if _has_reviewed_target_rect_error(error):
            _fail("review_target_invalid", "人工译文位置无效，请重新导出审核结果")
        _fail("review_schema_invalid", "Review JSON does not match schema 1.1")
    except (OSError, TypeError, json.JSONDecodeError):
        _fail("review_schema_invalid", "Review JSON does not match schema 1.1")

    source_hash = _current_hash(source_path, "source")
    glossary_hash = _current_hash(glossary_path, "glossary")
    page_count = _pdf_page_count(source_path)

    if review.job_id != manifest.job_id:
        _fail("review_job_mismatch", "Review job does not match")
    if (
        review.source.filename != manifest.source.filename
        or review.source.filename != source_path.name
    ):
        _fail("review_source_filename_mismatch", "Review source filename does not match")
    if review.source.sha256 != manifest.source.sha256 or review.source.sha256 != source_hash:
        _fail("review_source_hash_mismatch", "Review source hash does not match")
    if review.source.page_count != manifest.source.page_count or review.source.page_count != page_count:
        _fail("review_source_page_count_mismatch", "Review source page count does not match")
    if (
        review.glossary.filename != manifest.glossary.filename
        or review.glossary.filename != glossary_path.name
    ):
        _fail("review_glossary_filename_mismatch", "Review glossary filename does not match")
    if review.glossary.sha256 != manifest.glossary.sha256 or review.glossary.sha256 != glossary_hash:
        _fail("review_glossary_hash_mismatch", "Review glossary hash does not match")
    _validate_item_binding(review.items, trusted_items)
    trusted_items_by_id = {item.item_id: item for item in trusted_items}
    moved_items = [item for item in review.items if item.reviewed_target_rect is not None or item.reviewed_source_bbox is not None]
    if moved_items:
        try:
            page_rects = {
                page["page_index"]: pymupdf.Rect(0, 0, page["width"], page["height"])
                for page in _pages(raw_expected_output, load_thumbnails=False)
            }
        except (TypeError, ValueError, RuntimeError):
            _fail("review_job_invalid", "Review job binding is invalid")
    for item in moved_items:
        if item.reviewed_source_bbox is not None and not page_rects[item.page_index].contains(pymupdf.Rect(item.reviewed_source_bbox)):
            _fail("review_source_rect_invalid", "重新框选的原文位置超出页面，请重新调整")
        _validate_reviewed_target_rect(
            item, trusted_items_by_id[item.item_id], page_rects[item.page_index]
        )
    _validate_pipeline(review, trusted_pipeline)
    if review.blocking_issues != trusted_blocking_issues:
        _fail("review_blocking_mismatch", "Review blocking issues do not match the job")
    if trusted_blocking_issues:
        _fail("review_blocked", "Review contains unresolved blocking issues")
    if review.review_completed_at is None:
        _fail("review_incomplete", "Review completion time is missing")

    seen_ids: set[str] = set()
    for item in review.items:
        if item.item_id in seen_ids:
            _fail("review_item_duplicate", "Review contains duplicate item identifiers")
        seen_ids.add(item.item_id)
        if item.page_index >= page_count:
            _fail("review_item_page_mismatch", "Review item page is outside the source PDF")
        if item.review_status is None:
            _fail("review_status_incomplete", "Every review item needs an explicit status")
        if item.review_status is ReviewStatus.APPROVED_EDITED and not _nonblank(
            item.reviewed_translation
        ):
            _fail(
                "review_edited_translation_missing",
                "An edited approval needs a reviewed translation",
            )
        if item.review_status is ReviewStatus.APPROVED and not _nonblank(
            item.suggested_translation
        ):
            _fail("review_translation_missing", "An approval needs a validated translation")
        base_provenance = all(
            _nonblank(value)
            for value in (
                item.translation_host,
                item.translation_model,
                item.translation_prompt_version,
            )
        )
        if item.translation_execution_mode.value == "main_agent":
            role_provenance = item.translation_agent_role is None or _nonblank(
                item.translation_agent_role
            )
        else:
            role_provenance = _nonblank(item.translation_agent_role)
        if not base_provenance or not role_provenance:
            _fail(
                "review_provenance_incomplete",
                "Every review item needs complete translation provenance",
            )
        if item.review_status is not ReviewStatus.SKIPPED:
            _validate_final_translation(item)
    return review


def _mapping(value: Any) -> dict[str, Any]:
    converted = _json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("review output must be a mapping")
    return converted


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _blocking_issues(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = _json_value(output.get("blocking_issues", []))
    if not isinstance(value, list) or not all(isinstance(issue, dict) for issue in value):
        raise ValueError("blocking_issues must be a list of mappings")
    return value


def _pipeline(output: dict[str, Any], items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw = output.get("pipeline")
    aggregate = _aggregate_item_provenance(items)
    if raw is None:
        raw = {
            "parser": output.get("parser"),
            "translation_executor": output.get("translation_executor", "host_agent"),
            **aggregate,
        }
    info = PipelineInfo.model_validate(raw)
    if items and any(
        getattr(info, field) != value for field, value in aggregate.items()
    ):
        raise ValueError("pipeline contradicts item provenance")
    return info.model_dump(mode="json")


def _aggregate_item_provenance(items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    if not items:
        return {}
    fields = {
        "host": "translation_host",
        "execution_mode": "translation_execution_mode",
        "model": "translation_model",
        "prompt_version": "translation_prompt_version",
    }
    aggregate: dict[str, str] = {}
    for pipeline_field, item_field in fields.items():
        values = {str(item.get(item_field, "")).strip() for item in items}
        if not values or "" in values:
            raise ValueError("item translation provenance is incomplete")
        aggregate[pipeline_field] = next(iter(values)) if len(values) == 1 else "mixed"
    return aggregate


def _items(output: dict[str, Any]) -> list[dict[str, Any]]:
    if "items" in output:
        raw_items = output["items"]
        if not isinstance(raw_items, list):
            raise TypeError("items must be a list")
        items = [_mapping(item) for item in raw_items]
        for item in items:
            item["review_status"] = None
            item["reviewed_translation"] = None
            item["reviewed_target_rect"] = None
            item["reviewed_source_bbox"] = None
            item["reviewed_font_size"] = None
        return items

    candidates = output.get("candidates")
    translations = output.get("translations")
    if not isinstance(candidates, list) or not isinstance(translations, list):
        raise ValueError("items or candidates with translations are required")
    translated = {_mapping(value)["item_id"]: _mapping(value) for value in translations}
    items: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = _mapping(raw_candidate)
        if not candidate.get("should_translate", True):
            continue
        translation = translated[candidate["item_id"]]
        translator = _mapping(translation["translator"])
        bbox = candidate.get("source_bbox")
        if bbox is None:
            raise ValueError("review candidates need source coordinates")
        glossary_hits = candidate.get("glossary_hits", [])
        locked_tokens = candidate.get("locked_tokens")
        if locked_tokens is None:
            locked_text = candidate.get("locked_text", {})
            locked_tokens = [token["value"] for token in locked_text.get("tokens", [])]
        risks: list[str] = list(translation.get("warnings", []))
        confidence = candidate.get("coordinate_confidence", "low")
        model = translator.get("model")
        if confidence != "high":
            risks.append("coordinate_confidence")
        if model == "unknown":
            risks.append("unknown_model")
        items.append(
            {
                "item_id": candidate["item_id"],
                "page_index": candidate["page_index"],
                "page_type": candidate["page_type"],
                "source_text": candidate["source_text"],
                "normalized_text": candidate["normalized_text"],
                "source_bbox": bbox,
                "source_kind": candidate["source_kind"],
                "coordinate_confidence": confidence,
                "decision_reason": candidate["decision_reason"],
                "locked_tokens": locked_tokens,
                "glossary_hits": glossary_hits,
                "suggested_translation": translation.get(
                    "translated_text", translation.get("translation")
                ),
                "reviewed_translation": None,
                "review_status": None,
                "risk_level": "low" if candidate.get("auto_approvable") and not risks else "high",
                "translation_host": translator.get("host"),
                "translation_execution_mode": translator.get("execution_mode"),
                "translation_model": model,
                "translation_agent_role": translator.get("agent_role"),
                "translation_prompt_version": translator.get("prompt_version"),
                "placement_strategy": candidate.get("placement_strategy"),
                "target_rect": candidate.get("target_rect"),
                "reviewed_target_rect": None,
                "font_size": candidate.get("font_size"),
                "leader_line": candidate.get("leader_line"),
                "warnings": risks,
            }
        )
    return items


def _pages(
    output: dict[str, Any], *, load_thumbnails: bool = True
) -> list[dict[str, Any]]:
    raw_pages = output.get("pages")
    if raw_pages is None and "thumbnails" in output:
        raw_pages = [
            {"page_index": index, "thumbnail": thumbnail}
            for index, thumbnail in enumerate(output["thumbnails"])
        ]
    if not isinstance(raw_pages, list) or not raw_pages:
        raise ValueError("at least one review page is required")
    pages: list[dict[str, Any]] = []
    for position, raw_page in enumerate(raw_pages):
        page = _mapping(raw_page)
        thumbnail = page.get("thumbnail", page.get("thumbnail_data_uri", page.get("thumbnail_path")))
        width = page.get("width")
        height = page.get("height")
        crop_box = page.get("crop_box")
        if (width is None or height is None) and isinstance(crop_box, list) and len(crop_box) == 4:
            width = float(crop_box[2]) - float(crop_box[0])
            height = float(crop_box[3]) - float(crop_box[1])
        pages.append(
            {
                "page_index": int(page.get("page_index", position)),
                "width": float(width) if width is not None else None,
                "height": float(height) if height is not None else None,
                "thumbnail": (
                    _thumbnail_data_uri(thumbnail) if load_thumbnails else thumbnail
                ),
            }
        )
    return pages


def _thumbnail_data_uri(value: Any) -> str:
    if isinstance(value, str) and value.startswith(_PNG_PREFIX):
        encoded = value[len(_PNG_PREFIX) :]
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("thumbnail is not valid base64") from None
    elif isinstance(value, (str, Path)):
        decoded = Path(value).read_bytes()
        encoded = base64.b64encode(decoded).decode("ascii")
    else:
        raise ValueError("thumbnail must be a PNG path or data URI")
    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("thumbnail must be PNG")
    return _PNG_PREFIX + encoded


def _manifest_page_count(job: JobManifest, pages: Sequence[Mapping[str, Any]]) -> int:
    page_count = job.source.page_count
    if page_count is None and job.source.path is not None:
        page_count = _pdf_page_count(Path(job.source.path))
    if page_count is None:
        page_count = len(pages)
    if page_count != len(pages):
        raise ValueError("review page count does not match job")
    expected_indexes = list(range(page_count))
    actual_indexes = [int(page["page_index"]) for page in pages]
    if actual_indexes != expected_indexes:
        raise ValueError("review pages must be complete and ordered")
    return page_count


def _current_hash(path: Path, kind: str) -> str:
    try:
        return sha256_file(path)
    except OSError:
        _fail(f"review_{kind}_unavailable", f"Current {kind} file cannot be read")


def _pdf_page_count(path: Path) -> int:
    try:
        document = pymupdf.open(path)
        try:
            if document.needs_pass or document.page_count <= 0:
                raise ValueError("source PDF is unavailable")
            return document.page_count
        finally:
            document.close()
    except (OSError, RuntimeError, ValueError):
        _fail("review_source_unavailable", "Current source PDF cannot be inspected")


def _validate_item_binding(
    reviewed_items: Sequence[ReviewItem], expected_items: Sequence[ReviewItem]
) -> None:
    reviewed_ids = [item.item_id for item in reviewed_items]
    expected_ids = [item.item_id for item in expected_items]
    if len(reviewed_ids) != len(set(reviewed_ids)):
        _fail("review_item_duplicate", "Review contains duplicate item identifiers")
    if len(expected_ids) != len(set(expected_ids)):
        _fail("review_job_invalid", "Expected review items contain duplicate identifiers")
    if set(reviewed_ids) != set(expected_ids):
        _fail("review_item_set_mismatch", "Review item set does not match the job")

    expected_by_id = {item.item_id: item for item in expected_items}
    immutable_fields = set(ReviewItem.model_fields) - _MUTABLE_REVIEW_FIELDS
    for item in reviewed_items:
        actual = item.model_dump(mode="json", include=immutable_fields)
        expected = expected_by_id[item.item_id].model_dump(
            mode="json", include=immutable_fields
        )
        if actual != expected:
            _fail("review_item_mismatch", "Review item content does not match the job")


def _validate_reviewed_target_rect(
    item: ReviewItem, trusted: ReviewItem, page_rect: pymupdf.Rect
) -> None:
    moved = item.reviewed_target_rect
    if moved is None:
        return
    original = trusted.target_rect
    if original is None:
        _fail("review_target_invalid", "人工译文位置无效，请重新导出审核结果")
    moved_rect = pymupdf.Rect(moved)
    original_rect = pymupdf.Rect(original)
    if not page_rect.contains(moved_rect):
        _fail("review_target_invalid", "人工译文位置超出页面，请重新调整")
    if (
        abs(moved_rect.width - original_rect.width) > 0.1
        or abs(moved_rect.height - original_rect.height) > 0.1
    ):
        _fail("review_target_invalid", "人工译文框大小已改变，请重新导出审核结果")


def _has_reviewed_target_rect_error(error: ValidationError) -> bool:
    for detail in error.errors():
        location = detail["loc"]
        if (
            len(location) >= 3
            and location[0] == "items"
            and isinstance(location[1], int)
            and location[2] == "reviewed_target_rect"
        ):
            return True
    return False


def _validate_review_timestamp(payload: Any) -> None:
    if not isinstance(payload, Mapping):
        return
    value = payload.get("review_completed_at")
    if value is None:
        return
    if not isinstance(value, str):
        _fail("review_timestamp_invalid", "Review completion time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("review_timestamp_invalid", "Review completion time is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("review_timestamp_invalid", "Review completion time needs a timezone")


def _validate_pipeline(review: ReviewDocument, trusted_pipeline: PipelineInfo) -> None:
    try:
        aggregate = _aggregate_item_provenance(
            [item.model_dump(mode="json") for item in review.items]
        )
    except ValueError:
        _fail(
            "review_provenance_incomplete",
            "Review item translation provenance is incomplete",
        )
    if (
        any(getattr(review.pipeline, field) != value for field, value in aggregate.items())
        or review.pipeline != trusted_pipeline
    ):
        _fail("review_pipeline_mismatch", "Review pipeline contradicts item provenance")


def _validate_final_translation(item: ReviewItem) -> None:
    final_translation = (
        item.reviewed_translation
        if item.review_status is ReviewStatus.APPROVED_EDITED
        else item.suggested_translation
    )
    if not _nonblank(final_translation):
        _fail("review_translation_missing", "Approved item has no final translation")

    exact_dnt_values: set[str] = set()
    for hit in item.glossary_hits:
        if not isinstance(hit, Mapping):
            _fail("review_glossary_invalid", "Review glossary hit is invalid")
        if bool(hit.get("do_not_translate")):
            exact_value = _project_do_not_translate_value(item.source_text, hit)
            if exact_value is None:
                _fail(
                    "review_glossary_dnt_mismatch",
                    "Final translation changed a do-not-translate term",
                )
            exact_dnt_values.add(exact_value)

    if any(
        item.source_text.count(value) <= 0
        or final_translation.count(value) != item.source_text.count(value)
        for value in exact_dnt_values
    ):
        _fail(
            "review_glossary_dnt_mismatch",
            "Final translation changed a do-not-translate term",
        )

    tokens: list[LockedToken] = []
    cursor = 0
    for value in item.locked_tokens:
        start = item.source_text.find(value, cursor)
        if start < 0:
            _fail("review_locked_token_mismatch", "Final translation changed locked tokens")
        end = start + len(value)
        tokens.append(LockedToken(value=value, start=start, end=end, kind="review"))
        cursor = end
    locked = LockedText(text=item.source_text, tokens=tuple(tokens))
    if not validate_locked_tokens(locked, final_translation):
        _fail("review_locked_token_mismatch", "Final translation changed locked tokens")

    normalized_translation = normalize_term(final_translation)
    for hit in item.glossary_hits:
        if bool(hit.get("do_not_translate")):
            continue
        target = str(hit.get("target_term") or "")
        if not target or normalize_term(target) not in normalized_translation:
            _fail(
                "review_glossary_target_missing",
                "Final translation omitted an authoritative glossary target",
            )


def _project_do_not_translate_value(
    source_text: str, hit: Mapping[str, Any]
) -> str | None:
    start = hit.get("start")
    end = hit.get("end")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    if not isinstance(end, int) or isinstance(end, bool):
        return None
    projected = _project_normalized_span(source_text, start, end)
    if projected is None:
        return None
    source_start, source_end = projected
    exact_value = source_text[source_start:source_end]
    references = {
        normalize_term(str(value))
        for value in (hit.get("matched_text"), hit.get("source_term"))
        if isinstance(value, str) and value
    }
    if not exact_value or normalize_term(exact_value) not in references:
        return None
    return exact_value


def _project_normalized_span(
    source_text: str, start: int, end: int
) -> tuple[int, int] | None:
    normalized_length = len(normalize_term(source_text))
    if start < 0 or end <= start or end > normalized_length:
        return None
    prefix_lengths = [
        len(normalize_term(source_text[:index]))
        for index in range(len(source_text) + 1)
    ]
    source_start = next(
        (index - 1 for index, length in enumerate(prefix_lengths) if length > start),
        None,
    )
    source_end = next(
        (index for index, length in enumerate(prefix_lengths) if length >= end),
        None,
    )
    while (
        source_end is not None
        and source_end < len(source_text)
        and unicodedata.combining(source_text[source_end])
    ):
        source_end += 1
    if (
        source_start is None
        or source_end is None
        or source_start < 0
        or source_end <= source_start
    ):
        return None
    return source_start, source_end


def _nonblank(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fail(code: str, message: str) -> None:
    raise TechpackError(code, message, {"error_code": code}) from None


__all__ = ["build_review_html", "load_review"]
