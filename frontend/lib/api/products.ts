import { api, type ListResponse } from "@/lib/api";

export type ProductStatus = "CANDIDATE" | "APPROVED" | "ACTIVE" | "PAUSED" | "DISCONTINUED";
export type MarginType = "PURCHASE_AGENCY" | "ROCKET_GROWTH" | "OVERSEAS_DIRECT" | "GENERAL_RESELL";

export interface Product {
  id: string;
  business_key: string;
  name_ko: string;
  status: ProductStatus;
  margin_type: MarginType;
  cost_amount: number | null;
  cost_currency: string | null;
  cost_fx_rate: number | null;
  cost_amount_foreign: number | null;
  has_variants: boolean;
  created_at: string;
}

export type ProductListResponse = ListResponse<Product>;

/* ─── 옵션 그룹 / 값 ─── */

export interface OptionValue {
  id: string;
  value: string;
  sort_order: number;
}

/**
 * 옵션 그룹.
 *
 * **값(`values`)이 함께 오지 않는다.** 백엔드 `OptionGroupOut` 은 그룹 자체만 준다.
 * 값은 `GET /products/option-groups/{id}/values` 로 따로 받는다.
 * (예전 타입은 `values: OptionValue[]` 를 선언해 두어, 화면이 `undefined` 를 map 하다 터졌다.)
 */
export interface OptionGroup {
  id: string;
  product_id: string;
  name: string;
  sort_order: number;
}

/* ─── Variant ─── */

export interface OptionGroupWithValues extends OptionGroup {
  values: OptionValue[];
}

export interface ProductVariant {
  id: string;
  product_id: string;
  /** 조합 식별자. 백엔드가 요구하는 필수 값이다 */
  variant_key: string;
  option_values_json: Record<string, string>; // {"색상":"빨강","사이즈":"M"}
  sku_external: string | null;
  cost_amount: number | null;
  sale_price: number | null;
  extra_price: number;
  /** 백엔드 컬럼명은 `stock_quantity` 다. `stock` 이 아니다 */
  stock_quantity: number | null;
  is_active: boolean;
}

/** `PATCH /products/variants/{id}` 가 받는 필드. 여기 없는 건 서버가 무시한다. */
export type VariantPatch = Partial<
  Pick<ProductVariant,
    "variant_key" | "option_values_json" | "sku_external" |
    "cost_amount" | "sale_price" | "extra_price" | "stock_quantity" | "is_active">
>;

export interface VariantCreateBody extends VariantPatch {
  variant_key: string;
  option_values_json: Record<string, string>;
}

/**
 * 상세 조회 응답 — 목록보다 필드가 많다.
 *
 * `version_token` 과 `open_approvals` 는 상세에서만 내려온다. 목록에 실으면
 * 응답이 커지는 데다, 저장하려면 어차피 상세를 한 번 읽어야 한다.
 */
export interface ProductDetailResponse extends Product {
  sku: string | null;
  name_en: string | null;
  category_path: string | null;
  supplier_id: string | null;
  supply_url: string | null;
  memo: string | null;
  updated_at: string | null;

  /**
   * 낙관적 동시성 토큰. 저장할 때 그대로 되돌려준다.
   * 그 사이 다른 사람이 저장했으면 `PRODUCT_VERSION_CONFLICT`(409) 로 거절된다.
   */
  version_token: string | null;

  /** 이 상품에 걸려 있는 진행 중 승인 요청 요약 */
  open_approvals: OpenApprovalSummary[];
}

export interface OpenApprovalSummary {
  id: string;
  business_key: string;
  request_type: string;
  status: "DRAFT" | "PENDING" | "APPROVED";
  requested_at: string;
  requested_by: string;
  effective_from: string | null;
  auto_apply_status: string | null;
}

/** `GET /approvals/field-ownership` 의 대상별 항목. 화면이 표를 하드코딩하지 않게 서버가 준다. */
export interface FieldOwnership {
  /** request_type → 그 유형이 소유한 BE 필드명 */
  request_types: Record<string, string[]>;
  /** 승인이 필요한 전체 필드 */
  approval_fields: string[];
  /** 값이 가려질 수 있는 필드 */
  sensitive_fields: string[];
}

export interface ProductCreate {
  name_ko: string;
  margin_type: MarginType;
  status?: ProductStatus;
  cost_amount?: number | null;
  cost_currency?: string | null;
  cost_fx_rate?: number | null;
  cost_amount_foreign?: number | null;
}

