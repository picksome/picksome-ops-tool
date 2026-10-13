/**
 * 데이터 어댑터.
 *
 * 화면은 **여기 정의된 인터페이스만** 안다. 뒤에 실제 BE 가 있는지 mock 이 있는지 모른다.
 * BE 엔드포인트가 생기면 이 파일의 구현만 바꾸면 되고 화면 코드는 그대로다.
 *
 * 세 가지 상태가 있다:
 *   ① `live`     — BE 엔드포인트가 있고 그대로 쓴다 (상품/판매/사업비용 목록 등)
 *   ② `fallback` — BE 를 먼저 부르고, 실패하거나 비어 있으면 mock 으로 대체한다
 *   ③ `mock`     — BE 가 아직 없다. 전부 mock (수익배분 규칙, 연동, 알림 규칙 등)
 *
 * 각 함수의 doc 주석에 상태를 표시했다. `docs/41_redesign_worklog.md` 의
 * "mock 처리된 API" 표와 짝을 이룬다.
 */

import { api, ApiError } from "@/lib/api";
import { approvalsApi, type ApprovalRequest } from "@/lib/api/approvals";
import {
  classifyProductChanges,
  diffAgainst,
  productsApi,
  type FieldOwnership,
  type ProductDetailResponse,
  type ProductVariant,
} from "@/lib/api/products";
import type {
  DistributionResult,
  ListParams,
  MarketplaceSettlementRow,
  Paged,
  ProductDetail,
  ProductOption,
  ProductOptionGroup,
  ProductRow,
  SaleRow,
} from "@/lib/adapters/types";
import {
  MOCK_PRODUCTS,
  MOCK_PRODUCT_DETAIL,
  MOCK_SETTLEMENTS,
} from "@/lib/adapters/mock-data";

/* ────────────────────────────────────────────────────────────
 * 공통 헬퍼
 * ──────────────────────────────────────────────────────────── */

/**
 * mock fallback 을 켤지.
 *
 * **운영에서는 절대 켜지지 않는다.** 플래그를 실수로 켠 채 배포해도
 * `NODE_ENV === "production"` 이면 무시한다 — 운영에서 BE 장애가 mock 으로 가려지면
 * 사용자는 없는 데이터를 편집하고 저장이 안 되는 이유를 알 수 없다.
 */
function mockFallbackEnabled(): boolean {
  return (
    process.env.NODE_ENV !== "production" &&
    process.env.NEXT_PUBLIC_ENABLE_MOCK_FALLBACK === "true"
  );
}

/** mock 응답에 약간의 지연을 준다 — skeleton/로딩 상태가 실제로 보이는지 검증하기 위해. */
function delay<T>(value: T, ms = 180): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

/**
 * BE 를 먼저 부르고 실패하면 mock 으로 떨어진다.
 *
 * 401(미인증)까지 mock 으로 가리면 로그인 문제를 못 보므로 그대로 던진다.
 * 그 외(404/500/네트워크)는 "아직 BE 가 없거나 죽었다"로 보고 mock 을 쓴다 —
 * 개편 중에는 BE 가 따라오지 않은 화면이 많고, 화면 검증이 막히면 안 된다.
 */
async function withFallback<T>(
  fetcher: () => Promise<T>,
  fallback: () => T | Promise<T>,
  label: string,
): Promise<T & { __mock?: boolean }> {
  try {
    return (await fetcher()) as T & { __mock?: boolean };
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) throw err;
    if (process.env.NODE_ENV !== "production") {
      console.info(`[adapter] ${label}: BE 응답 없음 → mock 사용`, err);
    }
    const value = await fallback();
    return value as T & { __mock?: boolean };
  }
}

/** 문자열 토큰 AND 매칭 — BE `services/search.py` 와 같은 규칙. */
function matchTokens(haystack: Array<string | null | undefined>, query: string): boolean {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8);
  if (tokens.length === 0) return true;
  const joined = haystack.filter(Boolean).join(" ").toLowerCase();
  return tokens.every((t) => joined.includes(t));
}

function paginate<T>(rows: T[], params: ListParams): Paged<T> {
  const page = params.page ?? 1;
  const size = params.pageSize ?? 50;
  const start = (page - 1) * size;
  return { items: rows.slice(start, start + size), total: rows.length };
}

function sortRows<T>(rows: T[], sort: string | undefined, dir: "asc" | "desc" | undefined): T[] {
  if (!sort) return rows;
  const factor = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = (a as Record<string, unknown>)[sort];
    const bv = (b as Record<string, unknown>)[sort];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * factor;
    return String(av).localeCompare(String(bv), "ko") * factor;
  });
}

