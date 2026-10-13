/**
 * 검증센터 API 클라이언트.
 *
 * 이 화면이 지키는 것
 * ----------------
 * · **PASS / FAIL / NOT_VERIFIED 를 섞지 않는다.** 서버가 셋으로 나눠 주는
 *   것을 그대로 옮긴다. 프론트에서 NOT_VERIFIED 를 PASS 에 더하면
 *   "환불 검증 표본이 없다" 가 "환불 검증이 통과했다" 로 보인다
 * · **임계값을 만들지 않는다.** 판정은 서버가 한다
 * · **모르는 값을 0 으로 그리지 않는다.** UNKNOWN 은 UNKNOWN 으로 보여준다
 */

import { api } from "@/lib/api";

export type Verdict = "PASS" | "FAIL" | "NOT_VERIFIED" | "NOT_AVAILABLE";
export type Confidence = "ACTUAL" | "ESTIMATED" | "UNKNOWN";

/** 사용자에게 보이는 말. 내부 enum 을 그대로 노출하지 않는다. */
export const VERDICT_LABEL: Record<Verdict, string> = {
  PASS: "확인됨",
  FAIL: "불일치",
  NOT_VERIFIED: "미검증",
  NOT_AVAILABLE: "확인 불가",
};

export const CONFIDENCE_LABEL: Record<Confidence, string> = {
  ACTUAL: "실측",
  ESTIMATED: "추정",
  UNKNOWN: "미확인",
};

/** 성분 출처. 서버가 준 코드를 사람 말로 옮긴다. */
export const SOURCE_LABEL: Record<string, string> = {
  SETTLEMENT: "판매처 정산서",
  ORDER_LEDGER: "판매 장부",
  ORDER_PURCHASE_COST: "주문 매입액",
  VARIANT_MASTER: "옵션 마스터 원가",
  PRODUCT_MASTER: "상품 마스터 원가",
  POLICY: "마켓 수수료 정책",
  POLICY_MISSING: "정책 없음",
  POLICY_OR_UNKNOWN: "정책 또는 미확인",
  MASTER_OR_UNKNOWN: "마스터 또는 미확인",
  NO_REVENUE: "매출 없음",
  NOT_AVAILABLE: "원천 없음",
  UNKNOWN: "미확인",
};

export const COMPONENT_LABEL: Record<string, string> = {
  revenue: "매출",
  cost: "원가",
  shipping_cost: "매입 배송비",
  marketplace_fee: "판매수수료",
  ad_cost: "광고비",
  return_cost: "반품비",
  fulfillment_fee: "풀필먼트비",
  payment_fee: "결제수수료",
  vat: "부가세",
};

export interface TraceComponent {
  value: number;
  source: string;
  confidence: Confidence;
}

export interface SyncAccountFreshness {
  account: string;
  configured: boolean;
  blocked_by: string | null;
  last_success_at: string | null;
  last_attempt_at: string | null;
  last_status: string | null;
  last_error_code: string | null;
}

export interface VerificationSummary {
  period: { from: string | null; to: string | null };
  overall: "PASS" | "ATTENTION" | "FAIL";
  verdicts: Record<string, Verdict>;
  counts: Record<string, number>;
  freshness: {
    orders: { state: string; enabled: boolean; accounts: SyncAccountFreshness[] };
    claims: { state: string; enabled: boolean; accounts: SyncAccountFreshness[] };
    settlement: {
      state: string;
      latest_settlement_date: string | null;
      rows_total: number;
      imports: number;
    };
  };
  orders: {
    ledger_orders: number;
    ledger_orders_from_api: number;
    sync_created_since_split: number;
    status: Verdict;
    note: string;
  };
  sync: {
    runs_checked: number;
    reconciliation_failures: number;
    runs_not_succeeded: number;
    legacy_unclassified_runs: number;
    legacy_reason: string | null;
    status: Verdict;
  };
  settlement: {
    rows_persisted: number;
    rows_classified: number;
    unclassified: number;
    by_status: Record<string, number>;
    orders_checked: number;
    amount_verified: number;
    amount_failure_count: number;
    refund_excluded: number;
    refund_formula: Verdict;
    status: Verdict;
    formula: string;
  };
  confidence: {
    orders_total: number;
    no_margin_row: number;
    actual: number;
    estimated: number;
    unknown: number;
    without_calculation_evidence: number;
    buckets_sum_matches_total: boolean;
  };
  coverage: Record<string, { n: number; of: number }>;
  /**
   * 매출을 실정산 확인분과 추정분으로 나눈 것.
   *
   * 두 몫을 더하면 장부 매출이다 — 겹치지 않는다. 정산서가 아직 안 온 주문이
   * 있는 것은 오류가 아니라 coverage 다.
   */
  revenue_split: {
    orders: number;
    ledger_revenue: number;
    settled_orders: number;
    settled_ledger_revenue: number;
    settlement_net_revenue: number;
    settled_difference: number;
    estimated_orders: number;
    estimated_revenue: number;
    note: string;
  };
  inventory: {
    external_reconciliation: Verdict;
    external_reason: string;
    balance_is_derived: boolean;
    balance_note: string;
    checks: Record<string, number>;
    status: Verdict;
  };
  settlement_margin_coverage: {
    settlement_matched_orders: number;
    margin_on_actual_basis: number;
    difference: number;
    status: Verdict;
  };
  integrity: { checks: Record<string, number>; status: Verdict };
  unknown_as_zero: { checks: Record<string, number>; status: Verdict };
  evidence_gates: Record<string, { state: Verdict; reason: string | null }>;
  month_close: { closed_months: number; status: Verdict; reason?: string };
  open_issues: { open_total: number; blocking: number; warning: number };
}

