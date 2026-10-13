import { api, type ListResponse } from "@/lib/api";

export type VatInclusionType = "INCLUDED" | "EXCLUDED" | "EXEMPT";
export type EvidenceStatus = "NONE" | "NEEDS_CHECK" | "PRESENT" | "SUBMITTED_TO_ACCOUNTANT";

export interface ExpenseOut {
  id: string;
  expense_date: string;
  category_id: string;
  description: string;
  gross_amount: number;
  supply_amount: number;
  vat_amount: number;
  vat_inclusion_type: VatInclusionType;
  tax_type: string;
  tax_policy_id: string | null;
  is_manual_tax_override: boolean;
  payment_method: string;
  payment_card_holder_id: string | null;
  is_personal_card: boolean;
  is_business_use: boolean;
  evidence_type: string;
  evidence_status: EvidenceStatus;
  is_tax_deductible: boolean;
  is_settlement_deductible: boolean;
  attributable_partner_id: string | null;
  supplier_id: string | null;
  memo: string | null;
  linked_settlement_id: string | null;
  created_at: string;

  /**
   * 조인 필드 — 서버가 이름까지 채워 내려준다.
   * **저장 입력이 아니다.** 이름으로 수정 요청을 보내면 동명이인에서 갈린다.
   */
  category_name: string | null;
  card_holder_name: string | null;
  attributable_partner_name: string | null;
  supplier_name: string | null;
}

export interface ExpenseCreate {
  expense_date: string;
  category_id: string;
  description: string;
  gross_amount: number;
  supply_amount?: number | null;
  vat_amount?: number | null;
  vat_inclusion_type: VatInclusionType;
  tax_type?: string;
  is_manual_tax_override?: boolean;
  payment_method: string;
  payment_card_holder_id?: string | null;
  is_personal_card?: boolean;
  is_business_use?: boolean;
  evidence_type?: string;
  evidence_status?: EvidenceStatus;
  is_tax_deductible?: boolean;
  is_settlement_deductible?: boolean;
  attributable_partner_id?: string | null;
  supplier_id?: string | null;
  memo?: string | null;
}

/**
 * 목록 필터.
 *
 * 백엔드가 실제로 읽는 파라미터만 둔다. 없는 파라미터를 보내면 FastAPI 가
 * 조용히 무시해서 **필터가 걸린 것처럼 보이지만 전체가 나온다** — 사용자는
 * 자기가 지정한 조건이 반영됐다고 믿는다.
 */
export interface ExpenseFilters {
  from?: string;
  to?: string;
  category_id?: string;
  evidence_status?: EvidenceStatus | "";
  q?: string;
  payment_method?: string;
  payment_card_holder_id?: string;
  page?: number;
  page_size?: number;
}

export const expensesApi = {
  list: (filters: ExpenseFilters = {}) =>
    api.get<ListResponse<ExpenseOut>>("/expenses", {
      query: {
        from: filters.from || undefined,
        to: filters.to || undefined,
        category_id: filters.category_id || undefined,
        evidence_status: filters.evidence_status || undefined,
        q: filters.q || undefined,
        payment_method: filters.payment_method || undefined,
        payment_card_holder_id: filters.payment_card_holder_id || undefined,
        page: filters.page ?? 1,
        page_size: filters.page_size ?? 50,
      },
    }),

  get: (id: string) => api.get<ExpenseOut>(`/expenses/${id}`),

  create: (body: ExpenseCreate) => api.post<ExpenseOut>("/expenses", body),

  /**
   * 부분 수정. **승인 대상 필드를 여기로 보내지 않는다** — 403(`APPROVAL_REQUIRED`)이 온다.
   * 승인이 필요한 변경은 `saveExpenseWithApproval` 이 나눠 보낸다.
   */
  update: (id: string, body: Record<string, unknown>) =>
    api.patch<ExpenseOut>(`/expenses/${id}`, body),

  delete: (id: string) => api.delete(`/expenses/${id}`),

  /** 승인 대상 필드 표. 대상 유형별로 내려온다(`expense` 키를 쓴다). */
  fieldOwnership: () =>
    api.get<Record<string, {
      request_types: Record<string, string[]>;
      approval_fields: string[];
      sensitive_fields: string[];
    }>>("/approvals/field-ownership"),
};
