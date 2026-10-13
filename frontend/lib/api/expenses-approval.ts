/**
 * 사업비용 승인 · 잠긴 월 조정 클라이언트.
 *
 * 상품과 같은 원칙이다 — **승인 대상 필드는 직접 저장 경로로 보내지 않는다.**
 * 비용 금액은 순이익의 입력값이고 순이익이 곧 파트너 수익배분이다.
 */

import { api } from "@/lib/api";

export type CloseState = "OPEN" | "REVIEW" | "CONFIRMED" | "LOCKED";

export interface ExpenseLockState {
  expense_id: string;
  year_month: string;
  close_state: CloseState;
  /** true 면 원본을 못 고치고 조정으로 처리해야 한다 */
  requires_adjustment: boolean;
}

export type AdjustmentType =
  | "LATE_ENTRY" | "CORRECTION" | "REVERSAL" | "RECLASSIFICATION";

export const ADJUSTMENT_TYPE_LABEL: Record<AdjustmentType, string> = {
  LATE_ENTRY: "누락분 등록",
  CORRECTION: "금액 정정",
  REVERSAL: "취소·되돌림",
  RECLASSIFICATION: "재분류",
};

export interface ExpenseAdjustment {
  id: string;
  original_expense_id: string | null;
  adjustment_expense_id: string;
  adjustment_type: AdjustmentType;
  adjustment_reason: string;
  original_period: string | null;
  adjustment_period: string;
  /** 차액. 원본을 늘리면 양수, 되돌리면 음수 */
  amount_delta: number;
  approval_request_id: string | null;
  created_at: string;
}

export const expenseApprovalApi = {
  lockState: (expenseId: string) =>
    api.get<ExpenseLockState>(`/expenses/${expenseId}/lock-state`),

  adjustments: (expenseId: string) =>
    api.get<ExpenseAdjustment[]>(`/expenses/${expenseId}/adjustments`),

  createAdjustment: (expenseId: string, body: {
    adjustment_type: AdjustmentType;
    adjustment_reason: string;
    adjustment_date: string;
    amount_delta: number;
    payload?: Record<string, unknown> | null;
  }) => api.post<ExpenseAdjustment>(`/expenses/${expenseId}/adjustments`, body),
};

/* ────────────────────────────────────────────────────────────
 * 변경 분류 — 상품과 같은 규칙, 비용 도메인 라벨
 * ──────────────────────────────────────────────────────────── */

/** 비용 필드 한글 라벨. JSON 을 그대로 보여주지 않기 위해 필요하다. */
export const EXPENSE_FIELD_LABELS: Record<string, string> = {
  gross_amount: "금액",
  supply_amount: "공급가액",
  vat_amount: "부가세",
  vat_inclusion_type: "부가세 포함 여부",
  tax_type: "과세 유형",
  expense_date: "비용 발생일",
  category_id: "비용 카테고리",
  payment_method: "결제수단",
  payment_card_holder_id: "결제자",
  is_personal_card: "개인카드 사용",
  is_business_use: "업무용 여부",
  is_tax_deductible: "세무 공제",
  is_settlement_deductible: "정산 차감",
  attributable_partner_id: "귀속 파트너",
  supplier_id: "거래처",
  description: "비용 내용",
  evidence_type: "증빙 유형",
  evidence_status: "증빙 상태",
  memo: "메모",
};

export const EXPENSE_REQUEST_TYPE_LABELS: Record<string, string> = {
  EXPENSE_FINANCIAL_CHANGE: "금액·정산 변경",
  EXPENSE_EVIDENCE_CHANGE: "증빙 변경",
};

/**
 * enum 값 한글 라벨. 필드명 → 값 → 라벨.
 *
 * 라벨을 화면마다 따로 두면 승인 diff 는 `BUSINESS_CARD`, 목록은 "사업자카드" 가 되어
 * 같은 값이 두 이름으로 보인다. **입력 폼과 승인 diff 가 같은 표를 쓴다.**
 */
export const EXPENSE_VALUE_LABELS: Record<string, Record<string, string>> = {
  payment_method: {
    BUSINESS_CARD: "사업자카드",
    PERSONAL_CARD: "개인카드",
    BANK_TRANSFER: "계좌이체",
    CASH: "현금",
    OTHER: "기타",
  },
  vat_inclusion_type: {
    INCLUDED: "부가세 포함",
    EXCLUDED: "부가세 별도",
    EXEMPT: "면세",
  },
  // enum_tax_type — DB 는 3종뿐이다. 없는 값을 목록에 넣으면 저장에서 500 이 난다.
  tax_type: {
    GENERAL: "일반과세",
    SIMPLE: "간이과세",
    EXEMPT: "면세",
  },
  evidence_type: {
    TAX_INVOICE: "세금계산서",
    CASH_RECEIPT: "현금영수증",
    CARD_SLIP: "카드전표",
    BANK_TRANSFER: "이체증빙",
    SIMPLE_RECEIPT: "간이영수증",
    SELF_DECLARED: "자체작성",
    OTHER: "기타",
  },
  evidence_status: {
    NONE: "없음",
    NEEDS_CHECK: "확인 필요",
    PRESENT: "보유",
    SUBMITTED_TO_ACCOUNTANT: "세무사 제출",
  },
};

/** 선택 목록용 — 표의 순서를 그대로 쓴다. */
export function optionsOf(field: keyof typeof EXPENSE_VALUE_LABELS | string) {
  return Object.entries(EXPENSE_VALUE_LABELS[field] ?? {}).map(([value, label]) => ({
    value, label,
  }));
}

/**
 * 잠긴 달 안내 문구.
 *
 * "수정할 수 없습니다" 로 끝내지 않는다 — 사용자는 그래서 뭘 해야 하는지를 알아야 한다.
 */
export function lockGuidance(state: ExpenseLockState): string | null {
  if (!state.requires_adjustment) return null;
  const [y, m] = state.year_month.split("-");
  return (
    `이 비용이 귀속된 ${y}년 ${Number(m)}월은 ` +
    `${state.close_state === "LOCKED" ? "잠겨" : "확정되어"} 있습니다. ` +
    "기존 비용을 변경할 수 없으며 현재 열린 월에 정정 조정을 생성해야 합니다."
  );
}

/** 개인카드 상환 상태. **비용 승인과 별개다** — 합치면 승인=지급이 된다. */
export type ReimbursementStatus =
  | "NOT_REQUIRED" | "PENDING" | "APPROVED" | "PAID" | "PARTIALLY_PAID" | "CANCELLED";

export const REIMBURSEMENT_LABEL: Record<ReimbursementStatus, string> = {
  NOT_REQUIRED: "상환 불필요",
  PENDING: "상환 대기",
  APPROVED: "상환 승인",
  PAID: "지급 완료",
  PARTIALLY_PAID: "부분 지급",
  CANCELLED: "취소",
};

/**
 * 개인카드 비용의 상환 필요 여부.
 *
 * **지급 장부는 아직 없다.** 그래서 상태는 `NOT_REQUIRED` 와 `PENDING` 만 나온다 —
 * 지급됐다고 표시할 근거가 없는데 `PAID` 를 만들면 거짓말이 된다.
 */
export function reimbursementOf(expense: {
  is_personal_card: boolean; gross_amount: number;
  payment_card_holder_id: string | null;
}): { status: ReimbursementStatus; amount: number | null; payeeId: string | null } {
  if (!expense.is_personal_card) {
    return { status: "NOT_REQUIRED", amount: null, payeeId: null };
  }
  return {
    status: "PENDING",
    amount: expense.gross_amount,
    payeeId: expense.payment_card_holder_id,
  };
}