/* ────────────────────────────────────────────────────────────
 * 상품  — ② fallback
 * ──────────────────────────────────────────────────────────── */

interface BeProduct {
  id: string;
  business_key: string;
  name_ko: string;
  sku: string | null;
  category_path: string | null;
  status: ProductRow["status"];
  margin_type: ProductRow["marginType"];
  supply_url: string | null;
  cost_amount: number | null;
  has_variants: boolean;
  memo: string | null;
  created_at: string;
}

/** BE 상품 → 화면 모델. BE 에 없는 필드는 null 로 둔다(거짓 값을 지어내지 않는다). */
function mapBeProduct(p: BeProduct): ProductRow {
  return {
    id: p.id,
    businessKey: p.business_key,
    name: p.name_ko,
    sku: p.sku,
    imageUrl: null,
    status: p.status,
    marginType: p.margin_type,
    category: p.category_path,
    marketplaces: [],
    marketplaceProductNos: {},
    sourceUrl: p.supply_url,
    supplierName: null,
    costEffectiveFrom: null,
    priceEffectiveFrom: null,
    totalCost: p.cost_amount,
    salePrice: null,
    expectedSettlement: null,
    expectedNetProfit: null,
    marginRate: null,
    optionCount: p.has_variants ? 1 : 0,
    ownerId: null,
    ownerName: null,
    memo: p.memo,
    createdAt: p.created_at,
    updatedAt: p.created_at,
  };
}

/**
 * DB 에 아직 컬럼이 없어 화면에서 0 으로 보이는 원가 세부 항목.
 *
 * `docs/46_sale_price_schema_design.md` 의 판매가·원가 스키마 결정이 끝나야 저장할 수 있다.
 * 0 을 진짜 값처럼 보여주면 사용자가 **관세가 0원이라고 믿고** 판매가를 정한다.
 */
const UNSUPPORTED_PRODUCT_FIELDS = [
  "domesticShippingAbroad", "internationalShipping", "customsDuty", "importVat",
  "domesticShipping", "packagingCost", "otherCost", "costEffectiveFrom",
  "salePrice", "priceEffectiveFrom", "ownerId", "channels",
] as const;

/** 화면 `ProductDetail` ← BE 상세. **없는 값은 지어내지 않는다.** */
function mapBeProductDetail(p: ProductDetailResponse): ProductDetail {
  const row = mapBeProduct(p as unknown as BeProduct);
  const fxRate = p.cost_fx_rate === null ? 1 : Number(p.cost_fx_rate);
  return {
    ...row,
    name: p.name_ko,
    updatedAt: p.updated_at ?? p.created_at,
    versionToken: p.version_token,
    openApprovals: (p.open_approvals ?? []).map((a) => ({
      id: a.id,
      businessKey: a.business_key,
      requestType: a.request_type,
      status: a.status,
      requestedAt: a.requested_at,
      effectiveFrom: a.effective_from,
    })),
    unsupportedFields: [...UNSUPPORTED_PRODUCT_FIELDS],
    sourcing: {
      sourceUrl: p.supply_url,
      supplierName: null, // supplier_id 는 있으나 이름 조인은 BE 미제공
      purchaseCurrency: p.cost_currency ?? "KRW",
      // 해외통화 매입가가 있으면 그 값, 없으면 원화 원가가 곧 매입가다.
      purchasePrice: p.cost_amount_foreign ?? p.cost_amount ?? 0,
      exchangeRate: Number.isFinite(fxRate) && fxRate > 0 ? fxRate : 1,
      domesticShippingAbroad: 0,
      internationalShipping: 0,
      customsDuty: 0,
      importVat: 0,
      domesticShipping: 0,
      packagingCost: 0,
      otherCost: 0,
      costEffectiveFrom: null,
      sourceCheckedAt: null,
    },
    pricing: {
      salePrice: 0,
      buyerShippingFee: 0,
      priceEffectiveFrom: null,
      commissionRate: 0,
      adCostRate: 0,
      returnRate: 0,
      fixedCostAllocation: 0,
      targetMarginRate: 0,
    },
    channels: [],
    options: [],
    history: [],
  };
}

