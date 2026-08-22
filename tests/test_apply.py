from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

from techpack_pdf.apply import apply_review
from techpack_pdf.models import FileArtifact, PipelineInfo, ReviewDocument, ReviewItem


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_source(path: Path, *, blocked: bool = False) -> None:
    document = pymupdf.open()
    page = document.new_page(width=320, height=320)
    page.insert_text((20, 35), "SOURCE A", fontsize=9)
    page.insert_text((20, 85), "SOURCE B", fontsize=9)
    page.insert_text((20, 135), "SOURCE C", fontsize=9)
    page.draw_line((15, 205), (295, 205), width=1)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pixmap.clear_with(180)
    page.insert_image(pymupdf.Rect(20, 225, 60, 255), pixmap=pixmap)
    page.add_text_annot((100, 245), "existing reviewer note")
    legacy = page.add_freetext_annot(
        pymupdf.Rect(130, 235, 205, 262),
        "legacy editable note",
        fontsize=7,
        text_color=(0.0, 0.0, 0.0),
        fill_color=None,
        border_color=None,
        border_width=0,
    )
    legacy.set_info(title="legacy-review", subject="pre-existing")
    legacy.update()
    page.set_cropbox(pymupdf.Rect(0, 0, 300, 300))
    if blocked:
        full = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 300), False)
        full.clear_with(100)
        page.insert_image(page.rect, pixmap=full, overlay=True)
    document.save(path)
    document.close()


def _review(source: Path, *, include_skipped: bool = True, secret: str = "SOURCE") -> ReviewDocument:
    items = [
        _review_item(
            "p001-i001",
            f"{secret} A",
            "大身面料",
            [20.0, 20.0, 75.0, 38.0],
            [175.0, 20.0, 285.0, 48.0],
            "approved",
        ),
        _review_item(
            "p001-i002",
            f"{secret} B",
            "领宽（人工修改并保留完整审核译文内容）",
            [20.0, 70.0, 75.0, 88.0],
            [175.0, 70.0, 285.0, 103.0],
            "approved_edited",
        ),
        _review_item(
            "p001-i003",
            f"{secret} C",
            "袖口宽度",
            [20.0, 120.0, 75.0, 138.0],
            [175.0, 120.0, 285.0, 150.0],
            "approved",
        ),
    ]
    if include_skipped:
        items.append(
            _review_item(
                "p001-i004",
                "SKIPPED PRIVATE TEXT",
                "不应写入",
                [20.0, 160.0, 100.0, 178.0],
                [175.0, 160.0, 285.0, 185.0],
                "skipped",
            )
        )
    return ReviewDocument(
        schema_version="1.1",
        job_id=f"{_sha256(source)[:12]}-20260822T000000Z",
        source=FileArtifact(
            filename=source.name,
            sha256=_sha256(source),
            path=source,
            page_count=1,
        ),
        glossary=FileArtifact(filename="glossary.csv", sha256="a" * 64),
        pipeline=PipelineInfo(
            parser="pymupdf+mineru",
            translation_executor="host_agent",
            host="codex",
            execution_mode="subagent",
            model="unknown",
            prompt_version="1.0",
        ),
        items=items,
        blocking_issues=[],
        review_completed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )


def _review_item(
    item_id: str,
    source_text: str,
    translation: str,
    source_bbox: list[float],
    target_rect: list[float],
    status: str,
) -> ReviewItem:
    return ReviewItem(
        item_id=item_id,
        page_index=0,
        page_type="bom",
        source_text=source_text,
        normalized_text=source_text.casefold(),
        source_bbox=source_bbox,
        source_kind="body",
        coordinate_confidence="high",
        decision_reason="field_rule",
        locked_tokens=[],
        glossary_hits=[],
        suggested_translation=translation,
        reviewed_translation=translation if status == "approved_edited" else None,
        review_status=status,
        risk_level="low",
        translation_host="codex",
        translation_execution_mode="subagent",
        translation_model="unknown",
        translation_agent_role="techpack-translator",
        translation_prompt_version="1.0",
        placement_strategy="review_target",
        target_rect=target_rect,
        font_size=7.0,
        leader_line=None,
        warnings=[],
    )


