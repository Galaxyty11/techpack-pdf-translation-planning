from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pymupdf
import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.mineru import MinerUClient, _TableHTMLParser, _table_nodes
from techpack_pdf.pdf_analysis import inspect_pdf


def _multipart_parts(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: "
        + request.headers["content-type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + request.content
    )
    return {
        part.get_param("name", header="content-disposition"): (
            part.get_filename(),
            part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    }


def _parse_single_v2_table(tmp_path: Path, table_body: str) -> dict[str, object]:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "discarded_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "table",
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
                "table_body": table_body,
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    return MinerUClient(transport=httpx.MockTransport(handler)).parse(source)


def _parse_v2_tables(
    tmp_path: Path,
    raw_pages: list[dict[str, object]],
    content_list: list[dict[str, object]],
) -> dict[str, object]:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps({"pdf_info": raw_pages})
    content_json = json.dumps(content_list)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_json,
                    }
                },
            },
        )

    return MinerUClient(transport=httpx.MockTransport(handler)).parse(source)


def test_parse_uses_page_scoped_middle_tables_over_merged_content_tables(
    tmp_path: Path,
) -> None:
    parsed = _parse_v2_tables(
        tmp_path,
        [
            {
                "page_idx": 0,
                "page_size": [200, 120],
                "preproc_blocks": [
                    {
                        "type": "block",
                        "spans": [
                            {
                                "type": "table",
                                "bbox": [10, 10, 190, 90],
                                "html": (
                                    "<table><tr><td>Placement</td><td>Component</td></tr>"
                                    "<tr><td>MIDDLE BODY</td><td>Middle shell</td></tr></table>"
                                ),
                            }
                        ],
                    }
                ],
            },
            {
                "page_idx": 1,
                "page_size": [200, 120],
                "preproc_blocks": [
                    {
                        "type": "table",
                        "bbox": [20, 10, 180, 100],
                        "html": (
                            "<table><tr><td colspan=\"2\">Measurement Sheet</td></tr>"
                            "<tr><td>POM</td><td>Description</td></tr>"
                            "<tr><td>001</td><td>Back neck width</td></tr></table>"
                        ),
                    }
                ],
            },
        ],
        [
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 1000],
                "table_body": (
                    "<table><tr><td>Placement</td><td>Component</td></tr>"
                    "<tr><td>CONTENT BODY</td><td>Content shell</td></tr>"
                    "<tr><td colspan=\"2\">Measurement Sheet</td></tr>"
                    "<tr><td>POM</td><td>Description</td></tr>"
                    "<tr><td>999</td><td>Merged measurement</td></tr></table>"
                ),
            },
            {
                "type": "table",
                "page_idx": 1,
                "bbox": [0, 0, 1000, 1000],
                "table_body": "",
            },
        ],
    )

    pages = parsed["pages"]
    assert [node["text"] for node in pages[0]["nodes"]] == [
        "Placement",
        "Component",
        "MIDDLE BODY",
        "Middle shell",
    ]
    assert [node["text"] for node in pages[1]["nodes"]] == [
        "Measurement Sheet",
        "POM",
        "Description",
        "001",
        "Back neck width",
    ]
    description = pages[1]["nodes"][-1]
    assert description["bbox"] == [100.0, 70.0, 180.0, 100.0]
    assert "CONTENT BODY" not in [node["text"] for node in pages[0]["nodes"]]
    assert "Merged measurement" not in [node["text"] for node in pages[0]["nodes"]]


def test_parse_preserves_multiple_middle_tables_in_source_order(tmp_path: Path) -> None:
    parsed = _parse_v2_tables(
        tmp_path,
        [
            {
                "page_idx": 0,
                "page_size": [200, 120],
                "preproc_blocks": [
                    {
                        "children": [
                            {
                                "type": "table",
                                "bbox": [10, 10, 90, 50],
                                "html": "<table><tr><td>Material</td></tr><tr><td>First fabric</td></tr></table>",
                            },
                            {
                                "type": "table",
                                "bbox": [110, 10, 190, 50],
                                "html": "<table><tr><td>Component</td></tr><tr><td>Second trim</td></tr></table>",
                            },
                        ]
                    }
                ],
            }
        ],
        [
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [0, 0, 1000, 1000],
                "table_body": "<table><tr><td>Content fallback</td></tr></table>",
            }
        ],
    )

    assert [node["text"] for node in parsed["pages"][0]["nodes"]] == [
        "Material",
        "First fabric",
        "Component",
        "Second trim",
    ]


