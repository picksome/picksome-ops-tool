"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { toast } from "sonner";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { PageShell } from "@/components/shell/page-shell";
import { FilterBar, SearchInput, SelectFilter } from "@/components/filters/filter-bar";
import { DateRangeFilter } from "@/components/filters/date-range-filter";
import { DataTable } from "@/components/table/data-table";
import { TablePagination } from "@/components/table/table-pagination";
import {
  ColumnSettings, ExportButton, SavedViews, downloadCsv, toCsv, useColumnPreferences,
  type ColumnSpec,
} from "@/components/table/table-toolbar";
import { CalcBreakdown, CalcHelpButton, CalcSummary } from "@/components/calc/calc-breakdown";
import { useListQuery } from "@/hooks/useListQuery";
import { useSaleChannels } from "@/hooks/useSaleChannels";
import { productAdapter, salesAdapter } from "@/lib/adapters";
import { useFeePolicies, useTaxRules } from "@/hooks/usePolicies";
import type { SaleRow } from "@/lib/adapters/types";
import { calculateMargin, resolveFeePolicy, resolveTaxRule } from "@/lib/calc/engine";
import { formatDate, formatKRW, formatPercent } from "@/lib/format";

/**
 * 판매처 라벨은 **실제 채널 목록**에서 온다 (`useSaleChannels`).
 * mock 상수를 쓰면 조직이 추가한 커스텀 채널이 코드로 보인다.
 */

const ORDER_STATUS = [
  { value: "DELIVERED", label: "배송완료" },
  { value: "SHIPPING", label: "배송중" },
  { value: "RETURNED", label: "반품" },
  { value: "CANCELLED", label: "취소" },
];
const STATUS_LABEL: Record<string, string> = Object.fromEntries(
  ORDER_STATUS.map((s) => [s.value, s.label]),
);

const COLUMN_SPECS: ColumnSpec[] = [
  { id: "saleDate", label: "판매일" },
  { id: "marketplace", label: "판매처" },
  { id: "orderNo", label: "주문번호" },
  { id: "productName", label: "상품명", locked: true },
  { id: "sku", label: "SKU" },
  { id: "marketplaceProductNo", label: "판매처 상품번호" },
  { id: "buyerRef", label: "구매자" },
  { id: "quantity", label: "수량" },
  { id: "salePrice", label: "판매금액" },
  { id: "netProfit", label: "순이익" },
  { id: "marginRate", label: "마진율" },
  { id: "status", label: "주문 상태" },
  { id: "ownerName", label: "담당자" },
];
const DEFAULT_HIDDEN = ["marketplaceProductNo", "buyerRef"];

