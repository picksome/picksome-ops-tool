/**
 * Sourcing Core API 클라이언트 (M037).
 *
 * 세 가지 원가를 구분한다
 * --------------------
 * ```
 * product_source_cost_version   공급처에서 관측된 사실 (여기)
 * product.cost_amount           지금 계산에 쓰이는 값 (승인으로만 바뀐다)
 * product_margin_actual         이미 팔린 건에 귀속된 원가 (변하지 않는다)
 * ```
 *
 * **소싱 원가를 등록해도 상품 원가는 바뀌지 않는다.** 적용하려면 `COST_CHANGE`
 * 승인을 거친다 — `requestCostAdoption()` 이 그 요청을 만든다.
 *
 * 계산은 서버가 한다
 * ---------------
 * landed cost·권장가·민감도를 프론트에서 다시 계산하지 않는다. 두 곳의 산식이
 * 갈라지는 순간 어느 쪽이 맞는지 사용자가 알 수 없다.
 */

import { api, apiRequest, type ListResponse } from "@/lib/api";
import { approvalsApi } from "./approvals";

export type SupplierType =
  | "WHOLESALE" | "MANUFACTURER" | "OVERSEAS_MARKETPLACE"
  | "RETAIL" | "AGENT" | "OTHER";

export const SUPPLIER_TYPE_LABEL: Record<SupplierType, string> = {
  WHOLESALE: "도매",
  MANUFACTURER: "제조사",
  OVERSEAS_MARKETPLACE: "해외 마켓",
  RETAIL: "소매",
  AGENT: "구매대행",
  OTHER: "기타",
};

export type SupplierStatus = "ACTIVE" | "PAUSED" | "BLOCKED" | "ARCHIVED";

export const SUPPLIER_STATUS_LABEL: Record<SupplierStatus, string> = {
  ACTIVE: "거래중",
  PAUSED: "일시중지",
  BLOCKED: "거래중단",
  ARCHIVED: "보관",
};

/** 어느 국면의 환율인가. 하나의 "현재 환율" 로 뭉개지 않는다. */
export type FxRateType =
  | "SOURCING" | "PRICE_SETTING" | "PURCHASE" | "PAYMENT" | "ACCOUNTING";

export const FX_RATE_TYPE_LABEL: Record<FxRateType, string> = {
  SOURCING: "소싱 검토",
  PRICE_SETTING: "가격 설정",
  PURCHASE: "실제 구매",
  PAYMENT: "대금 지급",
  ACCOUNTING: "회계 인식",
};

export interface SupplierSite {
  id: string;
  supplier_code: string;
  name: string;
  supplier_type: SupplierType;
  country_code: string | null;
  website_url: string | null;
  website_domain: string | null;
  default_currency: string;
  status: SupplierStatus;
  rating: number | null;
  memo?: string | null;
  supports_api: boolean;
  integration_status: string;
  /** 로그인 계정이 있다는 사실만. **비밀번호는 어떤 경로로도 내려오지 않는다.** */
  credential_present: boolean;
  linked_product_count: number;
  last_used_at?: string | null;
}

export interface ProductSource {
  id: string;
  supplier_site_id: string;
  supplier_name: string;
  supplier_code: string;
  country_code: string | null;
  source_url: string | null;
  external_source_product_id?: string | null;
  currency: string;
  moq: number | null;
  lead_time_days: number | null;
  is_primary: boolean;
  status: "ACTIVE" | "PAUSED" | "DISCONTINUED";
  last_checked_at: string | null;
}

export interface CostVersion {
  id: string;
  purchase_price: number | null;
  currency: string;
  valid_from: string;
  /** null = 현재 유효. 반열린 구간 `[valid_from, valid_to)`. */
  valid_to: string | null;
  domestic_shipping: number | null;
  international_shipping: number | null;
  customs_duty: number | null;
  import_vat: number | null;
  packaging_cost: number | null;
  other_cost: number | null;
  source_type: string;
  observed_at: string | null;
  /** true 면 권한이 없어 금액이 지워진 것이다 — "원가 미등록" 이 아니다. */
  redacted?: boolean;
}

export interface LandedCostLine {
  key: string;
  label: string;
  amount: number;
  currency: string;
  amount_krw: number;
  /** false 면 입력하지 않아 0 인 것이다. 실제 0 과 구분한다. */
  provided: boolean;
}

export interface LandedCost {
  total_krw: number | null;
  currency: string;
  breakdown: LandedCostLine[];
  fx: { rate: string; source_type: string; observed_at: string | null } | null;
  /** 비어 있지 않으면 `total_krw` 는 신뢰할 수 없다. */
  missing: string[];
  is_complete: boolean;
}

