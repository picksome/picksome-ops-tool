"use client";

import * as React from "react";
import { Calculator, Crown, Target } from "lucide-react";
import { cn } from "@/lib/utils";
import { PageShell, PageTabs, Field } from "@/components/shell/page-shell";
import { Badge } from "@/components/ui/badge";
import { MoneyInput, PercentInput } from "@/components/ui/money-input";
import {
  CalcBreakdown, CalcHelpButton, CalcSummary, TargetPrices,
} from "@/components/calc/calc-breakdown";
import { useListQuery } from "@/hooks/useListQuery";
import { MOCK_CATEGORIES } from "@/lib/adapters";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import { useFeePolicies, useTaxRules } from "@/hooks/usePolicies";
import { calculateMargin, compareMarketplaces, emptyCalcInput, resolveFeePolicy } from "@/lib/calc/engine";
import type { MarginCalcInput, TaxRule } from "@/lib/calc/types";
import { todayKst } from "@/lib/date-range";
import { formatKRW, formatPercent } from "@/lib/format";

/**
 * 마진 계산기 (보조 도구).
 *
 * 등록가 시뮬레이션과 마진계산기를 **한 화면의 모드 전환**으로 합쳤다.
 * 둘은 같은 식의 방향만 다르다 — 원가→가격이냐 가격→마진이냐.
 * 주 사용처는 상품 등록/수정 drawer 의 "가격·마진" 탭이고, 여기는 상품 없이
 * 빠르게 계산해 보는 자리다.
 */
const MODES = [
  { key: "margin", label: "마진 계산 (가격 → 마진)" },
  { key: "price", label: "등록가 역산 (마진 → 가격)" },
];

const SELECT_CLASS =
  "ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs";

