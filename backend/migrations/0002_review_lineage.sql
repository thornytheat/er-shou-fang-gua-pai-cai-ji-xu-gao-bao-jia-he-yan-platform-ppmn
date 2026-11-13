-- 0002 复核结论血缘：簇分代 + 工单依据快照 + 下架软标记 + 簇变迁事件（MySQL 8.0）
--
-- 背景：补采 / 改价 / 下架会让簇拆开或合并。此前人工确认的结论跟着旧挂牌编号
-- 留在新工单上，拆簇后无关房源被算进行情。本迁移让结论随房源关系重新校验：
--   * listing_clusters 按 member_hash 分代，旧代 superseded 留底；
--   * price_flags 记录 basis_hash / basis_snapshot（结论依据的平台与价格），
--     依据或判定规则变化 -> 旧结论 stale，另起一代 pending 工单；
--   * 行情导出只取 valid_state='current' 的结论；
--   * cluster_events 记录拆簇 / 并簇 / 成员变更 / 解散的前后快照。

ALTER TABLE listing_clusters
  ADD COLUMN member_hash    VARCHAR(64) NOT NULL DEFAULT '' AFTER primary_listing_id,
  ADD COLUMN status         VARCHAR(16) NOT NULL DEFAULT 'active' AFTER member_hash,
  ADD COLUMN generation     INT NOT NULL DEFAULT 1 AFTER status,
  ADD COLUMN superseded_at  DATETIME NULL AFTER generation,
  DROP INDEX uq_cluster_primary,
  ADD KEY ix_cluster_primary (primary_listing_id),
  ADD KEY ix_cluster_member_hash (member_hash),
  ADD KEY ix_cluster_status (status);

ALTER TABLE price_flags
  ADD COLUMN basis_hash          VARCHAR(64) NULL AFTER detected_at,
  ADD COLUMN basis_snapshot      JSON NULL AFTER basis_hash,
  ADD COLUMN valid_state         VARCHAR(16) NOT NULL DEFAULT 'current' AFTER basis_snapshot,
  ADD COLUMN invalidated_at      DATETIME NULL AFTER valid_state,
  ADD COLUMN invalidation_reason VARCHAR(32) NULL AFTER invalidated_at,
  ADD COLUMN superseded_by       INT NULL AFTER invalidation_reason,
  ADD COLUMN generation          INT NOT NULL DEFAULT 1 AFTER superseded_by,
  DROP INDEX uq_flags_listing,
  ADD KEY ix_flags_listing (listing_id),
  ADD KEY ix_flags_listing_state (listing_id, valid_state),
  ADD KEY ix_flags_basis_hash (basis_hash),
  ADD KEY ix_flags_valid_state (valid_state);

ALTER TABLE listings
  ADD COLUMN is_active  TINYINT(1) NOT NULL DEFAULT 1 AFTER is_primary,
  ADD COLUMN removed_at DATETIME NULL AFTER is_active,
  ADD KEY ix_listings_active (is_active);

CREATE TABLE IF NOT EXISTS cluster_events (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  event_kind      VARCHAR(16)  NOT NULL,
  trigger_kind    VARCHAR(24)  NOT NULL DEFAULT 'recompute',
  actor_email     VARCHAR(160) NOT NULL DEFAULT 'system',
  block_key       VARCHAR(60)  NOT NULL DEFAULT '',
  before_clusters JSON NULL,
  after_clusters  JSON NULL,
  listing_ids     JSON NULL,
  created_at      DATETIME NOT NULL,
  KEY ix_cluster_events_kind (event_kind),
  KEY ix_cluster_events_block (block_key),
  KEY ix_cluster_events_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
