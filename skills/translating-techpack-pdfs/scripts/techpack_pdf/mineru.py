"""Synchronous boundary client for the local MinerU service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

import httpx

from .errors import TechpackError
from .glossary import normalize_term
from .pdf_analysis import PdfManifest


@dataclass(frozen=True)
class DegradedPage:
    page_index: int
    risk_level: Literal["medium"] = "medium"


@dataclass(frozen=True)
class NativeOnlyDegradation:
    mode: Literal["degraded_native_only"] = "degraded_native_only"
    pages: tuple[DegradedPage, ...] = ()


@dataclass(frozen=True)
class _RawTableCell:
    text: str
    colspan: int
    rowspan: int


@dataclass(frozen=True)
class _PlacedTableCell:
    text: str
    row: int
    column: int
    colspan: int
    rowspan: int


_TABLE_COLUMN_ROLES = {
    "placement": "placement",
    "location": "placement",
    "position": "placement",
    "part": "component",
    "component": "component",
    "material": "material",
    "fabric": "material",
    "composition": "composition",
    "fiber content": "composition",
    "weight": "weight",
    "usage": "use",
    "use": "use",
    "point": "pom_description",
    "measurement point": "pom_description",
    "pom description": "pom_description",
    "description": "description",
    "fit notes": "note",
    "notes": "note",
    "comments": "note",
    "remarks": "note",
    "instruction": "instruction",
    "instructions": "instruction",
    "action": "action",
    "issue": "issue",
    "correction": "correction",
    "conclusion": "conclusion",
    "caption": "caption",
}
_RETAINED_TABLE_HEADERS = frozenset(
    {
        "image",
        "component size",
        "size",
        "pom",
        "spec",
        "tolerance",
        "tol -",
        "tol +",
        "target",
        "factory",
        "actual",
        "actual diff",
        "revised",
        "uom",
        "unit",
        "article number",
        "supplier",
        "vendor",
        "color",
        "colour",
        "quantity",
        "qty",
        "code",
        "number",
        "status",
        "date",
        "revision",
        "request number",
        "sample id",
        "dimension",
        "label",
    }
)
_LOGICAL_TABLE_TITLES = frozenset(
    {
        "bill of material",
        "bill of materials",
        "construction detail",
        "how to measure",
        "how to measure guide",
        "label and pack",
        "measurement sheet",
        "sample style review",
        "style additional images",
        "style category fields",
        "style general information",
        "style sample",
    }
)


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_RawTableCell]] = []
        self._row: list[_RawTableCell] | None = None
        self._cell_text: list[str] | None = None
        self._cell_spans = (1, 1)
        self._table_depth = 0
        self._primary_table_seen = False
        self._capturing_primary_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._capturing_primary_table = not self._primary_table_seen
                self._primary_table_seen = True
            return
        if self._primary_table_seen and (
            not self._capturing_primary_table or self._table_depth != 1
        ):
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            attributes = dict(attrs)
            self._cell_text = []
            self._cell_spans = (
                _html_span(attributes.get("colspan")),
                _html_span(attributes.get("rowspan")),
            )

    def handle_data(self, data: str) -> None:
        if (
            self._cell_text is not None
            and (not self._primary_table_seen or self._capturing_primary_table)
            and self._table_depth <= 1
        ):
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth == 1:
                self._capturing_primary_table = False
            if self._table_depth:
                self._table_depth -= 1
            return
        if self._primary_table_seen and (
            not self._capturing_primary_table or self._table_depth != 1
        ):
            return
        if tag in {"td", "th"} and self._row is not None and self._cell_text is not None:
            text = " ".join("".join(self._cell_text).split())
            colspan, rowspan = self._cell_spans
            self._row.append(_RawTableCell(text, colspan, rowspan))
            self._cell_text = None
            self._cell_spans = (1, 1)
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


class MinerUClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.transport = transport

    def parse_or_degrade(
        self,
        path: Path,
        manifest: PdfManifest,
    ) -> dict[str, Any] | NativeOnlyDegradation:
        try:
            return self.parse(path)
        except TechpackError as exc:
            if exc.code not in {"mineru_unavailable", "mineru_invalid_response"}:
                raise
            incomplete_page = next(
                (page for page in manifest.pages if not page.native_text_complete),
                None,
            )
            if incomplete_page is not None:
                raise TechpackError(
                    "mineru_unavailable",
                    "MinerU failed and native text is incomplete",
                    {
                        "page_index": incomplete_page.page_index,
                        "error_code": "native_text_incomplete",
                    },
                ) from exc
            return NativeOnlyDegradation(
                pages=tuple(DegradedPage(page.page_index) for page in manifest.pages)
            )

    def parse(self, path: Path) -> dict[str, Any]:
        source_path = Path(path)
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                health = client.get("/health")
                health.raise_for_status()
                health_payload = health.json()
                if (
                    not isinstance(health_payload, dict)
                    or health_payload.get("status") != "healthy"
                    or health_payload.get("protocol_version") != 2
                ):
                    raise _unavailable()

                with source_path.open("rb") as source:
                    response = client.post(
                        "/file_parse",
                        files={"files": (source_path.name, source, "application/pdf")},
                        data={
                            "backend": "hybrid-engine",
                            "effort": "medium",
                            "parse_method": "auto",
                            "return_middle_json": "true",
                            "return_content_list": "true",
                            "response_format_zip": "false",
                        },
                    )
                response.raise_for_status()
        except TechpackError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise _unavailable() from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TechpackError(
                "mineru_invalid_response",
                "MinerU returned an invalid response",
                {"error_code": "malformed_json"},
            ) from exc
        if not isinstance(payload, dict):
            raise TechpackError(
                "mineru_invalid_response",
                "MinerU returned an invalid response",
                {"error_code": "non_object_json"},
            )
        return _normalize_response(payload)


def _normalize_response(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    if not isinstance(results, dict):
        if "status" in payload and "results" in payload:
            raise _invalid_response("invalid_task_envelope")
        return payload
    if (
        payload.get("status") != "completed"
        or payload.get("error") is not None
        or len(results) != 1
    ):
        raise _invalid_response("invalid_task_envelope")
    result = next(iter(results.values()))
    if not isinstance(result, dict):
        raise _invalid_response("invalid_task_result")
    try:
        middle = json.loads(result["middle_json"])
        content = json.loads(result["content_list"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise _invalid_response("invalid_nested_json") from exc
    if not isinstance(middle, dict) or not isinstance(content, list):
        raise _invalid_response("invalid_nested_json")
    raw_pages = middle.get("pdf_info")
    if not isinstance(raw_pages, list):
        raise _invalid_response("missing_pdf_info")

    pages: dict[int, dict[str, Any]] = {}
    page_sizes: dict[int, tuple[float, float]] = {}
    middle_tables: dict[int, list[tuple[str, list[float]]]] = {}
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            raise _invalid_response("invalid_pdf_page")
        page_index = raw_page.get("page_idx")
        page_size = raw_page.get("page_size")
        if (
            not isinstance(page_index, int)
            or page_index in pages
            or not isinstance(page_size, list)
            or len(page_size) != 2
        ):
            raise _invalid_response("invalid_pdf_page")
        try:
            width, height = (float(value) for value in page_size)
        except (TypeError, ValueError) as exc:
            raise _invalid_response("invalid_pdf_page") from exc
        if width <= 0 or height <= 0:
            raise _invalid_response("invalid_pdf_page")
        pages[page_index] = {
            "page_index": page_index,
            "title": "",
            "table_headers": [],
            "visual_features": [],
            "nodes": [],
        }
        page_sizes[page_index] = (width, height)
        if "preproc_blocks" not in raw_page:
            continue
        preproc_blocks = raw_page["preproc_blocks"]
        if not isinstance(preproc_blocks, list):
            raise _invalid_response("invalid_preproc_blocks")
        middle_tables[page_index] = _middle_table_spans(
            preproc_blocks,
            page_sizes[page_index],
        )

    for page_index, tables in middle_tables.items():
        for table_html, table_bbox in tables:
            if "table" not in pages[page_index]["visual_features"]:
                pages[page_index]["visual_features"].append("table")
            table_nodes, headers = _table_nodes(table_html, table_bbox)
            pages[page_index]["nodes"].extend(table_nodes)
            pages[page_index]["table_headers"].extend(headers)

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        page_index = item.get("page_idx")
        if page_index not in pages:
            continue
        if item_type == "table":
            if page_index in middle_tables:
                continue
            if "table" not in pages[page_index]["visual_features"]:
                pages[page_index]["visual_features"].append("table")
            table_body = item.get("table_body")
            if not isinstance(table_body, str) or not table_body.strip():
                continue
            table_bbox = _scaled_bbox(item.get("bbox"), page_sizes[page_index])
            table_nodes, headers = _table_nodes(table_body, table_bbox)
            pages[page_index]["nodes"].extend(table_nodes)
            pages[page_index]["table_headers"].extend(headers)
            continue
        if item_type == "image":
            if "image" not in pages[page_index]["visual_features"]:
                pages[page_index]["visual_features"].append("image")
            continue
        if item_type != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        role = "title" if item.get("text_level") == 2 else "body"
        node = {
            "text": text,
            "bbox": _scaled_bbox(item.get("bbox"), page_sizes[page_index]),
            "field_role": role,
        }
        pages[page_index]["nodes"].append(node)
        if role == "title" and not pages[page_index]["title"]:
            pages[page_index]["title"] = text
    return {"pages": [pages[index] for index in sorted(pages)]}


def _middle_table_spans(
    preproc_blocks: list[object],
    page_size: tuple[float, float],
) -> list[tuple[str, list[float]]]:
    tables: list[tuple[str, list[float]]] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") == "table":
            html = value.get("html")
            if isinstance(html, str) and html.strip() and "bbox" in value:
                tables.append((html, _direct_bbox(value["bbox"], page_size)))
        for child in value.values():
            visit(child)

    visit(preproc_blocks)
    return tables


def _html_span(raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else 1
    except ValueError:
        return 1
    return value if 1 <= value <= 1000 else 1


def _table_nodes(
    html: str,
    bbox: list[float],
) -> tuple[list[dict[str, Any]], list[str]]:
    parser = _TableHTMLParser()
    parser.feed(html)
    placed = _place_table_cells(_logical_table_rows(parser.rows))
    if not placed:
        return [], []
    row_count = max(cell.row + cell.rowspan for cell in placed)
    column_count = max(cell.column + cell.colspan for cell in placed)
    header_row = _table_header_row(placed, row_count)
    column_roles: dict[int, str] = {}
    for cell in placed:
        if cell.row != header_row:
            continue
        role = _table_column_role(cell.text)
        for column in range(cell.column, cell.column + cell.colspan):
            column_roles[column] = role
    left, top, right, bottom = bbox
    cell_width = (right - left) / column_count
    cell_height = (bottom - top) / row_count
    nodes: list[dict[str, Any]] = []
    headers: list[str] = []
    previous_text = ""
    for cell in placed:
        if not cell.text:
            continue
        if cell.row <= header_row:
            role = "table_header"
        else:
            inline_role = _inline_table_role(cell.text, previous_text)
            role = (
                inline_role
                if inline_role != "table_cell"
                else column_roles.get(cell.column, "table_cell")
            )
        nodes.append(
            {
                "text": cell.text,
                "bbox": [
                    left + cell.column * cell_width,
                    top + cell.row * cell_height,
                    left + (cell.column + cell.colspan) * cell_width,
                    top + (cell.row + cell.rowspan) * cell_height,
                ],
                "field_role": role,
            }
        )
        if cell.row == header_row:
            headers.append(cell.text)
        previous_text = cell.text
    return nodes, headers


def _logical_table_rows(rows: list[list[_RawTableCell]]) -> list[list[_RawTableCell]]:
    first_title: str | None = None
    for index, row in enumerate(rows):
        row_titles = [
            normalize_term(cell.text)
            for cell in row
            if normalize_term(cell.text) in _LOGICAL_TABLE_TITLES
        ]
        if not row_titles:
            continue
        if first_title is None:
            if any(title != row_titles[0] for title in row_titles):
                return []
            first_title = row_titles[0]
            continue
        if any(title != first_title for title in row_titles):
            return _clip_rowspans_at_boundary(rows[:index])
    return rows


def _clip_rowspans_at_boundary(
    rows: list[list[_RawTableCell]],
) -> list[list[_RawTableCell]]:
    row_count = len(rows)
    return [
        [
            cell
            if cell.rowspan <= row_count - row_index
            else _RawTableCell(cell.text, cell.colspan, row_count - row_index)
            for cell in row
        ]
        for row_index, row in enumerate(rows)
    ]


def _table_header_row(placed: list[_PlacedTableCell], row_count: int) -> int:
    rows = range(row_count)
    scored = [
        (
            sum(
                _table_column_role(cell.text) != "table_cell"
                for cell in placed
                if cell.row == row and cell.text
            ),
            row,
        )
        for row in rows
    ]
    score, row = max(scored, key=lambda item: (item[0], -item[1]))
    if score:
        return row
    return next(
        (
            candidate
            for candidate in rows
            if sum(cell.row == candidate and bool(cell.text) for cell in placed) > 1
        ),
        placed[0].row,
    )


def _table_column_role(text: str) -> str:
    header = " ".join(text.casefold().split()).strip(" :")
    if header in _RETAINED_TABLE_HEADERS:
        return "retained_table_value"
    return _TABLE_COLUMN_ROLES.get(header, "table_cell")


def _inline_table_role(text: str, previous_text: str) -> str:
    if ":" not in text:
        return "table_cell"
    label = " ".join(text.split(":", 1)[0].casefold().split())
    previous_label = (
        " ".join(previous_text.split(":", 1)[0].casefold().split())
        if ":" in previous_text
        else ""
    )
    if label == "description":
        return "description" if previous_label == "pom" else "retained_table_value"
    if label in {"notes", "fit notes", "comments", "remarks"}:
        return "note"
    if label in {"instruction", "instructions", "construction detail"}:
        return "instruction"
    if label in {"action", "issue", "correction", "conclusion", "caption"}:
        return label
    if label in _RETAINED_TABLE_HEADERS or label in {
        "style",
        "season",
        "division",
        "category",
        "designer",
        "tech designer",
        "sourcing",
        "stage",
        "measurement",
        "measurement type",
        "size class",
        "base size",
        "grade rule",
        "sample type",
        "requested on",
        "required by",
        "approval status",
    }:
        return "retained_table_value"
    return "table_cell"


def _place_table_cells(rows: list[list[_RawTableCell]]) -> list[_PlacedTableCell]:
    occupied: set[tuple[int, int]] = set()
    placed: list[_PlacedTableCell] = []
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row:
            while any(
                (row_index, candidate) in occupied
                for candidate in range(column, column + cell.colspan)
            ):
                column += 1
            placed.append(
                _PlacedTableCell(
                    cell.text,
                    row_index,
                    column,
                    cell.colspan,
                    cell.rowspan,
                )
            )
            for occupied_row in range(row_index, row_index + cell.rowspan):
                for occupied_column in range(column, column + cell.colspan):
                    occupied.add((occupied_row, occupied_column))
            column += cell.colspan
    return placed


def _scaled_bbox(value: object, page_size: tuple[float, float]) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise _invalid_response("invalid_content_bbox")
    try:
        left, top, right, bottom = (float(coordinate) for coordinate in value)
    except (TypeError, ValueError) as exc:
        raise _invalid_response("invalid_content_bbox") from exc
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        raise _invalid_response("invalid_content_bbox")
    width, height = page_size
    return [
        left * width / 1000,
        top * height / 1000,
        right * width / 1000,
        bottom * height / 1000,
    ]


def _direct_bbox(value: object, page_size: tuple[float, float]) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise _invalid_response("invalid_middle_table_bbox")
    try:
        left, top, right, bottom = (float(coordinate) for coordinate in value)
    except (TypeError, ValueError) as exc:
        raise _invalid_response("invalid_middle_table_bbox") from exc
    width, height = page_size
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise _invalid_response("invalid_middle_table_bbox")
    return [left, top, right, bottom]


def _invalid_response(error_code: str) -> TechpackError:
    return TechpackError(
        "mineru_invalid_response",
        "MinerU returned an invalid response",
        {"error_code": error_code},
    )


def _unavailable() -> TechpackError:
    return TechpackError("mineru_unavailable", "MinerU service is unavailable")
