"""复核结论血缘：规则指纹、判定依据哈希、簇变迁事件分类（纯函数，不碰 DB）。

口径：
  rule_fingerprint  全部去重/判定阈值常量的哈希。阈值任一变化 -> 新指纹 ->
                    所有旧结论的依据自动对不上，必须失效重算。
  member_hash       簇成员 listing_id 集合的哈希。成员集合即簇身份：
                    补采/改价/下架导致成员变化 -> 新哈希 -> 新的一代簇。
  basis_hash        一条价格结论的「依据指纹」：规则版本 + 目标房源单价 +
                    所在簇成员及其价格 + 小区×户型的中位/MAD/样本量。
                    只有 basis_hash 与当前重算一致的人工结论，才允许进入行情导出。
"""
from __future__ import annotations

import hashlib
import json

from src.constants.limits import (
    AREA_TOL_SQM,
    CONFIDENCE_WEIGHTS,
    DEV_BORDER_PCT,
    DEV_FLAG_PCT,
    FLOOR_TOL,
    MIN_SAMPLES_FOR_FLAG,
    PRICE_TOL_PCT,
    SAMPLE_FULL,
    SIM_THRESHOLD,
    SIM_WEIGHTS,
    Z_FULL,
    Z_THRESHOLD,
)


def _short_hash(payload: object) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def rule_fingerprint() -> str:
    """判定规则版本：去重与判定的全部阈值常量任一变化都会得到新指纹。"""
    return "r" + _short_hash(
        {
            "sim_weights": SIM_WEIGHTS,
            "area_tol_sqm": AREA_TOL_SQM,
            "floor_tol": FLOOR_TOL,
            "price_tol_pct": PRICE_TOL_PCT,
            "sim_threshold": SIM_THRESHOLD,
            "min_samples": MIN_SAMPLES_FOR_FLAG,
            "z_threshold": Z_THRESHOLD,
            "dev_flag_pct": DEV_FLAG_PCT,
            "dev_border_pct": DEV_BORDER_PCT,
            "confidence_weights": CONFIDENCE_WEIGHTS,
            "sample_full": SAMPLE_FULL,
            "z_full": Z_FULL,
        }
    )


def member_hash(member_ids: list[int]) -> str:
    """簇身份 = 排序后的成员 listing_id 集合。"""
    return _short_hash(sorted(int(mid) for mid in member_ids))


def basis_hash(
    *,
    listing_id: int,
    unit_price: int | float | None,
    members: list[tuple[int, int | float | None]],
    median_price: float | None,
    mad: float | None,
    sample_size: int,
) -> str:
    """结论依据指纹：规则 + 目标单价 + 簇成员及其价格 + 中位/MAD/样本量。"""
    return _short_hash(
        {
            "rule": rule_fingerprint(),
            "listing_id": int(listing_id),
            "unit_price": int(unit_price or 0),
            "members": sorted([int(mid), int(price or 0)] for mid, price in members),
            "median": median_price,
            "mad": mad,
            "sample": int(sample_size),
        }
    )


def build_basis_snapshot(
    *,
    listing_id: int,
    unit_price: int | float | None,
    members: list[dict],
    median_price: float | None,
    mad: float | None,
    sample_size: int,
    computed_at: str,
) -> dict:
    """给人看的依据快照：结论依据了哪些平台、哪些价格、哪版规则。"""
    return {
        "rule_version": rule_fingerprint(),
        "listing_id": int(listing_id),
        "unit_price": int(unit_price or 0),
        "members": members,
        "median_price": median_price,
        "mad": mad,
        "sample_size": int(sample_size),
        "thresholds": {
            "z": Z_THRESHOLD,
            "dev_flag_pct": DEV_FLAG_PCT,
            "dev_border_pct": DEV_BORDER_PCT,
            "min_samples": MIN_SAMPLES_FOR_FLAG,
            "sim_threshold": SIM_THRESHOLD,
        },
        "computed_at": computed_at,
    }


def invalidation_reason(old_basis: dict | None, new_basis: dict | None) -> str:
    """对比新旧依据快照，给出失效原因（优先级：规则 > 成员 > 价格 > 判定环境）。"""
    if not old_basis:
        # 旧结论没有依据快照（特性上线前的数据），无法证明与当前判定一致
        return "judgment_changed"
    new_basis = new_basis or {}
    if old_basis.get("rule_version") != new_basis.get("rule_version"):
        return "rule_changed"
    old_members = {int(m["listing_id"]): m.get("unit_price") for m in old_basis.get("members", [])}
    new_members = {int(m["listing_id"]): m.get("unit_price") for m in new_basis.get("members", [])}
    if set(old_members) != set(new_members):
        return "members_changed"
    if int(old_basis.get("unit_price") or 0) != int(new_basis.get("unit_price") or 0):
        return "price_changed"
    if any(old_members[mid] != new_members[mid] for mid in old_members):
        return "price_changed"
    # 成员与目标价都没变，但中位/MAD/样本变了（同小区户型的其他房源变了）
    return "judgment_changed"


def plan_cluster_events(old_clusters: list[dict], new_clusters: list[dict]) -> list[dict]:
    """把「失效旧簇 -> 新建簇」的成员重叠关系分类成事件（纯函数）。

    入参均已剔除 member_hash 相同的延续簇；每个元素至少含 member_ids。
    返回 [{kind, before: [old...], after: [new...]}]：
      1 旧 -> 多 新  split（拆簇）
      多 旧 -> 1 新  merge（并簇）
      1 旧 -> 1 新   reform（成员变更）
      多 旧 -> 多 新  reform（重组）
      旧簇没有任何后继  dissolved（解散，单独成事件）
      全新出现的簇（无旧簇重叠）不记事件——新房源上架是常态。
    """
    parent = list(range(len(old_clusters) + len(new_clusters)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    new_memberships = [set(c["member_ids"]) for c in new_clusters]
    for old_index, old in enumerate(old_clusters):
        old_members = set(old["member_ids"])
        for new_index, members in enumerate(new_memberships):
            if old_members & members:
                union(old_index, len(old_clusters) + new_index)

    components: dict[int, dict[str, list[int]]] = {}
    for old_index in range(len(old_clusters)):
        components.setdefault(find(old_index), {"old": [], "new": []})["old"].append(old_index)
    for new_index in range(len(new_clusters)):
        root = find(len(old_clusters) + new_index)
        components.setdefault(root, {"old": [], "new": []})["new"].append(new_index)

    events: list[dict] = []
    for component in components.values():
        old_idx = component["old"]
        new_idx = component["new"]
        if not old_idx:
            continue  # 全新簇，不记事件
        if not new_idx:
            for one in old_idx:
                events.append({"kind": "dissolved", "before": [old_clusters[one]], "after": []})
            continue
        if len(old_idx) == 1 and len(new_idx) > 1:
            kind = "split"
        elif len(old_idx) > 1 and len(new_idx) == 1:
            kind = "merge"
        else:
            kind = "reform"
        events.append(
            {
                "kind": kind,
                "before": [old_clusters[i] for i in old_idx],
                "after": [new_clusters[i] for i in new_idx],
            }
        )
    return events
