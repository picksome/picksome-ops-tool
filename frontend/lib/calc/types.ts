/**
 * 가격·마진 계산 엔진 — 타입 정의.
 *
 * 등록가 시뮬레이션과 마진계산기, 상품 등록 화면의 인라인 계산기가
 * **모두 이 타입과 이 엔진 하나를 쓴다.** 화면마다 식이 갈리면
 * "계산기에선 20%인데 상품 화면에선 18%" 가 된다.
 *
 * 금액은 전부 **원 단위 정수**(`Won`), 비율은 소수(0.11 = 11%)다.
 * 산술은 `lib/money.ts` 를 통해서만 한다 — 직접 `*` 로 곱하지 않는다.
 */

import type { Rate, Won } from "@/lib/money";

/* ────────────────────────────────────────────────────────────
 * 세금 규칙 모델
 *
 * 간이과세/일반과세를 코드에 하드코딩하지 않는다. 세법은 자주 바뀌고,
 * 업종별 부가가치율·기준금액은 세무사 확인이 필요한 값이다.
 * 규칙을 데이터로 두고 버전·유효기간을 붙여 **과거 계산을 재현**할 수 있게 한다.
 * ──────────────────────────────────────────────────────────── */

export type TaxProfileType =
  | "GENERAL"   // 일반과세자
  | "SIMPLE"    // 간이과세자
  | "EXEMPT"    // 면세사업자
  | "CORPORATE"; // 법인

/** 매출세액을 어떻게 볼 것인가. */
export type OutputVatPolicy =
  /** 판매가가 부가세 포함가. 매출세액 = 판매가 × r/(1+r). 국내 소비자 판매의 기본. */
  | "INCLUDED_IN_PRICE"
  /** 판매가가 공급가액. 매출세액 = 판매가 × r. */
  | "ADDED_TO_PRICE"
  /** 간이과세 — 납부세액 = 공급대가 × 업종별 부가가치율 × 세율. */
  | "DEEMED_VALUE_ADDED"
  /** 면세 — 매출세액 없음. */
  | "NONE"
  /** 수출 영세율 — 매출세액 0, 매입세액은 공제. */
  | "ZERO_RATED";

/** 매입세액 공제를 어떻게 볼 것인가. */
export type InputVatCreditPolicy =
  /** 적격증빙 매입의 부가세를 전액 공제 (일반과세자). */
  | "FULL_CREDIT"
  /** 매입액 × 고정 공제율 (간이과세자). */
  | "FLAT_RATE_CREDIT"
  /** 공제 없음 (면세사업자). */
  | "NO_CREDIT";

/** 수입 부가세(관세청 납부분) 공제 여부. */
export type ImportVatCreditPolicy = "CREDITABLE" | "NOT_CREDITABLE";

export interface TaxRateParameters {
  /** 부가가치세율. 보통 0.1 */
  vatRate: Rate;
  /**
   * 간이과세 업종별 부가가치율. `DEEMED_VALUE_ADDED` 일 때만 사용.
   * ⚠️ 업종 판정과 실제 비율은 **세무사 확인 필요**. 기본값을 정답으로 취급하지 않는다.
   */
  deemedValueAddedRate?: Rate;
  /** 간이과세 매입세액 공제율. `FLAT_RATE_CREDIT` 일 때만 사용. */
  inputCreditFlatRate?: Rate;
  /**
   * 소득세·법인세 등 이익에 부과되는 세금의 개략 실효세율.
   * 순이익 추정에만 쓰는 **참고값**이다. 실제 신고세액이 아니다.
   */
  incomeTaxEffectiveRate?: Rate;
}

export interface TaxRule {
  id: string;
  taxProfileType: TaxProfileType;
  /** YYYY-MM-DD */
  effectiveFrom: string;
  /** YYYY-MM-DD. null = 현재 유효 */
  effectiveTo: string | null;
  outputVatPolicy: OutputVatPolicy;
  inputVatCreditPolicy: InputVatCreditPolicy;
  importVatCreditPolicy: ImportVatCreditPolicy;
  taxRateParameters: TaxRateParameters;
  description: string;
  /** 세무사·공식 자료로 확인한 시점. null 이면 화면에 "미확인" 경고를 띄운다. */
  verifiedAt: string | null;
  /** 규칙 버전. 계산 결과에 박제되어 과거 재현에 쓰인다. */
  ruleVersion: number;
}

/* ────────────────────────────────────────────────────────────
 * 판매처 수수료 정책
 * ──────────────────────────────────────────────────────────── */

export type FeeType = "RATE" | "FIXED" | "TIERED";

export interface FeeTier {
  min: Won;
  max: Won | null;
  rate?: Rate;
  fixed?: Won;
}

export interface MarketplaceFeePolicy {
  id: string;
  marketplace: string;
  /** null = 전체 카테고리 기본값 */
  category: string | null;
  feeType: FeeType;
  feeRate: Rate | null;
  fixedFee: Won | null;
  tiers?: FeeTier[];
  /**
   * 수수료율이 부가세를 포함한 값인지.
   * 마켓마다 표기가 다르다. 틀리면 수수료 자체가 10% 어긋난다.
   */
  feeVatIncluded: boolean;
  effectiveFrom: string;
  effectiveTo: string | null;
  description: string;
  /** 근거 (마켓 공지 URL, 계약서 등) */
  sourceReference: string | null;
  ruleVersion: number;
}

/* ────────────────────────────────────────────────────────────
 * 계산 입력
 * ──────────────────────────────────────────────────────────── */