export interface CostComparison {
  product_id: string;
  product_name: string;
  supplier_name: string;
  /** 지금 계산에 쓰이는 값. */
  current_cost_amount: number | null;
  landed_cost: LandedCost;
  /** 한쪽을 모르면 null — 0 으로 두면 "차이 없음" 으로 읽힌다. */
  difference: number | null;
  difference_rate: number | null;
}

/**
 * 대안 소싱처 한 줄.
 *
 * **순위가 없다 — 의도한 것이다.** 서버는 점수도 추천도 만들지 않는다. MOQ 를
 * 감당할 현금, 리드타임, 품질 이력은 숫자에 안 담기고, 하나로 줄을 세우면 그것들이
 * 사라진다. 화면은 사용자가 고른 기준으로 정렬만 한다.
 */
export interface SourceComparisonRow {
  product_source_id: string;
  supplier_name: string;
  supplier_code: string;
  country_code: string | null;
  currency: string;
  is_primary: boolean;
  status: string;
  moq: number | null;
  lead_time_days: number | null;
  /** 언제 확인한 값인가. 오래된 값과 어제 값을 같게 보이면 안 된다. */
  last_checked_at: string | null;
  landed_cost: LandedCost;
  /** 계산 불가면 null. 0 으로 두면 "차이 없음" 으로 읽힌다. */
  difference: number | null;
  difference_rate: number | null;
}

export interface SourceComparison {
  product_id: string;
  product_name: string;
  current_cost_amount: number | null;
  sources: SourceComparisonRow[];
  /** landed cost 를 실제로 계산할 수 있었던 줄 수. */
  comparable_count: number;
}

export interface RecommendedPrice {
  key: "break_even" | "minimum_recommended" | "recommended" | string;
  target_rate: string;
  recommended_price: number;
  expected_margin_amount: number;
  expected_margin_rate: string;
}

export interface Recommendation {
  available: boolean;
  missing?: string[];
  cost_krw?: number;
  marketplace?: string;
  prices?: RecommendedPrice[];
  landed_cost?: LandedCost | null;
  /** 나중에 "왜 권장가가 달라졌나" 에 답하기 위한 근거. */
  basis?: {
    as_of: string;
    source_cost_version_id: string | null;
    cost_overridden: boolean;
    fx: { rate: string; source_type: string; observed_at: string | null } | null;
    policy_versions: { policy_table: string; policy_row_id: string; version: number }[];
  };
}

export interface SensitivityRow {
  step: string;
  rate?: string;
  cost_krw?: number;
  available: boolean;
  margin_amount?: number;
  margin_rate?: string;
  missing?: string[];
}

export interface Sensitivity {
  fx: { available: boolean; missing?: string[]; currency?: string;
        base_rate?: string; scenarios?: SensitivityRow[] };
  cost: { available: boolean; missing?: string[]; base_cost_krw?: number;
          scenarios?: SensitivityRow[] };
}

export interface FxRate {
  id: string;
  base_currency: string;
  quote_currency: string;
  rate: string;
  rate_type: FxRateType;
  observed_at: string;
  source_type: string;
  source_name: string | null;
}

/** 사람이 읽는 이유. 코드를 그대로 보여주면 무엇을 해야 할지 알 수 없다. */
export const MISSING_LABEL: Record<string, string> = {
  SOURCE_COST: "매입가가 등록되지 않았습니다",
  NOT_FOREIGN_CURRENCY: "원화 소싱이라 환율 영향이 없습니다",
  "POLICY:tax_policy": "세금 정책이 없어 계산할 수 없습니다",
  "POLICY:marketplace_fee_policy": "판매처 수수료 정책이 없습니다",
  "POLICY:payment_fee_policy": "결제 수수료 정책이 없습니다",
};

export function missingLabel(code: string): string {
  if (MISSING_LABEL[code]) return MISSING_LABEL[code];
  if (code.startsWith("FX_RATE:")) {
    return `${code.slice(8)} 환율이 등록되지 않았습니다`;
  }
  return code;
}

