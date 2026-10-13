/**
 * 월 마감 API 클라이언트.
 *
 * ⚠️ 발견한 결함 (D2) — 이 파일은 존재하지 않는 엔드포인트를 부르고 있었다.
 * -------------------------------------------------------------------
 * 이전 구현은 전부 `/settlements/monthly/…` 를 호출했다. 백엔드에 `/settlements`
 * 로 시작하는 라우터는 **없다**(`/settlement`, `/monthly-close` 두 개다).
 * 즉 월정산 화면의 모든 요청이 404 였다.
 *
 * 이제 실제 경로를 쓴다.
 *
 * ```
 * GET    /monthly-close                      월 목록
 * GET    /monthly-close/{ym}                 월 상세 + 확정 snapshot
 * POST   /monthly-close/{ym}/validate        마감 검증 실행
 * POST   /monthly-close/{ym}/request-review  검토 요청
 * POST   /monthly-close/{ym}/confirm         확정 (경고 확인 필요)
 * POST   /monthly-close/{ym}/lock            잠금
 * POST   /monthly-close/{ym}/reopen-request  재오픈 승인 요청
 * GET    /monthly-close/{ym}/snapshot-preview 확정하면 박제될 숫자
 * GET    /monthly-close/{ym}/drift           확정 후 데이터 변경 탐지
 * POST   /settlement/{id}/calculate 계열은 /settlement 라우터에 남아 있다
 * ```
 *
 * 확정월과 진행중인 달을 섞지 않는다
 * ------------------------------
 * 목록의 `snapshot_net_profit` 은 확정된 달에만 채워진다. 화면은 그 값이
 * 있으면 "확정", 없으면 "잠정" 으로 표시해야 한다 — 같은 컬럼에 섞으면
 * "7월 순이익" 이 매일 달라 보인다.
 */

import { api, type ListResponse } from "@/lib/api";

export type CloseState = "OPEN" | "REVIEW" | "CONFIRMED" | "LOCKED";
export type CheckGrade = "BLOCKING" | "WARNING" | "INFO";

export interface ValidationItem {
  code: string;
  /** 기존 계약 유지 — INFO / WARNING / CRITICAL */
  severity: string;
  /** 새 3등급 — BLOCKING / WARNING / INFO */
  grade: CheckGrade;
  blocking: boolean;
  count: number;
  message: string;
  hint: string | null;
  domain: string;
  entity_type: string | null;
}

/** 확정 시점에 박제된 숫자. 실시간 재집계가 아니다. */
export interface CloseSnapshot {
  id: string;
  generation: number;
  taken_at_state: "CONFIRMED" | "LOCKED";
  taken_at: string;
  reason: string | null;

  order_count: number;
  gross_sales: number;

  cogs: number;
  shipping_cost: number;
  marketplace_fees: number;
  payment_fees: number;
  advertising_cost: number;
  fulfillment_fees: number;
  returns_cost: number;
  total_direct_cost: number;

  business_expenses: number;
  labor_expense: number;
  personal_card_expense: number;
  expense_vat: number;
  adjustment_total: number;

  /** false 면 정산 금액은 0원이 아니라 "모른다" 는 뜻이다. */
  settlement_connected: boolean;
  settlement_record_count: number;
  settlement_unmatched_count: number;
  confirmed_settlement_amount: number;

  net_profit: number;
  carryover_offset: number;
  distributable_profit: number;

  provisional_profit_total: number;
  settled_profit_total: number;
  final_profit_total: number;

  profit_basis: "SALES_AND_EXPENSE_ONLY" | "SETTLEMENT_RECONCILED";
  warning_count: number;
  source_cutoff_at: string;
  calculation_version: string;
  content_hash: string;
}

export interface MonthClose {
  year_month: string;
  close_state: CloseState;
  status: string;
  exists: boolean;

  total_revenue: number;
  total_direct_cost: number;
  total_common_expense: number;
  net_profit: number;
  distributable_amount: number;

  reopen_count: number;
  close_generation: number;
  confirmed_at: string | null;
  locked_at: string | null;
  validated_at: string | null;
  last_reopened_at: string | null;
  last_reopen_reason: string | null;