export const productsApi = {
  list: (params?: { page?: number; page_size?: number; q?: string; status?: ProductStatus; margin_type?: MarginType }) =>
    api.get<ProductListResponse>("/products", {
      query: params as Record<string, string | number | undefined>,
    }),

  get: (id: string) => api.get<Product>(`/products/${id}`),

  /** 상세 — version token 과 진행 중 승인 요청까지 포함한다. */
  detail: (id: string) => api.get<ProductDetailResponse>(`/products/${id}`),

  create: (body: ProductCreate) => api.post<Product>("/products", body),

  update: (id: string, body: Partial<ProductCreate>) =>
    api.patch<Product>(`/products/${id}`, body),

  /**
   * 임의 필드 직접 수정. **승인 대상 필드를 여기로 보내지 않는다** — 보내면 403 이 온다.
   *
   * `update()` 와 나눠 둔 이유: `update` 는 `ProductCreate` 부분집합만 받는 좁은 타입이라
   * `memo`·`supply_url` 같은 필드를 못 보낸다. 그렇다고 타입을 넓히면 기존 호출부에서
   * 오타를 잡아주지 못한다. 어댑터가 만든 변경분을 그대로 보내는 경로만 따로 둔다.
   *
   * `expectedVersion` 은 상세에서 받은 토큰. 그 사이 누가 저장했으면 409
   * (`PRODUCT_VERSION_CONFLICT`) 가 오고, 사용자가 남의 수정을 덮어쓰지 않는다.
   */
  patchFields: (
    id: string,
    patch: Record<string, unknown>,
    expectedVersion?: string | null,
  ) =>
    api.patch<ProductDetailResponse>(`/products/${id}`, {
      ...patch,
      ...(expectedVersion ? { expected_version: expectedVersion } : {}),
    }),

  /** 승인 대상 필드 표. 대상 유형별로 내려온다. */
  fieldOwnership: () =>
    api.get<Record<string, FieldOwnership>>("/approvals/field-ownership"),

  delete: (id: string) => api.delete(`/products/${id}`),

  /* ─── 옵션 그룹 ─── */
  getOptionGroups: (productId: string) =>
    api.get<OptionGroup[]>(`/products/${productId}/option-groups`),

  getOptionValues: (groupId: string) =>
    api.get<OptionValue[]>(`/products/option-groups/${groupId}/values`),

  /**
   * 그룹 + 값을 한 번에. 화면은 거의 항상 둘 다 필요하다.
   *
   * 서버가 nested 로 안 주기 때문에 여기서 합친다 — 화면마다 각자 합치면
   * 어떤 화면은 값을 빠뜨린다(실제로 그래서 `undefined.map()` 이 났다).
   */
  getOptionGroupsWithValues: async (productId: string): Promise<OptionGroupWithValues[]> => {
    const groups = await api.get<OptionGroup[]>(`/products/${productId}/option-groups`);
    return Promise.all(
      groups.map(async (g) => ({
        ...g,
        values: await api.get<OptionValue[]>(`/products/option-groups/${g.id}/values`),
      })),
    );
  },

  createOptionGroup: (productId: string, body: { name: string; sort_order?: number }) =>
    api.post<OptionGroup>(`/products/${productId}/option-groups`, body),

  // 경로에 productId 가 없다 — 그룹 id 만으로 찾는다(백엔드 라우트 기준).
  // productId 를 끼워 넣으면 404 다. 인자로는 남겨두지 않는다 — 안 쓰는 인자를
  // 받으면 호출부가 "이게 어딘가 쓰이겠지" 하고 잘못된 값을 넘긴다.
  updateOptionGroup: (gid: string, body: { name?: string; sort_order?: number }) =>
    api.patch<OptionGroup>(`/products/option-groups/${gid}`, body),

  deleteOptionGroup: (gid: string) => api.delete(`/products/option-groups/${gid}`),

  /* ─── 옵션 값 ─── */
  createOptionValue: (gid: string, body: { value: string; sort_order?: number }) =>
    api.post<OptionValue>(`/products/option-groups/${gid}/values`, body),

  updateOptionValue: (vid: string, body: { value?: string; sort_order?: number }) =>
    api.patch<OptionValue>(`/products/option-values/${vid}`, body),

  deleteOptionValue: (vid: string) => api.delete(`/products/option-values/${vid}`),

  /* ─── Variant ─── */
  getVariants: (productId: string) =>
    api.get<ProductVariant[]>(`/products/${productId}/variants`),

  updateVariant: (vid: string, body: VariantPatch) =>
    api.patch<ProductVariant>(`/products/variants/${vid}`, body),

  /**
   * 물리 삭제가 아니라 soft delete 다. 판매 이력이 있으면 백엔드가
   * `VARIANT_IN_USE`(409) 로 거절하고 비활성화를 권한다 — 과거 주문이 가리키던
   * 옵션이 사라지면 그 주문의 손익을 다시 계산할 수 없다.
   */
  deleteVariant: (vid: string) => api.delete(`/products/variants/${vid}`),

  createVariant: (productId: string, body: VariantCreateBody) =>
    api.post<ProductVariant>(`/products/${productId}/variants`, body),
};


