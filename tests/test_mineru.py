from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pymupdf
import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.mineru import MinerUClient
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