  validation_result: ValidationItem[] | null;
  acknowledged_warnings: string[];

  /** 확정된 달만 채워진다. 확정 숫자의 단일 진실이다. */
  snapshot: CloseSnapshot | null;
  /** "확정 순이익" / "확정 순이익 (정산 미연결 — 판매·비용 기준)" 등 */
  profit_label: string;

  profit_summary?: Record<string, number>;
}

export interface MonthCloseSummary {
  year_month: string;
  close_state: CloseState;
  net_profit: number;
  distributable_amount: number;
  reopen_count: number;
  close_generation: number;
  confirmed_at: string | null;
  locked_at: string | null;
  /** 확정된 달만 채워진다. 없으면 아직 잠정치다. */
  snapshot_net_profit: number | null;
  snapshot_distributable_profit: number | null;
  snapshot_profit_basis: string | null;
}

export interface CheckRun {
  id: string;
  trigger: string;
  close_state_at_run: CloseState;
  status: "RUNNING" | "COMPLETED" | "FAILED";
  blocking_count: number;
  warning_count: number;
  info_count: number;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface DriftReport {
  drifted: boolean;
  snapshot_hash: string;
  current_hash: string;
  calculation_version_changed: boolean;
  changed_fields: string[];
  snapshot_generation: number;
}

export interface ReopenQuota {
  reopen_count: number;
  limit: number;
}

export interface SnapshotGeneration {
  id: string;
  generation: number;
  taken_at_state: string;
  taken_at: string;
  reason: string | null;
  gross_sales: number;
  total_direct_cost: number;
  business_expenses: number;
  net_profit: number;
  distributable_profit: number;
  profit_basis: string;
  content_hash: string;
  superseded_at: string | null;
}

export const monthlyCloseApi = {
  list: (limit = 24) =>
    api.get<ListResponse<MonthCloseSummary>>("/monthly-close", { query: { limit } }),

  get: (ym: string) => api.get<MonthClose>(`/monthly-close/${ym}`),

  validate: (ym: string) =>
    api.post<ListResponse<ValidationItem>>(`/monthly-close/${ym}/validate`, {}),

  requestReview: (ym: string) =>
    api.post<MonthClose>(`/monthly-close/${ym}/request-review`, {}),

  /** 경고 코드를 명시하지 않으면 확정이 거부된다 — "보고 넘긴 것" 을 기록한다. */
  confirm: (ym: string, acknowledgedWarnings: string[]) =>
    api.post<MonthClose>(`/monthly-close/${ym}/confirm`, {
      acknowledged_warnings: acknowledgedWarnings,
    }),

  lock: (ym: string) => api.post<MonthClose>(`/monthly-close/${ym}/lock`, {}),

  snapshotPreview: (ym: string) =>
    api.get<CloseSnapshot>(`/monthly-close/${ym}/snapshot-preview`),

  snapshots: (ym: string) =>
    api.get<ListResponse<SnapshotGeneration>>(`/monthly-close/${ym}/snapshots`),

  drift: (ym: string) => api.get<DriftReport | null>(`/monthly-close/${ym}/drift`),

  checkRuns: (ym: string, limit = 20) =>
    api.get<ListResponse<CheckRun>>(`/monthly-close/${ym}/check-runs`, { query: { limit } }),

  reopenQuota: (ym: string) =>
    api.get<ReopenQuota>(`/monthly-close/${ym}/reopen-quota`),

  /** 재오픈은 승인을 거친다. 이 호출은 요청을 만들 뿐 달을 열지 않는다. */
  requestReopen: (ym: string, reason: string) =>
    api.post<{ id: string; business_key: string; status: string }>(
      `/monthly-close/${ym}/reopen-request`,
      { reason, submit: true },
    ),
};

/**
 * 하위 호환 별칭.
 *
 * 기존 화면이 `settlementsApi` 이름으로 import 하고 있어 한 번에 바꾸지 않는다.
 * 새 코드는 `monthlyCloseApi` 를 쓴다.
 */
export const settlementsApi = monthlyCloseApi;
