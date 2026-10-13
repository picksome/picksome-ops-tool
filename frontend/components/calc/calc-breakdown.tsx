"use client";

import * as React from "react";
import { AlertTriangle, HelpCircle, Info } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription,
} from "@/components/ui/sheet";
import { formatKRW, formatPercent } from "@/lib/format";
import type { MarginCalcResult } from "@/lib/calc/types";

/**
 * 계산 근거 패널.
 *
 * 상품 drawer · 판매장부 · 마진계산기가 **모두 이걸 쓴다.**
 * 숫자만 보여주고 근거를 감추면 사용자가 검증할 수 없고, 결국 엑셀로 되돌아간다.
 *
 * 각 줄에 `항목 / 산식 / 적용 근거 / 금액` 을 함께 표시한다.
 * 소계 줄(총원가·예상 정산액·공헌이익·순이익)은 굵게 + 배경으로 구분한다.
 */
export function CalcBreakdown({
  result,
  compact,
  className,
}: {
  result: MarginCalcResult;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("overflow-hidden rounded-lg border border-[--color-border]", className)}>
      <table className="w-full text-[12px]">
        <caption className="sr-only">마진 계산 근거</caption>
        <thead className="bg-[--color-muted]">
          <tr className="text-[11px] text-[--color-muted-foreground]">
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">항목</th>
            {!compact && (
              <th scope="col" className="px-3 py-1.5 text-left font-semibold">계산식 · 근거</th>
            )}
            <th scope="col" className="px-3 py-1.5 text-right font-semibold">금액</th>
          </tr>
        </thead>
        <tbody>
          {result.steps.map((step, i) => (
            <tr
              key={`${step.key}-${i}`}
              className={cn(
                "border-t border-[--color-border]",
                step.isSubtotal && "bg-[--color-surface-muted] font-semibold",
              )}
            >
              <th
                scope="row"
                className={cn(
                  "px-3 py-1.5 text-left font-normal",
                  step.isSubtotal && "font-semibold",
                )}
              >
                {step.label}
              </th>
              {!compact && (
                <td className="px-3 py-1.5 text-[--color-muted-foreground]">
                  <span className="tabular-nums">{step.formula}</span>
                  {step.source && (
                    <span className="ml-1.5 rounded bg-[--color-muted] px-1 py-0.5 text-[10px]">
                      {step.source}
                    </span>
                  )}
                </td>
              )}
              <td
                className={cn(
                  "ops-num px-3 py-1.5",
                  step.isSubtotal && "font-semibold",
                  step.isSubtotal && step.amount < 0 && "text-[--color-loss]",
                )}
              >
                {/* 부호를 붙여 차감인지 가산인지 눈으로 바로 구분되게 한다 */}
                {step.isDeduction && !step.isSubtotal ? "−" : ""}
                {formatKRW(Math.abs(step.amount))}
              </td>
            </tr>
          ))}
          <tr className="border-t-2 border-[--color-border] bg-[--color-card]">
            <th scope="row" className="px-3 py-2 text-left font-semibold">마진율</th>
            {!compact && (
              <td className="px-3 py-2 text-[--color-muted-foreground]">순이익 ÷ 판매가</td>
            )}
            <td
              className={cn(
                "ops-num px-3 py-2 font-semibold",
                (result.marginRate ?? 0) < 0 && "text-[--color-loss]",
                (result.marginRate ?? 0) >= 0.15 && "text-[--color-profit]",
              )}
            >
              {result.marginRate === null ? "-" : formatPercent(result.marginRate)}
            </td>
          </tr>
        </tbody>
      </table>

      {result.warnings.length > 0 && (
        <ul className="space-y-1 border-t border-[--color-border] bg-[--color-warning-bg] px-3 py-2">
          {result.warnings.map((w, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] text-[--color-warning]">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>{w}</span>
            </li>
          ))}
        </ul>
      )}

      <p className="border-t border-[--color-border] bg-[--color-muted] px-3 py-1.5 text-[10px] text-[--color-muted-foreground]">
        세금은 <strong>예상치</strong>입니다. 실제 신고세액은 매입 증빙·공제 요건에 따라 달라집니다.
        적용 규칙 v{result.appliedRules.taxRuleVersion}
        {result.appliedRules.feePolicyVersion !== null &&
          ` · 수수료 정책 v${result.appliedRules.feePolicyVersion}`}
      </p>
    </div>
  );
}

