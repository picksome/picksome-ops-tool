import { api } from "@/lib/api";

// M007(docs/27 D-287): 마켓이 sale_channel 테이블로 동적화되어 고정 union 이 아닌 string.
// 표시 이름은 하드코딩 맵 대신 hooks/useSaleChannels().labelOf() 로 조회.
export type SaleChannel = string;

export interface SaleOrder {
  id: string;
  business_key: string;
  sale_date: string;
  marketplace: SaleChannel;
  marketplace_order_no: string;
  /** 같은 주문번호에 여러 라인이 있을 때의 라인 번호 (M012). */
  line_no: number;
  product_id: string | null;
  product_snapshot_name: string | null;
  quantity: number;
  sale_price: number;
  ad_cost: number;
  shipping_fee: number;
  vat_inclusion_type: "INCLUDED" | "EXCLUDED" | "EXEMPT";
  memo: string | null;
  import_source: string;
  created_at: string;

  /**
   * 이 주문이 유입된 판매자 계정 (M032).
   *
   * **`marketplace` 와 같은 것이 아니다.** 쿠팡 본점과 쿠팡 2호점은 둘 다
   * `marketplace: "COUPANG"` 이지만 다른 계정이다.
   *
   * `null` 은 **"어느 계정인지 모른다"** 는 뜻이다 — M032 이전 주문이거나
   * 계정을 지정하지 않고 수동 등록한 주문이다. "계정이 없다" 가 아니다.
   * 화면은 채널 이름으로 대신 채우지 않는다.
   */
  integration_account_id: string | null;
  /** 계정 표시 이름. 계정이 없으면 null — 채널 이름을 넣지 않는다. */
  integration_account_label: string | null;
}

export interface SaleListResponse {
  items: SaleOrder[];
  total: number;
  page: number;
  page_size: number;
}

export interface SaleCreate {
  sale_date: string;
  marketplace: SaleChannel;
  marketplace_order_no: string;
  product_id?: string;
  product_snapshot_name?: string;
  quantity: number;
  sale_price: number;
  ad_cost?: number;
  shipping_fee?: number;
  vat_inclusion_type?: "INCLUDED" | "EXCLUDED" | "EXEMPT";
  memo?: string | null;
  /**
   * 유입 계정. 선택값이다 — 연동 계정을 아직 만들지 않은 조직도 수동 등록한다.
   * 서버가 채널 문자열로 계정을 추론하지 않으므로, 아는 쪽이 명시해야 한다.
   */
  integration_account_id?: string | null;
}

export interface SaleFilters {
  start_date?: string;
  end_date?: string;
  marketplace?: SaleChannel;
  /** 같은 판매처에 계정이 여럿일 때 스토어별로 좁힌다. */
  integration_account_id?: string;
  product_id?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

export const salesApi = {
  list: (params?: SaleFilters) =>
    api.get<SaleListResponse>("/sales", {
      query: params as Record<string, string | number | undefined>,
    }),

  create: (body: SaleCreate) => api.post<SaleOrder>("/sales", body),

  update: (id: string, body: Partial<SaleCreate>) =>
    api.patch<SaleOrder>(`/sales/${id}`, body),

  delete: (id: string) => api.delete(`/sales/${id}`),
};
