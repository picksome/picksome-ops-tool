import { api } from "@/lib/api";

/** 통합 검색 — docs/29_global_search.md */

export interface SearchProduct {
  id: string;
  business_key: string;
  sku: string | null;
  name: string;
  status: string;
  margin_type: string;
}

export interface SearchSale {
  id: string;
  business_key: string;
  sale_date: string;
  marketplace: string;
  name: string | null;
  sale_price: number;
}

export interface SearchExpense {
  id: string;
  expense_date: string;
  name: string | null;
  gross_amount: number;
  memo: string | null;
}

export interface SearchCounts {
  products: number;
  sales: number;
  expenses: number;
}

export interface SearchResult {
  products: SearchProduct[];
  sales: SearchSale[];
  expenses: SearchExpense[];
  /** limit 을 걷어낸 전체 건수 — "12건 중 5건" 표기에 쓴다 */
  counts: SearchCounts;
}

/** BE 가 2자 미만이면 빈 결과를 주지만, 왕복 자체를 아끼려고 여기서도 막는다 */
export const MIN_SEARCH_LEN = 2;

export const searchApi = {
  query: (q: string, limit = 5) =>
    api.get<SearchResult>("/search", { query: { q, limit } }),
};