def _snapshot(path: Path) -> dict:
    document = pymupdf.open(path)
    try:
        page = document[0]
        annotations = [
            value
            for value in page.annots()
            if value.info.get("title") != "techpack_pdf"
        ]
        return {
            "page_count": document.page_count,
            "media": tuple(page.mediabox),
            "crop": tuple(page.cropbox),
            "text": page.get_text(),
            "content_streams": tuple(
                (xref, hashlib.sha256(document.xref_stream(xref)).hexdigest())
                for xref in page.get_contents()
            ),
            "images": tuple((value[0], tuple(page.get_image_rects(value[0]))) for value in page.get_images(full=True)),
            "drawings": tuple(tuple(drawing["rect"]) for drawing in page.get_drawings()),
            "old": tuple(
                (annotation.xref, annotation.type, tuple(annotation.rect), annotation.info)
                for annotation in annotations
            ),
        }
    finally:
        document.close()


def test_apply_review_preserves_source_and_adds_only_three_editable_red_freetext_annotations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _make_source(source)
    before = _snapshot(source)

    result = apply_review(source, _review(source))

    expected = tmp_path / "source.pdf.annotated.pdf"
    assert result.success is True
    assert result.output_path == expected
    assert result.problems == ()
    assert result.unresolved_overlaps == ()
    assert expected.exists()

    after = _snapshot(expected)
    assert after["page_count"] == before["page_count"]
    assert after["media"] == pytest.approx(before["media"])
    assert after["crop"] == pytest.approx(before["crop"])
    assert all(line in after["text"] for line in before["text"].splitlines())
    assert after["content_streams"] == before["content_streams"]
    assert after["images"] == before["images"]
    assert after["drawings"] == before["drawings"]
    assert after["old"] == before["old"]

    document = pymupdf.open(expected)
    try:
        page = document[0]
        annotations = list(page.annots())
        new_annotations = [
            value
            for value in annotations
            if value.type[1] == "FreeText"
            and value.info.get("title") == "techpack_pdf"
        ]
        assert len(new_annotations) == 3
        assert len(annotations) == len(before["old"]) + 3
        contents = {value.info["content"] for value in new_annotations}
        assert contents == {
            "大身面料",
            "领宽（人工修改并保留完整审核译文内容）",
            "袖口宽度",
        }
        for annotation in new_annotations:
            metadata = json.loads(annotation.info["subject"])
            assert metadata["item_id"].startswith("p001-i00")
            assert metadata["source_page"] == 1
            assert metadata["tool_version"]
            assert "source_text" not in metadata
            assert annotation.type[1] == "FreeText"
            default_appearance = document.xref_get_key(annotation.xref, "DA")[1]
            assert "0.85 0.05 0.05 rg" in default_appearance
            assert 5.0 <= annotation.get_textpage().extractDICT()["blocks"][0]["lines"][0]["spans"][0]["size"] <= 7.0
            assert annotation.border["width"] == 0
    finally:
        document.close()


def test_apply_review_unresolved_layout_returns_safe_problem_and_leaves_no_output_or_temp(
    tmp_path: Path,
) -> None:
    source = tmp_path / "blocked.pdf"
    _make_source(source, blocked=True)
    private_text = "PRIVATE-CUSTOMER-MEASUREMENTS-DO-NOT-LOG"

    result = apply_review(source, _review(source, secret=private_text))

    assert result.success is False
    assert result.output_path is None
    assert result.unresolved_overlaps
    assert result.layout_rounds <= 10
    assert not (tmp_path / "blocked.pdf.annotated.pdf").exists()
    assert not list(tmp_path.glob(".blocked.pdf.*.tmp.pdf"))
    rendered = json.dumps(result.to_dict(), ensure_ascii=False)
    assert private_text not in rendered
    overlap = result.unresolved_overlaps[0]
    assert {
        "page_index",
        "item_id",
        "collision_object",
        "attempted_placements",
        "final_render_reference",
    } <= set(overlap)


def test_apply_review_does_not_overwrite_an_existing_final_file(tmp_path: Path) -> None:
    source = tmp_path / "already.pdf"
    _make_source(source)
    final = tmp_path / "already.pdf.annotated.pdf"
    final.write_bytes(b"existing-result")

    result = apply_review(source, _review(source))

    assert result.success is False
    assert final.read_bytes() == b"existing-result"
    assert result.problems[0]["code"] == "output_exists"


def test_apply_review_rejects_stale_source_without_leaking_review_text(tmp_path: Path) -> None:
    source = tmp_path / "stale.pdf"
    _make_source(source)
    review = _review(source, secret="PRIVATE-STYLE-INSTRUCTION")
    with source.open("ab") as stream:
        stream.write(b"\n% changed after validation")

    result = apply_review(source, review)

    assert result.success is False
    assert result.problems[0]["code"] == "source_mismatch"
    assert "PRIVATE-STYLE-INSTRUCTION" not in json.dumps(result.to_dict())
    assert not (tmp_path / "stale.pdf.annotated.pdf").exists()
