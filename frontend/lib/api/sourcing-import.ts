/**
 * 상품↔공급처 연결 가져오기 API 클라이언트.
 *
 * 재고 가져오기와 같은 모양이다 — `mode` 를 넘기고, 기본은 검토이며, 적용은
 * 결과를 본 뒤에만 보낸다. 가져오기마다 다른 규칙을 만들면 사용자가 매번 새로
 * 배워야 한다.
 */

import { ApiError, apiRequest } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

export type ImportMode = "ANALYZE" | "DRY_RUN" | "APPLY";

export type SourceOutcome =
  | "NEW_MAPPING" | "UPDATE_METADATA" | "EXISTING_SAME"
  | "CONFLICT_PRIMARY" | "UNMATCHED_PRODUCT" | "UNMATCHED_SUPPLIER" | "INVALID";

export const SOURCE_OUTCOME_LABEL: Record<SourceOutcome, string> = {
  NEW_MAPPING: "새 연결",
  UPDATE_METADATA: "정보 채움",
  EXISTING_SAME: "변화 없음",
  CONFLICT_PRIMARY: "주 소싱처 충돌",
  UNMATCHED_PRODUCT: "상품 못 찾음",
  UNMATCHED_SUPPLIER: "공급처 못 찾음",
  INVALID: "값 오류",
};

export const SOURCE_OUTCOME_HELP: Record<SourceOutcome, string> = {
  NEW_MAPPING: "상품과 공급처를 새로 연결합니다.",
  UPDATE_METADATA: "비어 있던 항목만 채웁니다. 기존 값은 덮지 않습니다.",
  EXISTING_SAME: "이미 같은 내용이라 아무것도 하지 않습니다.",
  CONFLICT_PRIMARY: "이미 다른 주 소싱처가 있습니다. 기존 것을 먼저 해제하세요.",
  UNMATCHED_PRODUCT: "SKU·상품번호로 찾지 못했습니다. 이름으로는 찾지 않습니다.",
  UNMATCHED_SUPPLIER: "공급처코드·도메인으로 찾지 못했습니다. 먼저 등록하세요.",
  INVALID: "리드타임·MOQ·매입가를 숫자로 읽을 수 없습니다.",
};

export interface SourceRow {
  line_no: number;
  outcome: SourceOutcome;
  product_name: string | null;
  supplier_name: string | null;
  source_url: string | null;
  lead_time_days: number | null;
  moq: number | null;
  is_primary: boolean;
  purchase_price: number | null;
  changes: Record<string, unknown>;
  message: string | null;
}

export interface SourceImportPlan {
  mode: ImportMode;
  rows_total: number;
  counts: Record<SourceOutcome, number>;
  /** ID/PW 열이 있었는지. 값은 읽지 않았다 */
  credential_detected: boolean;
  credential_headers_skipped: number;
  lead_time_available: number;
  moq_available: number;
  source_cost_candidates: number;
  problems: SourceRow[];
  changes: SourceRow[];
  file_problems: string[];
  dry_run: boolean;
  source_name?: string;
  created?: number;
  updated?: number;
  cost_versions?: number;
}

/** 재주문이 왜 안 도는지의 답. 각 항목이 차단 사유와 짝을 이룬다. */
export interface Completeness {
  products: number;
  with_source: number;
  without_source: number;
  without_primary: number;
  without_lead_time: number;
  without_moq: number;
  sources_without_cost: number;
}

export interface SourceTemplate {
  product_keys: string[];
  supplier_keys: string[];
  columns: {
    field: string;
    label: string;
    required: boolean;
    note?: string;
    aliases: string[];
  }[];
  notes: string[];
}

export const sourcingImportApi = {
  run: async (file: File, mode: ImportMode): Promise<SourceImportPlan> => {
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);

    const token = getAccessToken();
    const res = await fetch(`${BASE}/sourcing/import`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = body?.error ?? {};
      throw new ApiError(
        err.code ?? "IMPORT_FAILED",
        err.message ?? "가져오기에 실패했습니다.",
        res.status,
      );
    }
    return (body?.data ?? body) as SourceImportPlan;
  },

  completeness: () => apiRequest<Completeness>("/sourcing/import/completeness"),

  template: () => apiRequest<SourceTemplate>("/sourcing/import/template"),
};
