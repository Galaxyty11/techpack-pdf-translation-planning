"""Errors that retain only safe operational context."""

from collections.abc import Mapping
from typing import Any


_SENSITIVE = ("key", "token", "secret", "credential")
_OPERATIONAL_DETAIL_KEYS = frozenset(
    {
        "item_id",
        "job_id",
        "source_id",
        "glossary_id",
        "source_path",
        "glossary_path",
        "job_path",
        "page_index",
        "page_number",
        "status",
        "error_code",
    }
)


class TechpackError(Exception):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        safe_details = {
            key: value
            for key, value in self.details.items()
            if not self._is_sensitive(key) and self._is_operational(key)
        }
        return {"code": self.code, "message": self.message, "details": safe_details}

    @staticmethod
    def _is_sensitive(key: object) -> bool:
        normalized = str(key).casefold()
        return any(marker in normalized for marker in _SENSITIVE)

    @staticmethod
    def _is_operational(key: object) -> bool:
        return str(key).casefold() in _OPERATIONAL_DETAIL_KEYS
