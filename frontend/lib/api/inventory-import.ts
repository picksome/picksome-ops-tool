/**
 * 재고 가져오기 API 클라이언트 (M050).
 *
 * 왜 mode 를 인자로 받나
 * --------------------
 * 세 단계가 **같은 파일**에 대해 돌아야 한다. 경로를 나누면 분석은 A 파일로
 * 하고 적용은 B 파일로 하는 일이 생긴다.
 *
 * 화면은 반드시 DRY_RUN 을 먼저 보여주고, 사용자가 결과를 본 뒤에만 APPLY 를
 * 보낸다. 파일 선택 즉시 APPLY 하지 않는다 — 1,000행이 되돌릴 수 없이 들어간다.
 */

import { ApiError, apiRequest } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

/**
 * `lib/api.ts` 의 `api.*` 는 본문을 항상 JSON 으로 만든다. 파일 업로드는
 * `FormData` 여야 하고, Content-Type 을 우리가 정하면 boundary 가 빠져
 * 서버가 파싱하지 못한다 — 그래서 여기서만 fetch 를 직접 쓴다.
 *
 * 증빙 첨부(`attachments.ts`)가 같은 이유로 같은 예외를 두고 있다.
 */
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

export type ImportMode = "ANALYZE" | "DRY_RUN" | "APPLY";

/** 한 행의 처리 결과. 각각 사용자가 할 일이 다르다. */
export type RowOutcome =
  | "OPENING" | "ADJUSTMENT" | "UNCHANGED"
  | "UNMATCHED" | "AMBIGUOUS" | "INVALID" | "DUPLICATE";

export const OUTCOME_LABEL: Record<RowOutcome, string> = {
  OPENING: "최초 재고",
  ADJUSTMENT: "조정",
  UNCHANGED: "변화 없음",
  UNMATCHED: "상품 못 찾음",
  AMBIGUOUS: "옵션 불명",
  INVALID: "값 오류",
  DUPLICATE: "이미 반영됨",
};

export const OUTCOME_HELP: Record<RowOutcome, string> = {
  OPENING: "원장이 없던 상품입니다. 이 수량으로 시작합니다.",
  ADJUSTMENT: "현재 원장과의 차이만 기록합니다. 수량을 덮어쓰지 않습니다.",
  UNCHANGED: "현재 재고와 같아 아무것도 기록하지 않습니다.",
  UNMATCHED: "SKU·상품번호로 상품을 찾지 못했습니다. 이름으로는 찾지 않습니다.",
  AMBIGUOUS: "옵션이 여러 개입니다. 옵션코드를 넣어 주세요.",
  INVALID: "수량이나 날짜를 읽을 수 없습니다.",
  DUPLICATE: "같은 파일로 이미 반영된 행입니다. 재적용의 정상 결과입니다.",
};

export interface ImportRow {
  line_no: number;
  outcome: RowOutcome;
  identity: string | null;
  product_name: string | null;
  counted: number | null;
  current: number | null;
  delta: number | null;
  message: string | null;
}

export interface ImportPlan {
  mode: ImportMode;
  as_of_date: string | null;
  rows_total: number;
  counts: Record<RowOutcome, number>;
  /** 사람이 손봐야 하는 행만 */
  problems: ImportRow[];
  /** 실제로 원장이 바뀔 행만 */
  changes: ImportRow[];
  /** 행이 아니라 파일 수준의 문제 (열 누락 등) */
  file_problems: string[];
  dry_run: boolean;
  source_name?: string;
  /** APPLY 일 때만 */
  applied?: number;
  import_run_id?: string;
}

export interface ImportRun {
  id: string;
  source_name: string;
  mode: ImportMode;
  as_of_date: string | null;
  rows_total: number;
  opening: number;
  adjustment: number;
  unchanged: number;
  unmatched: number;
  ambiguous: number;
  invalid: number;
  duplicate: number;
  started_at: string;
  finished_at: string | null;
  actor_name: string;
}

export interface TemplateColumn {
  field: string;
  label: string;
  required: boolean;
  note?: string;
  aliases: string[];
}

export interface ImportTemplate {
  identity_columns: string[];
  columns: TemplateColumn[];
  notes: string[];
}

export const inventoryImportApi = {
  run: async (
    file: File, mode: ImportMode, asOfDate?: string,
  ): Promise<ImportPlan> => {
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    if (asOfDate) form.append("as_of_date", asOfDate);

    const token = getAccessToken();
    const res = await fetch(`${BASE}/inventory/import`, {
      method: "POST",
      // Content-Type 을 지정하지 않는다 — 브라우저가 boundary 와 함께 붙인다.
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
    return (body?.data ?? body) as ImportPlan;
  },

  runs: () => apiRequest<ImportRun[]>("/inventory/import/runs"),

  template: () => apiRequest<ImportTemplate>("/inventory/import/template"),
};