export interface CostInputs {
  /** 상품 매입가 (매입 통화 기준) */
  purchasePrice: Won;
  /** 매입 통화. KRW 면 환율 무시 */
  purchaseCurrency: string;
  /** 매입 통화 → KRW 환율. KRW 이면 1 */
  exchangeRate: number;
  /** 현지(중국) 내 배송비 — 매입 통화 기준 */
  domesticShippingAbroad: Won;
  /** 해외 배송비 (국제 운송) — 원화 */
  internationalShipping: Won;
  /** 관세 — 원화 */
  customsDuty: Won;
  /** 수입 부가세 — 원화. 공제 가능 여부는 세금 규칙이 결정한다 */
  importVat: Won;
  /** 국내 배송비 (판매자 부담) */
  domesticShipping: Won;
  /** 포장비 */
  packagingCost: Won;
  /** 기타 원가 */
  otherCost: Won;
}

export interface SellingInputs {
  /** 판매가 (마진 계산 모드에서 입력) */
  salePrice: Won;
  /** 구매자 부담 배송비 — 정산액에 더해진다 */
  buyerShippingFee: Won;
  marketplace: string;
  category: string | null;
  /** 판매수수료율. 정책에서 자동 채우되 사용자가 덮어쓸 수 있다 */
  commissionRate: Rate;
  /** 수수료에 부가세가 별도로 붙는지 */
  commissionVatSeparate: boolean;
  /** 결제(PG) 수수료율 */
  paymentFeeRate: Rate;
  /** 판매자 부담 쿠폰·즉시할인 (정액) */
  sellerCouponAmount: Won;
  /** 판매자 부담 쿠폰율 (정률). 정액과 병행 가능 */
  sellerCouponRate: Rate;
  /** 기타 판매처 차감액 */
  otherMarketplaceDeduction: Won;
  /** 광고비 — 정액 */
  adCostAmount: Won;
  /** 광고비율 — 판매가 대비 */
  adCostRate: Rate;
  /** 반품 예상률 */
  returnRate: Rate;
  /** 반품 1건당 비용 */
  returnCostPerCase: Won;
  /** 고정비 배분액 (사업 공통비를 상품 단위로 배분한 금액) */
  fixedCostAllocation: Won;
  /** 판매 수량 — 총액 표시에만 사용. 단가 계산은 1개 기준 */
  quantity: number;
}

export interface TargetInputs {
  /** 목표 이익 (원) — 목표 이익 판매가 역산에 사용 */
  targetProfit: Won;
  /** 목표 마진율 — 목표 마진 판매가 역산에 사용 */
  targetMarginRate: Rate;
}

export interface MarginCalcInput {
  cost: CostInputs;
  selling: SellingInputs;
  target: TargetInputs;
  taxRule: TaxRule;
  /** 적용된 수수료 정책 — 계산 결과에 박제된다 */
  feePolicy?: MarketplaceFeePolicy | null;
}

/* ────────────────────────────────────────────────────────────
 * 계산 출력
 * ──────────────────────────────────────────────────────────── */

/** 계산 근거 한 줄. 화면의 "계산 과정" 패널이 이 배열을 그대로 렌더한다. */
export interface CalcStep {
  key: string;
  label: string;
  /** 사람이 읽는 산식. 예: "32,900 × 11.0%" */
  formula: string;
  amount: Won;
  /** 차감 항목이면 true — 화면에서 −로 표시 */
  isDeduction: boolean;
  /** 적용 근거 (정책 이름·버전) */
  source?: string;
  /** 소계 줄이면 true — 굵게 표시 */
  isSubtotal?: boolean;
}

export interface MarginCalcResult {
  /* 원가 */
  /** 총원가 = 매입가(환산) + 현지배송 + 국제배송 + 관세 + 수입부가세(공제불가분) + 국내배송 + 포장 + 기타 */
  totalCost: Won;
  /** 매입가 환산액 (KRW) */
  purchaseCostKrw: Won;

  /* 판매처 차감 */
  /** 예상 판매수수료 (부가세 별도분 포함) */
  commissionFee: Won;
  commissionVat: Won;
  paymentFee: Won;
  couponCost: Won;
  otherDeduction: Won;
  /** 예상 정산액 */
  expectedSettlement: Won;

  /* 이익 */
  adCost: Won;
  returnCost: Won;
  /** 상품 공헌이익 */
  contributionProfit: Won;
  /** 예상 세금 (부가세 납부 추정 + 이익 관련 세금 추정) */
  estimatedTax: Won;
  /** 매출세액 */
  outputVat: Won;
  /** 매입세액 공제 */
  inputVatCredit: Won;
  /** 예상 순이익 */
  netProfit: Won;
  /** 마진율 = 순이익 / 판매가 */
  marginRate: Rate | null;
  /** 공헌이익률 */
  contributionRate: Rate | null;

  /* 역산 */
  /** 손익분기 판매가 (순이익 0) */
  breakEvenPrice: Won | null;
  /** 목표 이익 달성 판매가 */
  targetProfitPrice: Won | null;
  /** 목표 마진율 달성 판매가 */
  targetMarginPrice: Won | null;

  /* 부가 */
  steps: CalcStep[];
  warnings: string[];
  /** 계산에 쓰인 규칙 버전 — 결과 저장 시 함께 박제한다 */
  appliedRules: {
    taxRuleId: string;
    taxRuleVersion: number;
    feePolicyId: string | null;
    feePolicyVersion: number | null;
    calculatedAt: string;
  };
}
