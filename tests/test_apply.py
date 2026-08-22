from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

import techpack_pdf.apply as apply_module
from techpack_pdf.apply import apply_review
from techpack_pdf.models import (
    FileArtifact,
    JobManifest,
    PipelineInfo,
    ReviewDocument,
    ReviewItem,
    ReviewStatus,
)


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


def _review_bundle(
    tmp_path: Path,
    source: Path,
    *,
    include_skipped: bool = True,
    secret: str = "SOURCE",
    statuses: tuple[str, ...] | None = None,
) -> tuple[Path, JobManifest, dict]:
    glossary = tmp_path / f"{source.stem}-glossary.csv"
    glossary.write_text("source_term,target_term\nshell,大身\n", encoding="utf-8")
    review = _review(source, include_skipped=include_skipped, secret=secret)
    if statuses is not None:
        replacements = []
        for item, status in zip(review.items, statuses, strict=True):
            replacements.append(
                item.model_copy(
                    update={
                        "review_status": ReviewStatus(status),
                        "reviewed_translation": (
                            item.suggested_translation
                            if status == "approved_edited"
                            else None
                        ),
                    }
                )
            )
        review = review.model_copy(update={"items": replacements})
    job = JobManifest(
        job_id=review.job_id,
        source=review.source.model_copy(update={"path": source}),
        glossary=FileArtifact(
            filename=glossary.name,
            sha256=_sha256(glossary),
            path=glossary,
        ),
        job_dir=tmp_path / f"{source.stem}-job",
        created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    review = review.model_copy(update={"glossary": job.glossary})
    payload = review.model_dump(mode="json")
    expected_output = {
        "pipeline": deepcopy(payload["pipeline"]),
        "items": deepcopy(payload["items"]),
        "blocking_issues": deepcopy(payload["blocking_issues"]),
    }
    for item in expected_output["items"]:
        item["review_status"] = None
        item["reviewed_translation"] = None
    review_path = tmp_path / f"{source.stem}-review.json"
    review_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return review_path, job, expected_output


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

    review_path, job, expected_output = _review_bundle(tmp_path, source)
    result = apply_review(source, review_path, job, expected_output)

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

    review_path, job, expected_output = _review_bundle(
        tmp_path, source, secret=private_text
    )
    result = apply_review(source, review_path, job, expected_output)

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
    assert Path(overlap["final_render_reference"]).is_file()
    assert Path(overlap["final_render_reference"]).suffix == ".png"
    assert result.failure_report_path is not None
    report = json.loads(result.failure_report_path.read_text(encoding="utf-8"))
    assert private_text not in json.dumps(report, ensure_ascii=False)


def test_apply_review_does_not_overwrite_an_existing_final_file(tmp_path: Path) -> None:
    source = tmp_path / "already.pdf"
    _make_source(source)
    final = tmp_path / "already.pdf.annotated.pdf"
    final.write_bytes(b"existing-result")

    review_path, job, expected_output = _review_bundle(tmp_path, source)
    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert final.read_bytes() == b"existing-result"
    assert result.problems[0]["code"] == "output_exists"


def test_apply_review_rejects_stale_source_without_leaking_review_text(tmp_path: Path) -> None:
    source = tmp_path / "stale.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(
        tmp_path, source, secret="PRIVATE-STYLE-INSTRUCTION"
    )
    with source.open("ab") as stream:
        stream.write(b"\n% changed after validation")

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "review_validation_failed"
    assert "PRIVATE-STYLE-INSTRUCTION" not in json.dumps(result.to_dict())
    assert not (tmp_path / "stale.pdf.annotated.pdf").exists()


