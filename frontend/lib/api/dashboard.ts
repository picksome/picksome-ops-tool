/**
 * 대시보드 API 클라이언트 (Phase 45).
 *
 * 이전 구현(`lib/adapters/dashboard.ts`)은 `MOCK_PRODUCTS`/`MOCK_EXPENSES` 에서
 * 숫자를 만들어 냈다. 실제 금액처럼 보이는 sample 이었다. 그 파일은 삭제됐고
 * 대시보드는 이제 `GET /reports/dashboard` 하나만 본다.
 *
 * **여기서 순이익을 계산하지 않는다.** 집계는 전부 서버가 한다 — 마감 화면과
 * 대시보드가 각자 계산하면 두 화면이 다른 숫자를 말하게 된다.
 *
 * 월 단위인 이유
 * -----------
 * 마감 상태와 확정 snapshot 이 월 단위로 존재한다. 임의 날짜 구간은 확정월의
 * 박제 숫자와 진행월의 집계를 한 값에 섞게 되고, 그러면 그 값의 출처를 말할 수 없다.
 */

import { api } from "@/lib/api";

/** 숫자의 출처. 화면은 이 코드를 자기 사전으로 번역한다. */
export type DashboardSource = "LIVE" | "MONTH_CLOSE_SNAPSHOT";

/** KPI 가용성. `NOT_CONNECTED` 는 0원이 아니라 "아직 모른다" 는 뜻이다. */
export type KpiAvailability = "AVAILABLE" | "NOT_CONNECTED";

export type CloseState = "OPEN" | "REVIEW" | "CONFIRMED" | "LOCKED";

export type ProfitBasis = "SETTLEMENT_RECONCILED" | "SALES_AND_EXPENSE_ONLY";

export interface DashboardPeriod {
  year_month: string;
  start: string;
  end: string;
  close_state: CloseState;
  source: DashboardSource;
  profit_basis: ProfitBasis | null;
  profit_label: string;
  settlement_connected: boolean;
  snapshot_generation: number | null;
  snapshot_taken_at: string | null;
  is_confirmed: boolean;
}

export interface DashboardComparison {
  year_month: string;
  close_state: CloseState;
  source: DashboardSource;
  /** 두 기간의 source 가 같은가. 다르면 증감을 그대로 믿으면 안 된다. */
  comparable: boolean;
  note: string | null;
}

export interface DashboardKpi {
  key: string;
  label: string;
  /** `null` 은 0이 아니라 "값이 없다" 는 뜻이다. */
  value: number | null;
  format: "money" | "percent" | "count" | "status";
  source: DashboardSource;
  availability: KpiAvailability;
  higher_is_better: boolean;
  hint: string | null;
  previous: number | null;
  previous_source: DashboardSource | null;
  comparable: boolean;
}

export interface WaterfallStep {
  key: string;
  label: string;
  amount: number;
  kind: "start" | "deduction" | "total";
}

export interface TrendPoint {
  year_month: string;
  close_state: CloseState;
  source: DashboardSource;
  gross_sales: number;
  net_profit: number;
  business_expenses: number;
  margin_rate: number | null;
  profit_basis: ProfitBasis | null;
}

export interface MarketplaceBreakdown {
  marketplace: string;
  order_count: number;
  gross_sales: number;
  direct_cost: number;
  /** 순이익이 아니다 — 사업 공통비는 판매처에 귀속되지 않는다. */
  contribution_profit: number;
  contribution_rate: number | null;
  /** 마진이 아직 계산되지 않은 주문 수. 공헌이익이 과대평가된 정도. */
  unpriced_order_count: number;
}

export interface ExpenseBreakdown {
  category_name: string;
  amount: number;
  entry_count: number;
  ratio: number | null;
}

export interface MarginPoint {
  product_id: string | null;
  product_name: string;
  gross_sales: number;
  contribution_profit: number;
  contribution_rate: number | null;
  unpriced_order_count: number;
}

