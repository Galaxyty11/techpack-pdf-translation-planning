from __future__ import annotations

from pathlib import Path

import pymupdf

import techpack_pdf.apply as apply_module
from techpack_pdf.layout import Placement


def _placement(item_id: str, text: str, rect: tuple[float, float, float, float]):
    return Placement(
        item_id=item_id,
        page_index=0,
        text=text,
        rect=rect,
        font_size=7.0,
        strategy="review_target",
        wrapped_lines=(text,),
        same_semantic_region=True,
        leader_line=None,
        collision_count=0,
        in_bounds=True,
        source_distance=0.0,
        movement_distance=0.0,
        candidate_index=0,
    )


def test_mixed_cjk_then_winansi_annotations_write_and_verify(tmp_path: Path) -> None:
    source = tmp_path / "mixed-fonts.pdf"
    document = pymupdf.open()
    document.new_page(width=300, height=140)
    document.save(source)
    document.close()

    placements = (
        _placement("p001-i001", "拉链结构", (20.0, 20.0, 150.0, 45.0)),
        _placement("p001-i002", "SPRING SUMMER 2026", (20.0, 65.0, 180.0, 90.0)),
    )
    output = tmp_path / "mixed-fonts-output.pdf"
    output.write_bytes(source.read_bytes())
    output_document = pymupdf.open(output)
    written = apply_module._write_annotations(output_document, placements)
    output_document.saveIncr()
    output_document.close()

    verification = apply_module._verify_temp(
        source, output, placements, written=written
    )
    assert verification.problem is None
    assert verification.collisions == ()