/** 요약 3종 — 정산액 / 공헌이익 / 순이익. drawer 상단에 고정으로 띄운다. */
export function CalcSummary({ result }: { result: MarginCalcResult }) {
  const cells = [
    { label: "총원가", value: result.totalCost, tone: "neutral" as const },
    { label: "예상 정산액", value: result.expectedSettlement, tone: "neutral" as const },
    { label: "공헌이익", value: result.contributionProfit, tone: "profit" as const },
    { label: "예상 순이익", value: result.netProfit, tone: "profit" as const },
  ];
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-[--color-border] bg-[--color-border] sm:grid-cols-4">
      {cells.map((c) => (
        <div key={c.label} className="bg-[--color-card] px-3 py-2.5">
          <p className="text-[11px] text-[--color-muted-foreground]">{c.label}</p>
          <p
            className={cn(
              "mt-0.5 text-sm font-semibold tabular-nums",
              c.tone === "profit" && c.value > 0 && "text-[--color-profit]",
              c.value < 0 && "text-[--color-loss]",
            )}
          >
            {formatKRW(c.value)}
          </p>
        </div>
      ))}
    </div>
  );
}

/** 역산 결과 3종 — 손익분기 / 목표이익 / 목표마진. */
export function TargetPrices({ result }: { result: MarginCalcResult }) {
  const rows = [
    { label: "손익분기 판매가", value: result.breakEvenPrice, hint: "이 가격 아래로는 팔수록 손해입니다" },
    { label: "목표 이익 판매가", value: result.targetProfitPrice, hint: "입력한 목표 이익을 남기는 가격" },
    { label: "목표 마진 판매가", value: result.targetMarginPrice, hint: "입력한 목표 마진율을 달성하는 가격" },
  ];
  return (
    <dl className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center justify-between gap-3 px-3 py-2">
          <div className="min-w-0">
            <dt className="text-[12px] font-medium">{r.label}</dt>
            <p className="text-[10px] text-[--color-muted-foreground]">{r.hint}</p>
          </div>
          <dd className="shrink-0 text-sm font-semibold tabular-nums">
            {r.value === null ? (
              <span className="text-[11px] font-normal text-[--color-danger]">달성 불가</span>
            ) : (
              formatKRW(r.value)
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/* ────────────────────────────────────────────────────────────
 * 도움말 drawer — 계산식 전문 공개
 * ──────────────────────────────────────────────────────────── */

export function CalcHelpButton({ label = "계산식 보기" }: { label?: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
        <HelpCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
        {label}
      </Button>
      <CalcHelpDrawer open={open} onOpenChange={setOpen} />
    </>
  );
}

export function CalcHelpDrawer({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="[--sheet-w:560px] w-full overflow-y-auto">
        <SheetHeader>
          <SheetTitle>계산식</SheetTitle>
          <SheetDescription>
            화면에 표시되는 모든 금액이 어떻게 나오는지 정리했습니다.
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-5 px-4 pb-8 text-[13px] leading-relaxed">
          <HelpSection title="예상 정산액" formula={`판매가
+ 구매자 부담 배송비
− 판매수수료
− 수수료 부가세
− 결제수수료
− 판매자 부담 쿠폰
− 기타 판매처 차감액`}>
            판매처가 실제로 입금해 주는 금액입니다. 판매가와 다릅니다.
            수수료는 <strong>판매처·카테고리별 수수료 정책</strong>에서 자동으로 가져오고,
            정책이 없으면 직접 입력한 요율을 씁니다.
          </HelpSection>

          <HelpSection title="상품 공헌이익" formula={`예상 정산액
− 총원가 (매입가 + 배송 + 관세 + 포장 + 기타)
− 광고비
− 예상 반품 비용`}>
            상품 하나가 사업에 기여하는 금액입니다. 사무실 임대료 같은 고정비는 아직 빼지 않았습니다.
            <br />
            <strong>반품 비용 = (반품 1건당 비용 + 총원가) × 반품률</strong> —
            반품되면 회수 비용만 드는 게 아니라 원가도 함께 손실됩니다.
          </HelpSection>

          <HelpSection title="예상 순이익" formula={`상품 공헌이익
− 고정비 배분액
− 예상 세금`}>
            실제로 남는 돈입니다. 마진율은 <strong>순이익 ÷ 판매가</strong>로 계산합니다.
          </HelpSection>

          <HelpSection title="부가세 — 일반과세자" formula={`매출세액 = (판매가 + 배송비) × 10 ÷ 110
매입세액 = 적격증빙 매입의 부가세
납부세액 = 매출세액 − 매입세액`}>
            판매가가 <strong>부가세 포함가</strong>일 때의 계산입니다. 소비자 대상 판매의 기본입니다.
            <br />
            매입세액 공제 대상: 국내 배송비, 포장비, 판매수수료, 결제수수료, 광고비 중
            세금계산서·카드전표를 받은 건.
          </HelpSection>

          <HelpSection title="부가세 — 간이과세자" formula={`납부세액 = 공급대가 × 업종별 부가가치율 × 10%
매입세액공제 = 매입액 × 공제율`}>
            간이과세자는 매입세액 공제가 사실상 거의 없습니다.
            같은 매출·같은 원가라도 <strong>일반과세자와 남는 금액이 다릅니다.</strong>
            <br />
            <span className="text-[--color-warning]">
              업종별 부가가치율과 적용 기준금액은 세법 개정이 잦습니다.
              설정 &gt; 사업자·세금 설정에서 값을 관리하고, 세무 전문가 확인을 받으세요.
            </span>
          </HelpSection>

          <HelpSection title="수입 부가세" formula={`공제 가능 → 원가에 포함하지 않음
공제 불가   → 원가에 포함`}>
            관세청에 낸 수입 부가세를 매입세액으로 공제받을 수 있으면 <strong>원가가 아닙니다</strong>.
            나중에 돌려받는 돈이기 때문입니다. 원가에 넣으면 마진이 실제보다 낮게 나옵니다.
          </HelpSection>

          <HelpSection title="목표 판매가 역산" formula={`목표 판매가 =
  (고정 원가 + 목표 이익)
  ÷ (1 − 판매수수료율 − 결제수수료율 − 쿠폰부담률
       − 광고비율 − 세금효과율 − 목표마진율)`}>
            분모에는 <strong>판매가에 비례하는 비용만</strong> 넣습니다.
            반품 충당금은 원가 기준이라 판매가에 비례하지 않으므로 고정 항목으로 처리합니다.
            <br />
            분모가 0 이하이면 그 목표는 달성할 수 없습니다 —
            변동비 합계가 이미 판매가를 다 먹는다는 뜻입니다.
          </HelpSection>

          <div className="flex items-start gap-2 rounded-lg border border-[--color-info-border] bg-[--color-info-bg] p-3 text-[12px] text-[--color-info]">
            <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <p>
              모든 계산 결과에는 <strong>적용된 규칙 버전이 박제</strong>됩니다.
              나중에 수수료나 세금 정책이 바뀌어도 과거에 계산한 값은 그대로 재현됩니다.
            </p>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function HelpSection({
  title,
  formula,
  children,
}: {
  title: string;
  formula: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-1.5 text-sm font-semibold">{title}</h3>
      <pre className="ops-scroll-x mb-2 rounded-lg bg-[--color-muted] p-3 text-[11px] leading-relaxed">
        {formula}
      </pre>
      <div className="text-[12px] text-[--color-muted-foreground]">{children}</div>
    </section>
  );
}
