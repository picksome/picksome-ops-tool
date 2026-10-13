"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { toast } from "sonner";
import { Lock, Plus, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs } from "@/components/shell/page-shell";
import { FilterBar, SearchInput, SelectFilter } from "@/components/filters/filter-bar";
import { DateRangeFilter } from "@/components/filters/date-range-filter";
import { DataTable } from "@/components/table/data-table";
import { TablePagination } from "@/components/table/table-pagination";
import { ExportButton, downloadCsv, toCsv } from "@/components/table/table-toolbar";
import { ExpenseDrawer } from "@/components/expense/expense-drawer";
import { useListQuery } from "@/hooks/useListQuery";
import { useExpenseRefs } from "@/hooks/useExpenseRefs";
import { approvalsApi } from "@/lib/api/approvals";
import { expensesApi, type EvidenceStatus, type ExpenseOut } from "@/lib/api/expenses";
import { EXPENSE_VALUE_LABELS, optionsOf } from "@/lib/api/expenses-approval";
import { formatDate, formatKRW } from "@/lib/format";

/**
 * 사업비용 목록.
 *
 * **실제 백엔드를 본다.** 이 화면은 오랫동안 mock 어댑터 위에 있었고, 그동안
 * 화면에서 금액을 고치면 저장된 것처럼 보였지만 DB 는 그대로였다. 비용은
 * 순이익의 입력값이라 그 괴리가 곧 잘못된 수익배분이 된다.
 *
 * 편집은 드로어가 담당한다 — 금액·귀속일 변경은 승인을 거치고, 잠긴 달은
 * 조정으로 유도한다.
 */

const EVIDENCE_TONE: Record<EvidenceStatus, "default" | "info" | "success" | "warning"> = {
  NONE: "warning",
  NEEDS_CHECK: "warning",
  PRESENT: "success",
  SUBMITTED_TO_ACCOUNTANT: "info",
};

