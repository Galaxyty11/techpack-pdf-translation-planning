from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest

from techpack_pdf.errors import TechpackError
from techpack_pdf.mineru import MinerUClient


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
