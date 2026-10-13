"use client";

/**
 * 대시보드 (Phase 45).
 *
 * 이 화면은 **`GET /reports/dashboard` 하나만 본다.**
 * 예전에는 `MOCK_PRODUCTS`/`MOCK_EXPENSES` 로 숫자를 만들어 냈다 —
 * 실제 금액처럼 보이는 sample 이었고, DB 와 아무 관계가 없었다.
 *
 * 화면이 지키는 것
 * -------------
 * · 숫자마다 **출처를 표시한다.** 진행 중인 달은 집계, 확정된 달은 확정 snapshot
 * · **모르는 값을 0으로 그리지 않는다.** 정산 미연결은 "0원" 이 아니라 "—" 다
 * · 경고를 여기서 다시 판단하지 않는다. 운영 이슈 센터가 스캔한 결과를 보여주고
 *   클릭하면 필터된 목록으로 간다
 * · 월 단위다. 마감 상태와 확정 snapshot 이 월 단위이기 때문에, 임의 날짜 구간은
 *   확정월의 박제 숫자와 진행월 집계를 한 값에 섞게 된다
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import type { Route } from "next";
import { useQuery } from "@tanstack/react-query";
import {
  Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine,
  Scatter, ScatterChart, XAxis, YAxis, ZAxis,
} from "recharts";
import {
  AlertTriangle, ArrowRight, Banknote, CircleAlert, Info, Lock, RefreshCw, ShieldCheck,
} from "lucide-react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PageShell } from "@/components/shell/page-shell";
import { KpiTile } from "@/components/dashboard/kpi-tile";
import {
  AXIS_PROPS, CATEGORY_COLORS, CHART_COLORS, ChartCard, ChartLegend,
  moneyTick, percentTick, tooltipContent, Tooltip as RechartsTooltip,
} from "@/components/charts/primitives";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import {
  CLOSE_STATE_LABEL, SOURCE_LABEL, dashboardApi, issueTypeLabel,
  type DashboardResponse,
} from "@/lib/api/dashboard";
import { formatCompactKRW, formatKRW, formatPercent, formatYearMonth } from "@/lib/format";
import { cn } from "@/lib/utils";

const TREND_MONTHS = 6;

/** 서버가 준 available_months 가 오기 전까지 쓸 기본값 — 이번 달(KST). */
function currentYearMonth(): string {
  const now = new Date();
  const kst = new Date(now.getTime() + (now.getTimezoneOffset() + 540) * 60_000);
  return `${kst.getFullYear()}-${String(kst.getMonth() + 1).padStart(2, "0")}`;
}