export interface DashboardWarnings {
  open_total: number;
  blocking: number;
  warning: number;
  info: number;
  overdue: number;
  unassigned: number;
  by_type: Array<{ issue_type: string; severity: string; count: number }>;
  last_scan: { started_at: string; finished_at: string | null; status: string } | null;
  /** 스캔한 적 없음 ≠ 문제 없음. 화면이 둘을 구분한다. */
  never_scanned: boolean;
}

export interface ConfirmedSnapshot {
  year_month: string;
  close_state: CloseState;
  generation: number;
  taken_at: string;
  gross_sales: number;
  net_profit: number;
  distributable_profit: number;
  profit_basis: ProfitBasis;
  settlement_connected: boolean;
  source: DashboardSource;
  profit_label: string;
}

/**
 * 대시보드의 현금흐름 카드.
 *
 * **선택한 달을 따라가지 않는다.** 대시보드의 나머지는 "그 달에 얼마를 벌었나"
 * (손익)를 묻지만 이 카드는 "앞으로 통장이 버티나" 를 묻는다. 화면이 "8월
 * 현금흐름" 이라고 쓰지 않도록 `basis` 에 기준이 들어 있다.
 */
export interface DashboardCashflow {
  /** 예: "오늘부터 30일". 화면은 이 문구를 그대로 보여준다. */
  basis: string;
  range: { from: string; to: string; preset: string | null };
  /** false 면 잔액 자리에 0원을 쓰지 않는다 — "통장이 비었다" 로 읽힌다. */
  balance_available: boolean;
  baseline: { id: string; as_of_date: string; amount: number; memo: string | null } | null;
  projected_ending_balance: number | null;
  lowest_projected_balance: number | null;
  inflow: { expected: number; confirmed: number; actual: number };
  outflow: { expected: number; confirmed: number; actual: number };
  unpaid_payout: number;
  overdue_payout: number;
  /** false 면 실제 입금을 확인할 수단이 아직 없다 (은행·판매처 연동 없음). */
  actual_inflow_source_available: boolean;
}

export interface DashboardResponse {
  period: DashboardPeriod;
  comparison: DashboardComparison;
  kpis: DashboardKpi[];
  waterfall: WaterfallStep[];
  trend: TrendPoint[];
  by_marketplace: MarketplaceBreakdown[];
  expense_breakdown: ExpenseBreakdown[];
  margin_distribution: MarginPoint[];
  warnings: DashboardWarnings;
  confirmed_snapshots: ConfirmedSnapshot[];
  /**
   * 현금흐름 카드. **역할에 따라 아예 없다.**
   *
   * 대시보드는 OPERATOR·READONLY 도 읽지만 현금흐름은 ADMIN·ACCOUNTING 만
   * 본다. 서버가 볼 수 없는 사람에게는 이 키를 빼고 준다 — 화면은 없으면
   * 카드를 그리지 않는다(0원으로 그리면 안 된다).
   */
  cashflow?: DashboardCashflow;
  available_months: string[];
}

/** source 코드 → 사람이 읽는 짧은 라벨. 배지에 그대로 쓴다. */
export const SOURCE_LABEL: Record<DashboardSource, string> = {
  LIVE: "진행 중 집계",
  MONTH_CLOSE_SNAPSHOT: "확정 snapshot",
};

export const CLOSE_STATE_LABEL: Record<CloseState, string> = {
  OPEN: "열림",
  REVIEW: "검토 중",
  CONFIRMED: "확정",
  LOCKED: "잠금",
};

/**
 * 운영 이슈 유형 → 사람이 읽는 이름.
 *
 * 여기 없는 유형은 코드를 그대로 보여준다. **없는 유형을 지어내지 않는다** —
 * 스캐너가 새 유형을 추가했을 때 화면이 조용히 다른 이름을 붙이면 안 된다.
 */