export default function SalesPage() {
  const { channels, labelOf } = useSaleChannels();
  const { state, set, reset, hasActiveFilters, queryString } = useListQuery({
    defaults: { pageSize: 50, sort: "saleDate", dir: "desc" },
  });
  const [detail, setDetail] = React.useState<SaleRow | null>(null);
  const { visibility, setVisibility, order, setOrder, density, setDensity } =
    useColumnPreferences("sales", COLUMN_SPECS, DEFAULT_HIDDEN);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["sales", state],
    queryFn: () =>
      salesAdapter.list({
        q: state.q || undefined,
        from: state.from || undefined,
        to: state.to || undefined,
        marketplace: state.channel || undefined,
        status: state.status || undefined,
        page: state.page,
        pageSize: state.pageSize,
        sort: state.sort,
        dir: state.dir,
      }),
  });

  const rows = data?.items ?? [];

  const columns = React.useMemo<ColumnDef<SaleRow>[]>(
    () => [
      {
        id: "saleDate", accessorKey: "saleDate", header: "판매일", size: 92, meta: { align: "center" },
        cell: ({ getValue }) => <span className="tabular-nums">{formatDate(getValue<string>())}</span>,
      },
      {
        id: "marketplace", accessorKey: "marketplace", header: "판매처", size: 96,
        cell: ({ getValue }) => (
          <Badge variant="default" className="px-1.5 py-0 text-[10px]">
            {labelOf(getValue<string>())}
          </Badge>
        ),
      },
      {
        id: "orderNo", accessorKey: "orderNo", header: "주문번호", size: 120,
        cell: ({ getValue }) => (
          <span className="font-mono text-[11px] text-[--color-muted-foreground]">{getValue<string>()}</span>
        ),
      },
      {
        id: "productName", accessorKey: "productName", header: "상품명", size: 240,
        cell: ({ row }) => (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setDetail(row.original); }}
            className="ops-focusable block max-w-[240px] truncate text-left font-medium hover:text-[--color-accent] hover:underline"
            title={row.original.productName}
          >
            {row.original.productName}
          </button>
        ),
      },
      {
        id: "sku", accessorKey: "sku", header: "SKU", size: 90,
        cell: ({ getValue }) => (
          <span className="font-mono text-[11px] text-[--color-muted-foreground]">{getValue<string>() ?? "-"}</span>
        ),
      },
      {
        id: "marketplaceProductNo", accessorKey: "marketplaceProductNo", header: "판매처 상품번호", size: 120,
        cell: ({ getValue }) => <span className="font-mono text-[11px]">{getValue<string>() ?? "-"}</span>,
      },
      {
        id: "buyerRef", accessorKey: "buyerRef", header: "구매자", size: 90,
        cell: ({ getValue }) => <span className="text-[11px]">{getValue<string>() ?? "-"}</span>,
      },
      { id: "quantity", accessorKey: "quantity", header: "수량", size: 56, meta: { align: "right" } },
      {
        id: "salePrice", accessorKey: "salePrice", header: "판매금액", size: 104, meta: { align: "right" },
        cell: ({ getValue }) => formatKRW(getValue<number>()),
      },
      {
        id: "netProfit", accessorKey: "netProfit", header: "순이익", size: 100, meta: { align: "right" },
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
        id: "marginRate", accessorKey: "marginRate", header: "마진율", size: 78, meta: { align: "right" },
        cell: ({ getValue }) => {
          const v = getValue<number | null>();
          if (v === null) return <span className="text-[--color-muted-foreground]">-</span>;
          return (
            <span className={cn("tabular-nums", v < 0 ? "text-[--color-loss]" : v < 0.1 ? "text-[--color-warning]" : "")}>
              {formatPercent(v)}
            </span>
          );
        },
      },
      {
        // 주문 상태는 DB 에 컬럼이 없다. 값을 지어내는 대신 미지원임을 표시한다 —
        // 예전에는 모든 주문이 "배송완료" 로 보였다(반품된 것까지).
        id: "status", accessorKey: "status", header: "상태", size: 84, meta: { align: "center" },
        cell: ({ getValue }) => {
          const s = getValue<string | null>();
          if (!s) {
            return <span className="text-[10px] text-[--color-muted-foreground]">미지원</span>;
          }
          return (
            <Badge
              variant={s === "RETURNED" || s === "CANCELLED" ? "danger" : s === "SHIPPING" ? "info" : "success"}
              className="px-1.5 py-0 text-[10px]"
            >
              {STATUS_LABEL[s] ?? s}
            </Badge>
          );
        },
      },
      {
        id: "ownerName", accessorKey: "ownerName", header: "담당자", size: 72,
        cell: ({ getValue }) => <span className="text-[11px]">{getValue<string>() ?? "-"}</span>,
      },
    ],
    [labelOf],
  );

  function handleExport() {
    if (rows.length === 0) { toast.error("내보낼 데이터가 없습니다"); return; }
    downloadCsv(
      `판매장부_${state.from || "전체"}_${state.to || ""}`,
      toCsv(rows, [
        { header: "판매일", value: (r) => r.saleDate },
        { header: "판매처", value: (r) => labelOf(r.marketplace) },
        { header: "주문번호", value: (r) => r.orderNo },
        { header: "상품명", value: (r) => r.productName },
        { header: "SKU", value: (r) => r.sku },
        { header: "수량", value: (r) => r.quantity },
        { header: "판매금액", value: (r) => r.salePrice },
        { header: "순이익", value: (r) => r.netProfit },
        { header: "마진율", value: (r) => (r.marginRate === null ? "" : (r.marginRate * 100).toFixed(2) + "%") },
        { header: "주문상태", value: (r) => (r.status ? STATUS_LABEL[r.status] ?? r.status : "") },
        { header: "담당자", value: (r) => r.ownerName },
      ]),
    );
    toast.success(`${rows.length}건을 내보냈습니다`);
  }

  return (
    <>
      <PageShell
        title="판매장부"
        description="주문 단위 판매 기록과 건별 마진 계산 근거"
        actions={<CalcHelpButton label="계산식" />}
        filters={
          <FilterBar
            onReset={reset}
            hasActiveFilters={hasActiveFilters}
            resultCount={data?.total}
            actions={
              <>
                <SavedViews
                  scope="sales" currentQuery={queryString}
                  onApply={(q) => {
                    window.history.replaceState(null, "", q ? `?${q}` : location.pathname);
                    location.reload();
                  }}
                />
                <ColumnSettings
                  specs={COLUMN_SPECS} visibility={visibility} onVisibilityChange={setVisibility}
                  order={order} onOrderChange={setOrder} density={density} onDensityChange={setDensity}
                />
                <ExportButton onExport={handleExport} disabled={rows.length === 0} />
              </>
            }
          >
            <SearchInput
              className="w-64" value={state.q} onChange={(v) => set({ q: v })}
              placeholder="주문번호·상품명·SKU·상품번호·구매자·메모"
              ariaLabel="판매 통합 검색"
            />
            <DateRangeFilter
              value={{ from: state.from, to: state.to }}
              onChange={(r) => set({ from: r.from, to: r.to })}
            />
            <SelectFilter
              value={state.channel} onChange={(v) => set({ channel: v })}
              placeholder="판매처 전체" label="판매처"
              options={channels.map((c) => ({ value: c.code, label: c.display_name }))}
            />
          </FilterBar>
        }
        footer={
          <TablePagination
            page={state.page} pageSize={state.pageSize} total={data?.total ?? 0}
            onPageChange={(p) => set({ page: p })}
            onPageSizeChange={(s) => set({ pageSize: s, page: 1 })}
          />
        }
      >
        <div className="min-h-0 flex-1">
          <DataTable
            columns={columns} data={rows} isLoading={isLoading} isError={isError}
            onRetry={() => refetch()} getRowId={(r) => r.id} density={density}
            columnVisibility={visibility}
            onColumnVisibilityChange={(u) => setVisibility(typeof u === "function" ? u(visibility) : u)}
            columnOrder={order}
            onColumnOrderChange={(u) => setOrder(typeof u === "function" ? u(order) : u)}
            onRowClick={(r) => setDetail(r)}
            rowClassName={(r) => ((r.netProfit ?? 0) < 0 ? "bg-[--color-danger-bg]/40" : undefined)}
            emptyText={hasActiveFilters ? "조건에 맞는 판매가 없습니다" : "판매 기록이 없습니다"}
            emptyDescription={
              hasActiveFilters ? "기간이나 검색어를 조정해 보세요" : "쇼핑몰 API를 연결하거나 엑셀로 가져올 수 있습니다"
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

      <SaleDetailDrawer sale={detail} onClose={() => setDetail(null)} />
    </>
  );
}