/* ────────────────────────────────────────────────────────────
 * 변경 분류
 *
 * 상품 하나를 고치면 두 종류의 변경이 섞여 나온다.
 *   · 바로 저장되는 것 (메모, SKU …)
 *   · 승인을 받아야 하는 것 (원가, 상품명, 상태 …)
 *
 * 한 번의 저장 버튼으로 둘 다 처리하되, **무엇이 어떻게 처리되는지** 사용자에게
 * 미리 보여줘야 한다. 그래서 분류를 순수 함수로 떼어 놓는다 — 렌더링과 무관하고
 * 단위 테스트가 가능하다.
 * ──────────────────────────────────────────────────────────── */

export interface ClassifiedProductChanges {
  /** `PATCH /products/{id}` 로 바로 저장할 필드 */
  direct: Record<string, unknown>;
  /** request_type → 그 유형으로 승인 요청할 필드 */
  approval: Record<string, Record<string, unknown>>;
  /** 승인 대상인데 어느 유형에도 속하지 않은 필드 (있으면 서버 설정 오류) */
  unroutable: string[];
  hasDirect: boolean;
  hasApproval: boolean;
}

/**
 * 변경분을 "바로 저장" 과 "승인 필요" 로 나눈다.
 *
 * `ownership` 이 없으면(표 로딩 실패) **전부 직접 저장으로 보낸다.**
 * 추측으로 승인 요청을 만들면 사용자가 의도하지 않은 요청이 승인함에 쌓인다.
 * 직접 저장으로 보내면 백엔드가 `APPROVAL_REQUIRED`(403)로 정확히 거절한다 —
 * 방어선은 서버지 이 함수가 아니다.
 */
export function classifyProductChanges(
  changes: Record<string, unknown>,
  ownership: FieldOwnership | null,
): ClassifiedProductChanges {
  const direct: Record<string, unknown> = {};
  const approval: Record<string, Record<string, unknown>> = {};
  const unroutable: string[] = [];

  const approvalFields = new Set(ownership?.approval_fields ?? []);
  const requestTypes = ownership?.request_types ?? {};

  for (const [field, value] of Object.entries(changes)) {
    if (!approvalFields.has(field)) {
      direct[field] = value;
      continue;
    }
    const requestType = Object.keys(requestTypes).find((rt) =>
      requestTypes[rt].includes(field),
    );
    if (!requestType) {
      // 승인 대상인데 소유 유형이 없다 — 어느 요청으로 보낼지 알 수 없다.
      // 직접 저장으로 흘리면 403 이 나므로, 화면이 오류를 보여줄 수 있게 모아둔다.
      unroutable.push(field);
      continue;
    }
    (approval[requestType] ??= {})[field] = value;
  }

  return {
    direct,
    approval,
    unroutable,
    hasDirect: Object.keys(direct).length > 0,
    hasApproval: Object.keys(approval).length > 0,
  };
}

/** `""` 과 `null` 은 사용자 입장에서 같은 "빈 값". 구분하면 헛 diff 가 생긴다. */
function normalizeForDiff(v: unknown): unknown {
  if (v === "" || v === undefined) return null;
  if (typeof v === "string") return v.trim() === "" ? null : v;
  return v;
}

/**
 * 바뀐 것만 남긴다. 안 바뀐 필드까지 보내면 승인 diff 가 부풀어
 * 승인자가 무엇이 실제로 달라졌는지 못 찾는다.
 *
 * `undefined` 는 "안 건드림", `null` 은 "비움" 이다. 둘을 같게 다루면 값을 지울 수 없다.
 */
export function diffAgainst(
  base: Record<string, unknown>,
  next: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(next)) {
    if (v === undefined) continue;
    if (!Object.is(normalizeForDiff(base[k]), normalizeForDiff(v))) out[k] = v;
  }
  return out;
}