/** 화면 필드명 → BE 컬럼명. **여기 없는 필드는 서버로 보내지 않는다.** */
export function toBeProductFields(d: Partial<ProductDetail>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const put = (key: string, value: unknown) => {
    if (value !== undefined) out[key] = value;
  };
  put("name_ko", d.name);
  put("sku", d.sku);
  put("category_path", d.category);
  put("status", d.status);
  put("margin_type", d.marginType);
  put("memo", d.memo);
  put("supply_url", d.sourcing?.sourceUrl ?? d.sourceUrl);
  if (d.sourcing) {
    put("cost_currency", d.sourcing.purchaseCurrency);
    put("cost_fx_rate", d.sourcing.exchangeRate);
    // 원화 매입이면 `cost_amount`, 해외통화면 `cost_amount_foreign` 이다.
    // 둘 다 보내면 BE 가 환율로 재계산해 사용자가 입력한 원화값을 덮어쓴다.
    if ((d.sourcing.purchaseCurrency ?? "KRW") === "KRW") {
      put("cost_amount", d.sourcing.purchasePrice);
    } else {
      put("cost_amount_foreign", d.sourcing.purchasePrice);
    }
  }
  return out;
}

export interface ProductSaveOptions {
  /** 저장 전 원본. 변경분만 골라내는 기준이자 version token 의 출처. */
  base?: ProductDetail | null;
  /** 서버가 준 승인 대상 필드 표. **없으면 저장하지 않는다.** */
  ownership?: FieldOwnership | null;
  reason?: string;
  effectiveFrom?: string;
  /** 승인 요청을 바로 상신할지. false 면 DRAFT 로 남는다. */
  submit?: boolean;
}

export interface ProductSaveResult {
  detail: ProductDetail;
  /** 이번 저장으로 생성된 승인 요청 */
  approvals: ApprovalRequest[];
  /** 직접 저장된 필드가 있었는가 */
  directSaved: boolean;
}

export interface ProductListParams extends ListParams {
  status?: string;
  marketplace?: string;
  ownerId?: string;
  marginMin?: string;
  marginMax?: string;
  dateField?: "createdAt" | "costEffectiveFrom" | "priceEffectiveFrom";
}

function filterMockProducts(params: ProductListParams): ProductRow[] {
  let rows = [...MOCK_PRODUCTS];
  if (params.q) {
    rows = rows.filter((p) =>
      matchTokens(
        [p.name, p.sku, p.businessKey, p.sourceUrl, p.supplierName, p.memo,
         ...Object.values(p.marketplaceProductNos)],
        params.q!,
      ),
    );
  }
  if (params.status) rows = rows.filter((p) => p.status === params.status);
  if (params.marketplace) rows = rows.filter((p) => p.marketplaces.includes(params.marketplace!));
  if (params.ownerId) rows = rows.filter((p) => p.ownerId === params.ownerId);
  if (params.marginMin) {
    const min = Number(params.marginMin) / 100;
    rows = rows.filter((p) => (p.marginRate ?? -Infinity) >= min);
  }
  if (params.marginMax) {
    const max = Number(params.marginMax) / 100;
    rows = rows.filter((p) => (p.marginRate ?? Infinity) <= max);
  }
  const field = params.dateField ?? "createdAt";
  if (params.from) rows = rows.filter((p) => (p[field] ?? "") >= params.from!);
  if (params.to) rows = rows.filter((p) => (p[field] ?? "") <= params.to! + "￿");
  return sortRows(rows, params.sort, params.dir);
}