export default function ExpensesPage() {
  const { state, set, reset, hasActiveFilters } = useListQuery({
    defaults: { tab: "all", pageSize: 50 },
  });
  const tab = state.tab || "all";

  /**
   * 드로어 대상은 URL 이 소유한다 (`?expenseId=<id>`, 신규는 `?expenseId=new`).
   *
   * 상태로만 들고 있으면 승인 화면에서 "대상 비용 보기" 로 넘어올 방법이 없고,
   * 새로고침하면 드로어가 닫히며, 링크를 공유할 수도 없다.
   */
  const [drawer, setDrawerState] = React.useState<{ open: boolean; id: string | null }>({
    open: false, id: null,
  });

  const openDrawer = React.useCallback((id: string | null) => {
    setDrawerState({ open: true, id });
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.set("expenseId", id ?? "new");
    // replaceState — 드로어 여닫기가 뒤로가기 기록을 채우면 목록으로 돌아가려고
    // 뒤로가기를 여러 번 눌러야 한다.
    window.history.replaceState(null, "", url.toString());
  }, []);

  const closeDrawer = React.useCallback(() => {
    setDrawerState((p) => ({ ...p, open: false }));
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("expenseId");
    window.history.replaceState(null, "", url.toString());
  }, []);

  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const id = new URLSearchParams(window.location.search).get("expenseId");
    if (id) setDrawerState({ open: true, id: id === "new" ? null : id });
  }, []);

  const { activePartners, categories } = useExpenseRefs();

  const params = React.useMemo(() => ({
    from: state.from || undefined,
    to: state.to || undefined,
    category_id: state.extra.categoryId || undefined,
    q: state.q || undefined,
    payment_method: state.extra.paymentMethod || undefined,
    payment_card_holder_id: state.owner || undefined,
    // "증빙 없음" 탭은 별도 화면이 아니라 같은 목록의 서버 필터다.
    evidence_status: (tab === "evidence" ? "NONE" : state.status || "") as EvidenceStatus | "",
    page: state.page,
    page_size: state.pageSize,
  }), [state, tab]);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["expenses", params],
    queryFn: () => expensesApi.list(params),
  });

  /**
   * 목록에 승인 대기 표시를 붙인다.
   *
   * 행마다 조회하면 한 페이지에 N+1 이 난다. 대기 중인 요청은 보통 수십 건이라
   * 한 번에 받아 화면에서 붙인다 — 이게 없으면 사용자는 드로어를 열어야만
   * "이 비용은 승인 대기 중" 을 알 수 있다.
   */
  const { data: pendingApprovals } = useQuery({
    queryKey: ["approvals", "expense", "pending"],
    queryFn: () => approvalsApi.list({
      target_type: "expense", status: "PENDING", page_size: 200,
    }),
    staleTime: 30_000,
  });
  const pendingByExpense = React.useMemo(() => {
    const m = new Map<string, number>();
    for (const a of pendingApprovals?.items ?? []) {
      m.set(a.target_id, (m.get(a.target_id) ?? 0) + 1);
    }
    return m;
  }, [pendingApprovals]);

  const rows = data?.items ?? [];

  const columns = React.useMemo<ColumnDef<ExpenseOut>[]>(() => [
    {
      accessorKey: "expense_date", header: "발생일", size: 92, meta: { align: "center" },
      cell: ({ getValue }) => (
        <span className="tabular-nums">{formatDate(getValue<string>())}</span>
      ),
    },
    {
      accessorKey: "category_name", header: "카테고리", size: 110,
      cell: ({ getValue }) => getValue<string | null>() ?? "-",
    },
    {
      accessorKey: "description", header: "내용", size: 240,
      cell: ({ row }) => (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); openDrawer(row.original.id); }}
          className="ops-focusable block max-w-[240px] truncate text-left font-medium hover:text-[--color-accent] hover:underline"
        >
          {row.original.description}
        </button>
      ),
    },
    {
      accessorKey: "gross_amount", header: "금액", size: 104, meta: { align: "right" },
      cell: ({ getValue }) => formatKRW(getValue<number>()),
    },
    {
      accessorKey: "vat_amount", header: "부가세", size: 90, meta: { align: "right" },
      cell: ({ getValue }) => (
        <span className="text-[--color-muted-foreground]">{formatKRW(getValue<number>())}</span>
      ),
    },
    {
      accessorKey: "card_holder_name", header: "결제자", size: 92,
      cell: ({ row }) => (
        <span className="flex items-center gap-1 text-[11px]">
          {row.original.card_holder_name ?? "-"}
          {row.original.is_personal_card && (
            // 개인카드는 나중에 돌려줘야 하는 돈이다 — 목록에서 바로 보여야 한다
            <Badge variant="warning" className="px-1 py-0 text-[9px]">개인</Badge>
          )}
        </span>
      ),
    },
    {
      accessorKey: "payment_method", header: "결제수단", size: 88,
      cell: ({ getValue }) => (
        <span className="text-[11px]">
          {EXPENSE_VALUE_LABELS.payment_method[getValue<string>()] ?? getValue<string>()}
        </span>
      ),
    },
    {
      accessorKey: "evidence_status", header: "증빙", size: 92, meta: { align: "center" },
      cell: ({ getValue }) => {
        const s = getValue<EvidenceStatus>();
        return (
          <Badge variant={EVIDENCE_TONE[s] ?? "default"} className="px-1.5 py-0 text-[10px]">
            {EXPENSE_VALUE_LABELS.evidence_status[s] ?? s}
          </Badge>
        );
      },
    },
    {
      id: "flags", header: "상태", size: 96, meta: { align: "center" }, enableSorting: false,
      cell: ({ row }) => {
        const pending = pendingByExpense.get(row.original.id) ?? 0;
        return (
          <span className="flex items-center justify-center gap-1">
            {pending > 0 && (
              <Badge variant="info" className="gap-0.5 px-1 py-0 text-[10px]">
                <ShieldCheck className="h-2.5 w-2.5" aria-hidden />
                승인 {pending}
              </Badge>
            )}
            {row.original.linked_settlement_id && (
              <Badge variant="default" className="gap-0.5 px-1 py-0 text-[10px]" title="확정 정산에 반영됨">
                <Lock className="h-2.5 w-2.5" aria-hidden />
                정산
              </Badge>
            )}
            {!row.original.is_settlement_deductible && (
              <Badge variant="default" className="px-1 py-0 text-[10px]" title="순이익에서 차감하지 않음">
                미차감
              </Badge>
            )}
          </span>
        );
      },
    },
  ], [pendingByExpense, openDrawer]);

  function handleExport() {
    if (rows.length === 0) { toast.error("내보낼 데이터가 없습니다"); return; }
    downloadCsv(
      `사업비용_${state.from || "전체"}`,
      toCsv(rows, [
        { header: "발생일", value: (r) => r.expense_date },
        { header: "카테고리", value: (r) => r.category_name },
        { header: "내용", value: (r) => r.description },
        { header: "합계금액", value: (r) => r.gross_amount },
        { header: "공급가액", value: (r) => r.supply_amount },
        { header: "부가세", value: (r) => r.vat_amount },
        { header: "과세유형", value: (r) => EXPENSE_VALUE_LABELS.tax_type[r.tax_type] ?? r.tax_type },
        { header: "결제수단", value: (r) => EXPENSE_VALUE_LABELS.payment_method[r.payment_method] ?? r.payment_method },
        { header: "결제자", value: (r) => r.card_holder_name },
        { header: "개인카드", value: (r) => (r.is_personal_card ? "Y" : "N") },
        { header: "귀속 파트너", value: (r) => r.attributable_partner_name },
        { header: "거래처", value: (r) => r.supplier_name },
        { header: "증빙유형", value: (r) => EXPENSE_VALUE_LABELS.evidence_type[r.evidence_type] ?? r.evidence_type },
        { header: "증빙상태", value: (r) => EXPENSE_VALUE_LABELS.evidence_status[r.evidence_status] ?? r.evidence_status },
        { header: "세무공제", value: (r) => (r.is_tax_deductible ? "Y" : "N") },
        { header: "정산차감", value: (r) => (r.is_settlement_deductible ? "Y" : "N") },
        { header: "메모", value: (r) => r.memo },
      ]),
    );
    toast.success(`${rows.length}건을 내보냈습니다`);
  }

  const total = data?.total ?? 0;
  const sum = rows.reduce((acc, r) => acc + r.gross_amount, 0);

  return (
    <>
      <PageShell
        title="사업비용"
        description="증빙과 결제자를 함께 관리해 정산·세무 자료로 바로 씁니다. 금액 변경은 승인을 거칩니다."
        actions={
          <Button size="sm" onClick={() => openDrawer(null)}>
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            비용 등록
          </Button>
        }
        tabs={
          <PageTabs
            tabs={[
              { key: "all", label: "전체", count: tab === "all" ? total : undefined },
              { key: "evidence", label: "증빙 없음", count: tab === "evidence" ? total : undefined },
            ]}
            value={tab}
            onChange={(k) => set({ tab: k, page: 1 })}
          />
        }
        filters={
          <FilterBar
            onReset={reset} hasActiveFilters={hasActiveFilters} resultCount={total}
            actions={<ExportButton onExport={handleExport} disabled={rows.length === 0} />}
          >
            <SearchInput
              className="w-56" value={state.q} onChange={(v) => set({ q: v })}
              placeholder="내용·메모" ariaLabel="비용 검색"
            />
            <DateRangeFilter
              label="발생일"
              value={{ from: state.from, to: state.to }}
              onChange={(r) => set({ from: r.from, to: r.to })}
            />
            <SelectFilter
              value={state.extra.categoryId ?? ""} onChange={(v) => set({ extra: { categoryId: v } })}
              placeholder="카테고리 전체" label="비용 카테고리"
              options={categories.map((c) => ({ value: c.id, label: c.name }))}
            />
            <SelectFilter
              value={state.owner} onChange={(v) => set({ owner: v })}
              placeholder="결제자 전체" label="결제자"
              options={activePartners.map((p) => ({ value: p.id, label: p.name }))}
            />
            <SelectFilter
              value={state.extra.paymentMethod ?? ""}
              onChange={(v) => set({ extra: { paymentMethod: v } })}
              placeholder="결제수단 전체" label="결제수단" options={optionsOf("payment_method")}
            />
            {tab === "all" && (
              <SelectFilter
                value={state.status} onChange={(v) => set({ status: v })}
                placeholder="증빙 상태 전체" label="증빙 상태"
                options={optionsOf("evidence_status")}
              />
            )}
          </FilterBar>
        }
        footer={
          <div className="flex items-center justify-between gap-3">
            <p className="shrink-0 pl-1 text-[11px] text-[--color-muted-foreground]">
              이 페이지 합계 <span className="tabular-nums font-medium">{formatKRW(sum)}</span>
            </p>
            <TablePagination
              page={state.page} pageSize={state.pageSize} total={total}
              onPageChange={(p) => set({ page: p })}
              onPageSizeChange={(s) => set({ pageSize: s, page: 1 })}
            />
          </div>
        }
      >
        <div className="min-h-0 flex-1">
          <DataTable
            columns={columns} data={rows} isLoading={isLoading} isError={isError}
            onRetry={() => refetch()} getRowId={(r) => r.id}
            onRowClick={(r) => openDrawer(r.id)}
            emptyText={tab === "evidence" ? "증빙이 빠진 비용이 없습니다" : "등록된 비용이 없습니다"}
            emptyDescription={
              tab === "evidence"
                ? "모든 비용에 증빙 상태가 기록되어 있습니다"
                : "카드 명세서나 세금계산서를 보고 등록하세요"
            }
            className="h-full"
          />
        </div>
      </PageShell>

      <ExpenseDrawer
        expenseId={drawer.id}
        open={drawer.open}
        onOpenChange={(v) => { if (!v) closeDrawer(); }}
      />
    </>
  );
}
