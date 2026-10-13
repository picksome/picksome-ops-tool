/**
 * 수익배분 waterfall 계산.
 *
 * 단순 퍼센트 분배가 아니다. 참여자 유형과 지급 방식이 섞이면
 * **차감 순서가 결과를 바꾼다.** 순서를 코드 한 곳에 고정한다.
 *
 * ⚠️ 가장 중요한 구분:
 *   - **직원 급여는 비용**이다 → 순이익 계산 *전에* 빠진다
 *   - **동업자 배분은 이익 처분**이다 → 순이익 계산 *후에* 나뉜다
 *   직원을 배분 비율 표에 넣으면 급여가 비용으로 안 잡혀 순이익이 과대계상되고,
 *   세무 리포트의 손익이 실제와 어긋난다.
 *
 * 계산 순서:
 *   1. 총매출
 *   2. − 판매처 차감액 및 변동비
 *   3. − 사업비용
 *   4. − 세금·반품 충당금
 *   5. − 고정 지급액 (직원 급여·대표 정액 등)      ← priority 오름차순
 *   6. − 투자자 우선 배분 / 원금 회수
 *   7. = 잔여 배분 가능 이익
 *   8. → 참여자별 비율 배분
 */

import { applyRate, distributeByRatio, toWon, type Won } from "@/lib/money";
import type { DistributionResult, DistributionRule } from "@/lib/adapters/types";

export interface DistributionBase {
  yearMonth: string;
  grossRevenue: Won;
  marketplaceDeduction: Won;
  variableCost: Won;
  businessExpense: Won;
  taxAndReturnReserve: Won;
}

const TYPE_LABEL: Record<DistributionRule["participantType"], string> = {
  OWNER: "대표자",
  CO_FOUNDER: "공동사업자",
  INVESTOR: "투자자",
  EMPLOYEE: "직원",
  CONTRACTOR: "외주",
};

const BASE_LABEL: Record<DistributionRule["calculationBase"], string> = {
  FIXED: "고정 지급액",
  NET_PROFIT_RATE: "순이익 비율",
  REVENUE_RATE: "매출 비율",
  PRODUCT_PROFIT_RATE: "상품별 이익 비율",
  EXCESS_OVER_THRESHOLD: "기준금액 초과분",
  TIERED_BONUS: "단계별 성과급",
  INVESTOR_PRINCIPAL: "투자 원금 회수",
};

/** 규칙이 해당 월에 유효한지. */
function isEffective(rule: DistributionRule, yearMonth: string): boolean {
  if (!rule.isActive) return false;
  const monthStart = `${yearMonth}-01`;
  const monthEnd = `${yearMonth}-31`;
  if (rule.effectiveFrom > monthEnd) return false;
  if (rule.effectiveTo && rule.effectiveTo < monthStart) return false;
  return true;
}

/** 상한·하한 적용. */
function clamp(amount: Won, rule: DistributionRule): Won {
  let v = amount;
  if (rule.minimumAmount !== null && v < rule.minimumAmount) v = rule.minimumAmount;
  if (rule.capAmount !== null && v > rule.capAmount) v = rule.capAmount;
  return v;
}

