"""去重簇 + 均价快照 + 价格工单的重算（把纯算法落到库里，并维护结论血缘）。

一次 recompute 做四件事：
  1. 用 fingerprint_service 在「未下架挂牌」上切簇。簇身份 = 成员集合哈希：
     成员不变的簇延续原行；成员变化的旧簇置 superseded 留底，新簇另起一代，
     拆簇 / 并簇 / 成员变更 / 解散写入 cluster_events（含变化前后成员与价格）。
  2. 按 小区×户型 写 block_stats（去重前 naive_mean / 去重后 dedup_mean / 中位 / MAD）。
  3. 在去重后的主房源上跑稳健 z，为每个命中工单计算 basis_hash（规则版本 +
     簇成员及其价格 + 中位/MAD/样本）。依据不变的工单保留人工复核态；依据变了的
     旧工单连人工结论一起失效（stale，留依据快照），另起一代 pending 工单重算。
  4. 不再被判定命中的工单同样失效留底（下架 delisted / 判定变化 judgment_changed）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CLUSTER_EVENT_LABELS,
    INVALIDATION_LABELS,
    FlagType,
    FlagValidState,
    InvalidationReason,
    ReviewStatus,
)
from src.models.block_stat import BlockStat
from src.models.cluster import ListingCluster
from src.models.cluster_event import ClusterEvent
from src.models.listing import Listing
from src.models.price_flag import PriceFlag
from src.models.source import Source
from src.services import audit_service
from src.services.fingerprint_service import build_clusters
from src.services.lineage_service import (
    basis_hash,
    build_basis_snapshot,
    invalidation_reason,
    member_hash,
    plan_cluster_events,
)
from src.services.price_service import block_stats, classify
from src.utils.similarity import similarity_breakdown
from src.utils.timeutil import iso_date, iso_utc, utcnow


def _listing_dicts(db: Session) -> list[dict]:
    rows = db.scalars(select(Listing).where(Listing.is_active.is_(True))).all()
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "community": row.community,
            "layout": row.layout,
            "area_sqm": int(row.area_sqm) if row.area_sqm else None,
            "floor": row.floor,
            "total_floors": row.total_floors,
            "title": row.title,
            "unit_price": row.unit_price,
            "list_date": iso_date(row.list_date),
            "completeness": row.completeness,
        }
        for row in rows
    ]


def _member_payloads(member_ids: list[int], primary_id: int | None, listings: dict, sources: dict) -> list[dict]:
    """簇成员快照：结论/簇依据了哪些平台、哪些价格。"""
    payloads = []
    for mid in sorted(member_ids):
        listing = listings.get(mid)
        if listing is None:
            continue
        source = sources.get(listing.source_id)
        payloads.append(
            {
                "listing_id": mid,
                "source_name": source.name if source else None,
                "platform": source.platform if source else None,
                "source_listing_no": listing.source_listing_no,
                "unit_price": listing.unit_price,
                "is_primary": mid == primary_id,
            }
        )
    return payloads


def _cluster_snapshot(cluster_id: int | None, primary_id: int | None, member_ids: list[int], listings: dict, sources: dict) -> dict:
    return {
        "cluster_id": cluster_id,
        "primary_listing_id": primary_id,
        "member_ids": sorted(member_ids),
        "member_count": len(member_ids),
        "members": _member_payloads(member_ids, primary_id, listings, sources),
    }


def _fill_cluster(row: ListingCluster, cluster: dict, by_id: dict) -> None:
    row.member_count = cluster["member_count"]
    row.platform_count = cluster["platform_count"]
    row.avg_unit_price = cluster["avg_unit_price"]
    row.min_similarity = cluster["min_similarity"]
    row.max_similarity = cluster["max_similarity"]
    member_prices = [by_id[mid]["unit_price"] for mid in cluster["member_ids"] if by_id[mid]["unit_price"]]
    row.spread_pct = (
        round((max(member_prices) - min(member_prices)) / (sum(member_prices) / len(member_prices)) * 100, 2)
        if len(member_prices) > 1
        else None
    )


def _invalidate_flag(
    db: Session,
    flag: PriceFlag,
    reason: str,
    now,
    *,
    actor_email: str,
    actor_role: str,
    trigger: str,
    new_flag_id: int | None,
    new_flag_type: str | None,
) -> None:
    """旧结论失效：留依据快照与失效原因，写审计，绝不把人工结论搬到新工单上。"""
    flag.valid_state = FlagValidState.STALE.value
    flag.invalidated_at = now
    flag.invalidation_reason = reason
    flag.superseded_by = new_flag_id
    audit_service.record(
        db,
        actor_email=actor_email,
        actor_role=actor_role,
        action="flag.invalidate",
        entity="price_flag",
        entity_id=flag.id,
        detail={
            "listing_id": flag.listing_id,
            "reason": reason,
            "reason_label": INVALIDATION_LABELS.get(reason, reason),
            "old": {
                "flag_type": flag.flag_type,
                "review_status": flag.review_status,
                "reviewed_by": flag.reviewed_by,
                "generation": flag.generation,
            },
            "new_flag_id": new_flag_id,
            "new_flag_type": new_flag_type,
            "trigger": trigger,
        },
    )


def recompute(
    db: Session,
    *,
    actor_email: str = "system",
    actor_role: str = "admin",
    trigger: str = "recompute",
) -> dict:
    rows = _listing_dicts(db)
    result = build_clusters(rows)
    by_id = {row["id"]: row for row in rows}
    sources = {source.id: source for source in db.scalars(select(Source)).all()}
    all_listings = {row.id: row for row in db.scalars(select(Listing)).all()}
    now = utcnow()
    now_iso = iso_utc(now)

    # 0) 旧簇成员留底（下一步会清空归属；下架挂牌此刻仍挂在旧簇上，属于「变化前」）
    old_membership: dict[int, list[int]] = {}
    for listing_id, cluster_id in db.execute(
        select(Listing.id, Listing.cluster_id).where(Listing.cluster_id.isnot(None))
    ):
        old_membership.setdefault(cluster_id, []).append(listing_id)

    # 1) 簇代际：member_hash 相同的延续，不同的旧簇 superseded、新簇另起一代
    db.execute(Listing.__table__.update().values(cluster_id=None, is_primary=False))
    active_rows = db.scalars(select(ListingCluster).where(ListingCluster.status == "active")).all()
    by_mhash = {row.member_hash: row for row in active_rows if row.member_hash}
    # 老库升级：还没有 member_hash 的簇，只有成员集合与留底完全一致才按旧自然键
    # （主房源）认领；成员对不上就按真实变迁走事件，不掩盖升级当天的拆并
    legacy_by_primary = {row.primary_listing_id: row for row in active_rows if not row.member_hash}

    continued_hashes: set[str] = set()
    continued_rows: list[tuple[ListingCluster, dict]] = []
    new_payloads: list[dict] = []
    for cluster in result["clusters"]:
        mhash = member_hash(cluster["member_ids"])
        cluster["member_hash"] = mhash
        row = by_mhash.get(mhash)
        if row is not None and mhash in continued_hashes:
            row = None
        if row is None:
            legacy = legacy_by_primary.get(cluster["primary_id"])
            if legacy is not None and set(old_membership.get(legacy.id, [])) == set(cluster["member_ids"]):
                legacy_by_primary.pop(cluster["primary_id"])
                legacy.member_hash = mhash
                row = legacy
        if row is not None:
            continued_hashes.add(mhash)
            continued_rows.append((row, cluster))
        else:
            new_payloads.append(cluster)

    stale_rows = [row for row in active_rows if row.member_hash not in continued_hashes]
    for row in stale_rows:
        row.status = "superseded"
        row.superseded_at = now

    stale_member_sets = [(row, set(old_membership.get(row.id, []))) for row in stale_rows]
    created_rows: list[tuple[ListingCluster, dict]] = []
    for cluster in new_payloads:
        members = set(cluster["member_ids"])
        generations = [row.generation for row, old_set in stale_member_sets if old_set & members]
        row = ListingCluster(
            community=cluster["community"],
            layout=cluster["layout"],
            block_key=f"{cluster['community']}::{cluster['layout']}",
            primary_listing_id=cluster["primary_id"],
            member_hash=cluster["member_hash"],
            status="active",
            generation=(max(generations) + 1) if generations else 1,
        )
        db.add(row)
        db.flush()
        created_rows.append((row, cluster))

    for row, cluster in continued_rows + created_rows:
        row.primary_listing_id = cluster["primary_id"]
        _fill_cluster(row, cluster, by_id)
        for member_id in cluster["member_ids"]:
            db.execute(
                Listing.__table__.update()
                .where(Listing.id == member_id)
                .values(cluster_id=row.id, is_primary=member_id == cluster["primary_id"])
            )
    db.flush()

    # 2) 簇变迁事件：拆簇 / 并簇 / 成员变更 / 解散（变化前后的成员与价格都留底）
    event_counts = {"split": 0, "merge": 0, "reform": 0, "dissolved": 0}
    if stale_rows or created_rows:
        old_for_events = [
            {
                "cluster_id": row.id,
                "primary_id": row.primary_listing_id,
                "member_ids": old_membership.get(row.id, []),
                "block_key": row.block_key,
            }
            for row in stale_rows
        ]
        new_for_events = [
            {
                "cluster_id": row.id,
                "primary_id": cluster["primary_id"],
                "member_ids": cluster["member_ids"],
                "block_key": f"{cluster['community']}::{cluster['layout']}",
            }
            for row, cluster in created_rows
        ]
        for event in plan_cluster_events(old_for_events, new_for_events):
            before = [
                _cluster_snapshot(item["cluster_id"], item["primary_id"], item["member_ids"], all_listings, sources)
                for item in event["before"]
            ]
            after = [
                _cluster_snapshot(item["cluster_id"], item["primary_id"], item["member_ids"], all_listings, sources)
                for item in event["after"]
            ]
            listing_ids = sorted({mid for snap in before + after for mid in snap["member_ids"]})
            block_key = (event["before"] or event["after"])[0].get("block_key", "")
            event_row = ClusterEvent(
                event_kind=event["kind"],
                trigger_kind=trigger,
                actor_email=actor_email,
                block_key=block_key,
                before_clusters=before,
                after_clusters=after,
                listing_ids=listing_ids,
            )
            db.add(event_row)
            db.flush()
            audit_service.record(
                db,
                actor_email=actor_email,
                actor_role=actor_role,
                action=f"cluster.{event['kind']}",
                entity="cluster_event",
                entity_id=event_row.id,
                detail={
                    "kind_label": CLUSTER_EVENT_LABELS.get(event["kind"], event["kind"]),
                    "before": [{"cluster_id": s["cluster_id"], "member_ids": s["member_ids"]} for s in before],
                    "after": [{"cluster_id": s["cluster_id"], "member_ids": s["member_ids"]} for s in after],
                    "trigger": trigger,
                },
            )
            event_counts[event["kind"]] += 1

    # 3) 均价快照（两个口径，只算未下架挂牌）
    clusters = db.scalars(select(ListingCluster).where(ListingCluster.status == "active")).all()
    listings = db.scalars(select(Listing).where(Listing.is_active.is_(True))).all()
    listing_by_id = {row.id: row for row in listings}
    block_stats_map: dict[str, dict] = {}
    naive: dict[str, list[float]] = {}
    dedup: dict[str, list[float]] = {}
    unknown: dict[str, int] = {}
    for row in listings:
        key = f"{row.community}::{row.layout}"
        if row.area_sqm and row.unit_price:
            naive.setdefault(key, []).append(float(row.unit_price))
        else:
            unknown[key] = unknown.get(key, 0) + 1
    for cluster in clusters:
        primary = listing_by_id.get(cluster.primary_listing_id)
        if primary is None:
            continue
        key = f"{cluster.community}::{cluster.layout}"
        if primary.area_sqm and primary.unit_price:
            dedup.setdefault(key, []).append(float(primary.unit_price))

    existing_stats = {stat.block_key: stat for stat in db.scalars(select(BlockStat)).all()}
    for key in sorted(set(naive) | set(dedup)):
        community, layout = key.split("::", 1)
        stats = block_stats(dedup.get(key, []))
        naive_values = naive.get(key, [])
        stat_row = existing_stats.get(key)
        if stat_row is None:
            stat_row = BlockStat(block_key=key, community=community, layout=layout)
            db.add(stat_row)
        stat_row.all_count = len(naive_values)
        stat_row.primary_count = stats["sample_size"]
        stat_row.unknown_count = unknown.get(key, 0)
        stat_row.naive_mean = round(sum(naive_values) / len(naive_values), 2) if naive_values else None
        stat_row.dedup_mean = stats["mean_price"]
        stat_row.median_price = stats["median_price"]
        stat_row.mad = stats["mad"]
        stat_row.min_price = stats["min_price"]
        stat_row.max_price = stats["max_price"]
        stat_row.updated_at = utcnow()
        block_stats_map[key] = stats
    db.flush()

    # 4) 价格工单：依据不变保留人工结论；依据变了旧结论失效、另起一代重算
    members_by_primary = {cluster["primary_id"]: cluster["member_ids"] for cluster in result["clusters"]}
    existing_flags = db.scalars(select(PriceFlag)).all()
    current_flags = {flag.listing_id: flag for flag in existing_flags if flag.valid_state == FlagValidState.CURRENT.value}
    desired: dict[int, dict] = {}
    counts = {"high": 0, "low": 0, "uncertain": 0}
    for cluster in clusters:
        key = f"{cluster.community}::{cluster.layout}"
        stats = block_stats_map.get(key)
        if not stats or not stats["sample_size"]:
            continue
        primary = listing_by_id.get(cluster.primary_listing_id)
        if primary is None:
            continue
        verdict = classify(
            primary.unit_price,
            median_price=stats["median_price"],
            spread=stats["mad"],
            sample_size=stats["sample_size"],
            completeness=primary.completeness,
        )
        if not verdict["flag_type"]:
            continue
        member_ids = members_by_primary.get(primary.id, [primary.id])
        member_payloads = _member_payloads(member_ids, primary.id, all_listings, sources)
        snapshot = build_basis_snapshot(
            listing_id=primary.id,
            unit_price=primary.unit_price,
            members=member_payloads,
            median_price=stats["median_price"],
            mad=stats["mad"],
            sample_size=stats["sample_size"],
            computed_at=now_iso,
        )
        desired[primary.id] = {
            "verdict": verdict,
            "cluster_id": cluster.id,
            "basis_hash": basis_hash(
                listing_id=primary.id,
                unit_price=primary.unit_price,
                members=[(m["listing_id"], m["unit_price"]) for m in member_payloads],
                median_price=stats["median_price"],
                mad=stats["mad"],
                sample_size=stats["sample_size"],
            ),
            "snapshot": snapshot,
        }
        counts[verdict["flag_type"]] = counts.get(verdict["flag_type"], 0) + 1

    invalidated = 0
    created = 0
    kept = 0
    for listing_id, item in desired.items():
        verdict = item["verdict"]
        current = current_flags.get(listing_id)
        if (
            current is not None
            and current.basis_hash == item["basis_hash"]
            and current.flag_type == verdict["flag_type"]
        ):
            # 与当前成员及当前判定一致：人工结论继续有效，仅刷新派生字段
            current.cluster_id = item["cluster_id"]
            current.confidence = float(verdict["confidence"] or 0)
            kept += 1
            continue
        if current is not None:
            reason = invalidation_reason(current.basis_snapshot, item["snapshot"])
            invalidated += 1
        else:
            reason = None
        primary = listing_by_id[listing_id]
        flag = PriceFlag(
            listing_id=listing_id,
            cluster_id=item["cluster_id"],
            flag_type=verdict["flag_type"],
            community=primary.community,
            layout=primary.layout,
            unit_price=int(primary.unit_price or 0),
            median_price=float(item["snapshot"]["median_price"] or 0),
            deviation_pct=float(verdict["deviation_pct"] or 0),
            z_score=verdict["z_score"],
            confidence=float(verdict["confidence"] or 0),
            sample_size=int(item["snapshot"]["sample_size"]),
            review_status=ReviewStatus.PENDING.value,
            detected_at=now,
            basis_hash=item["basis_hash"],
            basis_snapshot=item["snapshot"],
            valid_state=FlagValidState.CURRENT.value,
            generation=(current.generation + 1) if current is not None else 1,
        )
        db.add(flag)
        db.flush()
        if current is not None:
            _invalidate_flag(
                db, current, reason, now,
                actor_email=actor_email, actor_role=actor_role, trigger=trigger,
                new_flag_id=flag.id, new_flag_type=verdict["flag_type"],
            )
        created += 1

    for listing_id, current in current_flags.items():
        if listing_id in desired:
            continue
        listing = all_listings.get(listing_id)
        reason = (
            InvalidationReason.DELISTED.value
            if listing is not None and not listing.is_active
            else InvalidationReason.JUDGMENT.value
        )
        _invalidate_flag(
            db, current, reason, now,
            actor_email=actor_email, actor_role=actor_role, trigger=trigger,
            new_flag_id=None, new_flag_type=None,
        )
        invalidated += 1
    db.flush()
    # 簇归属是用批量 UPDATE 写的，让会话里的对象失效，避免同一请求读到旧 cluster_id
    db.expire_all()

    return {
        "cluster_count": len(result["clusters"]),
        "duplicate_count": result.get("duplicate_clusters", 0),
        "dedup_count": result["dedup_count"],
        "member_total": sum(cluster["member_count"] for cluster in result["clusters"]),
        "edge_count": result["edge_count"],
        "pair_count": result["pair_count"],
        "flag_counts": counts,
        "flag_total": sum(counts.values()),
        "flag_kept": kept,
        "flag_created": created,
        "flag_invalidated": invalidated,
        "cluster_events": event_counts,
        "events_total": sum(event_counts.values()),
        "blocks": len(block_stats_map),
    }


def similarity_for(db: Session, cluster_id: int, member_id: int) -> float | None:
    """簇内成员与主房源的加权相似度（详情页并排对比用）。"""
    cluster = db.get(ListingCluster, cluster_id)
    if cluster is None:
        return None
    primary = db.get(Listing, cluster.primary_listing_id)
    member = db.get(Listing, member_id)
    if primary is None or member is None or primary.id == member.id:
        return None
    breakdown = similarity_breakdown(
        {
            "area_sqm": int(primary.area_sqm) if primary.area_sqm else None,
            "floor": primary.floor,
            "title": primary.title,
            "unit_price": primary.unit_price,
        },
        {
            "area_sqm": int(member.area_sqm) if member.area_sqm else None,
            "floor": member.floor,
            "title": member.title,
            "unit_price": member.unit_price,
        },
    )
    return breakdown["score"]


def flag_type_counts(db: Session) -> dict:
    rows = db.execute(
        select(PriceFlag.flag_type, func.count())
        .where(PriceFlag.valid_state == FlagValidState.CURRENT.value)
        .group_by(PriceFlag.flag_type)
    ).all()
    counts = {FlagType.HIGH.value: 0, FlagType.LOW.value: 0, FlagType.UNCERTAIN.value: 0}
    for flag_type, total in rows:
        counts[flag_type] = int(total)
    return counts


def list_events(db: Session, limit: int = 20) -> dict:
    """簇变迁时间线：拆簇 / 并簇 / 成员变更 / 解散，含变化前后的成员与价格。"""
    rows = db.scalars(select(ClusterEvent).order_by(ClusterEvent.id.desc()).limit(limit)).all()
    return {
        "items": [
            {
                "id": row.id,
                "event_kind": row.event_kind,
                "kind_label": CLUSTER_EVENT_LABELS.get(row.event_kind, row.event_kind),
                "trigger_kind": row.trigger_kind,
                "actor_email": row.actor_email,
                "block_key": row.block_key,
                "before_clusters": row.before_clusters or [],
                "after_clusters": row.after_clusters or [],
                "listing_ids": row.listing_ids or [],
                "created_at": iso_utc(row.created_at),
            }
            for row in rows
        ],
        "total": db.scalar(select(func.count()).select_from(ClusterEvent)) or 0,
    }