def test_parse_falls_back_to_content_table_when_middle_preproc_blocks_are_absent(
    tmp_path: Path,
) -> None:
    parsed = _parse_v2_tables(
        tmp_path,
        [{"page_idx": 0, "page_size": [200, 120]}],
        [
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [100, 100, 900, 900],
                "table_body": "<table><tr><td>Material</td></tr><tr><td>Fallback fabric</td></tr></table>",
            }
        ],
    )

    assert [node["text"] for node in parsed["pages"][0]["nodes"]] == [
        "Material",
        "Fallback fabric",
    ]


def test_parse_rejects_invalid_direct_middle_table_bbox_without_raw_content(
    tmp_path: Path,
) -> None:
    with pytest.raises(TechpackError) as raised:
        _parse_v2_tables(
            tmp_path,
            [
                {
                    "page_idx": 0,
                    "page_size": [200, 120],
                    "preproc_blocks": [
                        {
                            "type": "table",
                            "bbox": [0, 0, 201, 100],
                            "html": "<table><tr><td>SECRET RAW MIDDLE HTML</td></tr></table>",
                        }
                    ],
                }
            ],
            [],
        )

    assert raised.value.code == "mineru_invalid_response"
    assert raised.value.details == {"error_code": "invalid_middle_table_bbox"}
    assert "SECRET RAW MIDDLE HTML" not in str(raised.value)


def test_parse_checks_health_and_posts_required_multipart_contract(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={"status": "healthy", "version": "3.4.5", "protocol_version": 2},
            )

        assert request.url.path == "/file_parse"
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        parts = _multipart_parts(request)
        assert parts == {
            "files": ("source.pdf", source.read_bytes()),
            "backend": (None, b"hybrid-engine"),
            "effort": (None, b"medium"),
            "parse_method": (None, b"auto"),
            "return_middle_json": (None, b"true"),
            "return_content_list": (None, b"true"),
            "response_format_zip": (None, b"false"),
        }
        return httpx.Response(200, json={"results": [{"page": 0, "text": "COLLAR"}]})

    client = MinerUClient(transport=httpx.MockTransport(handler))

    assert client.parse(source) == {"results": [{"page": 0, "text": "COLLAR"}]}


