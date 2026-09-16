from __future__ import annotations

from techpack_pdf.matching import match_nodes
from techpack_pdf.models import CoordinateConfidence


PAGE_RECT = (0.0, 0.0, 841.89, 595.276)


def test_multiline_native_spans_match_as_one_high_confidence_source_box() -> None:
    native = [
        {"text": "FRONT POCKET ", "bbox": [61.62, 318.21, 116.15, 325.21]},
        {"text": "OPENING WIDTH", "bbox": [61.62, 326.20, 117.12, 333.20]},
    ]
    mineru = [
        {
            "text": "FRONT POCKET OPENING WIDTH",
            "bbox": [60.0, 317.0, 133.0, 334.0],
        }
    ]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert matched[0].coordinate_confidence is CoordinateConfidence.HIGH
    assert matched[0].source_bbox == (61.62, 318.21, 117.12, 333.20)
    assert matched[0].auto_approvable is True


def test_similar_multiline_rows_keep_distinct_union_boxes() -> None:
    native = [
        {"text": "FRONT POCKET ", "bbox": [61.62, 318.21, 116.15, 325.21]},
        {"text": "OPENING WIDTH", "bbox": [61.62, 326.20, 117.12, 333.20]},
        {"text": "FRONT POCKET BAG ", "bbox": [61.62, 336.14, 132.26, 343.14]},
        {"text": "LENGTH", "bbox": [61.62, 344.13, 88.92, 351.13]},
        {"text": "FRONT POCKET BAG ", "bbox": [61.62, 354.14, 132.27, 361.14]},
        {"text": "WIDTH", "bbox": [61.62, 362.13, 84.43, 369.13]},
    ]
    mineru = [
        {"text": "FRONT POCKET OPENING WIDTH", "bbox": [60.0, 317.0, 134.0, 334.0]},
        {"text": "FRONT POCKET BAG LENGTH", "bbox": [60.0, 335.0, 134.0, 352.0]},
        {"text": "FRONT POCKET BAG WIDTH", "bbox": [60.0, 353.0, 134.0, 370.0]},
    ]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert [item.coordinate_confidence for item in matched] == [
        CoordinateConfidence.HIGH,
        CoordinateConfidence.HIGH,
        CoordinateConfidence.HIGH,
    ]
    assert [item.source_bbox for item in matched] == [
        (61.62, 318.21, 117.12, 333.20),
        (61.62, 336.14, 132.26, 351.13),
        (61.62, 354.14, 132.27, 369.13),
    ]


def test_wrapped_exact_match_beats_distant_unwrapped_exact_match() -> None:
    native = [
        {"text": "FRONT POCKET OPENING WIDTH", "bbox": [61.62, 50.0, 150.0, 57.0]},
        {"text": "FRONT POCKET ", "bbox": [61.62, 318.21, 116.15, 325.21]},
        {"text": "OPENING WIDTH", "bbox": [61.62, 326.20, 117.12, 333.20]},
    ]
    mineru = [
        {
            "text": "FRONT POCKET OPENING WIDTH",
            "bbox": [60.0, 317.0, 133.0, 334.0],
        }
    ]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert matched[0].coordinate_confidence is CoordinateConfidence.HIGH
    assert matched[0].source_bbox == (61.62, 318.21, 117.12, 333.20)


def test_wrapped_match_can_skip_interleaved_same_line_value_cell() -> None:
    native = [
        {"text": "FRONT POCKET ", "bbox": [61.62, 318.21, 116.15, 325.21]},
        {"text": "XS", "bbox": [120.0, 318.21, 130.0, 325.21]},
        {"text": "OPENING WIDTH", "bbox": [61.62, 326.20, 117.12, 333.20]},
    ]
    mineru = [
        {
            "text": "FRONT POCKET OPENING WIDTH",
            "bbox": [60.0, 317.0, 133.0, 334.0],
        }
    ]

    matched = match_nodes(native, mineru, PAGE_RECT)

    assert matched[0].coordinate_confidence is CoordinateConfidence.HIGH
    assert matched[0].source_bbox == (61.62, 318.21, 117.12, 333.20)


def test_context_disambiguates_tied_single_and_wrapped_exact_candidates() -> None:
    native = [
        {"text": "BEFORE", "bbox": [5.0, 5.0, 25.0, 12.0]},
        {"text": "FRONT POCKET", "bbox": [5.0, 15.0, 25.0, 25.0]},
        {"text": "AFTER", "bbox": [5.0, 28.0, 25.0, 35.0]},
        {"text": "FRONT", "bbox": [65.0, 15.0, 74.0, 25.0]},
        {"text": "POCKET", "bbox": [75.0, 15.0, 85.0, 25.0]},
    ]
    mineru = [
        {"text": "BEFORE", "bbox": [35.0, 5.0, 55.0, 12.0]},
        {"text": "FRONT POCKET", "bbox": [35.0, 15.0, 55.0, 25.0]},
        {"text": "AFTER", "bbox": [35.0, 28.0, 55.0, 35.0]},
    ]

    matched = match_nodes(native, mineru, (0.0, 0.0, 100.0, 100.0))

    assert matched[1].coordinate_confidence is CoordinateConfidence.HIGH
    assert matched[1].source_bbox == (5.0, 15.0, 25.0, 25.0)
