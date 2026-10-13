import { api } from "@/lib/api";

export type ReportType =
  | "MONTHLY_SALES"
  | "MONTHLY_PURCHASE"
  | "EXPENSE"
  | "SALES_BY_CHANNEL"
  | "PRODUCT_PNL"
  | "SETTLEMENT_RECONCILIATION"
  | "PARTNER_DISTRIBUTION"
  | "VAT_REFERENCE"
  | "MISSING_EVIDENCE"
  // 운영 리포트 (Phase 15). 세무 제출용은 아니지만 같은 반출 경로를 쓴다 —
  // 반출 기록과 파일명 규칙을 두 벌 만들 이유가 없다.
  | "PAYOUT_LEDGER"
  | "CASHFLOW"
  | "OPERATION_ISSUES";

export const REPORT_LABELS: Record<ReportType, string> = {
  MONTHLY_SALES: "월별 매출 리포트",
  MONTHLY_PURCHASE: "월별 매입 리포트",
  EXPENSE: "사업비용 리포트",
  SALES_BY_CHANNEL: "판매처별 매출 리포트",
  PRODUCT_PNL: "상품별 손익 리포트",
  SETTLEMENT_RECONCILIATION: "쿠팡 정산 대사 리포트",
  PARTNER_DISTRIBUTION: "파트너별 수익배분 리포트",
  VAT_REFERENCE: "부가세 참고 리포트",
  MISSING_EVIDENCE: "증빙 누락 리포트",
  PAYOUT_LEDGER: "지급 장부 리포트",
  CASHFLOW: "현금흐름 리포트",
  OPERATION_ISSUES: "운영 이슈 리포트",
};

// 백엔드 ReportKind(snake_case, services/reports.py) 매핑
const BACKEND_KIND: Record<ReportType, string> = {
  MONTHLY_SALES: "monthly_sales",
  MONTHLY_PURCHASE: "monthly_purchase",
  EXPENSE: "monthly_expense",
  SALES_BY_CHANNEL: "by_marketplace",
  PRODUCT_PNL: "by_product_pl",
  SETTLEMENT_RECONCILIATION: "coupang_reconcile",
  PARTNER_DISTRIBUTION: "partner_distribution",
  VAT_REFERENCE: "vat_quarterly",
  MISSING_EVIDENCE: "evidence_missing",
  PAYOUT_LEDGER: "payout_ledger",
  CASHFLOW: "cashflow",
  OPERATION_ISSUES: "operation_issues",
};

function monthRange(ym: string): { period_start: string; period_end: string } {
  const [y, m] = ym.split("-").map(Number);
  const lastDay = new Date(y, m, 0).getDate();
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    period_start: `${ym}-01`,
    period_end: `${ym}-${pad(lastDay)}`,
  };
}

export interface ActualMarginItem {
  product_id: string;
  product_name: string;
  sku: string;
  year_month: string;
  expected_margin_amount: number | null;
  expected_margin_rate: number | null;
  actual_margin_amount: number;
  actual_margin_rate: number;
  delta_amount: number;
  delta_rate: number;
  gross_revenue: number;
  total_cost: number;
}

export interface ActualMarginListResponse {
  items: ActualMarginItem[];
  total: number;
  page: number;
  page_size: number;
  year_month: string;
}

export interface ActualMarginFilters {
  year_month?: string;
  page?: number;
  page_size?: number;
}

export const reportsApi = {
  download: async (type: ReportType, ym: string): Promise<void> => {
    const label = REPORT_LABELS[type].replace(/ /g, "");
    const filename = `${ym}_${label}.xlsx`;
    const { getAccessToken } = await import("@/lib/auth");

    const res = await fetch(`/api/v1/reports/export`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getAccessToken() ?? ""}`,
      },
      body: JSON.stringify({
        kind: BACKEND_KIND[type],
        ...monthRange(ym),
        format: "xlsx",
      }),
    });
    if (!res.ok) throw new Error(`다운로드 실패: ${res.status}`);

    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  },

  actualMargin: (filters: ActualMarginFilters = {}) =>
    api.get<ActualMarginListResponse>("/reports/actual-margin", {
      query: filters as Record<string, string | number | undefined>,
    }),
};
