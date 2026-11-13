/** 共享枚举（前端副本，后端唯一定义处见 backend/src/constants/enums.py）。 */

export const ROLE = {
  ADMIN: 'admin',
  ANALYST: 'analyst',
  CLIENT: 'client',
} as const;
export type RoleValue = (typeof ROLE)[keyof typeof ROLE];

export const FLAG_TYPE = {
  HIGH: 'high',
  LOW: 'low',
  UNCERTAIN: 'uncertain',
} as const;
export type FlagTypeValue = (typeof FLAG_TYPE)[keyof typeof FLAG_TYPE];

export const FLAG_TYPE_LABELS: Record<string, string> = {
  high: '虚高',
  low: '虚低',
  uncertain: '待确认',
};

export const FLAG_TYPE_ORDER = ['high', 'low', 'uncertain'];

export const REVIEW_STATUS = {
  PENDING: 'pending',
  CONFIRMED: 'confirmed',
  DISMISSED: 'dismissed',
} as const;

export const REVIEW_LABELS: Record<string, string> = {
  pending: '待复核',
  confirmed: '已确认',
  dismissed: '已驳回',
};

export const INVALIDATION_LABELS: Record<string, string> = {
  members_changed: '簇成员变化',
  price_changed: '价格变化',
  judgment_changed: '判定口径变化',
  rule_changed: '判定规则版本变化',
  delisted: '房源下架',
};

export const CLUSTER_EVENT_LABELS: Record<string, string> = {
  split: '拆簇',
  merge: '并簇',
  reform: '簇成员变更',
  dissolved: '簇解散',
};

export const INGEST_STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  success: '成功',
  partial: '部分成功',
  failed: '失败',
};

export const LAYOUT = ['一居', '两居', '三居'];

export const FLOOR_BAND = ['低楼层', '中楼层', '高楼层'];

export const PLATFORM_LABELS: Record<string, string> = {
  aggregator: '链家式聚合站',
  portal: '安居式信息港',
  forum: '业主论坛',
  moments: '中介朋友圈存档',
  notice: '门店群公告',
};

export const SORT_OPTIONS = [
  { value: 'unit_price', label: '单价' },
  { value: 'total_price_wan', label: '总价' },
  { value: 'area_sqm', label: '面积' },
  { value: 'list_date', label: '挂牌日' },
];
