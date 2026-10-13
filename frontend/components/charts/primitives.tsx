"use client";

import * as React from "react";
import { ResponsiveContainer, Tooltip, type TooltipProps } from "recharts";
import { BarChart3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { formatCompactKRW, formatKRW, formatPercent } from "@/lib/format";

/**
 * 차트 공통 규칙.
 *
 * 색은 디자인 토큰에 고정한다. 차트마다 색을 새로 고르면 같은 "매출"이
 * 화면마다 다른 색이 되어 대시보드가 읽히지 않는다.
 *
 * 색만으로 계열을 구분하지 않는다 — 범례에 라벨을 항상 붙이고,
 * 툴팁에 계열명을 명시한다(접근성).
 */
export const CHART_COLORS = {
  revenue: "#4F46E5",   // accent — 매출
  profit: "#059669",    // success — 수익
  loss: "#DC2626",      // danger — 손실
  warning: "#D97706",
  cost: "#64748B",
  grid: "#E5E7EB",
  axis: "#64748B",
} as const;

/** 비용 구성 등 카테고리 계열용 팔레트. 인디고에서 시작해 명도로 구분한다. */
export const CATEGORY_COLORS = [
  "#4F46E5", "#0891B2", "#059669", "#CA8A04", "#DC2626",
  "#7C3AED", "#DB2777", "#475569", "#0D9488", "#B45309",
];

export function ChartCard({
  title,
  description,
  actions,
  children,
  isLoading,
  isEmpty,
  emptyText = "이 기간에는 데이터가 없습니다",
  height = 260,
  className,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  isLoading?: boolean;
  isEmpty?: boolean;
  emptyText?: string;
  height?: number;
  className?: string;
}) {
  return (
    <section className={cn("ops-panel flex flex-col", className)}>
      <div className="flex items-start justify-between gap-3 border-b border-[--color-border] px-4 py-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">{title}</h3>
          {description && (
            <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">{description}</p>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
      <div className="flex-1 p-3" style={{ minHeight: height }}>
        {isLoading ? (
          <div className="flex h-full flex-col justify-end gap-2 p-2" style={{ height }}>
            <Skeleton className="h-1/3 w-full" />
            <Skeleton className="h-1/2 w-full" />
            <Skeleton className="h-3 w-24" />
          </div>
        ) : isEmpty ? (
          <div
            className="flex flex-col items-center justify-center gap-2 text-[--color-muted-foreground]"
            style={{ height }}
          >
            <BarChart3 className="h-8 w-8 opacity-30" aria-hidden />
            <p className="text-xs">{emptyText}</p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={height}>
            {children as React.ReactElement}
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}

/** 축 라벨 — 금액은 축약, 툴팁에서만 전체 금액을 보여준다. */
export const moneyTick = (v: number) => formatCompactKRW(v);
export const percentTick = (v: number) => `${(v * 100).toFixed(0)}%`;

export const AXIS_PROPS = {
  tick: { fontSize: 11, fill: CHART_COLORS.axis },
  axisLine: false,
  tickLine: false,
} as const;

type SeriesFormat = "money" | "percent" | "count";

/**
 * 공통 툴팁.
 * 금액은 절대 축약하지 않는다 — 툴팁은 "정확한 값을 확인하는 자리"다.
 */
export function ChartTooltip({
  active,
  payload,
  label,
  formatLabel,
  formats = {},
  extra,
}: TooltipProps<number, string> & {
  formatLabel?: (label: string) => string;
  formats?: Record<string, SeriesFormat>;
  extra?: (payload: Record<string, unknown>) => React.ReactNode;
}) {
  if (!active || !payload?.length) return null;
  const raw = (payload[0]?.payload ?? {}) as Record<string, unknown>;
  return (
    <div className="rounded-lg border border-[--color-border] bg-[--color-card] px-3 py-2 text-xs shadow-lg">
      <p className="mb-1.5 font-semibold">
        {formatLabel ? formatLabel(String(label)) : String(label)}
      </p>
      <ul className="space-y-1">
        {payload.map((entry) => {
          const key = String(entry.dataKey ?? entry.name);
          const fmt = formats[key] ?? "money";
          const value = entry.value ?? 0;
          return (
            <li key={key} className="flex items-center gap-2">
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: entry.color }}
                aria-hidden
              />
              <span className="text-[--color-muted-foreground]">{entry.name}</span>
              <span className="ml-auto font-medium tabular-nums">
                {fmt === "money"
                  ? formatKRW(value)
                  : fmt === "percent"
                    ? formatPercent(value)
                    : value.toLocaleString("ko-KR")}
              </span>
            </li>
          );
        })}
      </ul>
      {extra?.(raw)}
    </div>
  );
}

/** recharts 의 `content` prop 에 넘길 팩토리. */
export function tooltipContent(
  opts: {
    formatLabel?: (label: string) => string;
    formats?: Record<string, SeriesFormat>;
    extra?: (payload: Record<string, unknown>) => React.ReactNode;
  } = {},
) {
  const Rendered = (props: TooltipProps<number, string>) => (
    <ChartTooltip {...props} {...opts} />
  );
  Rendered.displayName = "ChartTooltipContent";
  return <Rendered />;
}

export { Tooltip };

/** 범례 — recharts 기본 범례는 위치 제어가 까다로워 직접 그린다. */
export function ChartLegend({
  items,
  className,
}: {
  items: Array<{ label: string; color: string; hint?: string }>;
  className?: string;
}) {
  return (
    <ul className={cn("flex flex-wrap items-center gap-3 px-1 pb-1", className)}>
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5 text-[11px]">
          <span
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: it.color }}
            aria-hidden
          />
          <span className="text-[--color-muted-foreground]">{it.label}</span>
          {it.hint && <span className="text-[--color-muted-foreground]">({it.hint})</span>}
        </li>
      ))}
    </ul>
  );
}
