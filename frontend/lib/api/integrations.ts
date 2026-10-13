import { api } from "@/lib/api";

export interface IntegrationAccountListItem {
  id: string;
  provider: string;
  account_label: string;
  auth_kind: string;
  status: string;
  last_synced_at: string | null;
  sale_channel_code: string | null;
}

export interface IntegrationAccountCreate {
  provider: string;
  account_label: string;
  sale_channel_code?: string | null;
}

export interface IntegrationAccountCreated {
  id: string;
  provider: string;
  account_label: string;
  api_key: string; // 발급 응답에서 1회만 노출 — 다시 조회 불가
}

export const integrationsApi = {
  list: () => api.get<IntegrationAccountListItem[]>("/integrations"),
  create: (body: IntegrationAccountCreate) =>
    api.post<IntegrationAccountCreated>("/integrations", body),
};
