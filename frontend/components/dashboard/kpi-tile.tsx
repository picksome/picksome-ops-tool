"use client";

import { ArrowDown, ArrowUp, Info, Minus } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatKRW, formatPercent, formatSignedKRW, formatSignedPercent } from "@/lib/format";
import { SOURCE_LABEL, type DashboardKpi } from "@/lib/api/dashboard";

/**
 * KPI 타일.
 *
 * 카드 남발을 피하려고 테두리 있는 박스 하나에 격자로 넣는다.
 *
 * 이 타일이 지키는 것
 * ----------------
 * · **모르는 값을 0으로 그리지 않는다.** `value === null` 이면 "—" 와 사유를 보여준다.
 *   정산이 연결되지 않은 달의 정산액을 "0 원" 으로 쓰면 사용자는 정산이 0원이라고 읽는다
 * · **출처가 다른 값을 말없이 비교하지 않는다.** 진행 중인 달(live)과 확정된 달
 *   (snapshot)의 증감은 기준이 달라 의미가 약하다 — 그럴 땐 증감 대신 그 사실을 쓴다
 * · 증감 색은 지표 성격에 따라 뒤집힌다(`higher_is_better`). 사업비용이 늘어난 걸
 *   초록으로 칠하면 안 된다. 색만으로 구분하지 않으려고 화살표를 항상 함께 쓴다
 */
export function KpiTile({ kpi, isLoading }: { kpi?: DashboardKpi; isLoading?: boolean }) {
  if (isLoading || !kpi) {
    return (
      <div className="border-b border-r border-[--color-border] p-4">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="mt-2.5 h-6 w-28" />
        <Skeleton className="mt-2 h-3 w-24" />
      </div>
    );
  }

  const { value, previous, format, higher_is_better: higherIsBetter } = kpi;
  const unavailable = value === null;

  const valueText = unavailable
    ? "—"
    : format === "money"
      ? formatKRW(value)
      : format === "percent"
        ? formatPercent(value)
        : `${value.toLocaleString("ko-KR")}${format === "count" ? "건" : ""}`;

  return (
    <div className="border-b border-r border-[--color-border] p-4 last:border-r-0">
      <div className="flex items-center gap-1">
        <p className="text-[11px] font-medium text-[--color-muted-foreground]">{kpi.label}</p>
        {kpi.hint && (
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className="ops-focusable text-[--color-muted-foreground]"
                aria-label={`${kpi.label} 설명`}
              >
                <Info className="h-3 w-3" aria-hidden />
              </button>
            </TooltipTrigger>
            <TooltipContent className="max-w-[260px]">{kpi.hint}</TooltipContent>
          </Tooltip>
        )}
      </div>

      <p
        className={cn(
          "mt-1.5 text-[19px] font-semibold tabular-nums tracking-tight",
          unavailable && "text-[--color-muted-foreground]",
          !unavailable && format === "money" && value < 0 && "text-[--color-loss]",
        )}
      >
        {valueText}
      </p>

      <KpiFooter kpi={kpi} unavailable={unavailable} previous={previous} format={format}
                 higherIsBetter={higherIsBetter} />
    </div>
  );
}

function KpiFooter({
  kpi, unavailable, previous, format, higherIsBetter,
}: {
  kpi: DashboardKpi;
  unavailable: boolean;
  previous: number | null;
  format: DashboardKpi["format"];
  higherIsBetter: boolean;
}) {
  // 값 자체가 없으면 증감도 없다. 왜 없는지를 쓴다.
  if (unavailable) {
    return (
      <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
        {kpi.availability === "NOT_CONNECTED" ? "연결된 데이터 없음" : "값 없음"}
      </p>
    );
  }

  // 비교 기준이 다르면 증감을 그리지 않는다 — 그리는 순간 사용자가 그걸 믿는다.
  if (!kpi.comparable || previous === null) {
    return (
      <p className="mt-1 text-[11px] text-[--color-muted-foreground]">
        {SOURCE_LABEL[kpi.source]}
        {previous !== null && kpi.previous_source && kpi.previous_source !== kpi.source
          ? ` · 이전 기간은 ${SOURCE_LABEL[kpi.previous_source]} — 기준이 달라 비교하지 않습니다`
          : " · 이전 기간 비교 없음"}
      </p>
    );
  }

  const value = kpi.value as number;
  const delta = value - previous;
  const deltaRate = previous !== 0 ? delta / Math.abs(previous) : null;
  const direction = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  const isGood = direction === "flat" ? null : (delta > 0) === higherIsBetter;

  const deltaText =
    format === "money"
      ? formatSignedKRW(delta)
      : format === "percent"
        ? formatSignedPercent(delta, 1)
        : `${delta > 0 ? "+" : delta < 0 ? "−" : ""}${Math.abs(delta).toLocaleString("ko-KR")}`;

  const DirIcon = direction === "up" ? ArrowUp : direction === "down" ? ArrowDown : Minus;
  const tone =
    isGood === null
      ? "text-[--color-neutral-value]"
      : isGood
        ? "text-[--color-profit]"
        : "text-[--color-loss]";

  return (
    <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px]">
      <DirIcon className={cn("h-3 w-3", tone)} aria-hidden />
      <span className={cn("tabular-nums font-medium", tone)}>{deltaText}</span>
      {deltaRate !== null && (
        <span className="tabular-nums text-[--color-muted-foreground]">
          ({formatSignedPercent(deltaRate, 1)})
        </span>
      )}
      <span className="text-[--color-muted-foreground]">전월 대비</span>
    </div>
  );
}
