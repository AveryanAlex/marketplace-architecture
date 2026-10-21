from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from marketplace_api.models.error_response import ErrorResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException


class APIError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict | None = None,
        headers: dict | None = None,
    ):
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details, headers
        super().__init__(message)


def error_response(
    status: int, code: str, message: str, details: dict | None = None, headers: dict | None = None
) -> JSONResponse:
    body = ErrorResponse(error_code=code, message=message, details=details)
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not include `input`: it may contain a password or token.
    fields = [
        {
            "field": ".".join(map(str, e["loc"])),
            "message": e["msg"],
            "type": e["type"],
        }
        for e in exc.errors()
    ]
    return error_response(400, "VALIDATION_ERROR", "Request validation failed", {"fields": fields})


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    codes = {
        404: "ROUTE_NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        401: "TOKEN_INVALID",
        403: "ACCESS_DENIED",
    }
    return error_response(
        exc.status_code,
        codes.get(exc.status_code, "VALIDATION_ERROR"),
        str(exc.detail),
        headers=exc.headers,
    )


async def integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    return error_response(400, "VALIDATION_ERROR", "Database constraint violated")


def invalid_field(field: str, message: str) -> Any:
    raise APIError(
        400,
        "VALIDATION_ERROR",
        "Request validation failed",
        {"fields": [{"field": field, "message": message}]},
    )
