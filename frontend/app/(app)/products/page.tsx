"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ExternalLink, ImageOff, Plus, Settings2 } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs } from "@/components/shell/page-shell";
import { FilterBar, RangeFilter, SearchInput, SelectFilter } from "@/components/filters/filter-bar";
import { DateRangeFilter } from "@/components/filters/date-range-filter";
import { DataTable } from "@/components/table/data-table";
import { TablePagination } from "@/components/table/table-pagination";
import {
  ColumnSettings, ExportButton, SavedViews, downloadCsv, toCsv, useColumnPreferences,
  type ColumnSpec,
} from "@/components/table/table-toolbar";
import { ProductDrawer } from "@/components/products/product-drawer";
import { OptionModal } from "@/components/products/option-modal";
import { useListQuery } from "@/hooks/useListQuery";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import { productAdapter } from "@/lib/adapters";
import type { ProductRow, ProductStatus } from "@/lib/adapters/types";
import { formatDate, formatKRW, formatPercent, supplierLabelFromUrl } from "@/lib/format";

const STATUS_LABEL: Record<ProductStatus, string> = {
  CANDIDATE: "소싱검토", APPROVED: "승인", ACTIVE: "판매중",
  PAUSED: "일시중지", DISCONTINUED: "단종",
};
const STATUS_TONE: Record<ProductStatus, "info" | "success" | "warning" | "default"> = {
  CANDIDATE: "default", APPROVED: "info", ACTIVE: "success",
  PAUSED: "warning", DISCONTINUED: "default",
};

/**
 * 판매처 코드 → 라벨은 **실제 채널 목록**에서 온다 (`useSaleChannels`).
 * 예전에는 `MOCK_MARKETPLACES` 상수였다 — 조직이 추가한 커스텀 채널은
 * 코드 그대로 보였고, 목록에 없는 채널은 존재하지 않는 것처럼 보였다.
 */

/** 상태 탭 — 기존 "아이템리스트"(소싱 후보)를 여기로 흡수했다. */
const STATUS_TABS = [
  { key: "", label: "전체" },
  { key: "CANDIDATE", label: "소싱검토" },
  { key: "APPROVED", label: "승인" },
  { key: "ACTIVE", label: "판매중" },
  { key: "PAUSED", label: "일시중지" },
  { key: "DISCONTINUED", label: "단종" },
];

const COLUMN_SPECS: ColumnSpec[] = [
  { id: "image", label: "대표 이미지" },
  { id: "name", label: "상품명", locked: true },
  { id: "sku", label: "내부 SKU" },
  { id: "marketplaces", label: "판매처" },
  { id: "marketplaceProductNo", label: "판매처 상품번호" },
  { id: "sourceUrl", label: "소스 상품 URL" },
  { id: "supplierName", label: "공급처" },
  { id: "costEffectiveFrom", label: "원가 적용일" },
  { id: "priceEffectiveFrom", label: "판매가 적용일" },
  { id: "totalCost", label: "총원가" },
  { id: "salePrice", label: "판매가" },
  { id: "expectedSettlement", label: "예상 정산액" },
  { id: "expectedNetProfit", label: "예상 순이익" },
  { id: "marginRate", label: "마진율" },
  { id: "optionCount", label: "옵션 수" },
  { id: "status", label: "상품 상태" },
  { id: "ownerName", label: "담당자" },
  { id: "updatedAt", label: "최종 수정일" },
];

/** 기본 숨김 — 18개를 다 켜면 가로 스크롤이 화면 세 배가 된다. */
const DEFAULT_HIDDEN = [
  "image", "marketplaceProductNo", "supplierName", "priceEffectiveFrom", "expectedSettlement",
];

const DATE_FIELDS = [
  { value: "createdAt", label: "등록일" },
  { value: "costEffectiveFrom", label: "원가 적용일" },
  { value: "priceEffectiveFrom", label: "판매가 적용일" },
];