export const productAdapter = {
  /**
   * ① live — `GET /products`.
   *
   * mock 은 개발 편의 플래그가 켜진 개발 환경에서만 쓴다. 운영에서 BE 장애를
   * mock 목록으로 덮으면 사용자는 실제로 존재하지 않는 상품을 보게 된다.
   */
  async list(params: ProductListParams): Promise<Paged<ProductRow>> {
    const fetchLive = async () => {
      const res = await api.get<{ items: BeProduct[]; total: number }>("/products", {
        query: {
          q: params.q || undefined,
          status: params.status || undefined,
          page: params.page ?? 1,
          page_size: params.pageSize ?? 50,
        },
      });
      if (!res?.items) throw new Error("unexpected shape");
      return { items: res.items.map(mapBeProduct), total: res.total };
    };
    if (!mockFallbackEnabled()) return fetchLive();
    return withFallback(
      fetchLive,
      () => delay(paginate(filterMockProducts(params), params)),
      "products.list",
    );
  },

  /**
   * ① live — `GET /products/{id}`.
   *
   * 읽기 실패는 개발 환경에서만 mock 으로 떨어진다(`NEXT_PUBLIC_ENABLE_MOCK_FALLBACK`).
   * 운영에서는 오류를 그대로 올린다 — 없는 상품을 mock 으로 보여주면
   * 사용자가 존재하지 않는 데이터를 편집하게 된다.
   */
  async detail(id: string): Promise<ProductDetail | null> {
    if (!mockFallbackEnabled()) {
      return mapBeProductDetail(await productsApi.detail(id));
    }
    return withFallback(
      async () => mapBeProductDetail(await productsApi.detail(id)),
      () => delay(MOCK_PRODUCT_DETAIL[id] ?? null),
      "products.detail",
    );
  },

  /** 서버가 알려주는 승인 대상 필드 표. 화면이 하드코딩하지 않는다. */
  async fieldOwnership(): Promise<FieldOwnership> {
    // **오류를 삼키지 않는다.** 이전에는 실패하면 null 을 돌려주고 모든 변경을
    // 직접 저장으로 보냈다. 그건 네트워크 장애를 "승인이 필요 없다" 로 해석한 것이다.
    // 서버가 403 으로 막아주긴 하지만, 사용자는 입력을 다 끝낸 뒤에야 실패를 본다.
    // 실패는 실패로 올리고 화면이 저장을 막는다(fail-closed).
    const all = await productsApi.fieldOwnership();
    const product = all?.product;
    if (!product) {
      throw new Error("승인 정책 응답에 product 항목이 없습니다.");
    }
    return product;
  },

  /**
   * ① live — 직접 저장 + 승인 요청 생성.
   *
   * **mock 성공으로 전환하지 않는다.** 저장이 실패했는데 성공처럼 보이면
   * 사용자는 바뀐 줄 알고 그 값으로 판매가를 정한다. 화면만 맞고 DB 는 옛날 값이다.
   *
   * 승인 대상 필드는 `PATCH /products` 로 보내지 않는다 — 백엔드가 403 으로 막고,
   * 막지 않았더라도 그건 승인 워크플로를 우회하는 경로다.
   */
  async save(
    id: string | null,
    patch: Partial<ProductDetail>,
    opts: ProductSaveOptions = {},
  ): Promise<ProductSaveResult> {
    if (!id) {
      const created = await productsApi.create(
        toBeProductFields(patch) as unknown as Parameters<typeof productsApi.create>[0],
      );
      // 생성 직후 상세를 다시 읽는다 — 생성 응답에는 version token 이 없다.
      const fresh = await productsApi.detail(created.id);
      return { detail: mapBeProductDetail(fresh), approvals: [], directSaved: true };
    }

    if (!opts.ownership) {
      // 소유권 표 없이 저장을 시도하면 안 된다. 화면이 저장 버튼을 막는 게 1차 방어고,
      // 이건 그 방어가 뚫렸을 때를 위한 2차 방어다.
      throw new Error(
        "승인 정책을 불러오지 못해 저장할 수 없습니다. 다시 불러온 뒤 시도해 주세요.",
      );
    }

    const base = opts.base ? toBeProductFields(opts.base) : {};
    const changes = diffAgainst(base, toBeProductFields(patch));
    const classified = classifyProductChanges(changes, opts.ownership);

    if (classified.unroutable.length > 0) {
      throw new Error(
        `승인 요청 유형을 결정할 수 없는 필드가 있습니다: ${classified.unroutable.join(", ")}`,
      );
    }

    // 승인 요청을 **먼저** 만든다. 직접 저장을 먼저 하면 승인 요청 생성이 실패했을 때
    // 일부만 반영된 상태로 남고, 사용자는 무엇이 저장됐는지 알 수 없다.
    const approvals: ApprovalRequest[] = [];
    for (const [requestType, fields] of Object.entries(classified.approval)) {
      approvals.push(
        await approvalsApi.create({
          target_type: "product",
          target_id: id,
          request_type: requestType,
          proposed_changes: fields,
          reason: opts.reason,
          effective_from: opts.effectiveFrom,
          submit: opts.submit ?? true,
        }),
      );
    }

    let detail: ProductDetail;
    if (classified.hasDirect) {
      detail = mapBeProductDetail(
        await productsApi.patchFields(id, classified.direct, opts.base?.versionToken),
      );
    } else {
      detail = mapBeProductDetail(await productsApi.detail(id));
    }
    return { detail, approvals, directSaved: classified.hasDirect };
  },

  /**
   * ① live — `GET /products/{id}/option-groups` + 그룹별 값 조회.
   *
   * 값은 그룹 응답에 안 들어 있어서 그룹마다 한 번 더 부른다. 그룹 수는 보통 1~3개라
   * 왕복이 문제 되지 않고, 서버 응답 모양을 추측하는 것보다 낫다.
   */
  async optionGroups(productId: string): Promise<ProductOptionGroup[]> {
    const groups = await productsApi.getOptionGroups(productId);
    return Promise.all(
      groups.map(async (g) => ({
        name: g.name,
        values: (await productsApi.getOptionValues(g.id)).map((v) => v.value),
      })),
    );
  },

  /** 그룹 원본(id 포함). 그룹·값을 수정하려면 id 가 필요하다. */
  async optionGroupsRaw(productId: string) {
    return productsApi.getOptionGroups(productId);
  },

  /** ① live — `GET /products/{id}/variants` */
  async options(productId: string): Promise<ProductOption[]> {
    const variants = await productsApi.getVariants(productId);
    return variants.map(mapBeVariant);
  },

  /**
   * ① live — variant 단건 저장.
   *
   * **일괄 저장 API 가 없다.** 그래서 화면이 바꾼 행만 골라 하나씩 보낸다.
   * 여기서 전체를 순회하며 다 보내면 안 바꾼 행까지 updated_at 이 갱신되고,
   * 변경 이력이 의미를 잃는다.
   */
  async saveOption(option: ProductOption): Promise<ProductOption> {
    const saved = await productsApi.updateVariant(option.id, {
      sku_external: option.optionSku,
      cost_amount: option.optionCost,
      extra_price: option.extraPrice,
      stock_quantity: option.stock,
      is_active: option.isActive,
    });
    return mapBeVariant(saved);
  },

  /** ① live — 새 조합 생성 */
  async createOption(productId: string, option: ProductOption): Promise<ProductOption> {
    const created = await productsApi.createVariant(productId, {
      variant_key: variantKeyOf(option.values),
      option_values_json: option.values,
      sku_external: option.optionSku,
      cost_amount: option.optionCost,
      extra_price: option.extraPrice,
      stock_quantity: option.stock,
      is_active: option.isActive,
    });
    return mapBeVariant(created);
  },

  /**
   * ① live — 비활성화.
   *
   * 삭제가 아니다. 판매 이력이 있는 조합을 지우면 그 주문의 손익을 다시 계산할 수 없다.
   * 백엔드도 `VARIANT_IN_USE`(409) 로 거절한다.
   */
  async disableOption(optionId: string): Promise<ProductOption> {
    return mapBeVariant(await productsApi.updateVariant(optionId, { is_active: false }));
  },

  /** 옵션 그룹 — ① live */
  createOptionGroup: (productId: string, name: string, sortOrder = 0) =>
    productsApi.createOptionGroup(productId, { name, sort_order: sortOrder }),
  renameOptionGroup: (groupId: string, name: string) =>
    productsApi.updateOptionGroup(groupId, { name }),
  createOptionValue: (groupId: string, value: string, sortOrder = 0) =>
    productsApi.createOptionValue(groupId, { value, sort_order: sortOrder }),
  renameOptionValue: (valueId: string, value: string) =>
    productsApi.updateOptionValue(valueId, { value }),
};