export function calculateDistribution(
  base: DistributionBase,
  rules: DistributionRule[],
): DistributionResult {
  const warnings: string[] = [];
  const steps: DistributionResult["steps"] = [];
  const payouts: DistributionResult["payouts"] = [];

  const active = rules
    .filter((r) => isEffective(r, base.yearMonth))
    .sort((a, b) => a.priority - b.priority);

  /* 1~4단계 */
  steps.push({ label: "총매출", amount: base.grossRevenue, isDeduction: false });
  steps.push({
    label: "판매처 차감액", amount: base.marketplaceDeduction, isDeduction: true,
    note: "수수료·배송비·광고·쿠폰 차감",
  });
  steps.push({ label: "변동비 (상품 원가 등)", amount: base.variableCost, isDeduction: true });
  steps.push({ label: "사업비용", amount: base.businessExpense, isDeduction: true });
  steps.push({
    label: "세금·반품 충당금", amount: base.taxAndReturnReserve, isDeduction: true,
    note: "추정치 — 실제 신고세액과 다를 수 있습니다",
  });

  let remaining =
    base.grossRevenue -
    base.marketplaceDeduction -
    base.variableCost -
    base.businessExpense -
    base.taxAndReturnReserve;

  const operatingProfit = remaining;
  steps.push({ label: "영업이익", amount: operatingProfit, isDeduction: false });

  /* 5단계 — 고정 지급액 */
  let fixedPayouts = 0;
  for (const rule of active.filter((r) => r.calculationBase === "FIXED")) {
    const amount = clamp(rule.fixedAmount ?? 0, rule);
    if (amount <= 0) continue;
    fixedPayouts += amount;
    remaining -= amount;
    payouts.push({
      participantId: rule.participantId,
      participantName: rule.participantName,
      participantType: rule.participantType,
      ruleLabel: BASE_LABEL.FIXED,
      amount,
      basis:
        rule.participantType === "EMPLOYEE" || rule.participantType === "CONTRACTOR"
          ? "고정 급여 — 비용으로 차감됩니다"
          : "정액 우선 지급",
    });
  }
  if (fixedPayouts > 0) {
    steps.push({
      label: "고정 지급액", amount: fixedPayouts, isDeduction: true,
      note: "직원 급여는 이익 배분이 아니라 비용입니다",
    });
  }

  /* 6단계 — 투자자 원금 회수 / 우선 배분 */
  let investorPriority = 0;
  for (const rule of active.filter((r) => r.calculationBase === "INVESTOR_PRINCIPAL")) {
    if (remaining <= 0) break;
    const target = rule.fixedAmount ?? 0;
    const amount = clamp(Math.min(target, Math.max(0, remaining)), rule);
    if (amount <= 0) continue;
    investorPriority += amount;
    remaining -= amount;
    payouts.push({
      participantId: rule.participantId,
      participantName: rule.participantName,
      participantType: rule.participantType,
      ruleLabel: BASE_LABEL.INVESTOR_PRINCIPAL,
      amount,
      basis: "투자 원금 우선 회수",
    });
  }
  if (investorPriority > 0) {
    steps.push({ label: "투자자 우선 배분", amount: investorPriority, isDeduction: true });
  }

  /* 기준금액 초과분 · 단계별 성과급 — 비율 배분 전에 처리한다 */
  for (const rule of active.filter((r) => r.calculationBase === "EXCESS_OVER_THRESHOLD")) {
    const threshold = rule.threshold ?? 0;
    const excess = Math.max(0, remaining - threshold);
    if (excess <= 0) continue;
    const amount = clamp(applyRate(excess, rule.percentage ?? 0), rule);
    if (amount <= 0) continue;
    remaining -= amount;
    payouts.push({
      participantId: rule.participantId,
      participantName: rule.participantName,
      participantType: rule.participantType,
      ruleLabel: BASE_LABEL.EXCESS_OVER_THRESHOLD,
      amount,
      basis: `${threshold.toLocaleString("ko-KR")}원 초과분의 ${((rule.percentage ?? 0) * 100).toFixed(1)}%${
        rule.capAmount ? ` (한도 ${rule.capAmount.toLocaleString("ko-KR")}원)` : ""
      }`,
    });
  }

  for (const rule of active.filter((r) => r.calculationBase === "TIERED_BONUS")) {
    if (!rule.tiers?.length) continue;
    let bonus = 0;
    const explain: string[] = [];
    for (const tier of rule.tiers) {
      const upper = tier.to ?? Number.MAX_SAFE_INTEGER;
      const slice = Math.max(0, Math.min(remaining, upper) - tier.from);
      if (slice <= 0) continue;
      const part = applyRate(slice, tier.rate);
      bonus += part;
      explain.push(
        `${tier.from.toLocaleString("ko-KR")}~${tier.to ? tier.to.toLocaleString("ko-KR") : "이상"} 구간 ${(tier.rate * 100).toFixed(1)}%`,
      );
    }
    const amount = clamp(bonus, rule);
    if (amount <= 0) continue;
    remaining -= amount;
    payouts.push({
      participantId: rule.participantId,
      participantName: rule.participantName,
      participantType: rule.participantType,
      ruleLabel: BASE_LABEL.TIERED_BONUS,
      amount,
      basis: explain.join(", "),
    });
  }

  /* 7단계 — 잔여 배분 가능 이익 */
  const distributableProfit = Math.max(0, remaining);
  steps.push({
    label: "배분 가능 이익", amount: distributableProfit, isDeduction: false,
    note: remaining < 0 ? "적자 — 비율 배분은 이루어지지 않습니다" : undefined,
  });

  if (remaining < 0) {
    warnings.push(
      `고정 지급액과 우선 배분이 영업이익을 ${Math.abs(remaining).toLocaleString("ko-KR")}원 초과합니다. 이 달은 비율 배분이 없습니다.`,
    );
  }

  /* 8단계 — 비율 배분 */
  const revenueRules = active.filter((r) => r.calculationBase === "REVENUE_RATE");
  for (const rule of revenueRules) {
    const amount = clamp(applyRate(base.grossRevenue, rule.percentage ?? 0), rule);
    if (amount <= 0) continue;
    payouts.push({
      participantId: rule.participantId,
      participantName: rule.participantName,
      participantType: rule.participantType,
      ruleLabel: BASE_LABEL.REVENUE_RATE,
      amount,
      basis: `총매출의 ${((rule.percentage ?? 0) * 100).toFixed(1)}%`,
    });
  }

  const profitRules = active.filter(
    (r) => r.calculationBase === "NET_PROFIT_RATE" || r.calculationBase === "PRODUCT_PROFIT_RATE",
  );

  if (profitRules.length > 0 && distributableProfit > 0) {
    const ratios = profitRules.map((r) => r.percentage ?? 0);
    const sum = ratios.reduce((a, b) => a + b, 0);
    if (Math.abs(sum - 1) > 0.0001) {
      warnings.push(
        `순이익 배분 비율의 합이 ${(sum * 100).toFixed(1)}% 입니다. 100% 가 되어야 잔액이 정확히 나뉩니다.`,
      );
    }
    // 잔수는 1원 단위로 비율이 큰 쪽부터 나눠준다 — 합계가 배분 가능액과 정확히 일치해야 한다.
    const normalized = sum > 0 ? ratios.map((r) => r / sum) : ratios;
    const amounts = distributeByRatio(distributableProfit, normalized);
    profitRules.forEach((rule, i) => {
      const amount = clamp(amounts[i] ?? 0, rule);
      if (amount === 0) return;
      payouts.push({
        participantId: rule.participantId,
        participantName: rule.participantName,
        participantType: rule.participantType,
        ruleLabel: BASE_LABEL[rule.calculationBase],
        amount,
        basis: `배분 가능 이익의 ${((rule.percentage ?? 0) * 100).toFixed(1)}%${
          sum !== 1 ? ` (합계 ${(sum * 100).toFixed(1)}% 기준 정규화)` : ""
        }`,
      });
    });
  } else if (profitRules.length === 0) {
    warnings.push("순이익 비율 배분 규칙이 없습니다. 잔여 이익이 배분되지 않고 남습니다.");
  }

  return {
    yearMonth: base.yearMonth,
    grossRevenue: base.grossRevenue,
    marketplaceDeduction: base.marketplaceDeduction,
    variableCost: base.variableCost,
    businessExpense: base.businessExpense,
    taxAndReturnReserve: base.taxAndReturnReserve,
    fixedPayouts,
    investorPriority,
    distributableProfit,
    payouts,
    steps,
    warnings,
  };
}

/** 참여자 유형 한글 라벨. */
export function participantTypeLabel(t: DistributionRule["participantType"]): string {
  return TYPE_LABEL[t];
}

/** 지급 방식 한글 라벨. */
export function calculationBaseLabel(b: DistributionRule["calculationBase"]): string {
  return BASE_LABEL[b];
}

/** 여러 월을 합산 — 기간 선택 시 사용. */
export function aggregateDistribution(results: DistributionResult[]) {
  const byParticipant = new Map<
    string,
    { name: string; type: DistributionRule["participantType"]; total: Won }
  >();
  for (const r of results) {
    for (const p of r.payouts) {
      const prev = byParticipant.get(p.participantId);
      byParticipant.set(p.participantId, {
        name: p.participantName,
        type: p.participantType,
        total: (prev?.total ?? 0) + p.amount,
      });
    }
  }
  return {
    months: results.length,
    grossRevenue: toWon(results.reduce((a, r) => a + r.grossRevenue, 0)),
    distributableProfit: toWon(results.reduce((a, r) => a + r.distributableProfit, 0)),
    participants: [...byParticipant.entries()]
      .map(([id, v]) => ({ participantId: id, ...v }))
      .sort((a, b) => b.total - a.total),
  };
}
