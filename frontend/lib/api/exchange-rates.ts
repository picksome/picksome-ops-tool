import { api } from "@/lib/api";

export interface ExchangeRateOut {
  currency: string;
  rate_to_krw: number;
}

export interface SyncRatesRequest {
  rate_date?: string;
  currencies?: string[];
}

export interface SyncRatesResult {
  rate_date: string;
  rates: Record<string, number>;
}

export interface ManualRateUpsert {
  currency: string;
  rate_date: string;
  rate_to_krw: number;
}

export const exchangeRatesApi = {
  get: (currency: string, at?: string) =>
    api.get<ExchangeRateOut>(`/exchange-rates/${currency}`, { query: at ? { at } : undefined }),

  sync: (body: SyncRatesRequest = {}) => api.post<SyncRatesResult>("/exchange-rates/sync", body),

  manual: (body: ManualRateUpsert) => api.post<ExchangeRateOut>("/exchange-rates/manual", body),
};
