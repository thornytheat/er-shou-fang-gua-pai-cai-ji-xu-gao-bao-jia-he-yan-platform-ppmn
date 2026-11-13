"""复核结论血缘：补采 / 改价 / 下架导致簇拆并后，旧结论失效重算（服务级，SQLite 内存库）。

被测链路：cluster_service.recompute（簇分代 + 事件）× flag_service（复核守卫 / 失效时间线）
× export_service（出口闸门）× ingest_service._mark_delisted（下架软标记）。

场景数据：澜庭·三居 8 条独立挂牌（面积两两不同，不会误聚簇）+ 一对重复挂牌
（79000 主房源 / 78000 成员，面积楼层标题一致）。去重后 9 套样本，中位 45500、
MAD 1000，79000 偏离 +73.6%、z≈22.6 → 稳定判 high。期望值全部手算。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.constants.enums import FlagValidState, ReviewStatus
from src.models import Base
from src.models.cluster import ListingCluster
from src.models.cluster_event import ClusterEvent
from src.models.listing import Listing
from src.models.price_flag import PriceFlag
from src.models.source import Source
from src.services import cluster_service, export_service, flag_service, lineage_service
from src.utils.errors import AppError

# 澜庭·三居：8 条独立挂牌（面积 91~105 两两不同，单价围绕中位）
SINGLETONS = [
    ("A1", 43000, 91, 3),
    ("A2", 44000, 93, 5),
    ("A3", 44500, 95, 7),
    ("A4", 45000, 97, 9),
    ("A5", 45500, 99, 11),
    ("A6", 46000, 101, 13),
    ("A7", 46500, 103, 15),
    ("A8", 47000, 105, 17),
]
# 同一套房在两个平台的重复挂牌：主房源 79000（挂牌早），成员 78000
OUTLIER = ("B1", 79000, 130, 10, "澜庭 三居 130㎡ 高楼层 急售", "2026-10-01")
TWIN = ("B2", 78000, 130, 10, "澜庭 三居 130㎡ 高楼层 急售 可谈", "2026-10-05")


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(
        Source(
            code="test",
            name="测试聚合站",
            platform="aggregator",
            base_url="fixture://test",
            frequency="daily",
            enabled=True,
        )
    )
    session.flush()
    yield session
    session.close()


def _add_listing(db: Session, no: str, price: int, area: int, floor: int, title: str, list_date: str) -> Listing:
    listing = Listing(
        source_id=1,
        source_listing_no=no,
        community="澜庭",
        layout="三居",
        area_sqm=area,
        floor=floor,
        total_floors=18,
        unit_price=price,
        total_price_wan=round(price * area / 10000, 2),
        list_date=date.fromisoformat(list_date),
        title=title,
        completeness=1.0,
    )
    db.add(listing)
    db.flush()
    return listing


@pytest.fixture()
def seeded(db: Session) -> dict:
    for no, price, area, floor in SINGLETONS:
        _add_listing(db, no, price, area, floor, f"澜庭 三居 {area}㎡ 精装 {no} 套", "2026-10-10")
    outlier = _add_listing(db, *OUTLIER[:4], OUTLIER[4], OUTLIER[5])
    twin = _add_listing(db, *TWIN[:4], TWIN[4], TWIN[5])
    result = cluster_service.recompute(db, actor_email="tester@rentlens.io", trigger="seed")
    return {"outlier": outlier, "twin": twin, "result": result}


def _current_flag(db: Session, listing_id: int) -> PriceFlag | None:
    return db.scalar(
        select(PriceFlag).where(
            PriceFlag.listing_id == listing_id,
            PriceFlag.valid_state == FlagValidState.CURRENT.value,
        )
    )


def _stale_flags(db: Session, listing_id: int) -> list[PriceFlag]:
    return db.scalars(
        select(PriceFlag).where(
            PriceFlag.listing_id == listing_id,
            PriceFlag.valid_state == FlagValidState.STALE.value,
        )
    ).all()


def _events(db: Session, kind: str | None = None) -> list[ClusterEvent]:
    query = select(ClusterEvent)
    if kind:
        query = query.where(ClusterEvent.event_kind == kind)
    return db.scalars(query).all()


class TestBaseline:
    def test_first_recompute_flags_outlier(self, db: Session, seeded: dict):
        """9 套样本，中位 45500 / MAD 1000，79000 判 high，其余正常。"""
        assert seeded["result"]["flag_counts"] == {"high": 1, "low": 0, "uncertain": 0}
        flag = _current_flag(db, seeded["outlier"].id)
        assert flag is not None and flag.flag_type == "high"
        assert flag.median_price == 45500
        assert flag.sample_size == 9
        assert flag.generation == 1
        assert flag.valid_state == "current"
        # 依据快照：结论依据了哪些平台、哪些价格
        members = flag.basis_snapshot["members"]
        assert {m["listing_id"] for m in members} == {seeded["outlier"].id, seeded["twin"].id}
        assert {m["unit_price"] for m in members} == {79000, 78000}
        assert flag.basis_snapshot["rule_version"] == lineage_service.rule_fingerprint()

    def test_idempotent_recompute_keeps_review(self, db: Session, seeded: dict):
        """数据不变时重算：无事件、无失效，人工结论原样保留。"""
        flag = _current_flag(db, seeded["outlier"].id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "电话核实为试探价", "analyst@rentlens.io")
        again = cluster_service.recompute(db, actor_email="tester@rentlens.io", trigger="detect")
        assert again["flag_invalidated"] == 0
        assert again["events_total"] == 0
        kept = _current_flag(db, seeded["outlier"].id)
        assert kept.id == flag.id
        assert kept.review_status == ReviewStatus.CONFIRMED.value
        assert kept.reviewed_by == "analyst@rentlens.io"


class TestSplit:
    def test_split_invalidates_confirmed_conclusion(self, db: Session, seeded: dict):
        """成员面积改到不相似 → 拆簇：已确认结论失效（members_changed），另起一代 pending。"""
        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认虚高", "admin@rentlens.io")
        old_cluster_id = flag.cluster_id

        twin.area_sqm = 135  # 面积差 5㎡，相似度跌破阈值 → 拆成两个单房源簇
        db.flush()
        result = cluster_service.recompute(db, actor_email="tester@rentlens.io", trigger="ingest")

        # 1) 簇事件：1 条拆簇，变化前后的成员与价格都留底
        splits = _events(db, "split")
        assert len(splits) == 1 and result["events_total"] == 1
        event = splits[0]
        assert event.before_clusters[0]["cluster_id"] == old_cluster_id
        assert sorted(event.before_clusters[0]["member_ids"]) == sorted([outlier.id, twin.id])
        assert {m["unit_price"] for m in event.before_clusters[0]["members"]} == {79000, 78000}
        assert len(event.after_clusters) == 2
        assert event.trigger_kind == "ingest"

        # 2) 旧簇 superseded 留底，新簇另起一代
        old_cluster = db.get(ListingCluster, old_cluster_id)
        assert old_cluster.status == "superseded" and old_cluster.superseded_at is not None
        db.refresh(outlier)
        new_cluster = db.get(ListingCluster, outlier.cluster_id)
        assert new_cluster.status == "active" and new_cluster.generation == 2

        # 3) 旧结论失效留底（复核人、依据快照都在），新工单 pending 重新来
        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1
        assert stale[0].invalidation_reason == "members_changed"
        assert stale[0].review_status == ReviewStatus.CONFIRMED.value  # 历史结论留底
        assert stale[0].reviewed_by == "admin@rentlens.io"
        assert stale[0].superseded_by is not None
        new_flag = _current_flag(db, outlier.id)
        assert new_flag is not None and new_flag.id == stale[0].superseded_by
        assert new_flag.generation == 2
        assert new_flag.review_status == ReviewStatus.PENDING.value  # 旧结论不串到新工单
        assert new_flag.reviewed_by is None
        assert {m["listing_id"] for m in new_flag.basis_snapshot["members"]} == {outlier.id}

    def test_stale_flag_rejects_review(self, db: Session, seeded: dict):
        """失效工单禁止复核（409），防止结论写到过期身份上。"""
        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        twin.area_sqm = 135
        db.flush()
        cluster_service.recompute(db, trigger="ingest")
        with pytest.raises(AppError) as exc:
            flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "晚了", "admin@rentlens.io")
        assert exc.value.status_code == 409


class TestMerge:
    def test_merge_records_event_and_recomputes(self, db: Session, seeded: dict):
        """两条独立挂牌变得相似 → 并簇事件；去重口径变化让旧结论失效重算。"""
        outlier = seeded["outlier"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")

        first = db.scalar(select(Listing).where(Listing.source_listing_no == "A1"))
        second = db.scalar(select(Listing).where(Listing.source_listing_no == "A2"))
        first.area_sqm = second.area_sqm  # 93
        first.floor = second.floor
        first.title = second.title
        db.flush()
        cluster_service.recompute(db, trigger="ingest")

        merges = _events(db, "merge")
        assert len(merges) == 1
        assert sorted(len(c["member_ids"]) for c in merges[0].before_clusters) == [1, 1]
        assert len(merges[0].after_clusters) == 1
        assert sorted(merges[0].after_clusters[0]["member_ids"]) == sorted([first.id, second.id])

        # 并簇后样本从 9 变 8、中位移动 → 旧确认结论失效，新工单 pending
        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1 and stale[0].invalidation_reason == "judgment_changed"
        new_flag = _current_flag(db, outlier.id)
        assert new_flag.review_status == ReviewStatus.PENDING.value
        assert new_flag.sample_size == 8


class TestPriceFix:
    def test_price_fix_invalidates_with_price_reason(self, db: Session, seeded: dict):
        """被标记房源改价 → 结论失效（price_changed），新工单不继承旧结论。"""
        outlier = seeded["outlier"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认虚高", "admin@rentlens.io")

        outlier.unit_price = 76000
        db.flush()
        cluster_service.recompute(db, actor_email="analyst@rentlens.io", trigger="price_fix")

        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1
        assert stale[0].invalidation_reason == "price_changed"
        new_flag = _current_flag(db, outlier.id)
        assert new_flag.review_status == ReviewStatus.PENDING.value
        assert new_flag.unit_price == 76000
        assert new_flag.reviewed_by is None  # 旧确认不串到新工单

    def test_member_price_fix_also_invalidates(self, db: Session, seeded: dict):
        """簇成员改价：中位没变，但结论依据的簇内价格变了 → 同样失效重算。"""
        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")

        twin.unit_price = 77000
        db.flush()
        cluster_service.recompute(db, trigger="price_fix")

        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1 and stale[0].invalidation_reason == "price_changed"
        new_flag = _current_flag(db, outlier.id)
        assert {m["unit_price"] for m in new_flag.basis_snapshot["members"]} == {79000, 77000}


class TestRuleChange:
    def test_rule_version_change_invalidates_all(self, db: Session, seeded: dict, monkeypatch):
        """判定规则版本变化 → 全部当前结论 rule_changed 失效重算。"""
        outlier = seeded["outlier"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")

        monkeypatch.setattr(lineage_service, "rule_fingerprint", lambda: "r-test-rules-v2")
        cluster_service.recompute(db, trigger="recompute")

        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1 and stale[0].invalidation_reason == "rule_changed"
        new_flag = _current_flag(db, outlier.id)
        assert new_flag.basis_snapshot["rule_version"] == "r-test-rules-v2"
        assert new_flag.review_status == ReviewStatus.PENDING.value


class TestDelist:
    def test_member_delist_changes_cluster(self, db: Session, seeded: dict):
        """成员下架 → 簇成员变化（reform 事件），结论失效（members_changed）。"""
        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")

        twin.is_active = False
        db.flush()
        cluster_service.recompute(db, trigger="ingest")

        reforms = _events(db, "reform")
        assert len(reforms) == 1
        assert sorted(reforms[0].before_clusters[0]["member_ids"]) == sorted([outlier.id, twin.id])
        assert reforms[0].after_clusters[0]["member_ids"] == [outlier.id]
        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1 and stale[0].invalidation_reason == "members_changed"

    def test_flagged_listing_delist_kills_flag(self, db: Session, seeded: dict):
        """被标记房源自身下架 → 结论失效（delisted），不再另起工单，也不进行情。"""
        outlier = seeded["outlier"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")

        outlier.is_active = False
        db.flush()
        cluster_service.recompute(db, trigger="ingest")

        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1 and stale[0].invalidation_reason == "delisted"
        assert _current_flag(db, outlier.id) is None
        rows = export_service.rows_for_export(db)
        assert all(row[6] != "B1" for row in rows)  # 挂牌编号 B1 已下架，不在行情里


class TestExportGate:
    def test_export_only_takes_current_conclusions(self, db: Session, seeded: dict):
        """出口闸门：失效的已确认结论不进行情，只导出与当前判定一致的当前代工单。"""
        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")

        twin.area_sqm = 135  # 拆簇 → 旧确认失效
        db.flush()
        cluster_service.recompute(db, trigger="ingest")

        rows = export_service.rows_for_export(db)
        header = export_service.HEADERS
        review_col = header.index("复核状态")
        flag_col = header.index("价格标记")
        no_col = header.index("挂牌编号")
        outlier_rows = [row for row in rows if row[no_col] == "B1"]
        assert len(outlier_rows) == 1
        # 导出的是新一代工单（待复核），不是失效的「已确认」
        assert outlier_rows[0][review_col] == "待复核"
        assert outlier_rows[0][flag_col] == "虚高"
        assert all(row[review_col] != "已确认" for row in rows)

    def test_history_keeps_invalidated_conclusions(self, db: Session, seeded: dict):
        """失效结论可追查：原因、失效前复核人、当时的依据快照都在。"""
        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")
        twin.area_sqm = 135
        db.flush()
        cluster_service.recompute(db, trigger="ingest")

        history = flag_service.list_invalidated(db)
        assert history["total"] == 1
        item = history["items"][0]
        assert item["invalidation_reason"] == "members_changed"
        assert item["invalidation_label"] == "簇成员变化"
        assert item["review_status"] == ReviewStatus.CONFIRMED.value
        assert item["reviewed_by"] == "admin@rentlens.io"
        assert {m["unit_price"] for m in item["basis_members"]} == {79000, 78000}
        # 统计口径：队列只数当前代，失效单列
        counts = flag_service.counts(db)
        assert counts["invalidated"] == 1
        assert counts["by_status"][ReviewStatus.CONFIRMED.value] == 0


class TestLegacyAdoption:
    def test_legacy_cluster_adopted_when_members_match(self, db: Session, seeded: dict):
        """老库升级：member_hash 为空的簇，成员集合一致时按主房源认领，不产生伪事件。"""
        for cluster in db.scalars(select(ListingCluster)).all():
            cluster.member_hash = ""
        db.flush()
        result = cluster_service.recompute(db, trigger="recompute")
        assert result["events_total"] == 0
        assert result["flag_invalidated"] == 0
        clusters = db.scalars(select(ListingCluster).where(ListingCluster.status == "active")).all()
        assert all(cluster.member_hash for cluster in clusters)

    def test_legacy_cluster_with_real_change_still_events(self, db: Session, seeded: dict):
        """老库升级：成员集合对不上（如下架）时不认领，按真实变迁记事件。"""
        twin = seeded["twin"]
        for cluster in db.scalars(select(ListingCluster)).all():
            cluster.member_hash = ""
        twin.is_active = False
        db.flush()
        result = cluster_service.recompute(db, trigger="ingest")
        assert result["events_total"] >= 1
        kinds = {event.event_kind for event in _events(db)}
        assert "reform" in kinds or "split" in kinds


class TestDelistIngest:
    def test_mark_delisted_and_relist(self, db: Session, seeded: dict):
        """补采时源上消失 → 下架软标记；重新出现 → 自动复活。下架触发结论失效。"""
        from src.services import ingest_service

        outlier, twin = seeded["outlier"], seeded["twin"]
        flag = _current_flag(db, outlier.id)
        flag_service.review(db, flag.id, ReviewStatus.CONFIRMED.value, "已确认", "admin@rentlens.io")
        source = db.scalar(select(Source).where(Source.code == "test"))

        # 本轮抓取结果里没有 B2（twin）→ 下架
        fetched = [{"source_code": "test", "source_listing_no": no} for no, *_ in SINGLETONS]
        fetched.append({"source_code": "test", "source_listing_no": "B1"})
        delisted = ingest_service._mark_delisted(
            db, fetched, [source], operator_email="t@rentlens.io", operator_role="admin"
        )
        assert [listing.source_listing_no for listing in delisted] == ["B2"]
        db.refresh(twin)
        assert twin.is_active is False and twin.removed_at is not None

        result = cluster_service.recompute(db, trigger="ingest")
        assert result["events_total"] >= 1
        stale = _stale_flags(db, outlier.id)
        assert len(stale) == 1 and stale[0].invalidation_reason == "members_changed"

        # 下一轮 B2 重新出现 → 复活并重新进簇
        ingest_service._upsert_listings(
            db,
            [
                {
                    "source_code": "test",
                    "source_listing_no": "B2",
                    "community": "澜庭",
                    "layout": "三居",
                    "area_sqm": 130,
                    "floor": 10,
                    "total_floors": 18,
                    "unit_price": 78000,
                    "total_price_wan": 101.4,
                    "list_date": "2026-10-05",
                    "title": "澜庭 三居 130㎡ 高楼层 急售 可谈",
                    "completeness": 1.0,
                }
            ],
            {"test": source},
        )
        db.refresh(twin)
        assert twin.is_active is True and twin.removed_at is None
        cluster_service.recompute(db, trigger="ingest")
        db.refresh(outlier)
        cluster = db.get(ListingCluster, outlier.cluster_id)
        assert cluster.member_count == 2


class TestEventClassification:
    """plan_cluster_events 纯函数：拆 / 并 / 改 / 散的分类边界。"""

    def test_split_merge_reform_dissolved(self):
        old = [
            {"member_ids": [1, 2, 3]},   # 拆成两簇
            {"member_ids": [4]},          # 与 5 并簇
            {"member_ids": [5]},
            {"member_ids": [6, 7]},       # 成员变更
            {"member_ids": [8]},          # 解散
        ]
        new = [
            {"member_ids": [1, 2]},
            {"member_ids": [3]},
            {"member_ids": [4, 5]},
            {"member_ids": [6, 9]},
            {"member_ids": [100]},        # 全新簇，不记事件
        ]
        events = lineage_service.plan_cluster_events(old, new)
        by_kind = {}
        for event in events:
            by_kind.setdefault(event["kind"], []).append(event)
        assert len(by_kind["split"]) == 1
        assert len(by_kind["split"][0]["after"]) == 2
        assert len(by_kind["merge"]) == 1
        assert len(by_kind["merge"][0]["before"]) == 2
        assert len(by_kind["reform"]) == 1
        assert by_kind["reform"][0]["before"][0]["member_ids"] == [6, 7]
        assert len(by_kind["dissolved"]) == 1
        assert by_kind["dissolved"][0]["before"][0]["member_ids"] == [8]

    def test_basis_hash_stability(self):
        """同一依据同一哈希；成员、价格、规则任一变化哈希都变。"""
        kwargs = {
            "listing_id": 1,
            "unit_price": 79000,
            "members": [(1, 79000), (2, 78000)],
            "median_price": 45500.0,
            "mad": 1000.0,
            "sample_size": 9,
        }
        base = lineage_service.basis_hash(**kwargs)
        assert lineage_service.basis_hash(**kwargs) == base
        assert lineage_service.basis_hash(**{**kwargs, "members": [(1, 79000)]}) != base
        assert lineage_service.basis_hash(**{**kwargs, "unit_price": 76000}) != base
        assert lineage_service.basis_hash(**{**kwargs, "median_price": 45750.0}) != base
