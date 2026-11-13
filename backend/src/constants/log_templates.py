"""审计动作中文标签。"""
from __future__ import annotations

ACTIONS: dict[str, str] = {
    "auth.login": "登录",
    "http.mutation": "写请求",
    "ingest.run": "运行采集",
    "ingest.schedule": "定时采集",
    "source.update": "修改采集源",
    "source.create": "新建采集源",
    "cluster.recompute": "重算去重簇",
    "cluster.split": "拆簇",
    "cluster.merge": "并簇",
    "cluster.reform": "簇成员变更",
    "cluster.dissolved": "簇解散",
    "flag.detect": "重跑价格核验",
    "flag.review": "复核价格工单",
    "flag.invalidate": "工单结论失效",
    "listing.price_fix": "修正挂牌单价",
    "listing.delisted": "挂牌下架",
    "export.market": "导出行情 CSV",
}