def test_parse_normalizes_completed_v2_result_into_internal_pages(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "_version_name": "2.5.4",
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [841, 595],
                    "para_blocks": [],
                    "discarded_blocks": [],
                    "preproc_blocks": [],
                }
            ],
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "text",
                "text": "MEASUREMENT SHEET",
                "text_level": 2,
                "bbox": [100, 100, 900, 200],
                "page_idx": 0,
            },
            {
                "type": "text",
                "text": "COLLAR WIDTH",
                "bbox": [100, 250, 900, 350],
                "page_idx": 0,
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "task_id": "00000000-0000-0000-0000-000000000000",
                "status": "completed",
                "backend": "hybrid-engine",
                "file_names": ["source.pdf"],
                "error": None,
                "version": "2.5.4",
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert parsed == {
        "pages": [
            {
                "page_index": 0,
                "title": "MEASUREMENT SHEET",
                "table_headers": [],
                "visual_features": [],
                "nodes": [
                    {
                        "text": "MEASUREMENT SHEET",
                        "bbox": [84.1, 59.5, 756.9, 119.0],
                        "field_role": "title",
                    },
                    {
                        "text": "COLLAR WIDTH",
                        "bbox": [84.1, 148.75, 756.9, 208.25],
                        "field_role": "body",
                    },
                ],
            }
        ]
    }


def test_parse_expands_v2_table_cells_with_spans_into_page_coordinates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "discarded_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "table",
                "bbox": [100, 400, 900, 800],
                "page_idx": 0,
                "img_path": "tables/table-0.jpg",
                "table_caption": [],
                "table_footnote": [],
                "table_body": (
                    "<table>"
                    '<tr><td colspan="4">MEASUREMENT</td></tr>'
                    "<tr><td>POINT</td><td>DESCRIPTION</td><td>SPEC</td><td>FIT NOTES</td></tr>"
                    '<tr><td rowspan="2">CHEST</td><td>BACK NECK WIDTH</td><td>50 cm</td><td>REDUCE WIDTH</td></tr>'
                    "<tr><td>FRONT NECK WIDTH</td><td>51 cm</td><td></td></tr>"
                    "</table>"
                ),
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert parsed == {
        "pages": [
            {
                "page_index": 0,
                "title": "",
                "table_headers": ["POINT", "DESCRIPTION", "SPEC", "FIT NOTES"],
                "visual_features": ["table"],
                "nodes": [
                    {
                        "text": "MEASUREMENT",
                        "bbox": [100.0, 400.0, 900.0, 500.0],
                        "field_role": "table_header",
                    },
                    {
                        "text": "POINT",
                        "bbox": [100.0, 500.0, 300.0, 600.0],
                        "field_role": "table_header",
                    },
                    {
                        "text": "DESCRIPTION",
                        "bbox": [300.0, 500.0, 500.0, 600.0],
                        "field_role": "table_header",
                    },
                    {
                        "text": "SPEC",
                        "bbox": [500.0, 500.0, 700.0, 600.0],
                        "field_role": "table_header",
                    },
                    {
                        "text": "FIT NOTES",
                        "bbox": [700.0, 500.0, 900.0, 600.0],
                        "field_role": "table_header",
                    },
                    {
                        "text": "CHEST",
                        "bbox": [100.0, 600.0, 300.0, 800.0],
                        "field_role": "pom_description",
                    },
                    {
                        "text": "BACK NECK WIDTH",
                        "bbox": [300.0, 600.0, 500.0, 700.0],
                        "field_role": "description",
                    },
                    {
                        "text": "50 cm",
                        "bbox": [500.0, 600.0, 700.0, 700.0],
                        "field_role": "retained_table_value",
                    },
                    {
                        "text": "REDUCE WIDTH",
                        "bbox": [700.0, 600.0, 900.0, 700.0],
                        "field_role": "note",
                    },
                    {
                        "text": "FRONT NECK WIDTH",
                        "bbox": [300.0, 700.0, 500.0, 800.0],
                        "field_role": "description",
                    },
                    {
                        "text": "51 cm",
                        "bbox": [500.0, 700.0, 700.0, 800.0],
                        "field_role": "retained_table_value",
                    },
                ],
            }
        ]
    }


def test_parse_maps_bom_columns_to_semantic_and_retained_roles(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "discarded_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "table",
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
                "table_body": (
                    "<table>"
                    '<tr><td colspan="5">Bill of Material</td></tr>'
                    "<tr><td>Style: 1805466</td><td colspan=\"4\">Season: SP26</td></tr>"
                    "<tr><td>Placement</td><td>Component</td><td>Usage</td><td>UOM</td><td>Supplier</td></tr>"
                    "<tr><td>BODY</td><td>4 WAY STRETCH FABRIC</td><td>1.0000</td><td>yd</td><td>SUMEC</td></tr>"
                    "</table>"
                ),
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert parsed["pages"][0]["table_headers"] == [
        "Placement",
        "Component",
        "Usage",
        "UOM",
        "Supplier",
    ]
    assert [
        (node["text"], node["field_role"])
        for node in parsed["pages"][0]["nodes"]
    ] == [
        ("Bill of Material", "table_header"),
        ("Style: 1805466", "table_header"),
        ("Season: SP26", "table_header"),
        ("Placement", "table_header"),
        ("Component", "table_header"),
        ("Usage", "table_header"),
        ("UOM", "table_header"),
        ("Supplier", "table_header"),
        ("BODY", "placement"),
        ("4 WAY STRETCH FABRIC", "component"),
        ("1.0000", "use"),
        ("yd", "retained_table_value"),
        ("SUMEC", "retained_table_value"),
    ]


def test_parse_projects_only_first_sibling_table_for_one_table_bbox(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "discarded_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "table",
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
                "table_body": (
                    "<table>"
                    "<tr><td>Placement</td><td>Component</td></tr>"
                    "<tr><td>BODY</td><td>Shell fabric</td></tr>"
                    "</table>"
                    "<table>"
                    "<tr><td>POM</td><td>Description</td></tr>"
                    "<tr><td>001</td><td>Measurement Sheet</td></tr>"
                    "</table>"
                ),
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert parsed["pages"][0]["table_headers"] == ["Placement", "Component"]
    assert [node["text"] for node in parsed["pages"][0]["nodes"]] == [
        "Placement",
        "Component",
        "BODY",
        "Shell fabric",
    ]


def test_table_html_parser_preserves_bare_row_fragments_without_table_tags() -> None:
    parser = _TableHTMLParser()

    parser.feed("<tr><td>BODY</td><td>Shell fabric</td></tr>")

    assert [[cell.text for cell in row] for row in parser.rows] == [
        ["BODY", "Shell fabric"]
    ]


def test_table_html_parser_ignores_nested_table_rows_without_corrupting_outer_row() -> None:
    parser = _TableHTMLParser()

    parser.feed(
        "<table><tr><td>BODY</td><td>Outer <table><tr><td>inner</td></tr>"
        "</table> fabric</td></tr></table>"
    )

    assert [[cell.text for cell in row] for row in parser.rows] == [
        ["BODY", "Outer fabric"]
    ]


def test_parse_truncates_a_logical_measurement_table_after_bom_rows(tmp_path: Path) -> None:
    parsed = _parse_single_v2_table(
        tmp_path,
        (
            "<table>"
            '<tr><td colspan="2">Bill of Material</td></tr>'
            "<tr><td>Placement</td><td>Component</td></tr>"
            "<tr><td>BODY</td><td>Shell fabric</td></tr>"
            '<tr><td colspan="2">Measurement Sheet</td></tr>'
            "<tr><td>POM</td><td>Description</td></tr>"
            "<tr><td>001</td><td>Back neck width</td></tr>"
            "</table>"
        ),
    )

    page = parsed["pages"][0]
    assert page["table_headers"] == ["Placement", "Component"]
    assert [node["text"] for node in page["nodes"]] == [
        "Bill of Material",
        "Placement",
        "Component",
        "BODY",
        "Shell fabric",
    ]


def test_table_nodes_truncate_a_later_how_to_measure_logical_title() -> None:
    nodes, headers = _table_nodes(
        (
            "<table>"
            '<tr><td colspan="2">Measurement Sheet</td></tr>'
            "<tr><td>POM</td><td>Description</td></tr>"
            "<tr><td>001</td><td>Back neck width</td></tr>"
            '<tr><td colspan="2">How To Measure Guide</td></tr>'
            "<tr><td>Instruction</td><td>Measure straight across</td></tr>"
            "</table>"
        ),
        [0, 0, 100, 100],
    )

    assert headers == ["POM", "Description"]
    assert [node["text"] for node in nodes] == [
        "Measurement Sheet",
        "POM",
        "Description",
        "001",
        "Back neck width",
    ]


def test_table_nodes_preserve_repeated_same_logical_title() -> None:
    nodes, _ = _table_nodes(
        (
            "<table>"
            '<tr><td colspan="2">Sample Style Review</td></tr>'
            "<tr><td>Action</td><td>Issue</td></tr>"
            "<tr><td>Reduce</td><td>Sleeve length</td></tr>"
            '<tr><td colspan="2">Sample Style Review</td></tr>'
            "<tr><td>Continue</td><td>Review</td></tr>"
            "</table>"
        ),
        [0, 0, 100, 100],
    )

    assert [node["text"] for node in nodes] == [
        "Sample Style Review",
        "Action",
        "Issue",
        "Reduce",
        "Sleeve length",
        "Sample Style Review",
        "Continue",
        "Review",
    ]


def test_table_nodes_preserve_non_exact_logical_title_reference() -> None:
    nodes, _ = _table_nodes(
        (
            "<table>"
            '<tr><td colspan="2">Bill of Material</td></tr>'
            "<tr><td>Placement</td><td>Component</td></tr>"
            "<tr><td>BODY</td><td>Shell fabric</td></tr>"
            '<tr><td colspan="2">SEE MEASUREMENT SHEET</td></tr>'
            "<tr><td>TAG</td><td>Hang tag</td></tr>"
            "</table>"
        ),
        [0, 0, 100, 100],
    )

    assert [node["text"] for node in nodes] == [
        "Bill of Material",
        "Placement",
        "Component",
        "BODY",
        "Shell fabric",
        "SEE MEASUREMENT SHEET",
        "TAG",
        "Hang tag",
    ]


def test_table_nodes_clip_retained_rowspan_at_logical_title_boundary() -> None:
    nodes, _ = _table_nodes(
        (
            "<table>"
            '<tr><td colspan="2">Bill of Material</td></tr>'
            '<tr><td rowspan="3">BODY</td><td>Shell fabric</td></tr>'
            "<tr><td>Shell fabric continued</td></tr>"
            '<tr><td colspan="2">Measurement Sheet</td></tr>'
            "<tr><td>POM</td><td>Description</td></tr>"
            "</table>"
        ),
        [0, 0, 2, 3],
    )

    by_text = {node["text"]: node["bbox"] for node in nodes}
    assert by_text["Bill of Material"] == [0.0, 0.0, 2.0, 1.0]
    assert by_text["BODY"] == [0.0, 1.0, 1.0, 3.0]
    assert "Measurement Sheet" not in by_text


def test_table_nodes_fail_closed_for_ambiguous_first_logical_title_row() -> None:
    nodes, headers = _table_nodes(
        (
            "<table>"
            "<tr><td>Bill of Material</td><td>Measurement Sheet</td></tr>"
            "<tr><td>Placement</td><td>Component</td></tr>"
            "<tr><td>BODY</td><td>Shell fabric</td></tr>"
            "</table>"
        ),
        [0, 0, 100, 100],
    )

    assert nodes == []
    assert headers == []


def test_table_nodes_normalize_full_width_logical_titles_before_boundary_check() -> None:
    nodes, _ = _table_nodes(
        (
            "<table>"
            '<tr><td colspan="2">Ｂｉｌｌ　ｏｆ　Ｍａｔｅｒｉａｌ</td></tr>'
            "<tr><td>Placement</td><td>Component</td></tr>"
            "<tr><td>BODY</td><td>Shell fabric</td></tr>"
            '<tr><td colspan="2">Measurement Sheet</td></tr>'
            "<tr><td>POM</td><td>Description</td></tr>"
            "</table>"
        ),
        [0, 0, 100, 100],
    )

    assert [node["text"] for node in nodes] == [
        "Ｂｉｌｌ ｏｆ Ｍａｔｅｒｉａｌ",
        "Placement",
        "Component",
        "BODY",
        "Shell fabric",
    ]


def test_parse_recovers_inline_measurement_roles_without_column_headers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "discarded_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "table",
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
                "table_body": (
                    "<table>"
                    '<tr><td colspan="2">Measurement Sheet</td></tr>'
                    "<tr><td>Style : 1805466</td><td>Description : CSG EVERYDAY SHORT</td></tr>"
                    "<tr><td>POM : 201A</td><td>Description : BACK NECK WIDTH</td></tr>"
                    '<tr><td colspan="2">Notes: REDUCE WIDTH</td></tr>'
                    "</table>"
                ),
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert [
        (node["text"], node["field_role"])
        for node in parsed["pages"][0]["nodes"]
    ] == [
        ("Measurement Sheet", "table_header"),
        ("Style : 1805466", "table_header"),
        ("Description : CSG EVERYDAY SHORT", "table_header"),
        ("POM : 201A", "retained_table_value"),
        ("Description : BACK NECK WIDTH", "description"),
        ("Notes: REDUCE WIDTH", "note"),
    ]


def test_parse_does_not_treat_page_title_as_an_instruction_column(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "discarded_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "table",
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
                "table_body": (
                    "<table>"
                    '<tr><td colspan="2">Construction Detail</td></tr>'
                    "<tr><td>Style</td><td>Season</td></tr>"
                    "<tr><td>1805466</td><td>SP26</td></tr>"
                    "</table>"
                ),
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert all(
        node["field_role"] != "instruction"
        for node in parsed["pages"][0]["nodes"]
    )


def test_parse_marks_v2_image_blocks_as_visual_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF synthetic boundary fixture")
    middle_json = json.dumps(
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [841, 595],
                    "para_blocks": [],
                    "discarded_blocks": [],
                    "preproc_blocks": [],
                }
            ]
        }
    )
    content_list = json.dumps(
        [
            {
                "type": "image",
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
                "img_path": "images/image-0.jpg",
                "content": "",
                "image_caption": [],
                "image_footnote": [],
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": None,
                "results": {
                    "source": {
                        "md_content": "",
                        "middle_json": middle_json,
                        "content_list": content_list,
                    }
                },
            },
        )

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert parsed == {
        "pages": [
            {
                "page_index": 0,
                "title": "",
                "table_headers": [],
                "visual_features": ["image"],
                "nodes": [],
            }
        ]
    }


@pytest.mark.parametrize(
    ("health_payload", "status_code"),
    [
        ({"status": "starting", "protocol_version": 2}, 200),
        ({"status": "healthy", "protocol_version": 1}, 200),
        ({"error": "unavailable"}, 503),
    ],
)
def test_parse_rejects_unhealthy_or_incompatible_service(
    tmp_path: Path, health_payload: dict[str, object], status_code: int
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=health_payload)
    )

    with pytest.raises(TechpackError) as raised:
        MinerUClient(transport=transport).parse(source)

    assert raised.value.code == "mineru_unavailable"


def test_parse_maps_connection_failures_to_mineru_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def cannot_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TechpackError) as raised:
        MinerUClient(transport=httpx.MockTransport(cannot_connect)).parse(source)

    assert raised.value.code == "mineru_unavailable"