/**
 * 판매 건 상세 — 계산 근거.
 * 판매금액부터 수수료·부가세·배송비·원가·광고비·반품충당금·세금을 거쳐 순이익까지
 * 단계별로 보여준다. 이게 없으면 "왜 이만큼 남았는지"를 확인할 방법이 없다.
 */
function SaleDetailDrawer({ sale, onClose }: { sale: SaleRow | null; onClose: () => void }) {
  const { labelOf } = useSaleChannels();
  // 실제 정책 API 를 읽는다. 예전에는 `settingsAdapter`(메모리 배열)였고,
  // 여기 뜨던 수수료·부가세는 전부 지어낸 값이었다.
  const { taxRules } = useTaxRules();
  const { feePolicies } = useFeePolicies();
  const { data: product } = useQuery({
    queryKey: ["product", sale?.productId],
    queryFn: () => productAdapter.detail(sale!.productId!),
    enabled: !!sale?.productId,
  });

  const result = React.useMemo(() => {
    if (!sale || !taxRules?.length) return null;
    // 판매 시점 기준으로 정책을 고른다 — 지금 요율이 아니라 그때 요율로 계산해야 재현된다
    const at = sale.saleDate;
    const taxRule = resolveTaxRule(taxRules, at) ?? taxRules[0];
    const feePolicy = resolveFeePolicy(feePolicies ?? [], sale.marketplace, product?.category ?? null, at);
    const s = product?.sourcing;
    const p = product?.pricing;
    return calculateMargin({
      cost: {
        purchasePrice: s?.purchasePrice ?? 0,
        purchaseCurrency: s?.purchaseCurrency ?? "KRW",
        exchangeRate: s?.exchangeRate ?? 1,
        domesticShippingAbroad: s?.domesticShippingAbroad ?? 0,
        internationalShipping: s?.internationalShipping ?? 0,
        customsDuty: s?.customsDuty ?? 0,
        importVat: s?.importVat ?? 0,
        domesticShipping: s?.domesticShipping ?? 0,
        packagingCost: s?.packagingCost ?? 0,
        otherCost: s?.otherCost ?? 0,
      },
      selling: {
        salePrice: sale.salePrice,
        buyerShippingFee: 0,
        marketplace: sale.marketplace,
        category: product?.category ?? null,
        commissionRate: p?.commissionRate ?? 0.11,
        commissionVatSeparate: true,
        paymentFeeRate: 0,
        sellerCouponAmount: 0,
        sellerCouponRate: 0,
        otherMarketplaceDeduction: 0,
        adCostAmount: 0,
        adCostRate: p?.adCostRate ?? 0,
        returnRate: p?.returnRate ?? 0,
        returnCostPerCase: 3000,
        fixedCostAllocation: p?.fixedCostAllocation ?? 0,
        quantity: sale.quantity,
      },
      target: { targetProfit: 0, targetMarginRate: p?.targetMarginRate ?? 0.2 },
      taxRule,
      feePolicy,
    });
  }, [sale, taxRules, feePolicies, product]);

  return (
    <Sheet open={!!sale} onOpenChange={(v) => !v && onClose()}>
      <SheetContent side="right" showCloseButton={false} className="[--sheet-w:600px] flex w-full flex-col gap-0 p-0">
        <SheetHeader className="shrink-0 border-b border-[--color-border] px-4 py-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <SheetTitle className="truncate text-[15px]">{sale?.productName}</SheetTitle>
              <SheetDescription className="mt-0.5 text-[11px]">
                {sale && (
                  <>
                    {formatDate(sale.saleDate)} · {labelOf(sale.marketplace)} ·{" "}
                    <span className="font-mono">{sale.orderNo}</span> · {sale.quantity}개
                    {" · "}
                    {/*
                      판매자 계정 (M032). 같은 판매처에 스토어가 여럿이면
                      "쿠팡" 만으로는 어느 스토어 매출인지 알 수 없다.

                      계정이 없으면 **채널 이름으로 대신 채우지 않는다** —
                      모른다는 사실을 그대로 쓴다.
                    */}
                    {sale.integrationAccountLabel ? (
                      <span>{sale.integrationAccountLabel}</span>
                    ) : (
                      <span className="text-[--color-muted-foreground]">연동계정 미확인</span>
                    )}
                  </>
                )}
              </SheetDescription>
            </div>
            <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="닫기">
              <X className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        </SheetHeader>

        <div className="flex-1 space-y-3 overflow-y-auto p-4">
          {!result ? (
            <p className="py-8 text-center text-xs text-[--color-muted-foreground]">
              계산 근거를 준비하고 있습니다…
            </p>
          ) : (
            <>
              <CalcSummary result={result} />
              <div className="flex items-center justify-between">
                <p className="text-xs font-semibold">계산 근거</p>
                <CalcHelpButton label="계산식 전체" />
              </div>
              <CalcBreakdown result={result} />
              {!product && (
                <p className="rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
                  이 판매 건에 연결된 상품 정보를 찾지 못해 원가를 0으로 계산했습니다.
                  상품 관리에서 상품을 연결하면 정확한 마진이 나옵니다.
                </p>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
