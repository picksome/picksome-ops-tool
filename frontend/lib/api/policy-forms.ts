/**
 * 정책 화면이 공유하는 목록·라벨 (Phase 13 마무리).
 *
 * 왜 따로 두나
 * ----------
 * 세금·수수료·비용 카테고리 화면이 전부 같은 `/policies/{kind}` 를 쓰는데,
 * 선택지 목록을 화면마다 적으면 백엔드 enum 이 바뀔 때 한 곳만 고쳐진다.
 * 그러면 화면은 저장 버튼을 누르는 순간 422 를 받는다.
 *
 * **여기 값은 DB enum 과 정확히 같아야 한다.** 없는 값을 넣으면 서버가 거부한다.
 */

/** `expense_category_policy.kind` — enum_expense_category_kind */
export const EXPENSE_CATEGORY_KINDS = [
  "COGS", "MARKETING", "LOGISTICS", "OFFICE", "UTILITY",
  "LABOR", "TAX", "FEE", "SUBSCRIPTION", "OTHER",
] as const;
export type ExpenseCategoryKind = (typeof EXPENSE_CATEGORY_KINDS)[number];

export const EXPENSE_CATEGORY_KIND_LABEL: Record<ExpenseCategoryKind, string> = {
  COGS: "상품 원가",
  MARKETING: "광고·마케팅",
  LOGISTICS: "물류·배송",
  OFFICE: "사무·비품",
  UTILITY: "공과금",
  LABOR: "인건비",
  TAX: "세금·공과",
  FEE: "수수료",
  SUBSCRIPTION: "구독·소프트웨어",
  OTHER: "기타",
};

/** `tax_policy.tax_type` */
export const TAX_TYPES = ["GENERAL", "SIMPLE", "EXEMPT"] as const;
export type TaxType = (typeof TAX_TYPES)[number];
export const TAX_TYPE_LABEL: Record<TaxType, string> = {
  GENERAL: "일반과세",
  SIMPLE: "간이과세",
  EXEMPT: "면세",
};

/** `tax_policy.calculation_method` */
export const TAX_CALC_METHODS = [
  "STANDARD_RATIO", "SIMPLE_DEEMED", "EXEMPT", "OVERSEAS_ZERO_RATED",
] as const;
export type TaxCalcMethod = (typeof TAX_CALC_METHODS)[number];
export const TAX_CALC_METHOD_LABEL: Record<TaxCalcMethod, string> = {
  STANDARD_RATIO: "일반 (매출세액 − 매입세액)",
  SIMPLE_DEEMED: "간이 (업종별 부가가치율)",
  EXEMPT: "면세 (부가세 없음)",
  OVERSEAS_ZERO_RATED: "영세율 (수출)",
};

/** `expenses.vat_inclusion_type` 과 같은 enum */
export const VAT_INCLUSIONS = ["INCLUDED", "EXCLUDED", "EXEMPT"] as const;
export type VatInclusion = (typeof VAT_INCLUSIONS)[number];
export const VAT_INCLUSION_LABEL: Record<VatInclusion, string> = {
  INCLUDED: "부가세 포함",
  EXCLUDED: "부가세 별도",
  EXEMPT: "면세",
};

/** `marketplace_fee_policy.fee_type` */
export const FEE_TYPES = ["RATE", "FIXED", "TIERED"] as const;
export type FeeType = (typeof FEE_TYPES)[number];
export const FEE_TYPE_LABEL: Record<FeeType, string> = {
  RATE: "정률 (%)",
  FIXED: "정액 (원)",
  TIERED: "구간별",
};

/** `enum_business_type` — 조직 형태별로 세금 계산이 다르다. */
export const BUSINESS_TYPES = [
  "SOLE_PROPRIETOR", "JOINT_BUSINESS", "CORPORATION", "TEAM_OPERATION",
] as const;
export type BusinessType = (typeof BUSINESS_TYPES)[number];
export const BUSINESS_TYPE_LABEL: Record<BusinessType, string> = {
  SOLE_PROPRIETOR: "개인사업자",
  JOINT_BUSINESS: "공동사업자",
  CORPORATION: "법인",
  TEAM_OPERATION: "팀 운영",
};

/** 오늘(YYYY-MM-DD). 새 정책의 기본 적용 시작일. */
export function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * 다음 버전 번호.
 *
 * 정책은 버전으로 과거를 재현한다. 같은 번호를 두 번 쓰면 "그때 어느 값이었나" 를
 * 답할 수 없다.
 */
export function nextVersion(existing: { version: number }[]): number {
  return existing.reduce((m, p) => Math.max(m, p.version), 0) + 1;
}