export default function DashboardPage() {
  const [yearMonth, setYearMonth] = useState<string>(currentYearMonth);
  const { labelOf } = useSaleChannels();

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dashboard", yearMonth],
    queryFn: () => dashboardApi.load({ year_month: yearMonth, trend_months: TREND_MONTHS }),
  });

  return (
    <TooltipProvider delayDuration={200}>
      <PageShell
        title="대시보드"
        description="상품에서 순이익까지의 흐름을 한 화면에서 확인합니다"
        actions={
          <div className="flex items-center gap-2">
            <label htmlFor="dash-month" className="text-xs text-[--color-muted-foreground]">
              기준 월
            </label>
            <select
              id="dash-month"
              value={yearMonth}
              onChange={(e) => setYearMonth(e.target.value)}
              className="ops-focusable h-8 rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              {(data?.available_months ?? [yearMonth]).map((m) => (
                <option key={m} value={m}>{formatYearMonth(m)}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => refetch()}
              className="ops-focusable flex h-8 items-center gap-1.5 rounded-md border border-[--color-border] px-2.5 text-xs"
              aria-label="새로고침"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} aria-hidden />
              새로고침
            </button>
          </div>
        }
      >
        {isError ? (
          <div className="ops-panel flex flex-col items-center gap-3 py-16">
            <AlertTriangle className="h-8 w-8 text-[--color-danger]" aria-hidden />
            <p className="text-sm">대시보드를 불러오지 못했습니다</p>
            <p className="max-w-md text-center text-xs text-[--color-muted-foreground]">
              {error instanceof Error ? error.message : "알 수 없는 오류"}
            </p>
            <button
              onClick={() => refetch()}
              className="ops-focusable rounded-md border border-[--color-border] px-3 py-1.5 text-xs"
            >
              다시 시도
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {data && <PeriodBanner data={data} />}
            {data && <WarningPanel warnings={data.warnings} />}

            {/* ── KPI ── */}
            <section aria-label="핵심 지표" className="ops-panel overflow-hidden">
              <div className="grid grid-cols-2 md:grid-cols-4 [&>*:nth-child(4n)]:border-r-0 [&>*:nth-last-child(-n+4)]:border-b-0">
                {(data?.kpis ?? Array.from({ length: 8 }).map(() => undefined)).map((kpi, i) => (
                  <KpiTile key={kpi?.key ?? i} kpi={kpi} isLoading={isLoading || !data} />
                ))}
              </div>
            </section>

            {/* ── 추세 + 워터폴 ── */}
            <div className="grid gap-4 xl:grid-cols-2">
              <TrendCard data={data} isLoading={isLoading} />
              <WaterfallCard data={data} isLoading={isLoading} />
            </div>

            {/* ── 판매처 비교 + 비용 구성 ── */}
            <div className="grid gap-4 xl:grid-cols-2">
              <MarketplaceCard data={data} isLoading={isLoading} labelOf={labelOf} />
              <ExpenseCard data={data} isLoading={isLoading} />
            </div>

            {/* ── 상품 분포 + 현금흐름 + 확정 월 ── */}
            <div className="grid gap-4 xl:grid-cols-3">
              <MarginScatterCard data={data} isLoading={isLoading} />
              {/* 현금흐름을 볼 수 없는 역할에게는 서버가 카드를 아예 주지 않는다.
                  없는 것을 0원으로 그리면 "통장이 비었다" 가 된다. */}
              {(!data || data.cashflow) && <CashflowPanel data={data} />}
              <ConfirmedMonthsPanel data={data} />
            </div>
          </div>
        )}
      </PageShell>
    </TooltipProvider>
  );
}

/* ────────────────────────────────────────────────────────────
 * 기준 안내 — 이 화면의 숫자가 어디서 왔는가
 * ──────────────────────────────────────────────────────────── */

