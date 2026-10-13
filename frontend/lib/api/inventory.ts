/**
 * 재고 · 발주 · 재주문 API 클라이언트 (M048).
 *
 * 화면이 반드시 지켜야 하는 두 가지
 * -----------------------------
 * ```
 * reserved_status = "NOT_CONNECTED"   "예약 0개" 라고 쓰지 않는다
 * tracked = false                     "재고 0" 이 아니라 "미관리" 로 쓴다
 * ```
 *
 * 둘 다 **모른다는 사실**이다. 0 으로 보여주면 사용자가 확인된 값으로 읽고,
 * 그 위에서 발주를 결정한다.
 *
 * 재주문 파라미터에 기본값을 두지 않는다
 * ---------------------------------
 * `velocityDays` / `coverageDays` 는 필수 인자다. 여기에 30 을 넣으면 서버가
 * 막아 놓은 것을 클라이언트가 도로 열어 준다.
 */

import { api } from "@/lib/api";

export interface InventoryItem {
  product_id: string;
  product_variant_id: string | null;
  product_name: string;
  product_key: string;
  variant_key: string | null;
  sku: string | null;
  on_hand: number;
  /** 발주했지만 아직 안 들어온 수량 */
  inbound: number;
  negative: boolean;
  last_movement_at: string | null;
}

export interface InventoryList {
  items: InventoryItem[];
  /** 항상 `NOT_CONNECTED` — 미출고 주문을 알 수 없다 */
  reserved_status: string;
  reserved_note: string;
}

export interface InventoryTransaction {
  id: string;
  transaction_type:
    | "OPENING" | "PURCHASE_RECEIPT" | "SALE" | "RETURN" | "ADJUSTMENT";
  quantity_delta: number;
  unit_cost: number | null;
  currency: string;
  occurred_at: string;
  reason: string | null;
  source_table: string | null;
  source_id: string | null;
  actor_name: string;
}

export const TXN_LABEL: Record<InventoryTransaction["transaction_type"], string> = {
  OPENING: "기초재고",
  PURCHASE_RECEIPT: "입고",
  SALE: "판매",
  RETURN: "반품",
  ADJUSTMENT: "조정",
};

/** 재주문을 추천할 수 없는 이유. 전부 다른 대응이 필요하다. */
export type BlockReason =
  | "NOT_TRACKED" | "NO_SALES_HISTORY" | "NO_SOURCE" | "NO_LEAD_TIME" | "NO_COST";

/** 무엇을 하면 되는지. "추천 불가" 만 보여주면 사용자가 막힌다. */
export const BLOCK_ACTION: Record<BlockReason, string> = {
  NOT_TRACKED: "기초 재고를 입력하세요",
  NO_SALES_HISTORY: "판매 이력이 쌓이면 계산됩니다",
  NO_SOURCE: "상품에 소싱처를 연결하세요",
  NO_LEAD_TIME: "소싱처에 리드타임을 입력하세요",
  NO_COST: "소싱 원가를 입력하세요",
};

export interface ReorderCandidate {
  product_id: string;
  product_variant_id: string | null;
  product_name: string;
  variant_key: string | null;
  sku: string | null;
  on_hand: number;
  inbound: number;
  sold_in_window: number;
  supplier_name: string | null;
  /** 문자열이다 — 0.33 같은 값을 부동소수로 다루지 않는다 */
  daily_velocity: string;
  days_of_cover: number;
  stockout_date: string;
  reorder_by_date: string;
  /** 발주 기한이 지났다. 지금 주문해도 늦는다 */
  overdue: boolean;
  lead_time_days: number;
  moq: number;
  recommended_quantity: number;
  unit_price: number;
  /** 단가를 어디서 가져왔나. 금액이 이상할 때 어디를 고칠지 알려 준다 */
  price_basis: "SOURCE_COST_VERSION" | "PRODUCT_COST";
  estimated_amount: number;
  currency: string;
  product_source_id: string;
  supplier_site_id: string;
}

export const PRICE_BASIS_LABEL: Record<ReorderCandidate["price_basis"], string> = {
  SOURCE_COST_VERSION: "소싱 원가",
  PRODUCT_COST: "상품 등록 원가",
};

export interface ReorderBlocked {
  product_id: string;
  product_name: string;
  sku: string | null;
  on_hand: number;
  block_reason: BlockReason;
  block_message: string;
}

export interface ReorderGroup {
  supplier_site_id: string;
  supplier_name: string | null;
  currency: string;
  lines: ReorderCandidate[];
  total_amount: number;
}