/**
 * 조합 식별자. 백엔드 `variant_key` 가 필수라 화면이 만들어 보내야 한다.
 *
 * 키 순서를 정렬한다 — `{색상,사이즈}` 와 `{사이즈,색상}` 이 다른 키가 되면
 * 같은 조합이 두 번 생긴다.
 */
export function variantKeyOf(values: Record<string, string>): string {
  return Object.keys(values)
    .sort()
    .map((k) => `${k}=${values[k]}`)
    .join("|");
}

/**
 * BE variant → 화면 옵션 모델.
 *
 * `autoName`/`customName`/`sourceOptionUrl`/`marketplaceOptionNo` 는 **DB 에 없다.**
 * 지어내지 않고 파생값이나 null 로 둔다 — 저장해도 사라지는 값을 편집 가능한 것처럼
 * 보여주면 사용자가 입력하고 잃는다.
 */
function mapBeVariant(v: ProductVariant): ProductOption {
  const values = (v.option_values_json ?? {}) as Record<string, string>;
  return {
    id: v.id,
    values,
    autoName: Object.values(values).join(" / "),
    customName: null,
    optionSku: v.sku_external,
    optionCost: v.cost_amount,
    extraPrice: v.extra_price ?? 0,
    stock: v.stock_quantity,
    isActive: v.is_active,
    sourceOptionUrl: null,
    marketplaceOptionNo: null,
  };
}

