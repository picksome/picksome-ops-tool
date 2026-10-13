/**
 * Finance 전용 표시 컴포넌트.
 * 모든 페이지에서 금액/마진율/순이익 표시를 일관되게 유지한다.
 */
import { cn } from "@/lib/utils";
import { formatKRW, formatPercent } from "@/lib/format";

/** KRW 금액 표시. 음수면 빨간색, 양수면 기본색 */
export function MoneyText({
  value,
  className,
  colored = false,
}: {
  value: number | null | undefined;
  className?: string;
  colored?: boolean;
}) {
  const formatted = formatKRW(value);
  return (
    <span
      className={cn(
        "tabular-nums text-right",
        colored && value != null && value < 0 && "text-[--color-danger]",
        colored && value != null && value > 0 && "text-[--color-success]",
        className,
      )}
    >
      {formatted}
    </span>
  );
}

/** 순이익/실마진 — 음수(적자)면 빨간, 양수(흑자)면 녹색 */
export function ProfitText({
  value,
  className,
}: {
  value: number | null | undefined;
  className?: string;
}) {
  return <MoneyText value={value} colored className={className} />;
}

/** 전월 대비 변화 (+12.4% 형태) — 색칠된 pill 배지 */
export function ProfitDelta({
  current,
  previous,
  className,
}: {
  current: number;
  previous: number;
  className?: string;
}) {
  if (!previous) return null;
  const delta = ((current - previous) / Math.abs(previous)) * 100;
  const positive = delta >= 0;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-1.5 py-0.5 text-xs font-medium tabular-nums",
        positive
          ? "bg-[--color-success-bg] text-[--color-success]"
          : "bg-[--color-danger-bg] text-[--color-danger]",
        className,
      )}
    >
      {positive ? "+" : ""}
      {delta.toFixed(1)}% vs 전월
    </span>
  );
}

/**
 * 마진율 구간별 Badge.
 * 30%+: 안정(green) / 20~30%: 정상(blue) / 10~20%: 주의(amber) / <10%: 위험(red) / <0%: 적자(red+굵음)
 */
export function MarginRateBadge({ rate }: { rate: number | null | undefined }) {
  if (rate == null) return <span className="text-xs text-[--color-muted-foreground]">-</span>;

  const pct = rate * 100;

  const { label, className } =
    pct < 0
      ? { label: `적자 ${formatPercent(rate)}`, className: "bg-[--color-danger-bg] text-[--color-danger] font-semibold border border-[--color-danger-border]" }
      : pct < 10
        ? { label: formatPercent(rate), className: "bg-[--color-danger-bg] text-[--color-danger] border border-[--color-danger-border]" }
        : pct < 20
          ? { label: formatPercent(rate), className: "bg-[--color-warning-bg] text-[--color-warning] border border-[--color-warning-border]" }
          : pct < 30
            ? { label: formatPercent(rate), className: "bg-[--color-info-bg] text-[--color-info] border border-[--color-info-border]" }
            : { label: formatPercent(rate), className: "bg-[--color-success-bg] text-[--color-success] border border-[--color-success-border]" };

  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs tabular-nums", className)}>
      {label}
    </span>
  );
}

/** KPI 카드 톤 — 카드마다 accent bar/icon chip 색을 구분해 리듬을 준다 */
export type KpiTone = "brand" | "success" | "info" | "warning" | "neutral";

const KPI_TONE_STYLES: Record<KpiTone, { bar: string; chipBg: string; chipFg: string }> = {
  brand:   { bar: "bg-[--color-accent]",  chipBg: "bg-[--color-accent-bg]",  chipFg: "text-[--color-accent]" },
  success: { bar: "bg-[--color-success]", chipBg: "bg-[--color-success-bg]", chipFg: "text-[--color-success]" },
  info:    { bar: "bg-[--color-info]",    chipBg: "bg-[--color-info-bg]",    chipFg: "text-[--color-info]" },
  warning: { bar: "bg-[--color-warning]", chipBg: "bg-[--color-warning-bg]", chipFg: "text-[--color-warning]" },
  neutral: { bar: "bg-[--color-muted-foreground]", chipBg: "bg-[--color-muted]", chipFg: "text-[--color-muted-foreground]" },
};

/** KPI 카드 — 대시보드/요약용 */
export function KpiCard({
  label,
  value,
  sub,
  delta,
  alert,
  icon: Icon,
  tone = "neutral",
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  delta?: React.ReactNode;
  alert?: boolean;
  icon?: React.ElementType;
  tone?: KpiTone;
}) {
  const resolvedTone = alert ? "warning" : tone;
  const { bar, chipBg, chipFg } = KPI_TONE_STYLES[resolvedTone];

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-lg border bg-[--color-card] p-4 pt-[18px]",
        alert ? "border-[--color-warning-border] bg-[--color-warning-bg]" : "border-[--color-border]",
      )}
    >
      <div className={cn("absolute inset-x-0 top-0 h-[3px]", bar)} />
      <div className="flex items-start justify-between gap-2">
        <p className="text-[13px] font-medium text-[--color-muted-foreground]">{label}</p>
        {Icon && (
          <div className={cn("flex h-7 w-7 shrink-0 items-center justify-center rounded-md", chipBg)}>
            <Icon className={cn("h-4 w-4", chipFg)} />
          </div>
        )}
      </div>
      <p className="mt-1.5 text-[32px] font-bold leading-tight tabular-nums text-[--color-foreground]">
        {value}
      </p>
      {delta && <div className="mt-1">{delta}</div>}
      {sub && <p className="mt-1 text-xs text-[--color-muted-foreground]">{sub}</p>}
    </div>
  );
}
