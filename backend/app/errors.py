"""统一业务错误：{"detail": "...", "code": "..."}。"""
from __future__ import annotations

from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


async def api_error_handler(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


def not_found(code: str, detail: str) -> ApiError:
    return ApiError(404, code, detail)


def conflict(code: str, detail: str) -> ApiError:
    return ApiError(409, code, detail)


def bad_request(code: str, detail: str) -> ApiError:
    return ApiError(400, code, detail)