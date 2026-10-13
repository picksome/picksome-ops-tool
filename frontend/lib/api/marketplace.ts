import { api } from "@/lib/api";
import type { SaleChannel } from "./sales";

export interface MarketplaceSettlementRecord {
  id: string;
  marketplace: SaleChannel;
  settlement_period_start: string;
  settlement_period_end: string;
  marketplace_order_no: string | null;
  /**
   * ⚠️ **실제 은행 입금일이라는 근거가 없다.**
   *
   * 스키마에 정의가 없고(M001 에 주석 없음) 실제 정산 파일도 아직 없다.
   * 이름만 보고 "지급 완료" 로 해석하면 현금흐름 전체가 거짓이 된다.
   * 현금흐름은 이 값을 ACTUAL 로 쓰지 않는다 (docs/54).
   */
  settled_at: string | null;
  gross_revenue: number;
  marketplace_fee: number;
  ad_cost: number;
  return_cost: number;
  fulfillment_fee: number;
  other_deductions: number;
  net_settlement: number;
  matched_sales_order_id: string | null;
  created_at: string;

  /**
   * 이 정산서가 속한 판매자 계정 (M033).
   *
   * **`marketplace` 와 다르다.** 쿠팡 본점과 2호점은 같은 `marketplace` 지만
   * 다른 계정이고, 정산서는 계정 단위로 온다.
   * `null` 은 "모른다" 이지 "계정 없음" 이 아니다 — 채널 이름으로 채우지 않는다.
   */
  integration_account_id: string | null;
  integration_account_label: string | null;
}

export interface MarketplaceSettlementListResponse {
  items: MarketplaceSettlementRecord[];
  total: number;
  page: number;
  page_size: number;
}

export const marketplaceApi = {
  list: (params?: {
    start_date?: string;
    end_date?: string;
    marketplace?: SaleChannel;
    /** 같은 판매처에 계정이 여럿일 때 스토어별 정산서를 나눠 본다. */
    integration_account_id?: string;
    page?: number;
    page_size?: number;
  }) =>
    api.get<MarketplaceSettlementListResponse>("/marketplace-settlements", {
      query: params as Record<string, string | number | undefined>,
    }),

  get: (id: string) =>
    api.get<MarketplaceSettlementRecord>(`/marketplace-settlements/${id}`),
};
