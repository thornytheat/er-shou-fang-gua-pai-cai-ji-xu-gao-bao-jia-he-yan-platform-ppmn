from __future__ import annotations

from pydantic import BaseModel, Field


class BasisMember(BaseModel):
    listing_id: int
    source_name: str | None = None
    platform: str | None = None
    source_listing_no: str | None = None
    unit_price: int | None = None
    is_primary: bool = False


class PriceFlagRow(BaseModel):
    id: int
    listing_id: int
    cluster_id: int | None = None
    flag_type: str
    flag_label: str
    community: str
    layout: str
    unit_price: int
    median_price: float
    deviation_pct: float
    z_score: float | None = None
    confidence: float
    sample_size: int
    review_status: str
    review_label: str
    review_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    detected_at: str | None = None
    valid_state: str = "current"
    generation: int = 1
    rule_version: str | None = None
    basis_members: list[BasisMember] = []
    invalidation_reason: str | None = None
    invalidated_at: str | None = None
    superseded_by: int | None = None
    title: str | None = None
    area_sqm: float | None = None
    list_date: str | None = None
    source_name: str | None = None


class PriceFlagPage(BaseModel):
    items: list[PriceFlagRow]
    total: int
    counts: dict = {}


class InvalidatedFlagRow(PriceFlagRow):
    invalidation_label: str = ""


class InvalidatedFlagPage(BaseModel):
    items: list[InvalidatedFlagRow]
    total: int


class ReviewIn(BaseModel):
    review_status: str = Field(pattern="^(pending|confirmed|dismissed)$")
    note: str | None = Field(default=None, max_length=300)