export interface ReorderResult {
  as_of: string;
  velocity_days: number;
  coverage_days: number;
  candidates: ReorderCandidate[];
  /** 절대 숨기지 않는다 — 숨기면 "다 괜찮다" 로 잘못 읽힌다 */
  blocked: ReorderBlocked[];
  groups?: ReorderGroup[];
  summary: {
    candidate_count: number;
    overdue_count: number;
    blocked_count: number;
    estimated_total: number;
  };
}

export type PurchaseOrderStatus =
  | "DRAFT" | "ORDERED" | "PARTIALLY_RECEIVED" | "RECEIVED" | "CANCELLED";

export const PO_STATUS_LABEL: Record<PurchaseOrderStatus, string> = {
  DRAFT: "초안",
  ORDERED: "발주함",
  PARTIALLY_RECEIVED: "일부 입고",
  RECEIVED: "입고 완료",
  CANCELLED: "취소",
};

export interface PurchaseOrderLine {
  id: string;
  product_id: string;
  product_variant_id: string | null;
  product_name: string;
  variant_key: string | null;
  quantity: number;
  received_quantity: number;
  unit_price: number;
  line_total: number;
}

export interface PurchaseOrder {
  id: string;
  business_key: string;
  status: PurchaseOrderStatus;
  currency: string;
  supplier_site_id: string;
  supplier_name: string | null;
  ordered_at: string | null;
  expected_at: string | null;
  memo: string | null;
  created_at: string;
  /** 자동화가 만든 초안. 사람이 확정해야 나간다 */
  created_by_automation: boolean;
  lines: PurchaseOrderLine[];
  total_amount: number;
}

export interface PurchaseOrderSummary {
  id: string;
  business_key: string;
  status: PurchaseOrderStatus;
  currency: string;
  supplier_name: string | null;
  expected_at: string | null;
  created_at: string;
  created_by_automation: boolean;
  line_count: number;
  total_amount: number;
}

export const inventoryApi = {
  list: (params: { search?: string; onlyNegative?: boolean; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.search) q.set("search", params.search);
    if (params.onlyNegative) q.set("only_negative", "true");
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return api.get<InventoryList>(`/inventory${qs ? `?${qs}` : ""}`);
  },

  transactions: (productId: string, variantId?: string | null) => {
    const q = variantId ? `?variant_id=${variantId}` : "";
    return api.get<InventoryTransaction[]>(
      `/inventory/${productId}/transactions${q}`,
    );
  },

  adjust: (body: {
    product_id: string;
    product_variant_id?: string | null;
    quantity_delta: number;
    reason: string;
  }) =>
    api.post<{
      transaction_id: string;
      on_hand_before: number;
      on_hand_after: number;
      negative: boolean;
    }>("/inventory/adjust", body),

  /**
   * 재주문 후보.
   *
   * 두 인자에 기본값이 없는 것이 의도다 — 호출부가 사용자 설정을 넘겨야 한다.
   */
  reorder: (velocityDays: number, coverageDays: number, groupBySupplier = false) => {
    const q = new URLSearchParams({
      velocity_days: String(velocityDays),
      coverage_days: String(coverageDays),
    });
    if (groupBySupplier) q.set("group_by_supplier", "true");
    return api.get<ReorderResult>(`/inventory/reorder?${q}`);
  },
};

export const purchaseOrderApi = {
  list: (status?: PurchaseOrderStatus[]) => {
    const q = (status ?? []).map((s) => `status=${s}`).join("&");
    return api.get<{ items: PurchaseOrderSummary[]; total: number }>(
      `/purchase-orders${q ? `?${q}` : ""}`,
    );
  },

  get: (id: string) => api.get<PurchaseOrder>(`/purchase-orders/${id}`),

  create: (body: {
    supplier_site_id: string;
    lines: {
      product_id: string;
      product_variant_id?: string | null;
      quantity: number;
      unit_price: number;
      product_source_id?: string | null;
    }[];
    expected_at?: string | null;
    memo?: string | null;
    currency?: string;
  }) => api.post<PurchaseOrder>("/purchase-orders", body),

  setStatus: (id: string, status: PurchaseOrderStatus, reason?: string) =>
    api.post<PurchaseOrder>(`/purchase-orders/${id}/status`, { status, reason }),

  /**
   * 입고.
   *
   * `receiptKey` 를 반드시 넘긴다. 이게 없으면 버튼을 두 번 눌렀을 때 재고가
   * 두 번 늘어난다 — 같은 수량 두 번 입고는 정상적인 일이라 서버가 수량만으로는
   * 구분할 수 없다.
   */
  receive: (
    id: string,
    receipts: { line_id: string; quantity: number }[],
    receiptKey: string,
    reason?: string,
  ) =>
    api.post<PurchaseOrder>(`/purchase-orders/${id}/receive`, {
      receipts,
      receipt_key: receiptKey,
      reason,
    }),
};
