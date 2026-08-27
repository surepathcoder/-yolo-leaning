"""Shared schema building blocks."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APISchema(BaseModel):
    """Base for every request/response model.

    ``from_attributes`` lets responses be built straight from ORM objects;
    ``extra="forbid"`` on inputs is set per-schema where it matters.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    """The envelope every failed request returns."""

    error: ErrorDetail


class Page(BaseModel, Generic[T]):
    """A page of results.

    Offset pagination is used deliberately: the dashboard needs jump-to-page
    and total counts, and tenant-scoped result sets are small enough that
    cursor pagination would trade usability for a benefit that does not
    materialise here.
    """

    items: list[T]
    total: int = Field(description="Total rows matching the filter")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


PageLimit = Annotated[int, Field(ge=1, le=200, description="Rows per page")]
PageOffset = Annotated[int, Field(ge=0, description="Rows to skip")]


class PaginationParams(BaseModel):
    limit: PageLimit = 50
    offset: PageOffset = 0


class TimeRange(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


class MessageResponse(BaseModel):
    """A simple acknowledgement for endpoints with nothing else to return."""

    message: str


class CountResponse(BaseModel):
    count: int