/** DB 에 저장되지 않는 옵션 필드. 화면이 "미지원" 을 표시하는 근거다. */
export const UNSUPPORTED_OPTION_FIELDS = [
  "customName", "sourceOptionUrl", "marketplaceOptionNo",
] as const;

/**
 * 옵션 그룹 조합 → Variant 매트릭스.
 * 옵션명은 "기본 상품명 + 옵션값들" 규칙으로 자동 생성하고, 사용자가 덮어쓸 수 있다.
 */
export function buildOptionMatrix(
  productId: string,
  groups: Array<{ name: string; values: string[] }>,
  existing: ProductOption[] = [],
  baseName = "",
): ProductOption[] {
  if (groups.length === 0) return [];
  let combos: Array<Record<string, string>> = [{}];
  for (const g of groups) {
    const next: Array<Record<string, string>> = [];
    for (const combo of combos) {
      for (const v of g.values) next.push({ ...combo, [g.name]: v });
    }
    combos = next;
  }
  const prev = new Map(existing.map((o) => [JSON.stringify(o.values), o]));
  return combos.map((values, i) => {
    const key = JSON.stringify(values);
    const existingOption = prev.get(key);
    const autoName = [baseName, ...Object.values(values)].filter(Boolean).join(" ");
    if (existingOption) return { ...existingOption, autoName, values };
    return {
      id: `${productId}-opt-${i}`,
      values,
      autoName,
      customName: null,
      optionSku: null,
      optionCost: null,
      extraPrice: 0,
      stock: null,
      isActive: true,
      sourceOptionUrl: null,
      marketplaceOptionNo: null,
    };
  });
}

/* ────────────────────────────────────────────────────────────
 * 판매장부  — ② fallback
 * ──────────────────────────────────────────────────────────── */

interface BeSale {
  id: string;
  sale_date: string;
  marketplace: string;
  marketplace_order_no: string;
  product_id: string | null;
  product_snapshot_name: string;
  quantity: number;
  sale_price: number;
  business_key?: string;
  /** 유입 계정 (M032). null 은 "모른다" — legacy 주문이거나 수동 등록 시 미지정. */
  integration_account_id: string | null;
  integration_account_label: string | null;
}

const MOCK_SALES: SaleRow[] = Array.from({ length: 48 }).map((_, i) => {
  const p = MOCK_PRODUCTS[i % MOCK_PRODUCTS.length];
  const day = 28 - (i % 28);
  const qty = (i % 3) + 1;
  const price = (p.salePrice ?? 10000) * qty;
  const net = Math.round(price * ((p.marginRate ?? 0.15) as number));
  return {
    id: `s-${String(i + 1).padStart(3, "0")}`,
    saleDate: `2026-07-${String(day).padStart(2, "0")}`,
    marketplace: p.marketplaces[0] ?? "COUPANG",
    // mock 에서도 계정을 지어내지 않는다. 실제 계정이 없으면 없는 것이다.
    integrationAccountId: null,
    integrationAccountLabel: null,
    orderNo: `${2026070000 + i * 7}`,
    productId: p.id,
    productName: p.name,
    sku: p.sku,
    marketplaceProductNo: Object.values(p.marketplaceProductNos)[0] ?? null,
    buyerRef: `구매자${String.fromCharCode(65 + (i % 26))}**`,
    quantity: qty,
    salePrice: price,
    status: i % 11 === 0 ? "RETURNED" : i % 7 === 0 ? "SHIPPING" : "DELIVERED",
    ownerName: p.ownerName,
    memo: i % 11 === 0 ? "단순 변심 반품" : null,
    netProfit: net,
    marginRate: price ? net / price : null,
  };
});

