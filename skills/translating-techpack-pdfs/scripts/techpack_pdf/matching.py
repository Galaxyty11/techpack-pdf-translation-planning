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


def match_nodes(
    native_spans: Sequence[Mapping[str, Any]],
    mineru_nodes: Sequence[Mapping[str, Any]],
    page_rect: Sequence[float],
) -> list[MatchedNode]:
    """Return one coordinate match per MinerU node on a single page."""
    diagonal = _diagonal(_bbox(page_rect))
    native_text = [normalize_term(str(span.get("text", ""))) for span in native_spans]
    mineru_text = [normalize_term(str(node.get("text", ""))) for node in mineru_nodes]

    return [
        _match_one(
            mineru_index,
            native_spans,
            mineru_nodes,
            native_text,
            mineru_text,
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
    diagonal: float,
) -> MatchedNode:
    text = mineru_text[mineru_index]
    exact = [index for index, candidate in enumerate(native_text) if candidate == text and text]
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
                -_distance_ratio(
                    _optional_bbox(native_spans[index].get("bbox")),
                    _optional_bbox(mineru_nodes[mineru_index].get("bbox")),
                    diagonal,
                ),
                index,
            )
            for index, candidate in enumerate(native_text)
            if candidate
        ),
        reverse=True,
    )
    if not ranked:
        return _unmatched(mineru_index, mineru_nodes, text)

    similarity, negative_distance, native_index = ranked[0]
    distance = -negative_distance
    confidence = (
        CoordinateConfidence.MEDIUM
        if similarity >= 0.92 and distance <= 0.03
        else CoordinateConfidence.LOW
    )
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