def test_default_timeout_allows_observed_mineru_cold_start(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        if request.extensions["timeout"]["read"] <= 50:
            raise httpx.ReadTimeout("simulated 49 second MinerU cold start", request=request)
        return httpx.Response(200, json={"pages": []})

    parsed = MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert parsed == {"pages": []}


def test_parse_rejects_non_object_json_response(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(200, json=["unexpected"])

    with pytest.raises(TechpackError) as raised:
        MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert raised.value.code == "mineru_invalid_response"


def test_parse_rejects_malformed_json_response(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(TechpackError) as raised:
        MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert raised.value.code == "mineru_invalid_response"


def test_parse_rejects_failed_v2_task_before_workflow_processing(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "task_id": "00000000-0000-0000-0000-000000000000",
                "status": "failed",
                "error": "upstream parse failed",
                "results": None,
            },
        )

    with pytest.raises(TechpackError) as raised:
        MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert raised.value.code == "mineru_invalid_response"
    assert raised.value.details == {"error_code": "invalid_task_envelope"}


def test_parse_rejects_completed_v2_task_with_error_payload(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "protocol_version": 2})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "error": "partial parse failure",
                "results": {
                    "source": {
                        "middle_json": json.dumps({"pdf_info": []}),
                        "content_list": "[]",
                    }
                },
            },
        )

    with pytest.raises(TechpackError) as raised:
        MinerUClient(transport=httpx.MockTransport(handler)).parse(source)

    assert raised.value.code == "mineru_invalid_response"
    assert raised.value.details == {"error_code": "invalid_task_envelope"}


