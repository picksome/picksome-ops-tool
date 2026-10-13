/**
 * 정산서 가져오기 API 클라이언트 (M056).
 *
 * 왜 mode 를 인자로 받나
 * --------------------
 * 두 단계가 **같은 파일**에 대해 돌아야 한다. 경로를 나누면 미리보기는 A
 * 파일로 하고 반영은 B 파일로 하는 일이 생긴다. `inventory-import` 와 같은
 * 이유·같은 모양이다.
 *
 * 화면은 반드시 DRY_RUN 을 먼저 보여주고, 사용자가 결과를 본 뒤에만 COMMIT
 * 을 보낸다.
 */

import { ApiError } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

export type SettlementImportMode = "DRY_RUN" | "COMMIT";

/**
 * 정산 행이 주문에 어떻게 붙었는가. **각각 사용자가 할 일이 다르다** —
 * 한 문구로 뭉치면 둘 다 방치된다.
 */
export const MATCHING_LABEL: Record<string, string> = {
  matched_exact: "자동 매칭",
  ambiguous: "모호 (직접 선택)",
  unmatched: "주문 못 찾음",
  excluded: "대사 대상 아님",
};

export const MATCHING_HELP: Record<string, string> = {
  matched_exact: "주문번호로 주문을 유일하게 찾았습니다.",
  ambiguous:
    "그 주문에 상품 라인이 여러 개라 어느 라인의 정산금인지 규칙으로는 알 수 없습니다. " +
    "정산 화면에서 직접 고르세요 (승인이 필요합니다).",
  unmatched:
    "그 주문번호로 장부에 잡힌 판매가 없습니다. 주문이 아직 안 들어왔거나, " +
    "주문과 무관한 정산행일 수 있습니다.",
  excluded: "광고비·보증금처럼 주문과 직접 연결되지 않는 행입니다.",
};

export interface SettlementImportSummary {
  mode: SettlementImportMode;
  source_name: string;
  source_sha256: string;
  sheet_name: string;
  /** 원본 행 수 */
  source_rows: number;
  parsed: number;
  /** 정산 행으로 만들 수 없던 행 */
  rejected: number;
  /** 이번에 새로 들어간 행 */
  inserted: number;
  /** 이미 있던 행 — 같은 파일을 다시 올리면 여기로 온다 */
  unchanged: number;
  /** 이미 있는데 금액이 다른 행. **덮어쓰지 않는다** */
  changed_not_applied: number;
  matched_exact: number;
  ambiguous: number;
  unmatched: number;
  excluded: number;
  no_order_no: number;
  relationship_mismatches: number;
  refund_like_rows: number;
  unknown_headers: string[];
  due_dates: string[];
  period_start: string | null;
  period_end: string | null;
  persisted: number;
  /** 원본 = 적재 + 거부. 한 행도 사라지지 않았는가 */
  reconciles: boolean;
  rejects: { row: number; reason: string; detail: string | null }[];
}

export interface SettlementImportRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  affected_rows: number;
  record_count: number;
  summary_json: Partial<SettlementImportSummary> | null;
}

export const settlementImportApi = {
  run: async (
    file: File,
    integrationAccountId: string,
    mode: SettlementImportMode,
  ): Promise<SettlementImportSummary> => {
    const form = new FormData();
    form.append("file", file);
    form.append("integration_account_id", integrationAccountId);
    form.append("mode", mode);

    const token = getAccessToken();
    const res = await fetch(`${BASE}/settlements/import`, {
      method: "POST",
      // Content-Type 을 지정하지 않는다 — 브라우저가 boundary 와 함께 붙인다.
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = body?.error ?? {};
      throw new ApiError(
        err.code ?? "SETTLEMENT_IMPORT_FAILED",
        err.message ?? "정산서를 읽지 못했습니다.",
        res.status,
      );
    }
    return (body?.data ?? body) as SettlementImportSummary;
  },

  runs: async (limit = 20): Promise<SettlementImportRun[]> => {
    const token = getAccessToken();
    const res = await fetch(`${BASE}/settlements/import/runs?limit=${limit}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new ApiError(
        body?.error?.code ?? "SETTLEMENT_RUNS_FAILED",
        body?.error?.message ?? "가져오기 이력을 불러오지 못했습니다.",
        res.status,
      );
    }
    return (body?.data ?? []) as SettlementImportRun[];
  },
};
