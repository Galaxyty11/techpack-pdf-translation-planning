"""Match MinerU semantic nodes to authoritative PyMuPDF coordinates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from .glossary import normalize_term
from .models import CoordinateConfidence


BBox = tuple[float, float, float, float]
MAX_NATIVE_SEQUENCE_SPANS = 4
NATIVE_SEQUENCE_X_TOLERANCE = 3.0


@dataclass(frozen=True)
class MatchedNode:
    mineru_index: int
    native_index: int | None
    text: str
    source_bbox: BBox | None
    mineru_bbox: BBox | None
    coordinate_confidence: CoordinateConfidence
    similarity: float
    distance_ratio: float | None
    auto_approvable: bool


@dataclass(frozen=True)
class _NativeSequence:
    indices: tuple[int, ...]
    text: str
    bbox: BBox


def match_nodes(
    native_spans: Sequence[Mapping[str, Any]],
    mineru_nodes: Sequence[Mapping[str, Any]],
    page_rect: Sequence[float],
) -> list[MatchedNode]:
    """Return one coordinate match per MinerU node on a single page."""
    diagonal = _diagonal(_bbox(page_rect))
    native_text = [normalize_term(str(span.get("text", ""))) for span in native_spans]
    mineru_text = [normalize_term(str(node.get("text", ""))) for node in mineru_nodes]
    native_sequences = _native_sequences(native_spans)

    return [
        _match_one(
            mineru_index,
            native_spans,
            mineru_nodes,
            native_text,
            mineru_text,
            native_sequences,
            diagonal,
        )
        for mineru_index in range(len(mineru_nodes))
    ]


def _match_one(
    mineru_index: int,
    native_spans: Sequence[Mapping[str, Any]],
    mineru_nodes: Sequence[Mapping[str, Any]],
    native_text: list[str],
    mineru_text: list[str],
    native_sequences: Sequence[_NativeSequence],
    diagonal: float,
) -> MatchedNode:
    text = mineru_text[mineru_index]
    exact = [index for index, candidate in enumerate(native_text) if candidate == text and text]
    sequence_exact = [
        candidate
        for candidate in native_sequences
        if candidate.text == text and text
    ]
    if sequence_exact:
        exact_candidates = list(sequence_exact)
        exact_candidates.extend(
            _NativeSequence(
                indices=(index,),
                text=native_text[index],
                bbox=_bbox(native_spans[index]["bbox"]),
            )
            for index in exact
        )
        selected = _select_sequence_exact(
            exact_candidates,
            mineru_index,
            native_text,
            mineru_text,
            _optional_bbox(mineru_nodes[mineru_index].get("bbox")),
            diagonal,
        )
        if selected is None:
            return _unmatched(mineru_index, mineru_nodes, text)
        return _sequence_result(
            mineru_index,
            selected,
            mineru_nodes,
            diagonal,
        )

    if len(exact) == 1:
        return _result(
            mineru_index,
            exact[0],
            native_spans,
            mineru_nodes,
            CoordinateConfidence.HIGH,
            1.0,
            diagonal,
        )
    if len(exact) > 1:
        selected = _disambiguate_exact(
            exact,
            mineru_index,
            native_spans,
            mineru_nodes,
            native_text,
            mineru_text,
            diagonal,
        )
        if selected is None:
            return _unmatched(mineru_index, mineru_nodes, text)
        return _result(
            mineru_index,
            selected,
            native_spans,
            mineru_nodes,
            CoordinateConfidence.HIGH,
            1.0,
            diagonal,
        )

    ranked = sorted(
        (
            (
                fuzz.ratio(text, candidate) / 100.0,
                _distance_ratio(
                    _optional_bbox(native_spans[index].get("bbox")),
                    _optional_bbox(mineru_nodes[mineru_index].get("bbox")),
                    diagonal,
                ),
                index,
            )
            for index, candidate in enumerate(native_text)
            if candidate
        ),
        key=lambda candidate: (-candidate[0], candidate[1], candidate[2]),
    )
    if not ranked:
        return _unmatched(mineru_index, mineru_nodes, text)

    eligible = [
        candidate
        for candidate in ranked
        if candidate[0] >= 0.92 and candidate[1] <= 0.03
    ]
    similarity, distance, native_index = eligible[0] if eligible else ranked[0]
    confidence = CoordinateConfidence.MEDIUM if eligible else CoordinateConfidence.LOW
    return _result(
        mineru_index,
        native_index,
        native_spans,
        mineru_nodes,
        confidence,
        similarity,
        diagonal,
    )


def _disambiguate_exact(
    candidates: list[int],
    mineru_index: int,
    native_spans: Sequence[Mapping[str, Any]],
    mineru_nodes: Sequence[Mapping[str, Any]],
    native_text: list[str],
    mineru_text: list[str],
    diagonal: float,
) -> int | None:
    ranked = sorted(
        (
            (
                _neighbor_score(index, mineru_index, native_text, mineru_text),
                -_distance_ratio(
                    _optional_bbox(native_spans[index].get("bbox")),
                    _optional_bbox(mineru_nodes[mineru_index].get("bbox")),
                    diagonal,
                ),
                index,
            )
            for index in candidates
        ),
        reverse=True,
    )
    if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
        return None
    return ranked[0][2]


def _neighbor_score(
    native_index: int,
    mineru_index: int,
    native_text: list[str],
    mineru_text: list[str],
) -> int:
    score = 0
    for offset in (-1, 1):
        native_neighbor = native_index + offset
        mineru_neighbor = mineru_index + offset
        if (
            0 <= native_neighbor < len(native_text)
            and 0 <= mineru_neighbor < len(mineru_text)
            and native_text[native_neighbor] == mineru_text[mineru_neighbor]
        ):
            score += 1
    return score


def _native_sequences(
    native_spans: Sequence[Mapping[str, Any]],
) -> list[_NativeSequence]:
    valid: list[tuple[int, str, BBox]] = []
    for index, span in enumerate(native_spans):
        text = normalize_term(str(span.get("text", "")))
        bbox = _optional_bbox(span.get("bbox"))
        if text and bbox is not None:
            valid.append((index, text, bbox))

    sequences: list[_NativeSequence] = []
    for start_position, (start_index, start_text, start_bbox) in enumerate(valid):
        pending = [(start_position, (start_index,), (start_text,), (start_bbox,))]
        while pending:
            current_position, indices, texts, boxes = pending.pop()
            if len(indices) >= MAX_NATIVE_SEQUENCE_SPANS:
                continue
            for next_position in _native_continuations(valid, current_position):
                next_index, next_text, next_bbox = valid[next_position]
                next_indices = (*indices, next_index)
                next_texts = (*texts, next_text)
                next_boxes = (*boxes, next_bbox)
                sequences.append(
                    _NativeSequence(
                        indices=next_indices,
                        text=normalize_term(" ".join(next_texts)),
                        bbox=_union_bbox(next_boxes),
                    )
                )
                pending.append(
                    (next_position, next_indices, next_texts, next_boxes)
                )
    return sequences


def _native_continuations(
    spans: Sequence[tuple[int, str, BBox]], current_position: int
) -> list[int]:
    _current_index, _current_text, current = spans[current_position]
    choices: list[tuple[float, float, int]] = []
    for position in range(current_position + 1, len(spans)):
        _index, _text, candidate = spans[position]
        vertical_gap = candidate[1] - current[3]
        aligned_left = abs(candidate[0] - current[0]) <= NATIVE_SEQUENCE_X_TOLERANCE
        max_vertical_gap = max(4.0, (current[3] - current[1]) * 0.75)
        if aligned_left and -1.0 <= vertical_gap <= max_vertical_gap:
            choices.append(
                (max(vertical_gap, 0.0), abs(candidate[0] - current[0]), position)
            )
            continue

        current_center_y = (current[1] + current[3]) / 2.0
        candidate_center_y = (candidate[1] + candidate[3]) / 2.0
        same_line = abs(candidate_center_y - current_center_y) <= 1.5
        horizontal_gap = candidate[0] - current[2]
        if same_line and -1.0 <= horizontal_gap <= 14.0:
            choices.append((0.0, max(horizontal_gap, 0.0), position))
    return [choice[2] for choice in sorted(choices)]


def _select_sequence_exact(
    candidates: Sequence[_NativeSequence],
    mineru_index: int,
    native_text: Sequence[str],
    mineru_text: Sequence[str],
    mineru_bbox: BBox | None,
    diagonal: float,
) -> _NativeSequence | None:
    ranked = sorted(
        [
            (
                _sequence_neighbor_score(
                    candidate,
                    mineru_index,
                    native_text,
                    mineru_text,
                ),
                -_distance_ratio(candidate.bbox, mineru_bbox, diagonal),
                candidate.indices,
                candidate,
            )
            for candidate in candidates
        ],
        reverse=True,
    )
    if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
        return None
    return ranked[0][3]


def _sequence_neighbor_score(
    candidate: _NativeSequence,
    mineru_index: int,
    native_text: Sequence[str],
    mineru_text: Sequence[str],
) -> int:
    score = 0
    boundaries = (
        (candidate.indices[0] - 1, mineru_index - 1),
        (candidate.indices[-1] + 1, mineru_index + 1),
    )
    for native_neighbor, mineru_neighbor in boundaries:
        if (
            0 <= native_neighbor < len(native_text)
            and 0 <= mineru_neighbor < len(mineru_text)
            and native_text[native_neighbor] == mineru_text[mineru_neighbor]
        ):
            score += 1
    return score


def _sequence_result(
    mineru_index: int,
    sequence: _NativeSequence,
    mineru_nodes: Sequence[Mapping[str, Any]],
    diagonal: float,
) -> MatchedNode:
    mineru_bbox = _optional_bbox(mineru_nodes[mineru_index].get("bbox"))
    return MatchedNode(
        mineru_index=mineru_index,
        native_index=sequence.indices[0],
        text=str(mineru_nodes[mineru_index].get("text", "")),
        source_bbox=sequence.bbox,
        mineru_bbox=mineru_bbox,
        coordinate_confidence=CoordinateConfidence.HIGH,
        similarity=1.0,
        distance_ratio=_distance_ratio(sequence.bbox, mineru_bbox, diagonal),
        auto_approvable=True,
    )


def _union_bbox(boxes: Sequence[BBox]) -> BBox:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _result(
    mineru_index: int,
    native_index: int,
    native_spans: Sequence[Mapping[str, Any]],
    mineru_nodes: Sequence[Mapping[str, Any]],
    confidence: CoordinateConfidence,
    similarity: float,
    diagonal: float,
) -> MatchedNode:
    source_bbox = _bbox(native_spans[native_index]["bbox"])
    mineru_bbox = _optional_bbox(mineru_nodes[mineru_index].get("bbox"))
    return MatchedNode(
        mineru_index=mineru_index,
        native_index=native_index,
        text=str(mineru_nodes[mineru_index].get("text", "")),
        source_bbox=source_bbox,
        mineru_bbox=mineru_bbox,
        coordinate_confidence=confidence,
        similarity=similarity,
        distance_ratio=_distance_ratio(source_bbox, mineru_bbox, diagonal),
        auto_approvable=confidence is CoordinateConfidence.HIGH,
    )


def _unmatched(
    mineru_index: int,
    mineru_nodes: Sequence[Mapping[str, Any]],
    text: str,
) -> MatchedNode:
    return MatchedNode(
        mineru_index=mineru_index,
        native_index=None,
        text=str(mineru_nodes[mineru_index].get("text", text)),
        source_bbox=None,
        mineru_bbox=_optional_bbox(mineru_nodes[mineru_index].get("bbox")),
        coordinate_confidence=CoordinateConfidence.LOW,
        similarity=0.0,
        distance_ratio=None,
        auto_approvable=False,
    )


def _bbox(values: Sequence[float]) -> BBox:
    if len(values) != 4:
        raise ValueError("bbox must contain four coordinates")
    bbox = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in bbox):
        raise ValueError("bbox coordinates must be finite")
    return bbox


def _optional_bbox(values: object) -> BBox | None:
    if values is None:
        return None
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    try:
        return _bbox(values)
    except (TypeError, ValueError):
        return None


def _diagonal(rect: BBox) -> float:
    diagonal = math.hypot(rect[2] - rect[0], rect[3] - rect[1])
    if diagonal <= 0:
        raise ValueError("page_rect must have positive area")
    return diagonal


def _distance_ratio(left: BBox | None, right: BBox | None, diagonal: float) -> float:
    if left is None or right is None:
        return math.inf
    left_center = ((left[0] + left[2]) / 2, (left[1] + left[3]) / 2)
    right_center = ((right[0] + right[2]) / 2, (right[1] + right[3]) / 2)
    return math.dist(left_center, right_center) / diagonal
