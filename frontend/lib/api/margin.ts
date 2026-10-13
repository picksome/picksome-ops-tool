import { api } from "@/lib/api";

export type MarginType = "PURCHASE_AGENCY" | "ROCKET_GROWTH" | "OVERSEAS_DIRECT" | "GENERAL_RESELL";
// M007(docs/27 D-287): 마켓이 sale_channel 테이블로 동적화되어 고정 union 이 아닌 string.
// 유효 코드 목록은 GET /sale-channels(hooks/useSaleChannels) 로 조회.
export type SaleChannel = string;
export type PaymentMethod = "BUSINESS_CARD" | "PERSONAL_CARD" | "BANK_TRANSFER" | "CASH" | "OTHER";

export interface MarginPreviewRequest {
  margin_type: MarginType;
  marketplace: SaleChannel;
  cost_amount: number;
  sale_price: number;
  payment_method: PaymentMethod;
  extras?: Record<string, unknown>;
  override_fields?: Record<string, { value: number; reason?: string }>;
}

export interface AppliedPolicy {
  table: string;
  version: number;
  effective_from: string;
  reliability_status: string;
  source_type: string;
  value: number;
}

export interface MarginPreviewResult {
  marketplace_fee: number;
  payment_fee: number;
  ad_cost: number;
  fulfillment_fee: number;
  return_risk_cost: number;
  vat: number;
  margin_amount: number;
  margin_rate: number;
  applied_policies: {
    marketplace_fee_policy?: AppliedPolicy;
    payment_fee_policy?: AppliedPolicy;
    tax_policy?: AppliedPolicy;
    fulfillment_fee_policy?: AppliedPolicy;
    return_risk_policy?: AppliedPolicy;
    ad_cost_policy?: AppliedPolicy;
    target_margin_policy?: AppliedPolicy;
  };
  warnings: string[];
}

export interface SimulateRow {
  target_rate: number;
  recommended_price: number;
  expected_margin: {
    margin_amount: number;
    margin_rate: number;
  };
}

export type SimulatePriceResult = SimulateRow[];

const DEFAULT_TARGET_RATES = [0.1, 0.15, 0.2, 0.25, 0.3, 0.35];

export const marginApi = {
  preview: (body: MarginPreviewRequest) =>
    api.post<MarginPreviewResult>("/margin/calculate", body),

  saveExpected: (body: MarginPreviewRequest & { product_id?: string }) =>
    api.post<{ saved_id: string }>("/margin/calculate", { ...body, save: true }),

  simulatePrice: (body: Pick<MarginPreviewRequest, "margin_type" | "marketplace" | "cost_amount" | "payment_method" | "extras">) =>
    api.post<SimulatePriceResult>("/margin/simulate", { ...body, target_rates: DEFAULT_TARGET_RATES }),
};