export interface OrderTrace {
  order: {
    sales_order_id: string;
    business_key: string;
    marketplace: string;
    marketplace_order_no: string | null;
    line_no: number | null;
    sale_date: string;
    quantity: number;
    product_name: string | null;
    import_source: string;
    profit_status: string;
  };
  /** SNAPSHOT = 계산 당시 박제된 근거 · RECONSTRUCTED = 지금 원천에서 되짚음 */
  evidence_kind: "SNAPSHOT" | "RECONSTRUCTED";
  formula: string;
  margin_calculated: boolean;
  lineage: string[];
  settlement_linked: boolean;
  components: Record<string, TraceComponent>;
  warnings?: string[];
  stored_amounts?: Record<string, number | string | null>;
  recomputed_margin?: number;
  margin_difference?: number;
  status?: Verdict;
  reason?: string;
}

export interface VerificationExceptionItem {
  code: string;
  count: number;
  detail: string;
  severity: "BLOCKING" | "WARNING";
}

export interface GoldenCandidate {
  sales_order_id: string;
  business_key: string;
  marketplace: string;
  sale_date: string;
  case_type: string;
  selection_reason: string;
  check_source: string;
  check_amounts: string;
}

export interface GoldenCase {
  case_id: string;
  sales_order_id: string;
  business_key: string;
  marketplace: string;
  sale_date: string;
  case_type: string;
  selection_reason: string;
  /** `DEFERRED` 는 **실패가 아니다** — "봤고 지금은 못 정한다" 이다 (M060). */
  status: "CANDIDATE" | "APPROVED" | "DEFERRED" | "RETIRED";
  expected: Record<string, number | null> | null;
  source_evidence: string | null;
  approved_at: string | null;
  revision: number;
  change_reason: string | null;
  defer_reason: string | null;
  deferred_at: string | null;
  note: string | null;
  /** 승인 당시 근거가 박제돼 있나. 없으면 M060 이전 승인이다. */
  has_evidence_snapshot: boolean;
}

export interface GoldenRegression {
  approved_cases: number;
  counts: Record<string, number>;
  cases: {
    case_id: string;
    sales_order_id: string;
    business_key: string;
    case_type: string;
    revision: number;
    verdict: "PASS" | "FAIL" | "NOT_COMPARABLE";
    reason: string | null;
    checks: {
      field: string;
      expected: number;
      actual: number;
      difference: number;
      verdict: "PASS" | "FAIL";
    }[];
  }[];
  status: string;
}

/** 성분 하나의 사실. **value 가 null 이면 모른다는 뜻이다 — 0 이 아니다.** */
export interface EvidenceFact {
  value: number | null;
  source: string;
  confidence: Confidence;
  source_ref: string | null;
  source_date: string | null;
  note: string | null;
}