function PeriodBanner({ data }: { data: DashboardResponse }) {
  const { period, comparison } = data;
  const confirmed = period.is_confirmed;

  return (
    <section
      aria-label="집계 기준"
      className={cn(
        "ops-panel flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3",
        confirmed && "border-[--color-success]",
      )}
    >
      {confirmed ? (
        <ShieldCheck className="h-4 w-4 shrink-0 text-[--color-success]" aria-hidden />
      ) : (
        <Info className="h-4 w-4 shrink-0 text-[--color-info]" aria-hidden />
      )}

      <span className="text-[13px] font-semibold">{formatYearMonth(period.year_month)}</span>

      <span className="rounded-full bg-[--color-muted] px-2 py-0.5 text-[10px] font-medium">
        {CLOSE_STATE_LABEL[period.close_state]}
      </span>

      <span className="rounded-full bg-[--color-muted] px-2 py-0.5 text-[10px] font-medium">
        {SOURCE_LABEL[period.source]}
        {period.snapshot_generation ? ` · ${period.snapshot_generation}세대` : ""}
      </span>

      {period.close_state === "LOCKED" && (
        <Lock className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
      )}

      <span className="text-xs text-[--color-muted-foreground]">{period.profit_label}</span>

      {!period.settlement_connected && (
        <span className="text-xs text-[--color-warning]">
          판매처 정산서가 연결되지 않아 정산 금액은 알 수 없습니다
        </span>
      )}

      {!comparison.comparable && comparison.note && (
        <span className="w-full text-[11px] text-[--color-muted-foreground]">
          {comparison.note}
        </span>
      )}
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 경고 — 운영 이슈를 그대로 보여준다
 * ──────────────────────────────────────────────────────────── */

function WarningPanel({ warnings }: { warnings: DashboardResponse["warnings"] }) {
  // 스캔한 적이 없으면 "0건" 은 "문제 없음" 이 아니라 "확인한 적 없음" 이다.
  if (warnings.never_scanned) {
    return (
      <section aria-label="확인이 필요한 항목" className="ops-panel px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Info className="h-4 w-4 shrink-0 text-[--color-info]" aria-hidden />
          <span className="text-[13px]">운영 이슈 스캔 기록이 없습니다</span>
          <span className="text-xs text-[--color-muted-foreground]">
            이슈 0건이 아니라 아직 확인한 적이 없다는 뜻입니다
          </span>
          <Link
            href={"/operations/issues" as Route}
            className="ops-focusable ml-auto flex items-center gap-1 text-xs font-medium"
          >
            이슈 센터에서 스캔
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
      </section>
    );
  }

  if (warnings.open_total === 0) return null;

  return (
    <section aria-label="확인이 필요한 항목" className="ops-panel overflow-hidden">
      <div className="flex items-center gap-2 border-b border-[--color-border] px-4 py-2.5">
        <h2 className="text-xs font-semibold">확인이 필요한 항목</h2>
        <span className="text-[11px] text-[--color-muted-foreground]">
          열린 이슈 {warnings.open_total}건
          {warnings.overdue > 0 && ` · 기한 초과 ${warnings.overdue}건`}
        </span>
        <Link
          href={"/operations/issues" as Route}
          className="ops-focusable ml-auto flex items-center gap-1 text-[11px] font-medium"
        >
          전체 보기
          <ArrowRight className="h-3 w-3" aria-hidden />
        </Link>
      </div>
      <ul className="divide-y divide-[--color-border]">
        {warnings.by_type.map((t) => {
          const Icon =
            t.severity === "BLOCKING" ? CircleAlert
              : t.severity === "WARNING" ? AlertTriangle
                : Info;
          return (
            <li key={`${t.issue_type}-${t.severity}`}>
              <Link
                href={`/operations/issues?issue_type=${encodeURIComponent(t.issue_type)}` as Route}
                className="ops-focusable flex items-center gap-3 px-4 py-2.5 hover:bg-[--color-muted]"
              >
                <Icon
                  className={cn(
                    "h-4 w-4 shrink-0",
                    t.severity === "BLOCKING" ? "text-[--color-danger]"
                      : t.severity === "WARNING" ? "text-[--color-warning]"
                        : "text-[--color-info]",
                  )}
                  aria-hidden
                />
                <span className="text-[13px] font-medium">{issueTypeLabel(t.issue_type)}</span>
                <span
                  className={cn(
                    "rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums",
                    t.severity === "BLOCKING" ? "bg-[--color-danger-bg] text-[--color-danger]"
                      : t.severity === "WARNING" ? "bg-[--color-warning-bg] text-[--color-warning]"
                        : "bg-[--color-info-bg] text-[--color-info]",
                  )}
                >
                  {t.count}건
                </span>
                <span className="min-w-0 flex-1 truncate text-xs text-[--color-muted-foreground]">
                  {t.severity === "BLOCKING" ? "월 마감을 막는 항목입니다" : ""}
                </span>
                <ArrowRight className="h-3.5 w-3.5 shrink-0 text-[--color-muted-foreground]" aria-hidden />
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 차트
 * ──────────────────────────────────────────────────────────── */

function TrendCard({ data, isLoading }: { data?: DashboardResponse; isLoading: boolean }) {
  const rows = data?.trend ?? [];
  const hasConfirmed = rows.some((r) => r.source === "MONTH_CLOSE_SNAPSHOT");

  return (
    <ChartCard
      title={`최근 ${TREND_MONTHS}개월 매출·순이익`}
      description={
        hasConfirmed
          ? "진한 막대는 확정된 달, 옅은 막대는 아직 진행 중인 달입니다"
          : "아직 확정된 달이 없어 모두 진행 중 집계입니다"
      }
      isLoading={isLoading}
      isEmpty={rows.length === 0}
      actions={
        <ChartLegend
          items={[
            { label: "매출", color: CHART_COLORS.revenue },
            { label: "순이익", color: CHART_COLORS.profit },
            { label: "마진율", color: CHART_COLORS.warning },
          ]}
        />
      }
    >
      <ComposedChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} vertical={false} />
        <XAxis dataKey="year_month" {...AXIS_PROPS} tickFormatter={formatYearMonth} minTickGap={8} />
        <YAxis yAxisId="money" {...AXIS_PROPS} tickFormatter={moneyTick} width={56} />
        <YAxis yAxisId="rate" orientation="right" {...AXIS_PROPS} tickFormatter={percentTick} width={44} />
        <RechartsTooltip
          content={tooltipContent({
            formatLabel: formatYearMonth,
            formats: { gross_sales: "money", net_profit: "money", margin_rate: "percent" },
            extra: (row) => (
              <p className="mt-1.5 border-t border-[--color-border] pt-1.5 text-[11px] text-[--color-muted-foreground]">
                {SOURCE_LABEL[row.source as "LIVE" | "MONTH_CLOSE_SNAPSHOT"]}
                {" · "}
                {CLOSE_STATE_LABEL[row.close_state as "OPEN"]}
              </p>
            ),
          })}
        />
        <Bar yAxisId="money" dataKey="gross_sales" name="매출" radius={[3, 3, 0, 0]} maxBarSize={28}>
          {rows.map((r) => (
            <Cell
              key={r.year_month}
              fill={CHART_COLORS.revenue}
              // 확정되지 않은 달은 옅게 — 같은 선 위의 두 출처를 눈으로 구분한다.
              fillOpacity={r.source === "MONTH_CLOSE_SNAPSHOT" ? 1 : 0.45}
            />
          ))}
        </Bar>
        <Bar yAxisId="money" dataKey="net_profit" name="순이익" radius={[3, 3, 0, 0]} maxBarSize={28}>
          {rows.map((r) => (
            <Cell
              key={r.year_month}
              fill={r.net_profit < 0 ? CHART_COLORS.loss : CHART_COLORS.profit}
              fillOpacity={r.source === "MONTH_CLOSE_SNAPSHOT" ? 1 : 0.45}
            />
          ))}
        </Bar>
        <Line
          yAxisId="rate" type="monotone" dataKey="margin_rate" name="마진율"
          stroke={CHART_COLORS.warning} strokeWidth={2} dot={false} connectNulls={false}
        />
      </ComposedChart>
    </ChartCard>
  );
}

function WaterfallCard({ data, isLoading }: { data?: DashboardResponse; isLoading: boolean }) {
  /** recharts 에 워터폴 전용 차트가 없다 — 투명 받침 막대 + 값 막대로 만든다. */
  const rows = useMemo(() => {
    if (!data) return [];
    let cursor = 0;
    return data.waterfall.map((step) => {
      if (step.kind === "start") {
        cursor = step.amount;
        return { ...step, base: 0, value: step.amount, display: step.amount };
      }
      if (step.kind === "total") {
        return { ...step, base: 0, value: Math.max(0, step.amount), display: step.amount };
      }
      cursor -= step.amount;
      return { ...step, base: Math.max(0, cursor), value: step.amount, display: -step.amount };
    });
  }, [data]);

  // 전부 0이면 그릴 게 없다. 0원짜리 막대를 그려서 "데이터가 있다"고 오해시키지 않는다.
  const allZero = rows.every((r) => r.amount === 0);

  return (
    <ChartCard
      title="총매출에서 순이익까지"
      description="매출이 어디서 얼마나 빠져나가는지 보여줍니다"
      isLoading={isLoading}
      isEmpty={rows.length === 0 || allZero}
      emptyText="이 달에는 집계된 판매·비용이 없습니다"
    >
      <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} vertical={false} />
        <XAxis dataKey="label" {...AXIS_PROPS} interval={0} angle={-30} textAnchor="end" height={72} />
        <YAxis {...AXIS_PROPS} tickFormatter={moneyTick} width={56} />
        <RechartsTooltip
          content={tooltipContent({
            formats: { value: "money" },
            extra: (row) => (
              <p className="mt-1.5 border-t border-[--color-border] pt-1.5 text-[11px] text-[--color-muted-foreground]">
                {(row.kind as string) === "deduction" ? "차감" : "합계"} ·{" "}
                {formatKRW(row.display as number)}
              </p>
            ),
          })}
        />
        <Bar dataKey="base" stackId="wf" fill="transparent" />
        <Bar dataKey="value" name="금액" stackId="wf" radius={[3, 3, 0, 0]} maxBarSize={40}>
          {rows.map((d) => (
            <Cell
              key={d.key}
              fill={
                d.kind === "start"
                  ? CHART_COLORS.revenue
                  : d.kind === "total"
                    ? (d.display as number) >= 0 ? CHART_COLORS.profit : CHART_COLORS.loss
                    : CHART_COLORS.cost
              }
            />
          ))}
        </Bar>
      </BarChart>
    </ChartCard>
  );
}

function MarketplaceCard({
  data, isLoading, labelOf,
}: {
  data?: DashboardResponse;
  isLoading: boolean;
  labelOf: (code: string) => string;
}) {
  const rows = data?.by_marketplace ?? [];
  const unpriced = rows.reduce((a, r) => a + r.unpriced_order_count, 0);

  return (
    <ChartCard
      title="판매처별 매출·공헌이익"
      description={
        unpriced > 0
          ? `사업 공통비 차감 전 금액입니다 · 마진 미계산 주문 ${unpriced}건 포함`
          : "사업 공통비는 판매처에 귀속되지 않아 공헌이익으로 표시합니다"
      }
      isLoading={isLoading}
      isEmpty={rows.length === 0}
      emptyText="이 달에 등록된 판매가 없습니다"
      actions={
        <ChartLegend
          items={[
            { label: "매출", color: CHART_COLORS.revenue },
            { label: "공헌이익", color: CHART_COLORS.profit },
          ]}
        />
      }
    >
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} horizontal={false} />
        <XAxis type="number" {...AXIS_PROPS} tickFormatter={moneyTick} />
        <YAxis
          type="category" dataKey="marketplace" {...AXIS_PROPS} width={78}
          tickFormatter={labelOf}
        />
        <RechartsTooltip
          content={tooltipContent({
            formatLabel: labelOf,
            formats: { gross_sales: "money", contribution_profit: "money" },
            extra: (row) => (
              <p className="mt-1.5 border-t border-[--color-border] pt-1.5 text-[11px] text-[--color-muted-foreground]">
                공헌이익률 {formatPercent(row.contribution_rate as number)} · 주문{" "}
                {(row.order_count as number)?.toLocaleString("ko-KR")}건
              </p>
            ),
          })}
        />
        <Bar dataKey="gross_sales" name="매출" fill={CHART_COLORS.revenue} radius={[0, 3, 3, 0]} maxBarSize={14} />
        <Bar dataKey="contribution_profit" name="공헌이익" fill={CHART_COLORS.profit} radius={[0, 3, 3, 0]} maxBarSize={14} />
      </BarChart>
    </ChartCard>
  );
}

function ExpenseCard({ data, isLoading }: { data?: DashboardResponse; isLoading: boolean }) {
  const rows = data?.expense_breakdown ?? [];
  return (
    <ChartCard
      title="비용 카테고리 구성"
      description="이 달에 집행된 사업비용 (정산 대상만)"
      isLoading={isLoading}
      isEmpty={rows.length === 0}
      emptyText="이 달에 등록된 사업비용이 없습니다"
    >
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} horizontal={false} />
        <XAxis type="number" {...AXIS_PROPS} tickFormatter={moneyTick} />
        <YAxis type="category" dataKey="category_name" {...AXIS_PROPS} width={96} />
        <RechartsTooltip
          content={tooltipContent({
            formats: { amount: "money" },
            extra: (row) => (
              <p className="mt-1.5 border-t border-[--color-border] pt-1.5 text-[11px] text-[--color-muted-foreground]">
                전체 비용의 {formatPercent(row.ratio as number)} · {row.entry_count as number}건
              </p>
            ),
          })}
        />
        <Bar dataKey="amount" name="금액" radius={[0, 3, 3, 0]} maxBarSize={18}>
          {rows.map((c, i) => (
            <Cell key={c.category_name} fill={CATEGORY_COLORS[i % CATEGORY_COLORS.length]} />
          ))}
        </Bar>
      </BarChart>
    </ChartCard>
  );
}

function MarginScatterCard({ data, isLoading }: { data?: DashboardResponse; isLoading: boolean }) {
  const rows = (data?.margin_distribution ?? []).filter((p) => p.contribution_rate !== null);
  return (
    <ChartCard
      title="상품별 매출과 공헌이익률"
      description="오른쪽 아래로 갈수록 많이 팔리지만 안 남는 상품입니다"
      className="xl:col-span-2"
      isLoading={isLoading}
      isEmpty={rows.length === 0}
      emptyText="이 달에 매출이 발생한 상품이 없습니다"
    >
      <ScatterChart margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} />
        <XAxis type="number" dataKey="gross_sales" name="매출" {...AXIS_PROPS} tickFormatter={moneyTick} />
        <YAxis
          type="number" dataKey="contribution_rate" name="공헌이익률"
          {...AXIS_PROPS} tickFormatter={percentTick} width={48}
        />
        <ZAxis type="number" dataKey="contribution_profit" range={[40, 260]} />
        <ReferenceLine
          y={0} stroke={CHART_COLORS.loss} strokeDasharray="4 4"
          label={{ value: "손익분기", fontSize: 10, fill: CHART_COLORS.loss, position: "insideTopLeft" }}
        />
        <RechartsTooltip
          content={tooltipContent({
            formats: {
              gross_sales: "money", contribution_rate: "percent", contribution_profit: "money",
            },
            formatLabel: () => "상품",
            extra: (row) => (
              <p className="mt-1.5 border-t border-[--color-border] pt-1.5 text-[11px] font-medium">
                {row.product_name as string}
              </p>
            ),
          })}
          cursor={{ strokeDasharray: "3 3" }}
        />
        <Scatter name="상품" data={rows}>
          {rows.map((p, i) => (
            <Cell
              key={p.product_id ?? `${p.product_name}-${i}`}
              fill={
                (p.contribution_rate ?? 0) < 0 ? CHART_COLORS.loss
                  : (p.contribution_rate ?? 0) < 0.1 ? CHART_COLORS.warning
                    : CHART_COLORS.profit
              }
            />
          ))}
        </Scatter>
      </ScatterChart>
    </ChartCard>
  );
}

/* ────────────────────────────────────────────────────────────
 * 현금흐름 — 이 화면에서 유일하게 "월" 이 아닌 카드
 *
 * 나머지 카드는 전부 "그 달에 얼마를 벌었나"(손익)를 답한다. 이 카드만
 * "앞으로 통장이 버티나"(현금)를 답한다. 둘을 합치면 매출 3천만이 현금
 * 3천만으로 읽힌다 — docs/54 §1 이 경고하는 바로 그 혼동이다.
 * 그래서 기준(`basis`)을 제목 옆에 항상 붙인다.
 * ──────────────────────────────────────────────────────────── */

function CashflowPanel({ data }: { data?: DashboardResponse }) {
  const cf = data?.cashflow;

  return (
    <section className="ops-panel flex flex-col">
      <div className="border-b border-[--color-border] px-4 py-3">
        <div className="flex items-center gap-1.5">
          <Banknote className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
          <h3 className="text-sm font-semibold">현금흐름</h3>
          {cf && (
            /* 선택한 달이 아니라는 사실을 제목 옆에서 바로 말한다. */
            <span className="rounded-full bg-[--color-muted] px-1.5 py-0.5 text-[10px]">
              {cf.basis}
            </span>
          )}
        </div>
        <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
          위 카드들과 달리 선택한 달이 아니라 앞으로의 입출금입니다
        </p>
      </div>

      {!cf ? (
        <div className="flex-1 px-4 py-10 text-center text-xs text-[--color-muted-foreground]">
          불러오는 중...
        </div>
      ) : (
        <div className="flex flex-1 flex-col gap-3 px-4 py-3">
          <div>
            <p className="text-[11px] text-[--color-muted-foreground]">예상 잔액</p>
            {cf.balance_available ? (
              <p
                className={cn(
                  "text-lg font-semibold tabular-nums",
                  (cf.projected_ending_balance ?? 0) < 0 && "text-[--color-loss]",
                )}
              >
                {formatKRW(cf.projected_ending_balance)}
              </p>
            ) : (
              <>
                {/* 0원으로 그리면 "통장이 비었다" 로 읽힌다. */}
                <p className="text-lg font-semibold text-[--color-muted-foreground]">—</p>
                <Link
                  href={"/finance/cashflow" as Route}
                  className="ops-focusable text-[11px] underline underline-offset-2"
                >
                  기준 잔액을 입력하면 계산됩니다
                </Link>
              </>
            )}
          </div>

          <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
            <dt className="text-[--color-muted-foreground]">실제 유출</dt>
            <dd className="text-right tabular-nums">{formatKRW(cf.outflow.actual)}</dd>
            <dt className="text-[--color-muted-foreground]">예정 유출</dt>
            <dd className="text-right tabular-nums">{formatKRW(cf.outflow.expected)}</dd>
            <dt className="text-[--color-muted-foreground]">확정 유입</dt>
            <dd className="text-right tabular-nums">{formatKRW(cf.inflow.confirmed)}</dd>
            <dt className="text-[--color-muted-foreground]">미지급 잔액</dt>
            <dd className="text-right tabular-nums">{formatKRW(cf.unpaid_payout)}</dd>
          </dl>

          {cf.overdue_payout > 0 && (
            <p className="flex items-start gap-1 text-[11px] text-[--color-warning]">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              지급 예정일이 지난 미지급 {formatKRW(cf.overdue_payout)}
            </p>
          )}

          {!cf.actual_inflow_source_available && (
            /* 설명 없는 "실제 유입 0원" 은 장부가 틀린 것처럼 보인다. */
            <p className="text-[11px] text-[--color-muted-foreground]">
              실제 입금은 은행·판매처 연동이 없어 자동 확인되지 않습니다. 정산서는
              금액 확정일 뿐이라 &lsquo;확정&rsquo;으로만 집계합니다.
            </p>
          )}

          <Link
            href={"/finance/cashflow" as Route}
            className="ops-focusable mt-auto flex items-center gap-1 text-xs font-medium"
          >
            현금흐름 자세히 보기
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
      )}
    </section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 확정된 달 — 진행월 카드와 나란히 두되 섞지 않는다
 * ──────────────────────────────────────────────────────────── */

function ConfirmedMonthsPanel({ data }: { data?: DashboardResponse }) {
  const rows = data?.confirmed_snapshots ?? [];

  return (
    <section className="ops-panel flex flex-col">
      <div className="border-b border-[--color-border] px-4 py-3">
        <h3 className="text-sm font-semibold">확정된 달</h3>
        <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
          확정 시점에 박제된 숫자입니다. 이후 데이터가 바뀌어도 변하지 않습니다
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-4 py-10 text-center">
          <p className="text-xs text-[--color-muted-foreground]">아직 확정된 달이 없습니다</p>
          <Link
            href={"/settlements/monthly" as Route}
            className="ops-focusable flex items-center gap-1 text-xs font-medium"
          >
            월 마감으로 이동
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
      ) : (
        <ul className="flex-1 divide-y divide-[--color-border]">
          {rows.map((s) => (
            <li key={`${s.year_month}-${s.generation}`}>
              <Link
                href={`/settlements/monthly/${s.year_month}` as Route}
                className="ops-focusable flex items-center justify-between gap-2 px-4 py-3 hover:bg-[--color-muted]"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[13px] font-medium">
                      {formatYearMonth(s.year_month)}
                    </span>
                    {s.close_state === "LOCKED" && (
                      <Lock className="h-3 w-3 text-[--color-muted-foreground]" aria-hidden />
                    )}
                    {s.generation > 1 && (
                      <span className="rounded-full bg-[--color-muted] px-1.5 py-0.5 text-[10px]">
                        {s.generation}세대
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 truncate text-[11px] text-[--color-muted-foreground]">
                    {s.profit_label}
                  </p>
                </div>
                <span
                  className={cn(
                    "shrink-0 text-sm font-semibold tabular-nums",
                    s.net_profit < 0 && "text-[--color-loss]",
                  )}
                >
                  {formatCompactKRW(s.net_profit)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
