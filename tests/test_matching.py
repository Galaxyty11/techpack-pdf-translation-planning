from __future__ import annotations

import pytest

from techpack_pdf.models import CoordinateConfidence
from techpack_pdf.matching import match_nodes


PAGE_RECT = (0.0, 0.0, 100.0, 100.0)


def test_unique_normalized_exact_match_is_high_and_uses_native_bbox() -> None:
    native = [{"text": "Collar – height", "bbox": [10, 10, 40, 20]}]
    mineru = [{"text": " COLLAR - HEIGHT ", "bbox": [11, 10, 41, 20]}]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert len(matched) == 1
    assert matched[0].coordinate_confidence is CoordinateConfidence.HIGH
    assert matched[0].source_bbox == (10.0, 10.0, 40.0, 20.0)
    assert matched[0].auto_approvable is True


def test_duplicate_exact_text_uses_center_distance_and_neighbor_context() -> None:
    native = [
        {"text": "Front", "bbox": [5, 5, 20, 10]},
        {"text": "Pocket", "bbox": [5, 15, 25, 25]},
        {"text": "Left", "bbox": [5, 30, 20, 35]},
        {"text": "Back", "bbox": [65, 5, 80, 10]},
        {"text": "Pocket", "bbox": [65, 15, 85, 25]},
        {"text": "Right", "bbox": [65, 30, 85, 35]},
    ]
    mineru = [
        {"text": "Back", "bbox": [64, 5, 79, 10]},
        {"text": "Pocket", "bbox": [64, 15, 84, 25]},
        {"text": "Right", "bbox": [64, 30, 84, 35]},
    ]

    matched = match_nodes(native, mineru, PAGE_RECT)

    pocket = matched[1]
    assert pocket.coordinate_confidence is CoordinateConfidence.HIGH
    assert pocket.source_bbox == (65.0, 15.0, 85.0, 25.0)
    assert pocket.auto_approvable is True


def test_tied_duplicate_exact_text_stays_low() -> None:
    native = [
        {"text": "Pocket", "bbox": [10, 10, 20, 20]},
        {"text": "Pocket", "bbox": [30, 10, 40, 20]},
    ]
    mineru = [{"text": "Pocket", "bbox": [20, 10, 30, 20]}]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert matched[0].coordinate_confidence is CoordinateConfidence.LOW
    assert matched[0].source_bbox is None
    assert matched[0].auto_approvable is False


def test_close_similarity_and_distance_thresholds_are_medium() -> None:
    native = [{"text": "abcdefghijklm", "bbox": [10, 10, 30, 20]}]
    mineru = [{"text": "abcdefghijklx", "bbox": [12, 10, 32, 20]}]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert matched[0].similarity == pytest.approx(12 / 13, abs=0.001)
    assert matched[0].distance_ratio < 0.03
    assert matched[0].coordinate_confidence is CoordinateConfidence.MEDIUM
    assert matched[0].source_bbox == (10.0, 10.0, 30.0, 20.0)
    assert matched[0].auto_approvable is False


@pytest.mark.parametrize(
    ("native", "mineru"),
    [
        (
            [{"text": "abcdefghijklm", "bbox": [10, 10, 30, 20]}],
            [{"text": "abcdefghijklx", "bbox": [20, 20, 40, 30]}],
        ),
        (
            [{"text": "abcdefghijklm", "bbox": [10, 10, 30, 20]}],
            [{"text": "totally different", "bbox": [11, 10, 31, 20]}],
        ),
    ],
)
def test_similarity_or_distance_outside_threshold_is_low(
    native: list[dict[str, object]], mineru: list[dict[str, object]]
) -> None:
    matched = match_nodes(native, mineru, PAGE_RECT)

    assert matched[0].coordinate_confidence is CoordinateConfidence.LOW
    assert matched[0].auto_approvable is False


def test_ocr_only_node_has_no_source_bbox_and_cannot_be_auto_approved() -> None:
    matched = match_nodes(
        [],
        [{"text": "OCR ONLY", "bbox": [10, 10, 40, 20]}],
        PAGE_RECT,
    )

    assert matched[0].coordinate_confidence is CoordinateConfidence.LOW
    assert matched[0].source_bbox is None
    assert matched[0].auto_approvable is False
