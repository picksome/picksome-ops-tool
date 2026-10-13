import { api } from "@/lib/api";

export interface SaleChannelItem {
  id: string;
  org_id: string | null;
  code: string;
  display_name: string;
  is_system_default: boolean;
  integration_kind: "NONE" | "API" | "BOT";
  is_active: boolean;
}

export interface SaleChannelCreate {
  code: string;
  display_name: string;
}

export const saleChannelsApi = {
  list: () => api.get<SaleChannelItem[]>("/sale-channels"),
  create: (body: SaleChannelCreate) => api.post<SaleChannelItem>("/sale-channels", body),
};