export const salesAdapter = {
  /** ② fallback — BE `/sales` 는 q/기간 필터를 이미 지원한다 */
  async list(params: ListParams & { marketplace?: string; status?: string }): Promise<Paged<SaleRow>> {
    return withFallback(
      async () => {
        const res = await api.get<{ items: BeSale[]; total: number }>("/sales", {
          query: {
            q: params.q || undefined,
            start_date: params.from || undefined,
            end_date: params.to || undefined,
            marketplace: params.marketplace || undefined,
            page: params.page ?? 1,
            page_size: params.pageSize ?? 50,
          },
        });
        if (!res?.items) throw new Error("unexpected shape");
        return {
          items: res.items.map<SaleRow>((s) => ({
            id: s.id,
            saleDate: s.sale_date,
            marketplace: s.marketplace,
            integrationAccountId: s.integration_account_id,
            integrationAccountLabel: s.integration_account_label,
            orderNo: s.marketplace_order_no,
            productId: s.product_id,
            productName: s.product_snapshot_name,
            sku: null,
            marketplaceProductNo: null,
            buyerRef: null,
            quantity: s.quantity,
            salePrice: s.sale_price,
            // ⚠️ `sales_order` 에 **주문 상태 컬럼이 없다.** 예전에는 여기서
            // "DELIVERED" 를 넣어서 모든 주문이 "배송완료" 로 보였다 —
            // 반품된 주문도, 배송 중인 주문도 전부. 화면이 "미지원" 을 표시하도록
            // null 을 준다.
            status: null,
            ownerName: null,
            memo: null,
            netProfit: null,
            marginRate: null,
          })),
          total: res.total,
        };
      },
      () => {
        let rows = [...MOCK_SALES];
        if (params.q)
          rows = rows.filter((s) =>
            matchTokens([s.orderNo, s.productName, s.sku, s.marketplaceProductNo, s.buyerRef, s.memo], params.q!),
          );
        if (params.from) rows = rows.filter((s) => s.saleDate >= params.from!);
        if (params.to) rows = rows.filter((s) => s.saleDate <= params.to!);
        if (params.marketplace) rows = rows.filter((s) => s.marketplace === params.marketplace);
        if (params.status) rows = rows.filter((s) => s.status === params.status);
        return delay(paginate(sortRows(rows, params.sort, params.dir), params));
      },
      "sales.list",
    );
  },
};

/* ────────────────────────────────────────────────────────────
 * 쇼핑몰 정산  — ③ mock (BE 는 저장 구조만 있고 예정/불일치 개념이 없다)
 * ──────────────────────────────────────────────────────────── */

export const settlementAdapter = {
  async list(
    params: ListParams & { marketplace?: string; tab?: string },
  ): Promise<Paged<MarketplaceSettlementRow>> {
    let rows = [...MOCK_SETTLEMENTS];
    if (params.tab === "scheduled") rows = rows.filter((r) => !r.completedDate);
    if (params.tab === "completed") rows = rows.filter((r) => !!r.completedDate && r.matchStatus !== "MISMATCH");
    if (params.tab === "mismatch") rows = rows.filter((r) => r.matchStatus === "MISMATCH");
    if (params.marketplace) rows = rows.filter((r) => r.marketplace === params.marketplace);
    if (params.q) rows = rows.filter((r) => matchTokens([r.marketplace, r.baseMonth], params.q!));
    if (params.from) rows = rows.filter((r) => (r.scheduledDate ?? "") >= params.from!);
    if (params.to) rows = rows.filter((r) => (r.scheduledDate ?? "") <= params.to!);
    return delay(paginate(sortRows(rows, params.sort, params.dir), params));
  },

  async counts() {
    return delay({
      scheduled: MOCK_SETTLEMENTS.filter((r) => !r.completedDate).length,
      completed: MOCK_SETTLEMENTS.filter((r) => !!r.completedDate && r.matchStatus !== "MISMATCH").length,
      mismatch: MOCK_SETTLEMENTS.filter((r) => r.matchStatus === "MISMATCH").length,
    });
  },

  async create(row: Omit<MarketplaceSettlementRow, "id">): Promise<MarketplaceSettlementRow> {
    const created = { ...row, id: `st-${Date.now()}` };
    MOCK_SETTLEMENTS.unshift(created);
    return delay(created, 300);
  },
};

/* ────────────────────────────────────────────────────────────
 * 사업비용 — 제거됨 (Phase 61 mock 감사)
 *
 * `expenseAdapter` 는 목록·저장·삭제·첨부를 전부 메모리 배열로 다뤘고
 * 첨부는 `URL.createObjectURL` 로 브라우저 메모리에만 올렸다.
 *
 * **이미 어느 화면도 쓰지 않는 죽은 코드였다.** 비용 화면은 M026/M027 에서
 * 실제 API(`lib/api/expenses.ts`, `lib/api/attachments.ts`)로 옮겨졌다.
 * 남겨 두면 다음 사람이 "이걸 쓰면 되겠네" 하고 되살린다.
 * ──────────────────────────────────────────────────────────── */

/* ────────────────────────────────────────────────────────────
 * 설정 — 카테고리 / 세금 / 수수료 / 구성원   — ③ mock
 * ──────────────────────────────────────────────────────────── */

