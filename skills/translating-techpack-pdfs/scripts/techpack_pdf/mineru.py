"""Synchronous boundary client for the local MinerU service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .errors import TechpackError


class MinerUClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.transport = transport

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
        return payload


def _unavailable() -> TechpackError:
    return TechpackError("mineru_unavailable", "MinerU service is unavailable")
