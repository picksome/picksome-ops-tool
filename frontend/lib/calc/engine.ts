/**
 * 가격·마진 계산 엔진.
 *
 * 등록가 시뮬레이션 / 마진계산기 / 상품 등록 인라인 계산기가 **이 함수 하나**를 쓴다.
 *
 * 산식 (docs/40_ops_improvement_plan.md R-07):
 *
 *   예상 정산액 = 판매가 + 구매자부담배송비
 *               − 판매수수료 − 수수료부가세 − 판매자부담쿠폰 − 기타판매처차감액
 *               − 결제수수료
 *
 *   상품 공헌이익 = 예상 정산액 − 총원가 − 광고비 − 예상 반품비용
 *
 *   예상 순이익  = 상품 공헌이익 − 고정비 배분액 − 예상 세금
 *
 *   목표 판매가  = (고정 원가 + 목표 이익)
 *                / (1 − 수수료율 − 결제수수료율 − 쿠폰율 − 광고비율 − 반품충당률 − 세금효과율)
 *
 * ⚠️ 세금은 **추정치**다. 과세유형별 규칙은 `TaxRule` 데이터로 주입되며
 * 이 파일에 세율을 하드코딩하지 않는다.
 */

import {
  applyRate,
  divideToWon,
  safeRate,
  splitVatIncluded,
  sumRates,
  toWon,
  type Rate,
  type Won,
} from "@/lib/money";
import type {
  CalcStep,
  MarginCalcInput,
  MarginCalcResult,
  MarketplaceFeePolicy,
  TaxRule,
} from "@/lib/calc/types";

/* ────────────────────────────────────────────────────────────
 * 수수료 정책 해석
 * ──────────────────────────────────────────────────────────── */

/** 정책에서 이 판매가에 적용될 수수료율/정액을 뽑는다. */
export function resolveFee(
  salePrice: Won,
  policy: MarketplaceFeePolicy | null | undefined,
  fallbackRate: Rate,
): { fee: Won; rate: Rate; fixed: Won; label: string } {
  if (!policy) {
    return {
      fee: applyRate(salePrice, fallbackRate),
      rate: fallbackRate,
      fixed: 0,
      label: "직접 입력",
    };
  }
  if (policy.feeType === "FIXED") {
    const fixed = policy.fixedFee ?? 0;
    return { fee: fixed, rate: 0, fixed, label: `${policy.marketplace} 정액` };
  }
  if (policy.feeType === "TIERED" && policy.tiers?.length) {
    for (const tier of policy.tiers) {
      const inRange = salePrice >= tier.min && (tier.max === null || salePrice < tier.max);
      if (!inRange) continue;
      if (tier.rate !== undefined) {
        return {
          fee: applyRate(salePrice, tier.rate),
          rate: tier.rate,
          fixed: 0,
          label: `${policy.marketplace} 구간요율`,
        };
      }
      if (tier.fixed !== undefined) {
        return { fee: tier.fixed, rate: 0, fixed: tier.fixed, label: `${policy.marketplace} 구간정액` };
      }
    }
    return { fee: 0, rate: 0, fixed: 0, label: `${policy.marketplace} 구간 미해당` };
  }
  const rate = policy.feeRate ?? fallbackRate;
  return {
    fee: applyRate(salePrice, rate),
    rate,
    fixed: 0,
    label: `${policy.marketplace} 정책 v${policy.ruleVersion}`,
  };
}

/** 기준일에 유효한 세금 규칙을 고른다. 없으면 null. */
export function resolveTaxRule(rules: TaxRule[], at: string): TaxRule | null {
  const candidates = rules.filter(
    (r) => r.effectiveFrom <= at && (r.effectiveTo === null || r.effectiveTo >= at),
  );
  if (candidates.length === 0) return null;
  // 여러 개면 가장 최근에 시작한 규칙 → 그중 버전이 높은 것
  return candidates.sort(
    (a, b) =>
      b.effectiveFrom.localeCompare(a.effectiveFrom) || b.ruleVersion - a.ruleVersion,
  )[0];
}

