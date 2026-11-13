from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class PriceFlag(Base):
    """实体 4：价格异常工单（虚高 / 虚低 / 待确认）。

    一个 listing 同时只有一条 valid_state=current 的工单；簇成员、价格或
    判定规则变化后，旧工单连人工结论一起置为 stale（保留依据快照可追查），
    另起一代 pending 工单重新走人工复核——旧结论不串到新的房源身份上。
    """

    __tablename__ = "price_flags"
    __table_args__ = (
        Index("ix_flags_type_status", "flag_type", "review_status"),
        Index("ix_flags_listing_state", "listing_id", "valid_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flag_type: Mapped[str] = mapped_column(String(12), index=True)
    community: Mapped[str] = mapped_column(String(40))
    layout: Mapped[str] = mapped_column(String(12))
    unit_price: Mapped[int] = mapped_column(Integer)
    median_price: Mapped[float] = mapped_column(Float)
    deviation_pct: Mapped[float] = mapped_column(Float)
    z_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    review_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # ---- 结论血缘：依据指纹 / 依据快照 / 有效态 / 失效信息 / 代际 ----
    basis_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    basis_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    valid_state: Mapped[str] = mapped_column(String(16), default="current", index=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
