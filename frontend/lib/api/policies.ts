/**
 * 정책 CRUD API 클라이언트.
 *
 * ⚠️ 이 파일의 이전 버전은 `/policies/marketplace-fees`, `/policies/tax` 를 불렀다.
 * 백엔드 라우터는 `/policies/{kind}` 이고 kind 는 snake_case(`marketplace_fee`)다.
 * **그 경로는 어디에도 존재하지 않았다.** 화면에서 쓰이지 않아 아무도 몰랐을 뿐이다
 * (D2 와 같은 종류의 결함 — 경로는 문자열이라 tsc·lint·build 가 잡지 못한다).
 * 그래서 실제 라우터에 맞춰 다시 썼고, 경로를 테스트로 고정했다.
 *
 * 정책은 버전과 유효기간을 갖는다
 * ---------------------------
 * 새 정책을 저장해도 과거 계산 결과는 바뀌지 않는다. 정책마다
 * `version` + `effective_from`/`effective_to` 가 있고, 확정된 배분·마감은
 * 그 시점 버전을 이미 박제해 두었다.
 *
 * 수명주기: 생성(DRAFT) → 승인(is_active=true) → 폐기(effective_to 종료)
 * **생성만으로는 적용되지 않는다.** 승인해야 계산에 쓰인다.
 */

import { api, type ListResponse } from "@/lib/api";

/** 백엔드 `PolicyKind` (app/domain/policy_crud.py) 와 **정확히** 같아야 한다. */
export type PolicyKind =
  | "marketplace_fee"
  | "payment_fee"
  | "tax"
  | "fulfillment"
  | "return_risk"
  | "ad_cost"
  | "target_margin"
  | "settlement"
  | "partner_share"
  | "expense_category"
  | "evidence_type";

export const POLICY_KIND_LABEL: Record<PolicyKind, string> = {
  marketplace_fee: "마켓 수수료",
  payment_fee: "결제 수수료",
  tax: "세금 정책",
  fulfillment: "풀필먼트 수수료",
  return_risk: "반품 위험비율",
  ad_cost: "광고비 비율",
  target_margin: "목표 마진율",
  settlement: "정산 정책",
  partner_share: "파트너 배분 비율",
  expense_category: "비용 카테고리",
  evidence_type: "증빙 유형",
};

export type PolicyReliabilityStatus =
  | "CONFIRMED"
  | "AUTO_DETECTED"
  | "NEEDS_REVIEW"
  | "NEEDS_VERIFICATION"
  | "EXPIRED"
  | "DEPRECATED";

export interface Policy {
  id: string;
  version: number;
  effective_from: string;
  effective_to: string | null;
  source_type: string;
  source_url: string | null;
  reliability_status: PolicyReliabilityStatus;
  memo: string | null;
  /** 승인 전에는 false. **false 면 계산에 쓰이지 않는다.** */
  is_active: boolean;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  /** 종류별 컬럼. 종류마다 키가 다르다 (백엔드 ALLOWED_COLUMNS). */
  fields: Record<string, unknown>;
}

export interface PolicyCreate {
  version: number;
  effective_from: string;
  effective_to?: string | null;
  source_type?: string;
  source_url?: string | null;
  reliability_status?: PolicyReliabilityStatus;
  memo?: string | null;
  fields: Record<string, unknown>;
}

export interface PolicyUpdate {
  effective_from?: string;
  effective_to?: string | null;
  reliability_status?: PolicyReliabilityStatus;
  memo?: string | null;
  fields?: Record<string, unknown>;
}

/* ── 종류별 fields 모양 ───────────────────────────────────────
 * 백엔드 `ALLOWED_COLUMNS` 와 짝을 이룬다.
 * **여기 없는 키는 서버가 거부한다** — 화면이 지원하지 않는 개념을
 * 입력받아 조용히 버리는 일이 없게 하려는 것이다.
 * ──────────────────────────────────────────────────────────── */

/** `partner_share` — 파트너별 잔여이익 분배 비율. */
export interface PartnerShareFields {
  partner_id: string;
  /** 0~1. 활성 정책의 합이 1.0 이어야 한다(백엔드가 강제). */
  share_ratio: number | string;
  /** 특정 월에만 적용할 때. null 이면 기본 규칙. */
  applies_year_month: string | null;
}

/** `settlement` — 우선 분배와 적자 정책. */
export interface SettlementPolicyFields {
  loss_policy: string | null;
  /** [{ partner_id, fixed_amount }] — 비율 분배 **전에** 정액으로 먼저 준다. */
  priority_distribution_json: Array<{ partner_id: string; fixed_amount: number }> | null;
  close_day: number | null;
}

export const policiesApi = {
  list: (kind: PolicyKind, params: { only_active?: boolean; limit?: number; offset?: number } = {}) =>
    api.get<ListResponse<Policy>>(`/policies/${kind}`, { query: params }),

  /** 기준일에 유효한 정책만. 계산이 실제로 쓰는 집합이다. */
  active: (kind: PolicyKind, at?: string) =>
    api.get<ListResponse<Policy>>(`/policies/${kind}/active`, { query: { at } }),

  get: (kind: PolicyKind, id: string) => api.get<Policy>(`/policies/${kind}/${id}`),

  /** 생성. **DRAFT 다** — `approve` 를 부르기 전까지 계산에 쓰이지 않는다. */
  create: (kind: PolicyKind, body: PolicyCreate) =>
    api.post<Policy>(`/policies/${kind}`, body),

  update: (kind: PolicyKind, id: string, body: PolicyUpdate) =>
    api.patch<Policy>(`/policies/${kind}/${id}`, body),

  approve: (kind: PolicyKind, id: string, note?: string) =>
    api.post<Policy>(`/policies/${kind}/${id}/approve`, { note: note ?? null }),

  /** 폐기. 행을 지우지 않고 `effective_to` 로 끝낸다 — 과거 계산 근거가 남아야 한다. */
  deprecate: (kind: PolicyKind, id: string, reason: string, effectiveTo?: string) =>
    api.post<Policy>(`/policies/${kind}/${id}/deprecate`, {
      reason,
      effective_to: effectiveTo ?? null,
    }),

  history: (kind: PolicyKind, id: string) =>
    api.get<ListResponse<{ changed_at: string; changed_by: string | null; before: unknown; after: unknown }>>(
      `/policies/${kind}/${id}/history`,
    ),
};