/** 기준일 + 마켓 + 카테고리에 맞는 수수료 정책. 카테고리 지정 정책이 기본값보다 우선. */
export function resolveFeePolicy(
  policies: MarketplaceFeePolicy[],
  marketplace: string,
  category: string | null,
  at: string,
): MarketplaceFeePolicy | null {
  const active = policies.filter(
    (p) =>
      p.marketplace === marketplace &&
      p.effectiveFrom <= at &&
      (p.effectiveTo === null || p.effectiveTo >= at),
  );
  if (active.length === 0) return null;
  const exact = active.filter((p) => p.category && category && p.category === category);
  const pool = exact.length > 0 ? exact : active.filter((p) => !p.category);
  const chosen = (pool.length > 0 ? pool : active).sort(
    (a, b) =>
      b.effectiveFrom.localeCompare(a.effectiveFrom) || b.ruleVersion - a.ruleVersion,
  )[0];
  return chosen ?? null;
}

/* ────────────────────────────────────────────────────────────
 * 세금
 * ──────────────────────────────────────────────────────────── */

interface TaxComputation {
  outputVat: Won;
  inputVatCredit: Won;
  vatPayable: Won;
  incomeTax: Won;
  total: Won;
  steps: CalcStep[];
  warnings: string[];
}

/**
 * 과세유형별 세금 추정.
 *
 * 일반과세자:
 *   매출세액 = 판매가 × r/(1+r)   (판매가가 부가세 포함가인 경우)
 *   매입세액 = 적격증빙 매입(원가·수수료·광고비)의 부가세
 *   납부세액 = 매출세액 − 매입세액
 *
 * 간이과세자:
 *   납부세액 = 공급대가 × 업종별 부가가치율 × 세율
 *   매입세액공제 = 매입액 × 고정 공제율
 *
 * ⚠️ 이 함수는 **신고세액을 계산하지 않는다.** 상품 단위 손익을 보기 위한 추정이다.
 */
