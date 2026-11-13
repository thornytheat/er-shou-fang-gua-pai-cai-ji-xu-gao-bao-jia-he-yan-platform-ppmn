from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ClusterEvent(Base):
    """实体 9：簇变迁事件（拆簇 / 并簇 / 成员变更 / 解散）。

    before/after 各是一组簇快照（含成员的平台与单价），变化前后都留底，
    核验与周复盘可据此追查「这套房原来跟谁一簇、现在跟谁一簇」。
    """

    __tablename__ = "cluster_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_kind: Mapped[str] = mapped_column(String(16), index=True)
    trigger_kind: Mapped[str] = mapped_column(String(24), default="recompute")
    actor_email: Mapped[str] = mapped_column(String(160), default="system")
    block_key: Mapped[str] = mapped_column(String(60), index=True, default="")
    before_clusters: Mapped[list] = mapped_column(JSON, default=list)
    after_clusters: Mapped[list] = mapped_column(JSON, default=list)
    listing_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
