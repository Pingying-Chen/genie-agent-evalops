"""Shared API shapes: the error envelope and cursor pagination.

Every error the API returns has the same JSON shape (`ErrorResponse`), so clients never
have to guess. Every list endpoint returns a `Page[T]`.
"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorBody(BaseModel):
    """The inside of an error response."""

    code: str = Field(
        description="Stable machine-readable code, e.g. `space_not_allowed`.", examples=["not_found"]
    )
    message: str = Field(description="Human-readable explanation.")
    details: dict[str, Any] | None = Field(default=None, description="Optional structured context.")
    request_id: str | None = Field(default=None, description="Correlates with server logs.")


class ErrorResponse(BaseModel):
    """Envelope for every non-2xx response."""

    error: ErrorBody


class Page(BaseModel, Generic[T]):
    """One page of a cursor-paginated list."""

    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Pass back as `cursor` to get the next page; null on the last page."
    )