function computeTax(
  salePrice: Won,
  buyerShipping: Won,
  creditableInputs: Won,
  importVat: Won,
  profitBeforeTax: Won,
  rule: TaxRule,
): TaxComputation {
  const steps: CalcStep[] = [];
  const warnings: string[] = [];
  const p = rule.taxRateParameters;
  const grossRevenue = salePrice + buyerShipping;

  let outputVat = 0;
  let inputVatCredit = 0;
  let vatPayable = 0;

  switch (rule.outputVatPolicy) {
    case "INCLUDED_IN_PRICE": {
      const { vat } = splitVatIncluded(grossRevenue, p.vatRate);
      outputVat = vat;
      steps.push({
        key: "outputVat",
        label: "매출세액",
        formula: `(판매가+배송비) × ${(p.vatRate * 100).toFixed(0)}/${(100 + p.vatRate * 100).toFixed(0)}`,
        amount: outputVat,
        isDeduction: true,
        source: `${rule.description} v${rule.ruleVersion}`,
      });
      break;
    }
    case "ADDED_TO_PRICE": {
      outputVat = applyRate(grossRevenue, p.vatRate);
      steps.push({
        key: "outputVat",
        label: "매출세액",
        formula: `(판매가+배송비) × ${(p.vatRate * 100).toFixed(1)}%`,
        amount: outputVat,
        isDeduction: true,
        source: `${rule.description} v${rule.ruleVersion}`,
      });
      break;
    }
    case "DEEMED_VALUE_ADDED": {
      const deemed = p.deemedValueAddedRate ?? 0;
      if (!p.deemedValueAddedRate) {
        warnings.push("간이과세 업종별 부가가치율이 설정되지 않아 세액을 0으로 계산했습니다.");
      }
      // 납부세액 = 공급대가 × 부가가치율 × 세율
      const base = applyRate(grossRevenue, deemed);
      outputVat = applyRate(base, p.vatRate);
      steps.push({
        key: "outputVat",
        label: "간이과세 납부세액",
        formula: `(판매가+배송비) × 부가가치율 ${(deemed * 100).toFixed(1)}% × ${(p.vatRate * 100).toFixed(0)}%`,
        amount: outputVat,
        isDeduction: true,
        source: `${rule.description} v${rule.ruleVersion}`,
      });
      break;
    }
    case "ZERO_RATED":
      outputVat = 0;
      steps.push({
        key: "outputVat", label: "매출세액 (영세율)", formula: "수출 영세율 — 0원",
        amount: 0, isDeduction: true, source: `${rule.description} v${rule.ruleVersion}`,
      });
      break;
    case "NONE":
    default:
      outputVat = 0;
      steps.push({
        key: "outputVat", label: "매출세액 (면세)", formula: "면세 — 0원",
        amount: 0, isDeduction: true, source: `${rule.description} v${rule.ruleVersion}`,
      });
      break;
  }

  switch (rule.inputVatCreditPolicy) {
    case "FULL_CREDIT": {
      // 적격증빙 매입은 부가세 포함가로 잡혀 있다고 본다 → 포함가에서 세액 분리
      const { vat } = splitVatIncluded(creditableInputs, p.vatRate);
      inputVatCredit = vat;
      steps.push({
        key: "inputVatCredit",
        label: "매입세액 공제",
        formula: `적격증빙 매입 ${creditableInputs.toLocaleString("ko-KR")}원의 부가세`,
        amount: inputVatCredit,
        isDeduction: false,
        source: "일반과세 — 전액 공제",
      });
      break;
    }
    case "FLAT_RATE_CREDIT": {
      const flat = p.inputCreditFlatRate ?? 0;
      inputVatCredit = applyRate(creditableInputs, flat);
      steps.push({
        key: "inputVatCredit",
        label: "매입세액 공제 (간이)",
        formula: `매입액 × ${(flat * 100).toFixed(1)}%`,
        amount: inputVatCredit,
        isDeduction: false,
        source: `${rule.description} v${rule.ruleVersion}`,
      });
      if (!p.inputCreditFlatRate) {
        warnings.push("간이과세 매입세액 공제율이 설정되지 않았습니다.");
      }
      break;
    }
    case "NO_CREDIT":
    default:
      inputVatCredit = 0;
      break;
  }

  if (rule.importVatCreditPolicy === "CREDITABLE" && importVat > 0) {
    inputVatCredit += importVat;
    steps.push({
      key: "importVatCredit",
      label: "수입 부가세 공제",
      formula: "관세청 납부 수입 부가세",
      amount: importVat,
      isDeduction: false,
      source: "수입 부가세 공제 가능",
    });
  }

  vatPayable = Math.max(0, outputVat - inputVatCredit);

  // 이익 관련 세금 — 참고용 실효세율. 적자면 0.
  const incomeRate = p.incomeTaxEffectiveRate ?? 0;
  const incomeTax = profitBeforeTax > 0 ? applyRate(profitBeforeTax, incomeRate) : 0;
  if (incomeRate > 0) {
    steps.push({
      key: "incomeTax",
      label: "소득세 추정",
      formula: `세전이익 × ${(incomeRate * 100).toFixed(1)}% (참고 실효세율)`,
      amount: incomeTax,
      isDeduction: true,
      source: "추정치 — 실제 신고세액과 다를 수 있음",
    });
  }

  if (!rule.verifiedAt) {
    warnings.push(
      `세금 규칙 "${rule.description}" 이 아직 확인되지 않았습니다(verifiedAt 없음). 세무 전문가 검토가 필요합니다.`,
    );
  }

  return {
    outputVat,
    inputVatCredit,
    vatPayable,
    incomeTax,
    total: vatPayable + incomeTax,
    steps,
    warnings,
  };
}

/* ────────────────────────────────────────────────────────────
 * 메인 계산
 * ──────────────────────────────────────────────────────────── */

