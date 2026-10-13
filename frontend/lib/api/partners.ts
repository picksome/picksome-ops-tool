/**
 * 파트너 · 비용 카테고리 조회.
 *
 * 비용 화면이 UUID 를 사람 이름으로 바꾸려면 두 목록이 필요하다.
 * 둘 다 **읽기 전용**이다 — 파트너 추가는 수익배분 비율과 함께 다뤄야 하고,
 * 카테고리 추가는 정책 화면(`/policies`)이 담당한다.
 */

import { api, type ListResponse } from "@/lib/api";

export interface Partner {
  id: string;
  name: string;
  display_order: number;
  is_active: boolean;
  linked_user_id: string | null;
}

export const partnersApi = {
  /**
   * `includeInactive` 는 과거 비용을 볼 때 필요하다 — 비활성 파트너가 결제한
   * 작년 비용의 결제자 이름이 사라지면 목록이 "미지정" 으로 보인다.
   */
  list: (includeInactive = false) =>
    api.get<Partner[]>("/partners", {
      query: { include_inactive: includeInactive || undefined },
    }),
};

/** `/policies/{kind}` 공통 응답의 한 행. 종류별 값은 `fields` 안에 있다. */
interface PolicyRow {
  id: string;
  is_active: boolean;
  effective_from: string;
  fields: Record<string, unknown>;
}

export interface ExpenseCategoryOption {
  id: string;
  name: string;
  /** 세무 공제 기본값 — 비용 등록 시 초기값으로 쓴다 */
  defaultTaxDeductible: boolean;
  defaultSettlementDeductible: boolean;
}

export const expenseCategoriesApi = {
  /**
   * 비용 카테고리는 **정책 테이블**이다(`expense_category_policy`).
   * 별도 CRUD 엔드포인트가 아니라 정책 라우터를 쓴다 — 하드코딩 금지 원칙에 따라
   * 카테고리도 effective_from/version 을 갖는 정책이기 때문이다.
   */
  list: async (): Promise<ExpenseCategoryOption[]> => {
    const res = await api.get<{ items: PolicyRow[] }>("/policies/expense_category", {
      query: { only_active: true, limit: 200 },
    });
    return (res.items ?? []).map((p) => ({
      id: p.id,
      name: String(p.fields.name ?? ""),
      defaultTaxDeductible: p.fields.default_tax_deductible !== false,
      defaultSettlementDeductible: p.fields.default_settlement_deductible !== false,
    }));
  },
};

/* ────────────────────────────────────────────────────────────
 * 거래처 (external_partner)
 *
 * **`partner` 와 다른 테이블이다.** `partner` 는 수익을 배분받는 내부 파트너고,
 * 거래처는 **돈을 지불하는 상대**(매입처·물류사·광고대행사)다.
 * `expenses.supplier_id` 와 `product.supplier_id` 가 여기를 가리킨다.
 * ──────────────────────────────────────────────────────────── */

export interface ExternalPartner {
  id: string;
  display_name: string;
  type: string | null;
  is_active: boolean;
  memo: string | null;
}

export interface ExternalPartnerListMeta {
  /** 서버가 알려준다 — 생성 API 가 없다는 사실을 화면이 각자 판단하지 않는다 */
  can_create: boolean;
}

export const externalPartnersApi = {
  list: (params: { q?: string; type?: string; active?: boolean; page?: number; page_size?: number } = {}) =>
    api.get<ListResponse<ExternalPartner> & ExternalPartnerListMeta>("/external-partners", {
      query: {
        q: params.q || undefined,
        type: params.type || undefined,
        active: params.active,
        page: params.page ?? 1,
        page_size: params.page_size ?? 50,
      },
    }),

  /** 단건 — **삭제된 거래처도 돌려준다.** 과거 비용의 이름이 사라지면 안 된다. */
  get: (id: string) =>
    api.get<ExternalPartner & { is_deleted: boolean }>(`/external-partners/${id}`),
};