export default function ProductsPage() {
  const { labelOf } = useSaleChannels();
  const { state, set, reset, hasActiveFilters, queryString } = useListQuery({
    defaults: { pageSize: 50, sort: "updatedAt", dir: "desc" },
  });

  /**
   * drawer 대상은 URL 이 소유한다 (`?productId=<id>`).
   *
   * 상태로만 들고 있으면 승인 화면에서 "이 상품 보기" 로 넘어올 방법이 없고,
   * 새로고침하면 drawer 가 닫히며, 링크를 공유할 수도 없다.
   */
  const [drawerId, setDrawerIdState] = React.useState<string | null>(null);

  const setDrawerId = React.useCallback((id: string | null) => {
    setDrawerIdState(id);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("productId", id);
    else url.searchParams.delete("productId");
    // replaceState 를 쓴다 — drawer 여닫기가 뒤로가기 기록을 채우면
    // 사용자가 목록으로 돌아가려고 뒤로가기를 여러 번 눌러야 한다.
    window.history.replaceState(null, "", url.toString());
  }, []);

  // 딥링크 진입: URL 에 productId 가 있으면 그 상품 drawer 를 연다.
  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const id = new URLSearchParams(window.location.search).get("productId");
    if (id) setDrawerIdState(id);
  }, []);
  const [optionTarget, setOptionTarget] = React.useState<ProductRow | null>(null);
  const { visibility, setVisibility, order, setOrder, density, setDensity } =
    useColumnPreferences("products", COLUMN_SPECS, DEFAULT_HIDDEN);

  const dateField = (state.extra.dateField ?? "createdAt") as
    "createdAt" | "costEffectiveFrom" | "priceEffectiveFrom";

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["products", state],
    queryFn: () =>
      productAdapter.list({
        q: state.q || undefined,
        status: state.status || undefined,
        marketplace: state.channel || undefined,
        ownerId: state.owner || undefined,
        from: state.from || undefined,
        to: state.to || undefined,
        dateField,
        marginMin: state.extra.marginMin,
        marginMax: state.extra.marginMax,
        page: state.page,
        pageSize: state.pageSize,
        sort: state.sort,
        dir: state.dir,
      }),
  });


  const rows = data?.items ?? [];

  const columns = React.useMemo<ColumnDef<ProductRow>[]>(
    () => [
      {
        id: "image", header: "이미지", size: 52, enableSorting: false,
        cell: ({ row }) =>
          row.original.imageUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={row.original.imageUrl} alt="" className="h-8 w-8 rounded object-cover" />
          ) : (
            <span className="flex h-8 w-8 items-center justify-center rounded bg-[--color-muted]">
              <ImageOff className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
            </span>
          ),
      },
      {
        id: "name", accessorKey: "name", header: "상품명", size: 260,
        cell: ({ row }) => (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setDrawerId(row.original.id); }}
            className="ops-focusable block max-w-[260px] truncate text-left font-medium hover:text-[--color-accent] hover:underline"
            title={row.original.name}
          >
            {row.original.name}
          </button>
        ),
      },
      {
        id: "sku", accessorKey: "sku", header: "SKU", size: 96,
        cell: ({ getValue }) => (
          <span className="font-mono text-[11px] text-[--color-muted-foreground]">
            {getValue<string>() ?? "-"}
          </span>
        ),
      },
      {
        id: "marketplaces", header: "판매처", size: 130, enableSorting: false,
        cell: ({ row }) => (
          <div className="flex flex-wrap gap-0.5">
            {row.original.marketplaces.length === 0 ? (
              <span className="text-[11px] text-[--color-muted-foreground]">미등록</span>
            ) : (
              row.original.marketplaces.map((m) => (
                <Badge key={m} variant="default" className="px-1 py-0 text-[10px]">
                  {labelOf(m)}
                </Badge>
              ))
            )}
          </div>
        ),
      },
      {
        id: "marketplaceProductNo", header: "판매처 상품번호", size: 130, enableSorting: false,
        cell: ({ row }) => {
          const nos = Object.values(row.original.marketplaceProductNos);
          return (
            <span className="font-mono text-[11px] text-[--color-muted-foreground]">
              {nos[0] ?? "-"}
              {nos.length > 1 && <span className="ml-1">외 {nos.length - 1}</span>}
            </span>
          );
        },
      },
      {
        id: "sourceUrl", header: "소스 URL", size: 110, enableSorting: false,
        cell: ({ row }) =>
          row.original.sourceUrl ? (
            <a
              href={row.original.sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="ops-focusable inline-flex items-center gap-1 text-[11px] text-[--color-accent] hover:underline"
            >
              {supplierLabelFromUrl(row.original.sourceUrl)}
              <ExternalLink className="h-3 w-3" aria-hidden />
            </a>
          ) : (
            // 소스 URL 이 없으면 재주문 때 공급처를 못 찾는다 — 눈에 띄게 표시한다
            <Badge variant="warning" className="px-1 py-0 text-[10px]">없음</Badge>
          ),
      },
      {
        id: "supplierName", accessorKey: "supplierName", header: "공급처", size: 110,
        cell: ({ getValue }) => (
          <span className="text-[11px]">{getValue<string>() ?? "-"}</span>
        ),
      },
      {
        id: "costEffectiveFrom", accessorKey: "costEffectiveFrom", header: "원가 적용일", size: 96,
        meta: { align: "center" },
        cell: ({ getValue }) => {
          const v = getValue<string | null>();
          const stale = v !== null && v < "2025-09-01";
          return (
            <span className={cn("text-[11px] tabular-nums", stale && "font-medium text-[--color-warning]")}>
              {formatDate(v)}
              {stale && <span className="ml-1" title="원가 확인이 오래됐습니다">⚠</span>}
            </span>
          );
        },
      },
      {
        id: "priceEffectiveFrom", accessorKey: "priceEffectiveFrom", header: "판매가 적용일", size: 96,
        meta: { align: "center" },
        cell: ({ getValue }) => (
          <span className="text-[11px] tabular-nums">{formatDate(getValue<string | null>())}</span>
        ),
      },
      {
        id: "totalCost", accessorKey: "totalCost", header: "총원가", size: 96, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number | null>()),
      },
      {
        id: "salePrice", accessorKey: "salePrice", header: "판매가", size: 96, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number | null>()),
      },
      {
        id: "expectedSettlement", accessorKey: "expectedSettlement", header: "예상 정산액",
        size: 104, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number | null>()),
      },
      {
        id: "expectedNetProfit", accessorKey: "expectedNetProfit", header: "예상 순이익",
        size: 104, meta: { align: "right" },
        cell: ({ getValue }) => {
          const v = getValue<number | null>();
          return (
            <span className={cn("font-medium", (v ?? 0) < 0 && "text-[--color-loss]")}>
              {formatKRW(v)}
            </span>
          );
        },
      },
      {
        id: "marginRate", accessorKey: "marginRate", header: "마진율", size: 84, meta: { align: "right" },
        cell: ({ getValue }) => {
          const v = getValue<number | null>();
          if (v === null) return <span className="text-[--color-muted-foreground]">-</span>;
          // 색만으로 구분하지 않는다 — 손실은 부호가, 저마진은 아이콘이 함께 알린다
          return (
            <span
              className={cn(
                "font-medium tabular-nums",
                v < 0 ? "text-[--color-loss]" : v < 0.1 ? "text-[--color-warning]" : "text-[--color-profit]",
              )}
            >
              {v < 0.1 && v >= 0 && <span className="mr-0.5" title="저마진">⚠</span>}
              {formatPercent(v)}
            </span>
          );
        },
      },
      {
        id: "optionCount", accessorKey: "optionCount", header: "옵션", size: 64, meta: { align: "center" },
        cell: ({ row }) => (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setOptionTarget(row.original); }}
            className={cn(
              "ops-focusable rounded px-1.5 py-0.5 text-[11px]",
              row.original.optionCount > 0
                ? "font-medium text-[--color-accent] hover:bg-[--color-accent-bg]"
                : "text-[--color-muted-foreground] hover:text-[--color-accent]",
            )}
            title="옵션 편집"
          >
            {row.original.optionCount > 0 ? `${row.original.optionCount}개` : "단일"}
          </button>
        ),
      },
      {
        id: "status", accessorKey: "status", header: "상태", size: 84, meta: { align: "center" },
        cell: ({ getValue }) => {
          const s = getValue<ProductStatus>();
          return (
            <Badge variant={STATUS_TONE[s]} className="px-1.5 py-0 text-[10px]">
              {STATUS_LABEL[s]}
            </Badge>
          );
        },
      },
      {
        id: "ownerName", accessorKey: "ownerName", header: "담당자", size: 76,
        cell: ({ getValue }) => (
          <span className="text-[11px]">{getValue<string>() ?? "-"}</span>
        ),
      },
      {
        id: "updatedAt", accessorKey: "updatedAt", header: "최종 수정", size: 96, meta: { align: "center" },
        cell: ({ getValue }) => (
          <span className="text-[11px] tabular-nums text-[--color-muted-foreground]">
            {formatDate(getValue<string>())}
          </span>
        ),
      },
      {
        id: "actions", header: "", size: 40, enableSorting: false, enableHiding: false,
        cell: ({ row }) => (
          <Button
            variant="ghost" size="icon-sm"
            onClick={(e) => { e.stopPropagation(); setDrawerId(row.original.id); }}
            aria-label={`${row.original.name} 수정`}
            title="수정"
          >
            <Settings2 className="h-3.5 w-3.5" aria-hidden />
          </Button>
        ),
      },
    ],
    [labelOf],
  );

  function handleExport() {
    if (rows.length === 0) {
      toast.error("내보낼 데이터가 없습니다");
      return;
    }
    const csv = toCsv(rows, [
      { header: "상품코드", value: (r) => r.businessKey },
      { header: "상품명", value: (r) => r.name },
      { header: "SKU", value: (r) => r.sku },
      { header: "상태", value: (r) => STATUS_LABEL[r.status] },
      { header: "판매처", value: (r) => r.marketplaces.map(labelOf).join(" / ") },
      { header: "소스 상품 URL", value: (r) => r.sourceUrl },
      { header: "공급처", value: (r) => r.supplierName },
      { header: "원가 적용일", value: (r) => r.costEffectiveFrom },
      { header: "판매가 적용일", value: (r) => r.priceEffectiveFrom },
      { header: "총원가", value: (r) => r.totalCost },
      { header: "판매가", value: (r) => r.salePrice },
      { header: "예상 정산액", value: (r) => r.expectedSettlement },
      { header: "예상 순이익", value: (r) => r.expectedNetProfit },
      { header: "마진율", value: (r) => (r.marginRate === null ? "" : (r.marginRate * 100).toFixed(2) + "%") },
      { header: "옵션 수", value: (r) => r.optionCount },
      { header: "담당자", value: (r) => r.ownerName },
      { header: "최종 수정일", value: (r) => r.updatedAt.slice(0, 10) },
    ]);
    downloadCsv(`상품목록_${new Date().toISOString().slice(0, 10)}`, csv);
    toast.success(`${rows.length}건을 내보냈습니다`);
  }

  return (
    <>
      <PageShell
        title="상품 관리"
        description="소싱부터 판매·마진까지 상품 한 건의 모든 정보를 한 곳에서 관리합니다"
        actions={
          <Button size="sm" onClick={() => setDrawerId(rows[0]?.id ?? null)}>
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            상품 등록
          </Button>
        }
        tabs={
          <PageTabs
            tabs={STATUS_TABS}
            value={state.status}
            onChange={(k) => set({ status: k })}
          />
        }
        filters={
          <FilterBar
            onReset={reset}
            hasActiveFilters={hasActiveFilters}
            resultCount={data?.total}
            actions={
              <>
                <SavedViews
                  scope="products"
                  currentQuery={queryString}
                  onApply={(q) => {
                    window.history.replaceState(null, "", q ? `?${q}` : location.pathname);
                    location.reload();
                  }}
                />
                <ColumnSettings
                  specs={COLUMN_SPECS}
                  visibility={visibility}
                  onVisibilityChange={setVisibility}
                  order={order}
                  onOrderChange={setOrder}
                  density={density}
                  onDensityChange={setDensity}
                />
                <ExportButton onExport={handleExport} disabled={rows.length === 0} />
              </>
            }
          >
            <SearchInput
              className="w-64"
              value={state.q}
              onChange={(v) => set({ q: v })}
              placeholder="상품명·SKU·상품번호·URL·공급처·메모"
              ariaLabel="상품 통합 검색"
            />
            <SelectFilter
              value={dateField}
              onChange={(v) => set({ extra: { dateField: v } })}
              placeholder="등록일"
              label="날짜 기준"
              options={DATE_FIELDS}
            />
            <DateRangeFilter
              label=""
              value={{ from: state.from, to: state.to }}
              onChange={(r) => set({ from: r.from, to: r.to })}
              presets={["last7", "last30", "thisMonth", "lastMonth"]}
            />
            <RangeFilter
              label="마진율"
              min={state.extra.marginMin ?? ""}
              max={state.extra.marginMax ?? ""}
              onChange={({ min, max }) => set({ extra: { marginMin: min, marginMax: max } })}
            />
          </FilterBar>
        }
        footer={
          <TablePagination
            page={state.page}
            pageSize={state.pageSize}
            total={data?.total ?? 0}
            onPageChange={(p) => set({ page: p })}
            onPageSizeChange={(s) => set({ pageSize: s, page: 1 })}
          />
        }
      >
        <div className="min-h-0 flex-1">
          <DataTable
            columns={columns}
            data={rows}
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
            selectable
            getRowId={(r) => r.id}
            density={density}
            columnVisibility={visibility}
            onColumnVisibilityChange={(u) =>
              setVisibility(typeof u === "function" ? u(visibility) : u)
            }
            columnOrder={order}
            onColumnOrderChange={(u) => setOrder(typeof u === "function" ? u(order) : u)}
            onRowClick={(r) => setDrawerId(r.id)}
            rowClassName={(r) =>
              (r.expectedNetProfit ?? 0) < 0 ? "bg-[--color-danger-bg]/40" : undefined
            }
            emptyText={hasActiveFilters ? "조건에 맞는 상품이 없습니다" : "등록된 상품이 없습니다"}
            emptyDescription={
              hasActiveFilters
                ? "필터를 조정하거나 초기화해 보세요"
                : "상품을 등록하면 소싱 원가와 마진이 자동으로 계산됩니다"
            }
            emptyAction={
              hasActiveFilters ? (
                <Button variant="outline" size="sm" onClick={reset}>필터 초기화</Button>
              ) : undefined
            }
            className="h-full"
          />
        </div>
      </PageShell>

      <ProductDrawer
        productId={drawerId}
        open={!!drawerId}
        onOpenChange={(v) => !v && setDrawerId(null)}
        onOpenOptions={(id) => {
          const p = rows.find((r) => r.id === id);
          if (p) { setDrawerId(null); setOptionTarget(p); }
        }}
      />

      <OptionModal
        productId={optionTarget?.id ?? null}
        productName={optionTarget?.name ?? ""}
        open={!!optionTarget}
        onOpenChange={(v) => !v && setOptionTarget(null)}
      />
    </>
  );
}
