"""核验队列：列表 / 复核 / 统计 / 失效结论追查。

队列只展示 valid_state=current 的工单（与当前簇成员、当前判定一致）；
失效的旧结论留在 price_flags 里（valid_state=stale），走 list_invalidated 追查。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    FLAG_TYPE_LABELS,
    FLAG_TYPE_ORDER,
    INVALIDATION_LABELS,
    REVIEW_LABELS,
    FlagValidState,
    ReviewStatus,
)
from src.models.listing import Listing
from src.models.price_flag import PriceFlag
from src.models.source import Source
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc, utcnow


def counts(db: Session) -> dict:
    current = select(PriceFlag).where(PriceFlag.valid_state == FlagValidState.CURRENT.value).subquery()
    by_type = dict(db.execute(select(current.c.flag_type, func.count()).group_by(current.c.flag_type)).all())
    by_status = dict(
        db.execute(select(current.c.review_status, func.count()).group_by(current.c.review_status)).all()
    )
    total = db.scalar(select(func.count()).select_from(current)) or 0
    invalidated = (
        db.scalar(
            select(func.count())
            .select_from(PriceFlag)
            .where(PriceFlag.valid_state == FlagValidState.STALE.value)
        )
        or 0
    )
    return {
        "total": int(total),
        "by_type": {key: int(by_type.get(key, 0)) for key in FLAG_TYPE_ORDER},
        "by_status": {
            ReviewStatus.PENDING.value: int(by_status.get(ReviewStatus.PENDING.value, 0)),
            ReviewStatus.CONFIRMED.value: int(by_status.get(ReviewStatus.CONFIRMED.value, 0)),
            ReviewStatus.DISMISSED.value: int(by_status.get(ReviewStatus.DISMISSED.value, 0)),
        },
        "invalidated": int(invalidated),
    }


def _basis_of(flag: PriceFlag) -> tuple[list[dict], str | None]:
    snapshot = flag.basis_snapshot or {}
    return snapshot.get("members", []), snapshot.get("rule_version")


def _serialize(flag: PriceFlag, listing: Listing | None, sources: dict) -> dict:
    basis_members, rule_version = _basis_of(flag)
    return {
        "id": flag.id,
        "listing_id": flag.listing_id,
        "cluster_id": flag.cluster_id,
        "flag_type": flag.flag_type,
        "flag_label": FLAG_TYPE_LABELS.get(flag.flag_type, flag.flag_type),
        "community": flag.community,
        "layout": flag.layout,
        "unit_price": flag.unit_price,
        "median_price": flag.median_price,
        "deviation_pct": flag.deviation_pct,
        "z_score": flag.z_score,
        "confidence": flag.confidence,
        "sample_size": flag.sample_size,
        "review_status": flag.review_status,
        "review_label": REVIEW_LABELS.get(flag.review_status, flag.review_status),
        "review_note": flag.review_note,
        "reviewed_by": flag.reviewed_by,
        "reviewed_at": iso_utc(flag.reviewed_at),
        "detected_at": iso_utc(flag.detected_at),
        "valid_state": flag.valid_state,
        "generation": flag.generation,
        "rule_version": rule_version,
        "basis_members": basis_members,
        "invalidation_reason": flag.invalidation_reason,
        "invalidated_at": iso_utc(flag.invalidated_at),
        "superseded_by": flag.superseded_by,
        "title": listing.title if listing else None,
        "area_sqm": listing.area_sqm if listing else None,
        "list_date": iso_date(listing.list_date) if listing else None,
        "source_name": sources[listing.source_id].name if listing and listing.source_id in sources else None,
    }


def list_flags(
    db: Session,
    *,
    flag_type: str | None = None,
    review_status: str | None = None,
    limit: int = 60,
) -> dict:
    query = (
        select(PriceFlag)
        .join(Listing, Listing.id == PriceFlag.listing_id)
        .where(PriceFlag.valid_state == FlagValidState.CURRENT.value)
    )
    if flag_type:
        query = query.where(PriceFlag.flag_type == flag_type)
    if review_status:
        query = query.where(PriceFlag.review_status == review_status)
    query = query.order_by(PriceFlag.flag_type.asc(), PriceFlag.confidence.desc())
    rows = db.scalars(query.limit(limit)).all()
    sources = {source.id: source for source in db.scalars(select(Source)).all()}
    items = []
    for flag in rows:
        listing = db.get(Listing, flag.listing_id)
        items.append(_serialize(flag, listing, sources))
    return {"items": items, "total": len(items), "counts": counts(db)}


def list_invalidated(db: Session, limit: int = 20) -> dict:
    """失效结论时间线：旧结论因何失效、失效前是谁确认的、依据了哪些平台与价格。"""
    rows = db.scalars(
        select(PriceFlag)
        .where(PriceFlag.valid_state == FlagValidState.STALE.value)
        .order_by(PriceFlag.invalidated_at.desc(), PriceFlag.id.desc())
        .limit(limit)
    ).all()
    sources = {source.id: source for source in db.scalars(select(Source)).all()}
    items = []
    for flag in rows:
        listing = db.get(Listing, flag.listing_id)
        payload = _serialize(flag, listing, sources)
        payload["invalidation_label"] = INVALIDATION_LABELS.get(
            flag.invalidation_reason or "", flag.invalidation_reason or "未知原因"
        )
        items.append(payload)
    return {"items": items, "total": len(items)}


def review(db: Session, flag_id: int, review_status: str, note: str | None, actor_email: str) -> PriceFlag:
    flag = db.get(PriceFlag, flag_id)
    if flag is None:
        raise AppError("NOT_FOUND", 404, detail=f"flag_id={flag_id}")
    if flag.valid_state != FlagValidState.CURRENT.value:
        raise AppError(
            "CONFLICT",
            409,
            detail=f"工单 #{flag_id} 的结论已失效（{INVALIDATION_LABELS.get(flag.invalidation_reason or '', '未知原因')}），请复核该房源最新一代工单",
        )
    if review_status not in {item.value for item in ReviewStatus}:
        raise AppError("VALIDATION", 422, detail="review_status 不在枚举内")
    flag.review_status = review_status
    flag.review_note = note
    flag.reviewed_by = actor_email
    flag.reviewed_at = utcnow() if review_status != ReviewStatus.PENDING.value else None
    db.flush()
    return flag