def _native_manifest(tmp_path: Path, *, complete: bool):
    source = tmp_path / ("native-complete.pdf" if complete else "native-incomplete.pdf")
    document = pymupdf.open()
    document.new_page(width=100, height=100).insert_text((10, 20), "PAGE ONE")
    second = document.new_page(width=100, height=100)
    if complete:
        second.insert_text((10, 20), "PAGE TWO")
    else:
        pixmap = pymupdf.Pixmap(
            pymupdf.csRGB,
            pymupdf.IRect(0, 0, 100, 100),
            False,
        )
        pixmap.clear_with(200)
        second.insert_image(second.rect, pixmap=pixmap)
    document.save(source)
    document.close()
    return source, inspect_pdf(source, tmp_path / "manifest-job")


def test_parse_or_degrade_marks_every_complete_native_page_medium_risk(
    tmp_path: Path,
) -> None:
    source, manifest = _native_manifest(tmp_path, complete=True)

    def cannot_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    outcome = MinerUClient(
        transport=httpx.MockTransport(cannot_connect)
    ).parse_or_degrade(source, manifest)

    assert manifest.native_text_complete is True
    assert outcome.mode == "degraded_native_only"
    assert [(page.page_index, page.risk_level) for page in outcome.pages] == [
        (0, "medium"),
        (1, "medium"),
    ]


def test_parse_or_degrade_blocks_when_any_page_needs_ocr(tmp_path: Path) -> None:
    source, manifest = _native_manifest(tmp_path, complete=False)

    def cannot_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TechpackError) as raised:
        MinerUClient(
            transport=httpx.MockTransport(cannot_connect)
        ).parse_or_degrade(source, manifest)

    assert manifest.native_text_complete is False
    assert raised.value.code == "mineru_unavailable"
    assert raised.value.details == {
        "page_index": 1,
        "error_code": "native_text_incomplete",
    }