/**
 * 조직 설정 어댑터 — **정책·구성원 부분은 제거됐다.**
 *
 * 세금 규칙·수수료 정책·비용 카테고리·구성원은 전부 메모리 배열이었다.
 * "저장했습니다" 토스트는 떴지만 새로고침하면 사라졌고, 그런데도 마진 계산기와
 * 상품 드로어가 그 값으로 수수료·부가세를 계산했다.
 *
 * 지금은 실제 API 를 쓴다:
 *   세금·수수료      `hooks/usePolicies.ts` → `/policies/{kind}`
 *   비용 카테고리    `/policies/expense_category`
 *   구성원          `usersApi.directory()` → `/users/directory`
 *
 * 다시 mock 을 만들지 않도록 여기를 비워 둔다.
 */

/* ────────────────────────────────────────────────────────────
 * 수익배분 — 제거됨 (Phase 31 잔여 배선)
 *
 * `distributionAdapter` 는 배분 규칙을 **메모리 배열**에 저장했다. 화면은
 * "저장했습니다" 를 띄웠지만 DB 에는 아무 일도 없었고, 새로고침하면 사라졌다.
 * `monthlyBase` 는 mock 정산·비용이 비면 11,616,000 원 같은 **지어낸 금액**을
 * 돌려줬다 — 실제 매출처럼 보이는 숫자였다.
 *
 * 지금은 화면이 실제 API 만 쓴다:
 *   확정 배분  `lib/api/payouts.ts`  → /distributions/*
 *   배분 규칙  `lib/api/policies.ts` → /policies/partner_share, /policies/settlement
 * ──────────────────────────────────────────────────────────── */

/* ────────────────────────────────────────────────────────────
 * 알림 — 제거됨 (Phase 39)
 *
 * `notificationAdapter` 는 목록·읽음 처리·규칙을 전부 메모리 배열로 다뤘고,
 * `connectChannel()`/`testChannel()` 은 **성공 mock** 이었다 — 메모리 플래그를
 * 켜고 "연결됨 · 테스트 성공" 을 보여줬을 뿐 아무 데도 아무것도 보내지 않았다.
 *
 * 알림 기능에서 가장 나쁜 종류의 거짓말이다. 사용자는 장애 알림을 받고 있다고
 * 믿게 되고, 정작 장애가 났을 때 아무 연락도 못 받는다.
 *
 * 지금은 화면이 실제 API 만 쓴다: `lib/api/notifications.ts` → /notifications/*
 * ──────────────────────────────────────────────────────────── */

/** 비밀값 마스킹 — 앞 일부와 끝 4자만 남긴다. */
export function maskSecret(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length <= 8) return "••••";
  try {
    const url = new URL(trimmed);
    return `${url.origin}/••••••/${trimmed.slice(-4)}`;
  } catch {
    return `${trimmed.slice(0, 4)}••••••${trimmed.slice(-4)}`;
  }
}

/* ────────────────────────────────────────────────────────────
 * 판매처 연동 — 제거됨 (Phase 61 mock 감사)
 *
 * `integrationAdapter` 는 **외부 API 성공 mock** 이었다.
 *   connect()  → 메모리 플래그를 켜고 status: "OK"
 *   sync()     → productCount: 42 / orderCount: 118  ← 지어낸 숫자
 *
 * 쿠팡·네이버 자격증명도 커넥터 구현도 하나도 없는데 화면은
 * "연결됨 · 상품 42건 동기화 완료" 를 보여줬다. 사용자는 주문이 자동으로
 * 들어온다고 믿게 되고, 실제로는 아무것도 들어오지 않는다.
 *
 * 지금은 화면이 실제로 존재하는 두 가지만 보여준다:
 *   커넥터 지원 여부  `lib/api/channel-prices.ts` → /channel-listings/connector-support
 *   봇 연동 계정      `lib/api/integrations.ts`   → /integrations
 * ──────────────────────────────────────────────────────────── */

/* ────────────────────────────────────────────────────────────
 * 참조 데이터
 * ──────────────────────────────────────────────────────────── */

// 판매처 목록은 더 이상 여기서 나가지 않는다 — `useSaleChannels()` 가 조직이
// 등록한 채널 카탈로그(`/sale-channels`)를 읽는다. 상수로 박아 두면 조직이
// 추가한 채널이 화면에 없고, 그 채널의 수수료 정책도 계산에 쓰이지 않는다.
//
// `MOCK_CATEGORIES` 는 아직 남아 있다. 상품 카테고리는 백엔드에 카탈로그가
// 없어서(비용 카테고리와 다른 개념이다) 지금 대체할 대상이 없다.
export { MOCK_CATEGORIES } from "@/lib/adapters/mock-data";
export type { DistributionResult };