export const sourcingApi = {
  listSuppliers: (params: { q?: string; status?: string; limit?: number } = {}) =>
    api.get<ListResponse<SupplierSite>>("/supplier-sites", { query: params }),

  getSupplier: (id: string) => api.get<SupplierSite>(`/supplier-sites/${id}`),

  createSupplier: (body: {
    name: string;
    supplier_type?: SupplierType;
    country_code?: string | null;
    website_url?: string | null;
    default_currency?: string;
    rating?: number | null;
    memo?: string | null;
    credential_present?: boolean;
  }) => api.post<SupplierSite>("/supplier-sites", body),

  updateSupplier: (id: string, body: Record<string, unknown>) =>
    api.patch<SupplierSite>(`/supplier-sites/${id}`, body),

  /** 행을 지우지 않고 정리 표시 — 과거 소싱 기록이 남아야 한다. 사유 필수. */
  archiveSupplier: (id: string, reason: string) =>
    apiRequest<{ archived: boolean }>(`/supplier-sites/${id}`, {
      method: "DELETE",
      body: { reason },
    }),

  archiveSource: (sourceId: string, reason: string) =>
    apiRequest<{ archived: boolean }>(`/product-sources/${sourceId}`, {
      method: "DELETE",
      body: { reason },
    }),

  listSources: (productId: string) =>
    api.get<ListResponse<ProductSource>>(`/products/${productId}/sources`),

  addSource: (productId: string, body: {
    supplier_site_id: string;
    source_url?: string | null;
    external_source_product_id?: string | null;
    currency?: string;
    moq?: number | null;
    lead_time_days?: number | null;
    is_primary?: boolean;
  }) => api.post<ProductSource>(`/products/${productId}/sources`, body),

  updateSource: (sourceId: string, body: Record<string, unknown>) =>
    api.patch<ProductSource>(`/product-sources/${sourceId}`, body),

  /** 주 소싱처 지정. **한 번의 호출이 한 트랜잭션이다** — 내리고 올리기를 나누지 않는다. */
  setPrimary: (sourceId: string) =>
    api.post<ProductSource>(`/product-sources/${sourceId}/primary`, {}),

  costVersions: (sourceId: string) =>
    api.get<ListResponse<CostVersion> & { redacted?: boolean }>(
      `/product-sources/${sourceId}/cost-versions`,
    ),

  /** 새 원가. 기존 version 을 고치지 않고 앞 기간을 닫는다. */
  addCostVersion: (sourceId: string, body: {
    purchase_price: number;
    currency: string;
    valid_from: string;
    domestic_shipping?: number | null;
    international_shipping?: number | null;
    customs_duty?: number | null;
    import_vat?: number | null;
    packaging_cost?: number | null;
    other_cost?: number | null;
    memo?: string | null;
  }) => api.post<CostVersion>(`/product-sources/${sourceId}/cost-versions`, body),

  landedCost: (sourceId: string, at?: string) =>
    api.get<CostComparison>(`/product-sources/${sourceId}/landed-cost`, {
      query: { at },
    }),

  /** 서버가 한 번에 계산한다 — 화면에서 landed cost 를 다시 계산하지 않는다. */
  sourceComparison: (productId: string, at?: string) =>
    api.get<SourceComparison>(`/products/${productId}/source-comparison`, {
      query: { at },
    }),

  recommendation: (sourceId: string, marketplace: string, at?: string) =>
    api.get<Recommendation>(`/product-sources/${sourceId}/recommendation`, {
      query: { marketplace, at },
    }),

  sensitivity: (sourceId: string, marketplace: string, salePrice: number) =>
    api.get<Sensitivity>(`/product-sources/${sourceId}/sensitivity`, {
      query: { marketplace, sale_price: salePrice },
    }),

  listFxRates: (params: { base_currency?: string; rate_type?: FxRateType } = {}) =>
    api.get<ListResponse<FxRate>>("/fx-rates", { query: params }),

  createFxRate: (body: {
    base_currency: string;
    quote_currency?: string;
    rate: string;
    rate_type?: FxRateType;
    observed_at?: string;
    source_name?: string | null;
  }) => api.post<FxRate>("/fx-rates", body),
};

/**
 * 소싱 원가를 상품 원가로 채택 요청.
 *
 * **직접 PATCH 하지 않는다.** `product.cost_amount` 는 가격·마진 계산에 쓰이는
 * 값이고 기존 `COST_CHANGE` 승인 대상이다. 여기서 바로 바꾸면 승인이 장식이 된다.
 *
 * 근거(어느 공급처·어느 원가 version)는 `reason` 에 남긴다 — 승인 payload 는
 * 평면 스칼라만 담고, 승인자는 무엇을 근거로 바뀌는지 알아야 한다.
 */
export function requestCostAdoption(args: {
  productId: string;
  landedCostKrw: number;
  supplierName: string;
  costVersionId: string | null;
  fxRate: string | null;
}) {
  const parts = [`${args.supplierName} 소싱 예상 원가 반영`];
  if (args.costVersionId) parts.push(`원가 version ${args.costVersionId.slice(0, 8)}`);
  if (args.fxRate) parts.push(`환율 ${args.fxRate}`);

  return approvalsApi.create({
    target_type: "product",
    target_id: args.productId,
    request_type: "COST_CHANGE",
    proposed_changes: { cost_amount: args.landedCostKrw },
    reason: parts.join(" · "),
    submit: true,
  });
}
