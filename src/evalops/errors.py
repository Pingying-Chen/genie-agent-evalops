"""Error types and the FastAPI handlers that turn them into `ErrorResponse` JSON.

Services raise these exceptions; routers never build error responses by hand. Every
error — ours, request validation, or an unexpected crash — leaves the API in the same
`{"error": {code, message, details, request_id}}` shape.
"""

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from evalops.schemas.common import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class. Subclasses set the HTTP status and a stable `code`."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


class Unauthorized(AppError):
    """No on-behalf-of user token on the request."""

    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    """The viewer may not access this space."""

    status_code = 403
    code = "space_not_allowed"


class NotFound(AppError):
    """Also used for resources in a disallowed space, so existence never leaks."""

    status_code = 404
    code = "not_found"


class Conflict(AppError):
    """The request clashes with existing state (e.g. a category name already taken)."""

    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    """The body is well-formed but refers to something that does not exist (a rule, a category)."""

    status_code = 422
    code = "invalid_request"


class UpstreamError(AppError):
    """A Databricks API (Genie, SQL, Jobs, Lakebase) failed after retries."""

    status_code = 502
    code = "upstream_error"

    def __init__(
        self, service: str, message: str, *, status: int | None = None, details: dict[str, Any] | None = None
    ):
        super().__init__(
            f"{service}: {message}", details={"service": service, "status": status, **(details or {})}
        )
        self.service = service
        self.status = status


class RateLimited(UpstreamError):
    """Upstream kept returning 429 / TEMPORARILY_UNAVAILABLE after backoff."""

    status_code = 503
    code = "rate_limited"


class ConfigError(AppError):
    """Missing or invalid configuration (e.g. no Lakebase for the collector)."""

    status_code = 500
    code = "config_error"


class NotImplementedYet(AppError):
    """Skeleton placeholder: the endpoint exists in the contract but is not built yet."""

    status_code = 501
    code = "not_implemented"


def _response(status: int, body: ErrorBody) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(error=body).model_dump())


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so every error uses the `ErrorResponse` envelope."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        if exc.status_code >= 500:
            logger.error("%s %s -> %s: %s", request.method, request.url.path, exc.code, exc.message)
        return _response(
            exc.status_code,
            ErrorBody(code=exc.code, message=exc.message, details=exc.details, request_id=request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _response(
            422,
            ErrorBody(
                code="invalid_request",
                message="Request failed validation.",
                # A field validator's ValueError sits in `ctx`; as is, it is not JSON.
                details={"errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})},
                request_id=getattr(request.state, "request_id", None),
            ),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        logger.exception("unhandled error, request_id=%s", request_id)
        return _response(
            500, ErrorBody(code="internal_error", message="Unexpected server error.", request_id=request_id)
        )