export function calculateMargin(input: MarginCalcInput): MarginCalcResult {
  const { cost, selling, target, taxRule, feePolicy } = input;
  const steps: CalcStep[] = [];
  const warnings: string[] = [];
  const sp = selling.salePrice;

  /* ── 1. 원가 ── */
  const fx = cost.purchaseCurrency === "KRW" ? 1 : cost.exchangeRate || 1;
  const purchaseCostKrw = toWon((cost.purchasePrice + cost.domesticShippingAbroad) * fx);

  if (cost.purchaseCurrency !== "KRW" && !cost.exchangeRate) {
    warnings.push("환율이 입력되지 않아 1:1 로 계산했습니다.");
  }

  // 수입 부가세는 공제 가능하면 원가가 아니다 — 나중에 돌려받는 돈이기 때문이다.
  const importVatInCost =
    taxRule.importVatCreditPolicy === "CREDITABLE" ? 0 : cost.importVat;

  const totalCost =
    purchaseCostKrw +
    cost.internationalShipping +
    cost.customsDuty +
    importVatInCost +
    cost.domesticShipping +
    cost.packagingCost +
    cost.otherCost;

  steps.push({
    key: "purchase",
    label: "상품 매입원가",
    formula:
      cost.purchaseCurrency === "KRW"
        ? "매입가 + 현지 배송비"
        : `(매입가 + 현지배송) × 환율 ${fx.toLocaleString("ko-KR")}`,
    amount: purchaseCostKrw,
    isDeduction: true,
  });
  if (cost.internationalShipping)
    steps.push({ key: "intlShip", label: "해외 배송비", formula: "직접 입력", amount: cost.internationalShipping, isDeduction: true });
  if (cost.customsDuty)
    steps.push({ key: "customs", label: "관세", formula: "직접 입력", amount: cost.customsDuty, isDeduction: true });
  if (cost.importVat)
    steps.push({
      key: "importVat",
      label: "수입 부가세",
      formula: importVatInCost ? "공제 불가 — 원가에 포함" : "공제 가능 — 원가에서 제외",
      amount: cost.importVat,
      isDeduction: importVatInCost > 0,
      source: taxRule.importVatCreditPolicy === "CREDITABLE" ? "매입세액 공제 대상" : "공제 불가",
    });
  if (cost.domesticShipping)
    steps.push({ key: "domShip", label: "국내 배송비", formula: "직접 입력", amount: cost.domesticShipping, isDeduction: true });
  if (cost.packagingCost)
    steps.push({ key: "packaging", label: "포장비", formula: "직접 입력", amount: cost.packagingCost, isDeduction: true });
  if (cost.otherCost)
    steps.push({ key: "otherCost", label: "기타 원가", formula: "직접 입력", amount: cost.otherCost, isDeduction: true });

  steps.push({
    key: "totalCost", label: "총원가", formula: "위 원가 항목 합계",
    amount: totalCost, isDeduction: true, isSubtotal: true,
  });

  /* ── 2. 판매처 차감 → 예상 정산액 ── */
  const feeResolved = resolveFee(sp, feePolicy, selling.commissionRate);
  const commissionFee = feeResolved.fee;
  // 수수료가 VAT 별도 표기면 부가세를 따로 더한다. 포함이면 0.
  const commissionVatSeparate = feePolicy ? !feePolicy.feeVatIncluded : selling.commissionVatSeparate;
  const commissionVat = commissionVatSeparate
    ? applyRate(commissionFee, taxRule.taxRateParameters.vatRate)
    : 0;

  const paymentFee = applyRate(sp, selling.paymentFeeRate);
  const couponCost = selling.sellerCouponAmount + applyRate(sp, selling.sellerCouponRate);
  const otherDeduction = selling.otherMarketplaceDeduction;

  const expectedSettlement =
    sp + selling.buyerShippingFee - commissionFee - commissionVat - paymentFee - couponCost - otherDeduction;

  steps.push({ key: "salePrice", label: "판매가", formula: "입력값", amount: sp, isDeduction: false });
  if (selling.buyerShippingFee)
    steps.push({ key: "buyerShip", label: "구매자 부담 배송비", formula: "입력값", amount: selling.buyerShippingFee, isDeduction: false });
  steps.push({
    key: "commission", label: "판매수수료",
    formula: feeResolved.fixed
      ? `정액 ${feeResolved.fixed.toLocaleString("ko-KR")}원`
      : `판매가 × ${(feeResolved.rate * 100).toFixed(2)}%`,
    amount: commissionFee, isDeduction: true, source: feeResolved.label,
  });
  if (commissionVat)
    steps.push({
      key: "commissionVat", label: "수수료 부가세",
      formula: `수수료 × ${(taxRule.taxRateParameters.vatRate * 100).toFixed(0)}%`,
      amount: commissionVat, isDeduction: true, source: "수수료 VAT 별도 표기",
    });
  if (paymentFee)
    steps.push({ key: "paymentFee", label: "결제수수료", formula: `판매가 × ${(selling.paymentFeeRate * 100).toFixed(2)}%`, amount: paymentFee, isDeduction: true });
  if (couponCost)
    steps.push({ key: "coupon", label: "판매자 부담 쿠폰", formula: "정액 + 정률", amount: couponCost, isDeduction: true });
  if (otherDeduction)
    steps.push({ key: "otherDeduction", label: "기타 판매처 차감", formula: "직접 입력", amount: otherDeduction, isDeduction: true });

  steps.push({
    key: "settlement", label: "예상 정산액", formula: "판매가 + 배송비 − 판매처 차감액 합계",
    amount: expectedSettlement, isDeduction: false, isSubtotal: true,
  });

  /* ── 3. 공헌이익 ── */
  const adCost = selling.adCostAmount + applyRate(sp, selling.adCostRate);
  const returnCost = applyRate(selling.returnCostPerCase + totalCost, selling.returnRate);

  const contributionProfit = expectedSettlement - totalCost - adCost - returnCost;

  if (adCost)
    steps.push({ key: "adCost", label: "광고비", formula: "정액 + 판매가 × 광고비율", amount: adCost, isDeduction: true });
  if (returnCost)
    steps.push({
      key: "returnCost", label: "예상 반품 비용",
      formula: `(반품 1건당 비용 + 총원가) × 반품률 ${(selling.returnRate * 100).toFixed(1)}%`,
      amount: returnCost, isDeduction: true,
      source: "반품 시 원가도 함께 손실된다고 본다",
    });
  steps.push({
    key: "contribution", label: "상품 공헌이익", formula: "예상 정산액 − 총원가 − 광고비 − 반품비용",
    amount: contributionProfit, isDeduction: false, isSubtotal: true,
  });

  /* ── 4. 세금 → 순이익 ── */
  // 적격증빙 매입: 국내에서 세금계산서/카드전표를 받는 항목만.
  // 해외 매입분은 수입 부가세로 따로 다룬다(중복 공제 방지).
  const creditableInputs =
    cost.domesticShipping +
    cost.packagingCost +
    commissionFee +
    commissionVat +
    paymentFee +
    adCost;

  const profitBeforeTax = contributionProfit - selling.fixedCostAllocation;
  const tax = computeTax(
    sp,
    selling.buyerShippingFee,
    creditableInputs,
    cost.importVat,
    profitBeforeTax,
    taxRule,
  );

  if (selling.fixedCostAllocation)
    steps.push({
      key: "fixedCost", label: "고정비 배분액", formula: "직접 입력 (사업 공통비 배분)",
      amount: selling.fixedCostAllocation, isDeduction: true,
    });
  steps.push(...tax.steps);

  const netProfit = contributionProfit - selling.fixedCostAllocation - tax.total;

  steps.push({
    key: "netProfit", label: "예상 순이익", formula: "공헌이익 − 고정비 − 예상 세금",
    amount: netProfit, isDeduction: false, isSubtotal: true,
  });

  warnings.push(...tax.warnings);
  if (!feePolicy) {
    warnings.push("판매처 수수료 정책을 찾지 못해 직접 입력한 요율로 계산했습니다.");
  }
  if (sp > 0 && netProfit < 0) {
    warnings.push("이 조건에서는 적자입니다. 판매가·원가·광고비를 확인하세요.");
  }

  /* ── 5. 역산 ── */
  /**
   * 판매가에 비례하는 비용의 합계 요율.
   * 수수료·결제수수료·쿠폰율·광고비율은 판매가에 비례한다.
   * 반품충당은 (반품비 + 원가) × 반품률이라 판매가에 비례하지 않으므로 고정비로 넣는다.
   * 세금효과율은 매출세액이 판매가에 비례하는 만큼만 반영한다.
   */
  const vatRate = taxRule.taxRateParameters.vatRate;
  let taxEffectRate: Rate = 0;
  if (taxRule.outputVatPolicy === "INCLUDED_IN_PRICE") {
    taxEffectRate = vatRate / (1 + vatRate);
  } else if (taxRule.outputVatPolicy === "ADDED_TO_PRICE") {
    taxEffectRate = vatRate;
  } else if (taxRule.outputVatPolicy === "DEEMED_VALUE_ADDED") {
    taxEffectRate = (taxRule.taxRateParameters.deemedValueAddedRate ?? 0) * vatRate;
  }

  const commissionRateForSolve = feePolicy?.feeType === "RATE" || !feePolicy
    ? feeResolved.rate * (commissionVatSeparate ? 1 + vatRate : 1)
    : 0;
  const variableRate = sumRates(
    commissionRateForSolve,
    selling.paymentFeeRate,
    selling.sellerCouponRate,
    selling.adCostRate,
    taxEffectRate,
  );

  /** 판매가와 무관한 고정 비용 합계. */
  const fixedOutflow =
    totalCost +
    feeResolved.fixed * (commissionVatSeparate ? 1 + vatRate : 1) +
    selling.sellerCouponAmount +
    otherDeduction +
    selling.adCostAmount +
    returnCost +
    selling.fixedCostAllocation -
    selling.buyerShippingFee -
    tax.inputVatCredit;

  function solvePrice(extraProfit: Won, marginRate: Rate): Won | null {
    // sp × (1 − variableRate − marginRate) = fixedOutflow + extraProfit
    const denom = 1 - variableRate - marginRate;
    if (denom <= 0) return null;
    return divideToWon(fixedOutflow + extraProfit, denom);
  }

  const breakEvenPrice = solvePrice(0, 0);
  const targetProfitPrice = solvePrice(target.targetProfit, 0);
  const targetMarginPrice = solvePrice(0, target.targetMarginRate);

  if (targetMarginPrice === null && target.targetMarginRate > 0) {
    warnings.push(
      `목표 마진율 ${(target.targetMarginRate * 100).toFixed(1)}% 는 변동비 합계(${(variableRate * 100).toFixed(1)}%)를 감안하면 달성할 수 없습니다.`,
    );
  }

  return {
    totalCost,
    purchaseCostKrw,
    commissionFee,
    commissionVat,
    paymentFee,
    couponCost,
    otherDeduction,
    expectedSettlement,
    adCost,
    returnCost,
    contributionProfit,
    estimatedTax: tax.total,
    outputVat: tax.outputVat,
    inputVatCredit: tax.inputVatCredit,
    netProfit,
    marginRate: safeRate(netProfit, sp),
    contributionRate: safeRate(contributionProfit, sp),
    breakEvenPrice,
    targetProfitPrice,
    targetMarginPrice,
    steps,
    warnings,
    appliedRules: {
      taxRuleId: taxRule.id,
      taxRuleVersion: taxRule.ruleVersion,
      feePolicyId: feePolicy?.id ?? null,
      feePolicyVersion: feePolicy?.ruleVersion ?? null,
      calculatedAt: new Date().toISOString(),
    },
  };
}

