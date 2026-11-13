# migrations

运行时表结构由 SQLAlchemy `Base.metadata.create_all` 在 backend 启动时创建（演示项目不引入 Alembic）。

- `0001_initial.sql`：与 `src/models/*` 一一对应的等价 DDL，供 DBA 预审索引与走正式迁移流程。
- `0002_review_lineage.sql`：复核结论血缘——簇按 member_hash 分代（旧代 superseded 留底）、
  工单记录依据快照并支持失效（stale）重算、挂牌加下架软标记、新增 cluster_events 变迁事件表。
  对已存在库执行本文件；新库由 create_all 一步到位。
- `ingest_runs.trigger_kind` 对应模型里的 `IngestRun.trigger_kind`（不叫 `trigger`，避开 MySQL 保留字）。