export const ISSUE_TYPE_LABEL: Record<string, string> = {
  SALE_WITHOUT_PRODUCT: "상품 미연결 판매",
  SALE_ACCOUNT_MISSING: "자동수집 판매 계정 누락",
  SALE_ACCOUNT_UNASSIGNED: "판매자 계정 미지정",
  SALE_ACCOUNT_ORG_MISMATCH: "다른 조직 계정 귀속",
  SALE_ACCOUNT_MARKETPLACE_MISMATCH: "판매처·계정 불일치",
  ZERO_COST_SALE: "원가 0원 판매",
  MARGIN_NOT_CALCULATED: "마진 미계산",
  LOSS_SALE: "적자 판매",
  MISSING_EVIDENCE: "증빙 누락",
  ATTACHMENT_DELETE_PENDING: "증빙 삭제 대기",
  ATTACHMENT_DELETE_FAILED: "증빙 삭제 실패",
  PERIOD_RECALC_PENDING: "기간 재계산 대기",
  PERIOD_RECALC_STALE: "기간 재계산 지연",
  SETTLEMENT_NOT_CONNECTED: "정산 미연결",
  UNMATCHED_SETTLEMENT: "미매칭 정산",
  AMBIGUOUS_SETTLEMENT: "정산 매칭 후보 다건",
  SETTLEMENT_NOT_CONFIRMED: "정산 건 미확정",
  SETTLEMENT_ADJUSTMENT_NOT_IN_SNAPSHOT: "정산 정정 미반영",
  SETTLEMENT_REVENUE_MISMATCH: "정산서·장부 금액 불일치",
  SETTLEMENT_ACCOUNT_ORG_MISMATCH: "정산 다른 조직 계정",
  SETTLEMENT_ACCOUNT_MARKETPLACE_MISMATCH: "정산 판매처·계정 불일치",
  // 클레임 검증(M053·M054)이 내는 코드. 라벨이 없으면 화면이 코드를 그대로
  // 찍는다 — 설정 화면에서 `claim_lookback_days` 가 샌 것과 같은 종류다.
  CLAIM_FINANCIAL_UNRESOLVED: "취소·반품 금액 미확정",
  CLAIM_WITHOUT_ORDER: "원 주문 없는 취소·반품",
  CLAIM_PENDING: "확정 전 취소·반품",
  CLAIM_ADJUSTMENT_NOT_IN_SNAPSHOT: "취소·반품 조정 미반영",
  CASH_POSSIBLE_DOUBLE_COUNT: "현금 이중 계상 의심",
  STALE_EXPECTED_CASHFLOW: "지나간 예정 입출금",
  CASH_ACTUAL_UNEXPLAINED: "설명 없는 입출금",
  APPROVAL_PENDING: "승인 대기",
  APPROVAL_APPROVED_NOT_APPLIED: "승인 후 미적용",
  APPROVAL_APPLY_FAILED: "승인 적용 실패",
  APPROVAL_HELD_BY_LOCK: "잠긴 월로 보류된 승인",
  APPROVAL_HELD_LONG: "장기 미처리 승인",
  APPROVAL_RETRY_PENDING: "승인 재시도 대기",
  CHANNEL_PRICE_PUBLISH_FAILED: "판매처 가격 반영 실패",
  CONNECTOR_UNAVAILABLE: "판매처 연동 불가",
  DISTRIBUTION_RATIO_INVALID: "배분 비율 오류",
  UNVERIFIED_TAX_POLICY: "미검증 세금 정책",
  EXPIRED_FEE_POLICY: "만료된 수수료 정책",
  UNVERIFIED_CALC_POLICY: "미검증 계산 정책",
  PAYOUT_OVERDUE: "지급 지연",
  OPEN_BLOCKING_ISSUE: "미해결 차단 이슈",
};

export function issueTypeLabel(code: string): string {
  return ISSUE_TYPE_LABEL[code] ?? code;
}

export interface DashboardParams {
  year_month?: string;
  trend_months?: number;
}

export const dashboardApi = {
  load: (params: DashboardParams = {}) =>
    api.get<DashboardResponse>("/reports/dashboard", {
      query: {
        year_month: params.year_month,
        trend_months: params.trend_months,
      },
    }),
};
