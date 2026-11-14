"""One shape for every error the API raises itself: `{"detail": {"code", "message", ...}}`."""

from typing import Any

from fastapi import HTTPException


def api_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, **extra})