/* ────────────────────────────────────────────────────────────
 * 기본 입력값
 * ──────────────────────────────────────────────────────────── */

export function emptyCalcInput(taxRule: TaxRule): MarginCalcInput {
  return {
    cost: {
      purchasePrice: 0,
      purchaseCurrency: "KRW",
      exchangeRate: 1,
      domesticShippingAbroad: 0,
      internationalShipping: 0,
      customsDuty: 0,
      importVat: 0,
      domesticShipping: 0,
      packagingCost: 0,
      otherCost: 0,
    },
    selling: {
      salePrice: 0,
      buyerShippingFee: 0,
      marketplace: "COUPANG",
      category: null,
      commissionRate: 0.11,
      commissionVatSeparate: false,
      paymentFeeRate: 0,
      sellerCouponAmount: 0,
      sellerCouponRate: 0,
      otherMarketplaceDeduction: 0,
      adCostAmount: 0,
      adCostRate: 0,
      returnRate: 0,
      returnCostPerCase: 0,
      fixedCostAllocation: 0,
      quantity: 1,
    },
    target: { targetProfit: 0, targetMarginRate: 0.2 },
    taxRule,
    feePolicy: null,
  };
}

/**
 * 판매처별 비교 — 같은 원가·같은 판매가일 때 어디가 유리한지.
 * "어디에 올릴까"는 셀러가 매일 하는 판단인데 지금은 계산기를 여러 번 돌려야 알 수 있다.
 */
export function compareMarketplaces(
  base: MarginCalcInput,
  marketplaces: string[],
  policies: MarketplaceFeePolicy[],
  at: string,
): Array<{
  marketplace: string;
  policy: MarketplaceFeePolicy | null;
  commissionRate: Rate;
  result: MarginCalcResult;
}> {
  return marketplaces
    .map((mk) => {
      const policy = resolveFeePolicy(policies, mk, base.selling.category, at);
      const input: MarginCalcInput = {
        ...base,
        selling: { ...base.selling, marketplace: mk },
        feePolicy: policy,
      };
      const result = calculateMargin(input);
      return {
        marketplace: mk,
        policy,
        commissionRate: policy?.feeRate ?? base.selling.commissionRate,
        result,
      };
    })
    .sort((a, b) => b.result.netProfit - a.result.netProfit);
}
