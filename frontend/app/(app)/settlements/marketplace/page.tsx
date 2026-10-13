"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { toast } from "sonner";
import { FileUp, Plug, Plus } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs } from "@/components/shell/page-shell";
import { FilterBar, SearchInput, SelectFilter } from "@/components/filters/filter-bar";
import { DateRangeFilter } from "@/components/filters/date-range-filter";
import { DataTable } from "@/components/table/data-table";
import { TablePagination } from "@/components/table/table-pagination";
import { ExportButton, downloadCsv, toCsv } from "@/components/table/table-toolbar";
import { useListQuery } from "@/hooks/useListQuery";
import { settlementAdapter } from "@/lib/adapters";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import type { MarketplaceSettlementRow } from "@/lib/adapters/types";
import { formatDate, formatKRW, formatYearMonth } from "@/lib/format";


const SOURCE_LABEL: Record<string, string> = {
  API: "API 자동", UPLOAD: "파일 업로드", MANUAL: "수동 등록",
};

export default function MarketplaceSettlementPage() {
  const { state, set, reset, hasActiveFilters } = useListQuery({
    defaults: { tab: "scheduled", pageSize: 50, sort: "scheduledDate", dir: "desc" },
  });
  const tab = state.tab || "scheduled";

  const { data: counts } = useQuery({
    queryKey: ["settlementCounts"], queryFn: () => settlementAdapter.counts(),
  });

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["settlements", state],
    queryFn: () =>
      settlementAdapter.list({
        tab,
        q: state.q || undefined,
        from: state.from || undefined,
        to: state.to || undefined,
        marketplace: state.channel || undefined,
        page: state.page,
        pageSize: state.pageSize,
        sort: state.sort,
        dir: state.dir,
      }),
    enabled: tab !== "connections",
  });

  const rows = data?.items ?? [];
  // 판매처 이름은 조직이 등록한 채널 카탈로그에서 온다. 상수로 박아 두면
  // 조직이 추가한 커스텀 채널이 목록에도, 라벨에도 나타나지 않는다.
  const { channels, labelOf } = useSaleChannels();

  const columns = React.useMemo<ColumnDef<MarketplaceSettlementRow>[]>(
    () => [
      {
        accessorKey: "marketplace", header: "판매처", size: 110,
        cell: ({ getValue }) => (
          <Badge variant="default" className="px-1.5 py-0 text-[10px]">
            {labelOf(getValue<string>())}
          </Badge>
        ),
      },
      {
        accessorKey: "baseMonth", header: "정산 기준월", size: 100, meta: { align: "center" },
        cell: ({ getValue }) => formatYearMonth(getValue<string>()),
      },
      {
        accessorKey: "scheduledDate", header: "정산 예정일", size: 100, meta: { align: "center" },
        cell: ({ getValue }) => <span className="tabular-nums">{formatDate(getValue<string | null>())}</span>,
      },
      {
        accessorKey: "completedDate", header: "정산 완료일", size: 100, meta: { align: "center" },
        cell: ({ getValue }) => <span className="tabular-nums">{formatDate(getValue<string | null>())}</span>,
      },
      {
        accessorKey: "grossRevenue", header: "매출액", size: 110, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number>()),
      },
      {
        accessorKey: "commissionFee", header: "판매수수료", size: 100, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number>()),
      },
      {
        accessorKey: "shippingDeduction", header: "배송비 차감", size: 96, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number>()),
      },
      {
        accessorKey: "adAndCouponDeduction", header: "광고·쿠폰", size: 96, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number>()),
      },
      {
        accessorKey: "otherDeduction", header: "기타 차감", size: 90, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number>()),
      },
      {
        accessorKey: "expectedSettlement", header: "예상 정산액", size: 110, meta: { align: "right" },
        cell: ({ getValue }) => <span className="font-medium">{formatKRW(getValue<number>())}</span>,
      },
      {
        accessorKey: "actualDeposit", header: "실제 입금액", size: 110, meta: { align: "right" },
        cell: ({ getValue }) => {
          const v = getValue<number | null>();
          return v === null ? <span className="text-[--color-muted-foreground]">미입금</span> : formatKRW(v);
        },
      },
      {
        accessorKey: "difference", header: "차이", size: 100, meta: { align: "right" },
        cell: ({ getValue }) => {
          const v = getValue<number | null>();
          if (v === null) return <span className="text-[--color-muted-foreground]">-</span>;
          if (v === 0) return <span className="text-[--color-profit]">일치</span>;
          return <span className="font-semibold text-[--color-loss]">{formatKRW(v)}</span>;
        },
      },
      {
        accessorKey: "matchStatus", header: "매칭", size: 84, meta: { align: "center" },
        cell: ({ getValue }) => {
          const s = getValue<string>();
          return (
            <Badge
              variant={s === "MATCHED" ? "success" : s === "MISMATCH" ? "danger" : "default"}
              className="px-1.5 py-0 text-[10px]"
            >
              {s === "MATCHED" ? "일치" : s === "MISMATCH" ? "불일치" : "미매칭"}
            </Badge>
          );
        },
      },
      {
        accessorKey: "source", header: "등록", size: 84, meta: { align: "center" },
        cell: ({ getValue }) => (
          <span className="text-[11px] text-[--color-muted-foreground]">
            {SOURCE_LABEL[getValue<string>()] ?? getValue<string>()}
          </span>
        ),
      },
    ],
    // labelOf 는 채널 목록이 도착하면 바뀐다. 빈 배열로 두면 컬럼이 첫 렌더의
    // (아직 비어 있는) 라벨 함수를 계속 써서 판매처가 코드로만 보인다.
    [labelOf],
  );

  function handleExport() {
    if (rows.length === 0) { toast.error("내보낼 데이터가 없습니다"); return; }
    downloadCsv(
      `쇼핑몰정산_${tab}`,
      toCsv(rows, [
        { header: "판매처", value: (r) => labelOf(r.marketplace) },
        { header: "정산 기준월", value: (r) => r.baseMonth },
        { header: "정산 예정일", value: (r) => r.scheduledDate },
        { header: "정산 완료일", value: (r) => r.completedDate },
        { header: "매출액", value: (r) => r.grossRevenue },
        { header: "판매수수료", value: (r) => r.commissionFee },
        { header: "배송비 차감", value: (r) => r.shippingDeduction },
        { header: "광고·쿠폰 차감", value: (r) => r.adAndCouponDeduction },
        { header: "기타 차감", value: (r) => r.otherDeduction },
        { header: "예상 정산액", value: (r) => r.expectedSettlement },
        { header: "실제 입금액", value: (r) => r.actualDeposit },
        { header: "차이", value: (r) => r.difference },
      ]),
    );
    toast.success(`${rows.length}건을 내보냈습니다`);
  }

  const tabs = [
    { key: "scheduled", label: "정산 예정", count: counts?.scheduled },
    { key: "completed", label: "정산 완료", count: counts?.completed },
    { key: "mismatch", label: "정산 불일치", count: counts?.mismatch },
    { key: "connections", label: "연동 관리" },
  ];

  return (
    <PageShell
      title="쇼핑몰 정산"
      description="판매처가 실제로 입금한 금액을 우리 장부와 대조합니다"
      actions={
        tab !== "connections" ? (
          <div className="flex gap-2">
            <Button variant="outline" size="sm" asChild>
              <Link href="/settlements/marketplace/import">
                <FileUp className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                파일 업로드
              </Link>
            </Button>
            <Button size="sm" onClick={() => toast.info("수동 등록은 파일 업로드 화면에서 진행합니다")}>
              <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              수동 등록
            </Button>
          </div>
        ) : undefined
      }
      tabs={<PageTabs tabs={tabs} value={tab} onChange={(k) => set({ tab: k })} />}
      filters={
        tab !== "connections" ? (
          <FilterBar onReset={reset} hasActiveFilters={hasActiveFilters} resultCount={data?.total}
            actions={<ExportButton onExport={handleExport} disabled={rows.length === 0} />}
          >
            <SearchInput
              className="w-56" value={state.q} onChange={(v) => set({ q: v })}
              placeholder="판매처·정산 기준월" ariaLabel="정산 검색"
            />
            <DateRangeFilter
              label="정산 예정일"
              value={{ from: state.from, to: state.to }}
              onChange={(r) => set({ from: r.from, to: r.to })}
              presets={["thisMonth", "lastMonth", "last30"]}
            />
            <SelectFilter
              value={state.channel} onChange={(v) => set({ channel: v })}
              placeholder="판매처 전체" label="판매처"
              options={channels.map((c) => ({ value: c.code, label: c.display_name }))}
            />
          </FilterBar>
        ) : undefined
      }
      footer={
        tab !== "connections" ? (
          <TablePagination
            page={state.page} pageSize={state.pageSize} total={data?.total ?? 0}
            onPageChange={(p) => set({ page: p })}
            onPageSizeChange={(s) => set({ pageSize: s, page: 1 })}
          />
        ) : undefined
      }
    >
      {tab === "connections" ? (
        <div className="ops-panel flex flex-col items-center gap-3 py-16 text-center">
          <Plug className="h-8 w-8 text-[--color-muted-foreground] opacity-40" aria-hidden />
          <p className="text-sm font-medium">판매처 연동은 별도 화면에서 관리합니다</p>
          <p className="max-w-md text-xs text-[--color-muted-foreground]">
            API 키 등록·동기화 상태·마지막 실행 결과를 한 곳에서 봅니다.
          </p>
          <Button size="sm" asChild>
            <Link href="/integrations/marketplaces">쇼핑몰 API 연결로 이동</Link>
          </Button>
        </div>
      ) : (
        <div className="min-h-0 flex-1">
          {tab === "mismatch" && rows.length > 0 && (
            <p className="mb-3 rounded-lg border border-[--color-danger-border] bg-[--color-danger-bg] px-3 py-2 text-[11px] text-[--color-danger]">
              예상 정산액과 실제 입금액이 다릅니다. 반품 차감이 다음 달로 이월되었거나,
              수수료 정책이 실제와 다를 수 있습니다. <strong>자동으로 맞추지 않습니다</strong> —
              원인을 확인한 뒤 조정하세요.
            </p>
          )}
          <DataTable
            columns={columns} data={rows} isLoading={isLoading} isError={isError}
            onRetry={() => refetch()} getRowId={(r) => r.id}
            rowClassName={(r) => (r.matchStatus === "MISMATCH" ? "bg-[--color-danger-bg]/40" : undefined)}
            emptyText={
              tab === "mismatch" ? "불일치 건이 없습니다" :
              tab === "completed" ? "완료된 정산이 없습니다" : "예정된 정산이 없습니다"
            }
            emptyDescription={
              tab === "mismatch"
                ? "장부와 실입금이 모두 일치합니다"
                : "API 자동 동기화, 파일 업로드, 수동 등록 중 하나로 정산 내역을 추가하세요"
            }
            className={cn("h-full", tab === "mismatch" && rows.length > 0 && "h-[calc(100%-3rem)]")}
          />
        </div>
      )}
    </PageShell>
  );
}
