/**
 * 현금흐름 API 클라이언트 (Phase 6, docs/54_cashflow_semantics.md).
 *
 * 손익과 현금은 다른 질문이다
 * ------------------------
 * ```
 * 8월 매출 3,000만 / 순이익 600만    ← 손익  (/dashboard, /monthly-close)
 * 8월 실제 입금 1,900만 / 출금 1,700만 ← 현금  (여기)
 * ```
 * 매출 발생일 ≠ 입금일. 이 클라이언트는 매출을 현금으로 부르지 않는다.
 *
 * 화면이 지켜야 할 두 가지
 * --------------------
 * 1. `balance_available === false` 면 잔액 자리에 **0원을 쓰지 않는다.**
 *    기준 잔액이 없어 계산이 불가능하다는 뜻이다 — 0원은 "통장이 비었다" 로 읽힌다.
 * 2. `status` 를 무시하고 합계를 내지 않는다. `EXPECTED` 는 아직 안 들어온 돈이다.
 */

import { api, apiRequest, type ListResponse } from "@/lib/api";

export type CashDirection = "INFLOW" | "OUTFLOW";

/**
 * 무엇을 근거로 이 금액을 믿는가.
 *
 * `ACTUAL` 은 좁다 — 지급 기록이나 사람이 확인한 입출금이 있을 때만이다.
 * 판매처 정산서는 `settled_at` 이라는 이름에도 불구하고 `CONFIRMED` 가 최대다
 * (실제 은행 입금일이라는 근거가 없다 — docs/54 §3-2).
 */
export type CashStatus = "EXPECTED" | "CONFIRMED" | "ACTUAL";

export const CASH_STATUS_LABEL: Record<CashStatus, string> = {
  EXPECTED: "예상",
  CONFIRMED: "확정",
  ACTUAL: "실제",
};

/** 사용자가 "확정" 과 "실제" 를 같은 것으로 오해하지 않도록 근거를 함께 보여준다. */
export const CASH_STATUS_HINT: Record<CashStatus, string> = {
  EXPECTED: "아직 확정되지 않은 예정 금액입니다.",
  CONFIRMED: "금액은 확정됐지만 실제 입출금 기록은 확인되지 않았습니다.",
  ACTUAL: "지급 기록 또는 직접 확인한 입출금입니다.",
};

export const CASH_SOURCE_LABEL: Record<string, string> = {
  payout_payment: "지급 기록",
  payout_item: "지급 예정",
  marketplace_settlement_record: "판매처 정산서",
  manual_cash_event: "직접 입력",
};

export interface CashRow {
  occurred_on: string;
  direction: CashDirection;
  status: CashStatus;
  amount: number;
  label: string;
  source_type: string;
  source_id: string | null;
  /** 원본 화면 경로. 출처 없는 숫자를 보여주지 않기 위한 것이다. */
  drill_href: string | null;
}

export interface CashTimelinePoint {
  date: string;
  inflow: number;
  outflow: number;
  net: number;
  /** 기준 잔액이 없으면 `null` — 0 이 아니다. */
  balance: number | null;
}

export interface CashBaseline {
  id: string;
  as_of_date: string;
  amount: number;
  memo: string | null;
}

export interface CashflowSummary {
  range: { from: string; to: string; preset: string | null };
  baseline: CashBaseline | null;
  /** false 면 잔액을 계산할 수 없다. 화면은 "기준 잔액 미설정" 을 보여준다. */
  balance_available: boolean;
  inflow: { expected: number; confirmed: number; actual: number };
  outflow: { expected: number; confirmed: number; actual: number };
  projected_ending_balance: number | null;
  /** 기간 중 최저점. 말일 잔액만 보면 중간의 자금 부족을 놓친다. */
  lowest_projected_balance: number | null;
  unpaid_payout: number;
  overdue_payout: number;
  timeline: CashTimelinePoint[];
  rows: CashRow[];
  /** false 면 실제 입금을 확인할 수단이 아직 없다는 뜻이다 (은행 연동 없음). */
  actual_inflow_source_available: boolean;
}

export interface ManualCashEvent {
  id: string;
  direction: CashDirection;
  status: CashStatus;
  amount: number;
  currency?: string;
  occurred_at: string | null;
  expected_at: string | null;
  category: string | null;
  memo: string | null;
  source_type: string | null;
  source_id: string | null;
}

export interface ManualCashEventInput {
  direction: CashDirection;
  status: CashStatus;
  /** 항상 양수. 방향은 `direction` 이 정한다 — 음수로 방향을 표현하지 않는다. */
  amount: number;
  occurred_at?: string | null;
  expected_at?: string | null;
  category?: string | null;
  memo?: string | null;
  source_type?: string | null;
  source_id?: string | null;
}

export const cashflowApi = {
  summary: (params: { preset?: string; date_from?: string; date_to?: string } = {}) =>
    api.get<CashflowSummary>("/cashflow", { query: params }),

  getBaseline: () => api.get<CashBaseline | null>("/cashflow/baseline"),

  /** 같은 날짜는 덮어쓴다 — 같은 날 기준 잔액이 둘이면 어느 쪽이 맞는지 알 수 없다. */
  setBaseline: (body: { as_of_date: string; amount: number; memo?: string | null }) =>
    api.put<CashBaseline>("/cashflow/baseline", body),

  listManualEvents: (params: { date_from: string; date_to: string }) =>
    api.get<ListResponse<ManualCashEvent>>("/cashflow/manual-events", {
      query: params,
    }),

  /**
   * 이미 지급 기록이 있는 건을 `source_type`/`source_id` 로 또 넣으면 409 다.
   * 출금이 두 배로 잡히는 것을 DB 가 막는다.
   */
  createManualEvent: (body: ManualCashEventInput) =>
    api.post<ManualCashEvent>("/cashflow/manual-events", body),

  /** 사유가 필수다 — 나중에 금액이 왜 달라졌는지 설명할 수 있어야 한다. */
  deleteManualEvent: (id: string, reason: string) =>
    apiRequest<{ deleted: boolean }>(`/cashflow/manual-events/${id}`, {
      method: "DELETE",
      body: { reason },
    }),
};