export interface GoldenEvidence {
  order: {
    sales_order_id: string;
    business_key: string;
    marketplace: string;
    marketplace_order_no: string | null;
    line_no: number | null;
    sale_date: string;
    quantity: number;
    product_name: string | null;
    import_source: string;
    profit_status: string;
    ledger_sale_price: number;
  };
  settlement: {
    where: string;
    file_name: string;
    sheet_name: string | null;
    rows: number;
    period_start: string | null;
    row_refs: {
      row_type: string;
      line_reference: string | null;
      gross_revenue: number;
      marketplace_fee: number;
      other_deductions: number;
      net_settlement: number;
    }[];
  } | null;
  facts: Record<string, EvidenceFact>;
  /** 원천값으로 **따로 계산한** 결과. production 계산기를 부르지 않았다. */
  independent: {
    calculable: boolean;
    net_revenue: number | null;
    margin: number | null;
    assumptions: string[];
    unpriced: string[];
    reason?: string;
    vendor_check?: {
      reconstructed: number;
      vendor_net_settlement: number;
      difference: number;
    };
  };
  comparison: {
    comparable: boolean;
    reason?: string;
    all_match: boolean;
    mismatched: number;
    items: {
      field: string;
      label: string;
      independent: number;
      seller_os: number;
      difference: number;
      match: boolean;
    }[];
  };
  formula: string;
  golden_confidence: Confidence;
  evidence_state: "READY" | "SOURCE_EVIDENCE_INCOMPLETE";
  blockers: string[];
  warnings: string[];
  claims: {
    claim_type: string;
    claim_status: string;
    financial_effect: string;
    claim_quantity: number | null;
    claim_amount: number | null;
  }[];
  refund_financial: string | null;
  checklist: { label: string; detail: string; auto_matched: boolean | null }[];
  stored_calculation_has_evidence: boolean;
  margin_calculated: boolean;
}

export interface RecommendedSet {
  recommended: (GoldenCandidate & { covers: string })[];
  recommended_count: number;
  total_candidates: number;
  uncovered: { case_type: string; reason: string }[];
  note: string;
}

export interface GoldenCoverage {
  by_case_type: {
    case_type: string;
    covers: string;
    approved: number;
    deferred: number;
    candidate: number;
    covered: boolean;
  }[];
  covered: number;
  of: number;
}

/** 보류 사유. 사람 말은 화면이 붙인다. */
export const DEFER_REASON_LABEL: Record<string, string> = {
  SOURCE_MISSING: "원천자료가 부족하다",
  COST_UNCERTAIN: "원가를 더 확인해야 한다",
  FEE_ESTIMATED: "수수료가 추정이라 기준으로 삼기 이르다",
  CLAIM_IMPACT: "취소·반품 영향을 더 봐야 한다",
  OTHER: "기타",
};

export const CASE_TYPE_LABEL: Record<string, string> = {
  SETTLEMENT_EXACT: "정산 매칭",
  SETTLEMENT_WITH_SHIPPING: "배송비 매출 있음",
  SETTLEMENT_WITH_COUPON: "판매자 쿠폰 있음",
  ESTIMATED_ONLY: "정책 추정만",
  MULTI_LINE: "다중 라인",
  CLAIM_LINKED: "취소·반품 연결",
};

export const verificationApi = {
  summary: (params: { period_from?: string; period_to?: string } = {}) =>
    api.get<VerificationSummary>("/verification/summary", { query: params }),

  orderTrace: (salesOrderId: string) =>
    api.get<OrderTrace>(`/verification/orders/${salesOrderId}`),

  exceptions: () =>
    api.get<{
      verification: VerificationExceptionItem[];
      operation_issues: { open_total: number; blocking: number };
    }>("/verification/exceptions"),

  goldenSamples: () =>
    api.get<{
      cases: GoldenCase[];
      regression: GoldenRegression;
      coverage: GoldenCoverage;
      defer_reasons: string[];
    }>("/verification/golden-samples"),

  /** 한 후보의 원천·독립검산·차이를 한 번에. 사용자가 엑셀을 열 필요가 없다. */
  goldenEvidence: (salesOrderId: string) =>
    api.get<GoldenEvidence>(
      `/verification/golden-samples/evidence/${salesOrderId}`,
    ),

  goldenRecommended: () =>
    api.get<RecommendedSet>("/verification/golden-samples/recommended"),

  deferGolden: (caseId: string, body: { reason: string; note?: string }) =>
    api.post<{ case_id: string; status: string; reason: string }>(
      `/verification/golden-samples/${caseId}/defer`,
      body,
    ),

  goldenCandidates: (limit = 20) =>
    api.get<{ candidates: GoldenCandidate[] }>(
      "/verification/golden-samples/candidates",
      { query: { limit } },
    ),

  registerCandidates: (salesOrderIds: string[]) =>
    api.post<{ created: number; skipped_existing: number }>(
      "/verification/golden-samples/candidates",
      { sales_order_ids: salesOrderIds },
    ),

  /**
   * 기준값 승인.
   *
   * `expected` 는 **사용자가 원천을 보고 입력한 값**이어야 한다. 화면이
   * 현재 계산 결과를 채워 넣으면 회귀 검사가 항상 통과한다.
   */
  approveGolden: (
    caseId: string,
    body: {
      expected: Record<string, number | null>;
      source_evidence: string;
      change_reason?: string;
    },
  ) =>
    api.post<{ case_id: string; status: string; revision: number }>(
      `/verification/golden-samples/${caseId}/approve`,
      body,
    ),
};
