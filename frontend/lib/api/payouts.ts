/**
 * 확정 수익배분 · 지급 장부 API 클라이언트 (Phase 31~34).
 *
 * 계산과 현금을 구분한다
 * -------------------
 * ```
 * distribution  "얼마를 나눠야 하는가"   — 월 확정 snapshot 기준
 * payout        "실제로 보냈는가"        — 지급 기록이 있어야 PAID
 * ```
 *
 * 회계 효과가 항목마다 다르다. 화면은 이 값을 그대로 보여줘야 한다 —
 * 동업자 배분을 비용처럼 표시하면 사용자가 손익을 잘못 이해한다.
 */

import { api, type ListResponse } from "@/lib/api";

export type PayoutItemType =
  | "PARTNER_DISTRIBUTION"
  | "EMPLOYEE_SALARY"
  | "EXPENSE_REIMBURSEMENT"
  | "INVESTOR_DISTRIBUTION"
  | "OTHER";

export type AccountingEffect =
  | "ALREADY_EXPENSED"
  | "PROFIT_APPROPRIATION"
  | "NONE";

export type PayoutItemStatus =
  | "CALCULATED"
  | "APPROVED"
  | "PARTIALLY_PAID"
  | "PAID"
  | "CANCELLED"
  | "CARRIED_FORWARD";

export type PayoutBatchStatus =
  | "DRAFT"
  | "APPROVED"
  | "PARTIALLY_PAID"
  | "PAID"
  | "CANCELLED";

export const ITEM_TYPE_LABEL: Record<PayoutItemType, string> = {
  PARTNER_DISTRIBUTION: "동업자 배분",
  EMPLOYEE_SALARY: "직원 급여",
  EXPENSE_REIMBURSEMENT: "개인카드 상환",
  INVESTOR_DISTRIBUTION: "투자자 배분",
  OTHER: "기타",
};

/** 화면이 "이 지급이 손익에 어떤 영향을 주는가" 를 그대로 설명할 수 있게 한다. */
export const ACCOUNTING_EFFECT_LABEL: Record<AccountingEffect, string> = {
  ALREADY_EXPENSED: "이미 비용 반영됨",
  PROFIT_APPROPRIATION: "이익 처분 (비용 아님)",
  NONE: "손익 영향 없음",
};

export const ITEM_STATUS_LABEL: Record<PayoutItemStatus, string> = {
  CALCULATED: "계산됨",
  APPROVED: "지급 승인",
  PARTIALLY_PAID: "부분 지급",
  PAID: "지급 완료",
  CANCELLED: "취소",
  CARRIED_FORWARD: "이월",
};

export const BATCH_STATUS_LABEL: Record<PayoutBatchStatus, string> = {
  DRAFT: "작성중",
  APPROVED: "승인",
  PARTIALLY_PAID: "부분 지급",
  PAID: "지급 완료",
  CANCELLED: "취소",
};

export interface PayoutItem {
  id: string;
  item_type: PayoutItemType;
  accounting_effect: AccountingEffect;
  payee_partner_id: string | null;
  payee_name_snapshot: string;
  source_distribution_result_id: string | null;
  source_expense_id: string | null;
  due_amount: number;
  paid_amount: number;
  outstanding_amount: number;
  status: PayoutItemStatus;
  due_date: string | null;
  memo: string | null;
}

export interface PayoutBatch {
  id: string;
  business_key: string;
  title: string;
  period_year_month: string | null;
  status: PayoutBatchStatus;
  memo: string | null;
  source_distribution_run_id: string | null;
  created_at: string;
  approved_at: string | null;
  items: PayoutItem[];
  due_total: number;
  paid_total: number;
  outstanding_total: number;
}

export interface PayoutBatchSummary {
  id: string;
  business_key: string;
  title: string;
  period_year_month: string | null;
  status: PayoutBatchStatus;
  source_distribution_run_id: string | null;
  created_at: string;
  approved_at: string | null;
  due_total: number;
  paid_total: number;
  item_count: number;
}

export interface PayoutPayment {
  id: string;
  payment_date: string;
  amount: number;
  payment_reference: string | null;
  memo: string | null;
  recorded_at: string;
}

export interface ReimbursementCandidate {
  expense_id: string;
  expense_date: string;
  description: string;
  gross_amount: number;
  payment_card_holder_id: string | null;
  card_holder_name: string | null;
  evidence_status: string;
}

export interface DistributionResult {
  id: string;
  partner_id: string;
  partner_name_snapshot: string;
  distribution_kind: "PRIORITY" | "RATIO";
  calculation_base: number;
  ratio_used: string | null;
  priority_order: number | null;
  amount: number;
  note: string | null;
}

export interface DistributionRun {
  id: string;
  year_month: string;
  snapshot_generation: number;
  distributable_base: number;
  total_distributed: number;
  carryover_applied: number;
  status: "CALCULATED" | "APPROVED" | "SUPERSEDED" | "CANCELLED";
  applied_rule_versions_json: Record<string, unknown> | null;
  created_at: string;
  approved_at: string | null;
  results: DistributionResult[];
}

export interface DistributionPreview {
  year_month: string;
  /** 월이 확정되지 않았으면 true. 화면이 "확정 배분액" 이라고 부르면 안 된다. */
  is_provisional: boolean;
  distributable_base: number;
  total_distributed: number;
  results: DistributionResult[];
}

export const distributionsApi = {
  preview: (ym: string) =>
    api.get<DistributionPreview>(`/distributions/${ym}/preview`),
  get: (ym: string) => api.get<DistributionRun | null>(`/distributions/${ym}`),
  history: (ym: string) =>
    api.get<ListResponse<DistributionRun>>(`/distributions/${ym}/history`),
  create: (ym: string) => api.post<DistributionRun>(`/distributions/${ym}`, {}),
  approve: (ym: string, runId: string) =>
    api.post<DistributionRun>(`/distributions/${ym}/approve`, {}, {
      query: { run_id: runId },
    }),
};

export const payoutsApi = {
  list: (params: { status?: string; year_month?: string; limit?: number } = {}) =>
    api.get<ListResponse<PayoutBatchSummary>>("/payouts", { query: params }),

  get: (id: string) => api.get<PayoutBatch>(`/payouts/${id}`),

  createFromDistribution: (ym: string, dueDate?: string) =>
    api.post<PayoutBatch>(`/payouts/from-distribution/${ym}`, {
      due_date: dueDate ?? null,
    }),

  approve: (id: string) => api.post<PayoutBatch>(`/payouts/${id}/approve`, {}),

  reimbursementCandidates: (params: { year_month?: string } = {}) =>
    api.get<ListResponse<ReimbursementCandidate> & { total_amount: number }>(
      "/payouts/reimbursement-candidates",
      { query: params },
    ),

  createReimbursement: (body: {
    expense_ids: string[];
    title: string;
    due_date?: string;
  }) => api.post<PayoutBatch>("/payouts/reimbursements", body),

  payments: (itemId: string) =>
    api.get<ListResponse<PayoutPayment>>(`/payouts/items/${itemId}/payments`),

  /** 부분 지급을 지원한다. 남은 금액을 넘기면 422 로 거부된다. */
  recordPayment: (
    itemId: string,
    body: {
      payment_date: string;
      amount: number;
      payment_reference?: string;
      memo?: string;
    },
  ) =>
    api.post<{ payment_id: string; item: PayoutItem }>(
      `/payouts/items/${itemId}/payments`,
      body,
    ),
};
