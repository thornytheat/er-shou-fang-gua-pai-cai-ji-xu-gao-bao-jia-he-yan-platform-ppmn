from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ListingCluster(Base):
    """实体 3：去重簇（主房源 + 成员数 + 跨平台数）。

    簇身份 = member_hash（成员 listing_id 集合的哈希）。补采/改价/下架导致
    成员集合变化时，旧一代置为 superseded（保留可追查），新一代另起一行，
    generation 递增；primary_listing_id 不再唯一（旧代与新代可能同一主房源）。
    """

    __tablename__ = "listing_clusters"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    community: Mapped[str] = mapped_column(String(40), index=True)
    layout: Mapped[str] = mapped_column(String(12), index=True)
    block_key: Mapped[str] = mapped_column(String(60), index=True)
    primary_listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    member_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=1)
    platform_count: Mapped[int] = mapped_column(Integer, default=1)
    avg_unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