def test_apply_review_requires_task7_load_review_trust_boundary(tmp_path: Path) -> None:
    source = tmp_path / "trust.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    constructed = ReviewDocument.model_validate(
        json.loads(review_path.read_text(encoding="utf-8"))
    )

    result = apply_review(source, constructed, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "review_validation_failed"
    assert not (tmp_path / "trust.pdf.annotated.pdf").exists()


@pytest.mark.parametrize("tamper", ["item", "status", "glossary", "job"])
def test_apply_review_blocks_tampered_review_job_and_glossary_bindings(
    tmp_path: Path, tamper: str
) -> None:
    source = tmp_path / f"tampered-{tamper}.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    if tamper in {"item", "status"}:
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        if tamper == "item":
            payload["items"][0]["source_text"] = "PRIVATE TAMPERED TEXT"
        else:
            payload["items"][0]["review_status"] = None
        review_path.write_text(json.dumps(payload), encoding="utf-8")
    elif tamper == "glossary":
        Path(job.glossary.path).write_text("changed", encoding="utf-8")
    else:
        job = job.model_copy(update={"job_id": "different-job"})

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "review_validation_failed"
    assert "PRIVATE TAMPERED TEXT" not in json.dumps(result.to_dict())
    assert not source.with_name(source.name + ".annotated.pdf").exists()


def test_apply_review_rejects_source_argument_different_from_bound_job_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bound.pdf"
    other = tmp_path / "same-bytes.pdf"
    _make_source(source)
    other.write_bytes(source.read_bytes())
    review_path, job, expected_output = _review_bundle(tmp_path, source)

    result = apply_review(other, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "source_job_mismatch"
    assert not other.with_name(other.name + ".annotated.pdf").exists()


def test_apply_review_all_skipped_publishes_verified_faithful_copy(tmp_path: Path) -> None:
    source = tmp_path / "all-skipped.pdf"
    _make_source(source)
    before = _snapshot(source)
    review_path, job, expected_output = _review_bundle(
        tmp_path,
        source,
        include_skipped=False,
        statuses=("skipped", "skipped", "skipped"),
    )

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is True
    assert result.output_path == tmp_path / "all-skipped.pdf.annotated.pdf"
    assert _snapshot(result.output_path) == before


def test_apply_review_atomic_publish_does_not_clobber_a_racing_target(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "race.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    final = source.with_name(source.name + ".annotated.pdf")

    def racing_link(_source, target):
        Path(target).write_bytes(b"racing-writer")
        raise FileExistsError("racing target")

    monkeypatch.setattr(apply_module.os, "link", racing_link)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "output_exists"
    assert final.read_bytes() == b"racing-writer"
    assert not list(tmp_path.glob(".race.pdf.*.tmp.pdf"))


def test_apply_review_reports_failed_exact_temp_cleanup_without_deleting_other_files(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "cleanup.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    unrelated = tmp_path / "keep-me.tmp.pdf"
    unrelated.write_bytes(b"unrelated")
    real_unlink = Path.unlink

    def failed_link(_source, _target):
        raise OSError("publish unavailable")

    def guarded_unlink(path, *args, **kwargs):
        if path.name.startswith(".cleanup.pdf.") and path.name.endswith(".tmp.pdf"):
            raise PermissionError("locked temp")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(apply_module.os, "link", failed_link)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "temp_cleanup_failed"
    assert unrelated.read_bytes() == b"unrelated"
    retained = list(tmp_path.glob(".cleanup.pdf.*.tmp.pdf"))
    assert len(retained) == 2
    assert set(result.problems[0]["details"]["retained_paths"]) == {
        str(path) for path in retained
    }


def test_successful_link_with_failed_temp_unlink_rolls_back_only_its_final_name(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "post-link-cleanup.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    final = source.with_name(source.name + ".annotated.pdf")
    real_unlink = Path.unlink
    real_link = apply_module.os.link
    linked = False

    def mark_link(source_path, target_path):
        nonlocal linked
        result = real_link(source_path, target_path)
        if Path(target_path) == final:
            linked = True
        return result

    def fail_temp_only(path, *args, **kwargs):
        if linked and path.name.startswith(".post-link-cleanup.pdf.") and path.name.endswith(
            ".tmp.pdf"
        ):
            raise PermissionError("locked temp")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(apply_module.os, "link", mark_link)
    monkeypatch.setattr(Path, "unlink", fail_temp_only)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "temp_cleanup_failed"
    assert not final.exists()
    assert len(list(tmp_path.glob(".post-link-cleanup.pdf.*.tmp.pdf"))) == 1


def test_apply_review_detects_preexisting_annotation_object_or_appearance_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "old-annotation.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    real_write = apply_module._write_annotations

    def mutate_old_annotation(document, placements):
        real_write(document, placements)
        page = document[0]
        old = next(
            annotation
            for annotation in page.annots()
            if annotation.info.get("title") == "legacy-review"
        )
        old.set_opacity(0.35)
        old.update()

    monkeypatch.setattr(apply_module, "_write_annotations", mutate_old_annotation)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "verification_failed"
    assert not source.with_name(source.name + ".annotated.pdf").exists()


def test_layout_reflows_a_render_only_collision_inside_the_bounded_search(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "render-reflow.pdf"
    _make_source(source)
    review = _review(source, include_skipped=False)
    approved = tuple(review.items)
    first_signature = None

    def render_gate(page, placements):
        nonlocal first_signature
        signature = tuple((value.item_id, value.rect) for value in placements)
        if first_signature is None:
            first_signature = signature
        if signature == first_signature:
            return [
                apply_module.Collision(
                    page.number,
                    placements[0].item_id,
                    "render_overlap",
                    "original_render",
                    9,
                    300,
                )
            ]
        return []

    monkeypatch.setattr(apply_module, "detect_candidate_collisions", render_gate)
    document = pymupdf.open(source)
    try:
        layout, _attempted = apply_module._layout_document(document, approved)
    finally:
        document.close()

    assert layout.collisions == ()
    assert layout.rounds <= 10
    assert tuple((value.item_id, value.rect) for value in layout.placements) != first_signature


def test_final_real_render_collision_is_fed_back_into_the_shared_ten_round_budget(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "final-render-reflow.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    real_verify = apply_module._verify_temp
    calls = 0

    def collide_once(source_path, temp, baseline, placements, modified_pages, *, written=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return apply_module._VerificationOutcome(
                None,
                (
                    apply_module.Collision(
                        placements[0].page_index,
                        placements[0].item_id,
                        "render_overlap",
                        "original_render",
                        9,
                        300,
                    ),
                ),
            )
        return real_verify(
            source_path,
            temp,
            baseline,
            placements,
            modified_pages,
            written=written,
        )

    monkeypatch.setattr(apply_module, "_verify_temp", collide_once)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is True
    assert calls == 2
    assert result.layout_rounds <= 10


def test_layout_fails_closed_when_an_approved_item_has_no_remaining_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "candidate-exhausted.pdf"
    _make_source(source)
    approved = tuple(_review(source, include_skipped=False).items)
    real_rank = apply_module.rank_placements

    def omit_one(item, *args, **kwargs):
        if item.item_id == approved[0].item_id:
            return []
        return real_rank(item, *args, **kwargs)

    monkeypatch.setattr(apply_module, "rank_placements", omit_one)
    document = pymupdf.open(source)
    try:
        layout, _attempted = apply_module._layout_document(document, approved)
    finally:
        document.close()

    assert {value.item_id for value in layout.placements} != {
        value.item_id for value in approved
    }
    assert any(
        value.item_id == approved[0].item_id and value.kind == "candidate_exhausted"
        for value in layout.collisions
    )


@pytest.mark.parametrize("mode", ["drop", "duplicate"])
def test_apply_verifies_exact_approved_item_to_new_xref_mapping(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    source = tmp_path / f"xref-{mode}.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    real_write = apply_module._write_annotations

    def corrupt(document, placements):
        chosen = placements[:-1] if mode == "drop" else tuple(placements) + (placements[0],)
        return real_write(document, chosen)

    monkeypatch.setattr(apply_module, "_write_annotations", corrupt)
    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "verification_failed"
    assert not source.with_name(source.name + ".annotated.pdf").exists()


def test_existing_same_tool_title_annotation_is_preserved_and_not_counted_as_new(
    tmp_path: Path,
) -> None:
    source = tmp_path / "same-title.pdf"
    _make_source(source)
    document = pymupdf.open(source)
    page = document[0]
    old = page.add_freetext_annot(pymupdf.Rect(210, 240, 285, 270), "old tool title")
    old.set_info(title="techpack_pdf", subject="old unrelated metadata")
    old.update()
    document.saveIncr()
    old_xref = old.xref
    old_object = document.xref_object(old_xref, compressed=False)
    document.close()
    review_path, job, expected_output = _review_bundle(tmp_path, source)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is True
    output = pymupdf.open(result.output_path)
    try:
        assert output.xref_object(old_xref, compressed=False) == old_object
        assert len(list(output[0].annots())) == len(list(pymupdf.open(source)[0].annots())) + 3
    finally:
        output.close()


def test_publish_reports_both_retained_paths_when_temp_and_rollback_unlinks_fail(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "double-delete.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    final = source.with_name(source.name + ".annotated.pdf")
    real_unlink = Path.unlink

    linked = False
    real_link = apply_module.os.link

    def mark_link(source_path, target_path):
        nonlocal linked
        result = real_link(source_path, target_path)
        if Path(target_path) == final:
            linked = True
        return result

    def fail_both(path, *args, **kwargs):
        if linked and (path == final or (
            path.name.startswith(".double-delete.pdf.")
            and path.name.endswith(".tmp.pdf")
        )):
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(apply_module.os, "link", mark_link)
    monkeypatch.setattr(Path, "unlink", fail_both)
    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "publish_rollback_failed"
    assert result.problems[0]["details"]["final_path"] == str(final)
    assert final.exists()
    assert Path(result.problems[0]["details"]["temp_path"]).exists()


def test_source_change_after_validation_before_publish_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source-race.pdf"
    _make_source(source)
    review_path, job, expected_output = _review_bundle(tmp_path, source)
    real_verify = apply_module._verify_temp

    def mutate_after_write(*args, **kwargs):
        outcome = real_verify(*args, **kwargs)
        with source.open("ab") as stream:
            stream.write(b"\n% raced")
        return outcome

    monkeypatch.setattr(apply_module, "_verify_temp", mutate_after_write)
    result = apply_review(source, review_path, job, expected_output)

    assert result.success is False
    assert result.problems[0]["code"] == "source_mismatch"
    assert not source.with_name(source.name + ".annotated.pdf").exists()


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_apply_uses_crop_relative_unrotated_coordinates_on_rotated_cropped_pages(
    tmp_path: Path, rotation: int
) -> None:
    source = tmp_path / f"rotated-{rotation}.pdf"
    _make_source(source)
    document = pymupdf.open(source)
    page = document[0]
    page.set_cropbox(pymupdf.Rect(10, 10, 300, 300))
    page.set_rotation(rotation)
    document.saveIncr()
    document.close()
    review_path, job, expected_output = _review_bundle(tmp_path, source)

    result = apply_review(source, review_path, job, expected_output)

    assert result.success is True
    output = pymupdf.open(result.output_path)
    try:
        canonical = pymupdf.Rect(0, 0, output[0].cropbox.width, output[0].cropbox.height)
        rects = []
        for annot in output[0].annots():
            if annot.info.get("title") == "techpack_pdf" and annot.info.get(
                "subject", ""
            ).startswith("{"):
                rects.append(tuple(annot.rect))
        assert len(rects) == 3
        assert all(canonical.contains(pymupdf.Rect(rect)) for rect in rects)
    finally:
        output.close()


def test_unresolved_evidence_is_rendered_from_the_exact_failing_annotated_temp(
    tmp_path: Path,
) -> None:
    source = tmp_path / "evidence.pdf"
    _make_source(source)
    review = _review(source, include_skipped=False)
    document = pymupdf.open(source)
    try:
        placement = apply_module.rank_placements(
            review.items[0],
            pymupdf.Rect(0, 0, document[0].cropbox.width, document[0].cropbox.height),
        )[0]
    finally:
        document.close()
    temp = apply_module._unique_temp_path(source)
    temp.write_bytes(source.read_bytes())
    failing = pymupdf.open(temp)
    apply_module._write_annotations(failing, (placement,))
    failing[0].draw_rect(
        pymupdf.Rect(250, 260, 280, 290), color=(0.0, 0.0, 1.0), fill=(0.0, 0.0, 1.0)
    )
    failing.saveIncr()
    failing.close()
    exact = pymupdf.open(temp)
    try:
        expected_png = exact[0].get_pixmap(
            dpi=300, alpha=False, colorspace=pymupdf.csRGB, annots=True
        ).tobytes("png")
    finally:
        exact.close()
    layout = apply_module.LayoutResult(
        (placement,),
        (
            apply_module.Collision(
                0, placement.item_id, "render_overlap", "original_render", 9, 300
            ),
        ),
        10,
        False,
    )

    outcome = apply_module._write_unresolved_artifacts(
        source, layout, {placement.item_id: [placement]}, annotated_temp=temp
    )

    assert outcome.problem is None
    evidence = Path(outcome.unresolved[0]["final_render_reference"])
    assert hashlib.sha256(evidence.read_bytes()).digest() == hashlib.sha256(
        expected_png
    ).digest()
    assert apply_module._remove_exact_temp(temp, source)


@pytest.mark.parametrize("fail_at", [2, 3])
def test_unresolved_artifacts_are_transactional_on_multipage_or_json_failure(
    tmp_path: Path, monkeypatch, fail_at: int
) -> None:
    source = tmp_path / f"artifact-failure-{fail_at}.pdf"
    _make_source(source)
    document = pymupdf.open(source)
    document.new_page(width=320, height=320).insert_text((20, 30), "SECOND")
    document.saveIncr()
    document.close()
    real_write = apply_module._atomic_artifact_write
    calls = 0

    def fail_selected(path, data):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("injected artifact failure")
        return real_write(path, data)

    monkeypatch.setattr(apply_module, "_atomic_artifact_write", fail_selected)
    layout = apply_module.LayoutResult(
        (),
        (
            apply_module.Collision(0, "item-a", "render_overlap", "original"),
            apply_module.Collision(1, "item-b", "render_overlap", "original"),
        ),
        10,
        False,
    )
    annotated = pymupdf.open(source)
    try:
        outcome = apply_module._write_unresolved_artifacts(
            source, layout, {}, document=annotated
        )
    finally:
        annotated.close()

    assert outcome.problem is not None
    assert outcome.problem["code"] == "artifact_write_failed"
    assert not list(tmp_path.glob(f".{source.name}.unresolved.*"))


def test_attempted_history_merges_in_first_seen_order_without_duplicates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "attempts.pdf"
    _make_source(source)
    item = _review(source, include_skipped=False).items[0]
    candidates = apply_module.rank_placements(item, pymupdf.Rect(0, 0, 300, 300))

    merged = apply_module._merge_attempted(
        {item.item_id: candidates[:2]},
        {item.item_id: candidates[1:3]},
    )

    assert [apply_module._placement_signature(value) for value in merged[item.item_id]] == [
        apply_module._placement_signature(value) for value in candidates[:3]
    ]