export default function CalculatorPage() {
  const { state, set } = useListQuery({ defaults: { tab: "margin" } });
  const mode = state.tab || (state.extra.mode === "price" ? "price" : "margin");

  // 실제 정책 API 를 읽는다. 예전에는 `settingsAdapter`(메모리 배열)였고,
  // 여기 뜨던 수수료·부가세는 전부 지어낸 값이었다.
  const { taxRules } = useTaxRules();
  const { feePolicies } = useFeePolicies();
  // 판매처 목록도 조직이 등록한 채널 카탈로그에서 온다. 상수로 박아 두면
  // 조직이 추가한 채널은 비교표에도 선택지에도 나타나지 않는다 —
  // 그 채널의 수수료 정책이 있어도 계산에 쓰이지 않는다.
  const { channels, labelOf } = useSaleChannels();

  const [input, setInput] = React.useState<MarginCalcInput | null>(null);
  const [taxRuleId, setTaxRuleId] = React.useState<string>("");
  /** 일반/간이 나란히 비교 */
  const [compareTax, setCompareTax] = React.useState(false);

  // 세금 규칙이 로드되면 기본 입력을 만든다
  React.useEffect(() => {
    if (!taxRules?.length || input) return;
    const rule = taxRules[0];
    setTaxRuleId(rule.id);
    const base = emptyCalcInput(rule);
    base.cost.purchasePrice = 12000;
    base.cost.domesticShipping = 3000;
    base.cost.packagingCost = 400;
    base.selling.salePrice = 29900;
    base.selling.adCostRate = 0.05;
    base.selling.returnRate = 0.02;
    base.selling.returnCostPerCase = 3000;
    base.target.targetProfit = 5000;
    base.target.targetMarginRate = 0.2;
    setInput(base);
  }, [taxRules, input]);

  const activeRule: TaxRule | null = React.useMemo(
    () => taxRules?.find((r) => r.id === taxRuleId) ?? taxRules?.[0] ?? null,
    [taxRules, taxRuleId],
  );

  /** 판매처·카테고리가 바뀌면 수수료 정책을 다시 붙인다. */
  const effectiveInput: MarginCalcInput | null = React.useMemo(() => {
    if (!input || !activeRule) return null;
    const policy = resolveFeePolicy(
      feePolicies ?? [], input.selling.marketplace, input.selling.category, todayKst(),
    );
    return { ...input, taxRule: activeRule, feePolicy: policy };
  }, [input, activeRule, feePolicies]);

  const result = React.useMemo(
    () => (effectiveInput && effectiveInput.selling.salePrice > 0 ? calculateMargin(effectiveInput) : null),
    [effectiveInput],
  );

  /** 다른 과세유형으로 같은 조건을 계산 — 나란히 비교용 */
  const alternateResults = React.useMemo(() => {
    if (!effectiveInput || !taxRules) return [];
    return taxRules
      .filter((r) => r.id !== activeRule?.id)
      .map((r) => ({
        rule: r,
        result: effectiveInput.selling.salePrice > 0
          ? calculateMargin({ ...effectiveInput, taxRule: r })
          : null,
      }));
  }, [effectiveInput, taxRules, activeRule]);

  const comparison = React.useMemo(() => {
    if (!effectiveInput || !feePolicies || effectiveInput.selling.salePrice <= 0) return [];
    return compareMarketplaces(
      effectiveInput,
      channels.map((c) => c.code),
      feePolicies,
      todayKst(),
    );
  }, [effectiveInput, feePolicies, channels]);

  function patch(updater: (i: MarginCalcInput) => void) {
    setInput((prev) => {
      if (!prev) return prev;
      const next = structuredClone(prev);
      updater(next);
      return next;
    });
  }

  if (!input || !effectiveInput) {
    return (
      <PageShell title="마진 계산기" variant="detail">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </PageShell>
    );
  }

  return (
    <PageShell
      title="마진 계산기"
      description="상품 없이 빠르게 계산해 봅니다. 실제 상품은 상품 관리에서 등록하면서 바로 계산됩니다."
      actions={<CalcHelpButton />}
      tabs={<PageTabs tabs={MODES} value={mode} onChange={(k) => set({ tab: k })} />}
      variant="list"
    >
      <div className="grid min-h-0 flex-1 gap-4 overflow-y-auto lg:grid-cols-[minmax(0,380px)_1fr]">
        {/* ── 입력 ── */}
        <div className="space-y-3">
          <section className="ops-panel p-3">
            <h2 className="mb-2 text-xs font-semibold">판매 조건</h2>
            <div className="grid gap-2.5 sm:grid-cols-2">
              <Field label="판매처" htmlFor="c-mk">
                <select
                  id="c-mk" className={SELECT_CLASS} value={input.selling.marketplace}
                  onChange={(e) => patch((i) => { i.selling.marketplace = e.target.value; })}
                >
                  {channels.map((c) => (
                    <option key={c.code} value={c.code}>{c.display_name}</option>
                  ))}
                </select>
              </Field>
              <Field label="카테고리" htmlFor="c-cat">
                <select
                  id="c-cat" className={SELECT_CLASS} value={input.selling.category ?? ""}
                  onChange={(e) => patch((i) => { i.selling.category = e.target.value || null; })}
                >
                  <option value="">전체</option>
                  {MOCK_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </Field>
              <Field
                label="판매수수료율" htmlFor="c-comm"
                hint={effectiveInput.feePolicy
                  ? `정책 적용: ${effectiveInput.feePolicy.description}`
                  : "정책 없음 — 직접 입력"}
              >
                <PercentInput
                  id="c-comm" value={effectiveInput.feePolicy?.feeRate ?? input.selling.commissionRate}
                  disabled={!!effectiveInput.feePolicy}
                  onChange={(v) => patch((i) => { i.selling.commissionRate = v ?? 0; })}
                />
              </Field>
              <Field label="결제수수료율" htmlFor="c-pay">
                <PercentInput
                  id="c-pay" value={input.selling.paymentFeeRate}
                  onChange={(v) => patch((i) => { i.selling.paymentFeeRate = v ?? 0; })}
                />
              </Field>
              <Field label="판매가" htmlFor="c-sale">
                <MoneyInput
                  id="c-sale" value={input.selling.salePrice}
                  onChange={(v) => patch((i) => { i.selling.salePrice = v ?? 0; })}
                />
              </Field>
              <Field label="구매자 부담 배송비" htmlFor="c-bship">
                <MoneyInput
                  id="c-bship" value={input.selling.buyerShippingFee}
                  onChange={(v) => patch((i) => { i.selling.buyerShippingFee = v ?? 0; })}
                />
              </Field>
            </div>
          </section>

          <section className="ops-panel p-3">
            <h2 className="mb-2 text-xs font-semibold">원가</h2>
            <div className="grid gap-2.5 sm:grid-cols-2">
              <Field label="매입 통화" htmlFor="c-cur">
                <select
                  id="c-cur" className={SELECT_CLASS} value={input.cost.purchaseCurrency}
                  onChange={(e) => patch((i) => { i.cost.purchaseCurrency = e.target.value; })}
                >
                  {["KRW", "CNY", "USD", "JPY"].map((c) => <option key={c}>{c}</option>)}
                </select>
              </Field>
              <Field label="환율" htmlFor="c-fx">
                <MoneyInput
                  id="c-fx" value={Math.round(input.cost.exchangeRate)}
                  disabled={input.cost.purchaseCurrency === "KRW"}
                  onChange={(v) => patch((i) => { i.cost.exchangeRate = v ?? 1; })}
                />
              </Field>
              {([
                ["purchasePrice", "상품 매입가"],
                ["domesticShippingAbroad", "중국 내 배송비"],
                ["internationalShipping", "해외 배송비"],
                ["customsDuty", "관세"],
                ["importVat", "수입 부가세"],
                ["domesticShipping", "국내 배송비"],
                ["packagingCost", "포장비"],
                ["otherCost", "기타 비용"],
              ] as const).map(([key, label]) => (
                <Field key={key} label={label} htmlFor={`c-${key}`}>
                  <MoneyInput
                    id={`c-${key}`} value={input.cost[key] as number}
                    onChange={(v) => patch((i) => { (i.cost[key] as number) = v ?? 0; })}
                  />
                </Field>
              ))}
            </div>
          </section>

          <section className="ops-panel p-3">
            <h2 className="mb-2 text-xs font-semibold">변동비 · 목표</h2>
            <div className="grid gap-2.5 sm:grid-cols-2">
              <Field label="판매자 부담 쿠폰(정액)" htmlFor="c-cpn">
                <MoneyInput
                  id="c-cpn" value={input.selling.sellerCouponAmount}
                  onChange={(v) => patch((i) => { i.selling.sellerCouponAmount = v ?? 0; })}
                />
              </Field>
              <Field label="쿠폰(정률)" htmlFor="c-cpnr">
                <PercentInput
                  id="c-cpnr" value={input.selling.sellerCouponRate}
                  onChange={(v) => patch((i) => { i.selling.sellerCouponRate = v ?? 0; })}
                />
              </Field>
              <Field label="광고비(정액)" htmlFor="c-ad">
                <MoneyInput
                  id="c-ad" value={input.selling.adCostAmount}
                  onChange={(v) => patch((i) => { i.selling.adCostAmount = v ?? 0; })}
                />
              </Field>
              <Field label="광고비율" htmlFor="c-adr">
                <PercentInput
                  id="c-adr" value={input.selling.adCostRate}
                  onChange={(v) => patch((i) => { i.selling.adCostRate = v ?? 0; })}
                />
              </Field>
              <Field label="반품 예상률" htmlFor="c-ret">
                <PercentInput
                  id="c-ret" value={input.selling.returnRate}
                  onChange={(v) => patch((i) => { i.selling.returnRate = v ?? 0; })}
                />
              </Field>
              <Field label="반품 1건당 비용" htmlFor="c-retc">
                <MoneyInput
                  id="c-retc" value={input.selling.returnCostPerCase}
                  onChange={(v) => patch((i) => { i.selling.returnCostPerCase = v ?? 0; })}
                />
              </Field>
              <Field label="고정비 배분액" htmlFor="c-fix">
                <MoneyInput
                  id="c-fix" value={input.selling.fixedCostAllocation}
                  onChange={(v) => patch((i) => { i.selling.fixedCostAllocation = v ?? 0; })}
                />
              </Field>
              <Field label="목표 이익" htmlFor="c-tp">
                <MoneyInput
                  id="c-tp" value={input.target.targetProfit}
                  onChange={(v) => patch((i) => { i.target.targetProfit = v ?? 0; })}
                />
              </Field>
              <Field label="목표 마진율" htmlFor="c-tm">
                <PercentInput
                  id="c-tm" value={input.target.targetMarginRate}
                  onChange={(v) => patch((i) => { i.target.targetMarginRate = v ?? 0; })}
                />
              </Field>
            </div>
          </section>

          <section className="ops-panel p-3">
            <h2 className="mb-2 text-xs font-semibold">사업자 과세 유형</h2>
            <select
              className={SELECT_CLASS} value={taxRuleId} aria-label="사업자 과세 유형"
              onChange={(e) => setTaxRuleId(e.target.value)}
            >
              {(taxRules ?? []).map((r) => (
                <option key={r.id} value={r.id}>{r.description}</option>
              ))}
            </select>
            <label className="mt-2 flex items-center gap-1.5 text-[11px]">
              <input
                type="checkbox" checked={compareTax}
                onChange={(e) => setCompareTax(e.target.checked)}
                className="ops-focusable"
              />
              다른 과세유형과 나란히 비교
            </label>
            {activeRule && !activeRule.verifiedAt && (
              <p className="mt-2 rounded-md bg-[--color-warning-bg] p-2 text-[10px] leading-relaxed text-[--color-warning]">
                이 세금 규칙은 아직 확인되지 않았습니다. 설정 &gt; 사업자·세금 설정에서
                세율·부가가치율을 확인하고 <strong>세무 전문가 검토</strong>를 받으세요.
              </p>
            )}
          </section>
        </div>

        {/* ── 결과 ── */}
        <div className="space-y-3">
          {!result ? (
            <div className="ops-panel flex flex-col items-center gap-2 py-16 text-center">
              <Calculator className="h-8 w-8 text-[--color-muted-foreground] opacity-30" aria-hidden />
              <p className="text-sm">판매가를 입력하면 계산됩니다</p>
            </div>
          ) : (
            <>
              <CalcSummary result={result} />

              {mode === "price" && (
                <section className="ops-panel p-3">
                  <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold">
                    <Target className="h-3.5 w-3.5" aria-hidden /> 목표별 권장 판매가
                  </h2>
                  <TargetPrices result={result} />
                </section>
              )}

              {compareTax && alternateResults.length > 0 && (
                <section className="ops-panel overflow-hidden">
                  <h2 className="border-b border-[--color-border] px-3 py-2 text-xs font-semibold">
                    과세유형별 비교 — 같은 조건, 다른 세금 규칙
                  </h2>
                  <table className="w-full text-[12px]">
                    <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                      <tr>
                        <th scope="col" className="px-3 py-1.5 text-left font-semibold">과세유형</th>
                        <th scope="col" className="px-3 py-1.5 text-right font-semibold">예상 세금</th>
                        <th scope="col" className="px-3 py-1.5 text-right font-semibold">순이익</th>
                        <th scope="col" className="px-3 py-1.5 text-right font-semibold">마진율</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr className="border-t border-[--color-border] bg-[--color-accent-bg]">
                        <th scope="row" className="px-3 py-1.5 text-left font-medium">
                          {activeRule?.description}
                          <Badge variant="info" className="ml-1 px-1 py-0 text-[10px]">선택</Badge>
                        </th>
                        <td className="ops-num px-3 py-1.5">{formatKRW(result.estimatedTax)}</td>
                        <td className="ops-num px-3 py-1.5 font-semibold">{formatKRW(result.netProfit)}</td>
                        <td className="ops-num px-3 py-1.5">{formatPercent(result.marginRate)}</td>
                      </tr>
                      {alternateResults.map(({ rule, result: r }) =>
                        r ? (
                          <tr key={rule.id} className="border-t border-[--color-border]">
                            <th scope="row" className="px-3 py-1.5 text-left font-normal">{rule.description}</th>
                            <td className="ops-num px-3 py-1.5">{formatKRW(r.estimatedTax)}</td>
                            <td className={cn("ops-num px-3 py-1.5", r.netProfit > result.netProfit && "text-[--color-profit]")}>
                              {formatKRW(r.netProfit)}
                            </td>
                            <td className="ops-num px-3 py-1.5">{formatPercent(r.marginRate)}</td>
                          </tr>
                        ) : null,
                      )}
                    </tbody>
                  </table>
                  <p className="border-t border-[--color-border] bg-[--color-muted] px-3 py-1.5 text-[10px] text-[--color-muted-foreground]">
                    과세유형은 사업자 등록 상태로 정해집니다. 골라서 유리한 쪽을 택하는 것이 아니라
                    <strong> 어떤 차이가 생기는지 이해하기 위한 비교</strong>입니다.
                  </p>
                </section>
              )}

              {/* 판매처 비교 — "어디에 올릴까"는 매일 하는 판단이다 */}
              {comparison.length > 0 && (
                <section className="ops-panel overflow-hidden">
                  <h2 className="border-b border-[--color-border] px-3 py-2 text-xs font-semibold">
                    판매처별 비교 — 같은 원가·같은 판매가
                  </h2>
                  <div className="ops-scroll-x">
                    <table className="w-full text-[12px]">
                      <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                        <tr>
                          <th scope="col" className="px-3 py-1.5 text-left font-semibold">판매처</th>
                          <th scope="col" className="px-3 py-1.5 text-right font-semibold">수수료율</th>
                          <th scope="col" className="px-3 py-1.5 text-right font-semibold">수수료</th>
                          <th scope="col" className="px-3 py-1.5 text-right font-semibold">정산액</th>
                          <th scope="col" className="px-3 py-1.5 text-right font-semibold">순이익</th>
                          <th scope="col" className="px-3 py-1.5 text-right font-semibold">마진율</th>
                        </tr>
                      </thead>
                      <tbody>
                        {comparison.map((c, i) => (
                          <tr
                            key={c.marketplace}
                            className={cn(
                              "border-t border-[--color-border]",
                              i === 0 && "bg-[--color-success-bg]",
                            )}
                          >
                            <th scope="row" className="px-3 py-1.5 text-left font-normal">
                              <span className="inline-flex items-center gap-1">
                                {i === 0 && <Crown className="h-3 w-3 text-[--color-success]" aria-label="가장 유리" />}
                                {labelOf(c.marketplace)}
                                {!c.policy && (
                                  <Badge variant="warning" className="px-1 py-0 text-[9px]">정책없음</Badge>
                                )}
                              </span>
                            </th>
                            <td className="ops-num px-3 py-1.5">{formatPercent(c.commissionRate, 2)}</td>
                            <td className="ops-num px-3 py-1.5">{formatKRW(c.result.commissionFee + c.result.commissionVat)}</td>
                            <td className="ops-num px-3 py-1.5">{formatKRW(c.result.expectedSettlement)}</td>
                            <td className={cn("ops-num px-3 py-1.5 font-medium", c.result.netProfit < 0 && "text-[--color-loss]")}>
                              {formatKRW(c.result.netProfit)}
                            </td>
                            <td className="ops-num px-3 py-1.5">{formatPercent(c.result.marginRate)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}

              <CalcBreakdown result={result} />
            </>
          )}
        </div>
      </div>
    </PageShell>
  );
}
