"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { toast } from "sonner";
import { PlayCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs } from "@/components/shell/page-shell";
import { FilterBar, SearchInput, SelectFilter } from "@/components/filters/filter-bar";
import { DateRangeFilter } from "@/components/filters/date-range-filter";
import { DataTable } from "@/components/table/data-table";
import { TablePagination } from "@/components/table/table-pagination";
import { useListQuery } from "@/hooks/useListQuery";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { usersApi } from "@/lib/api/admin";
import {
  ApprovalDrawer, ApprovalStatusBadge, STATUS_LABEL,
} from "@/components/approvals/approval-drawer";
import {
  approvalsApi, describeApprovalError, REQUEST_TYPE_LABELS, TARGET_TYPE_LABELS,
  type ApprovalRequest, type ApprovalStatus,
} from "@/lib/api/approvals";
import { formatDate, formatDateTime } from "@/lib/format";

/**
 * 승인 대기함.
 *
 * 사이드바 위치: **알림·연동 그룹 바로 앞**. 승인은 "내가 처리해야 할 일" 이라
 * 알림과 같은 성격이고, 건수가 적을 때 독립 최상위 메뉴로 두면 과하다.
 * 헤더 배지·대시보드 경고에서 이 화면으로 딥링크한다.
 */

/**
 * 라벨은 `lib/api/approvals` 가 소유한다.
 *
 * 이 화면이 자체 표를 들고 있던 동안 비용·판매처가격 승인은 `EXPENSE_FINANCIAL_CHANGE`
 * 같은 날것으로 떴다. 승인함은 대상을 가리지 않고 한 목록에 섞기 때문에,
 * 라벨은 **대상별 파일이 등록하고 여기서는 조회만** 한다.
 */

/** 탭 → 목록 조회 파라미터. 서버 필터를 그대로 쓴다. */
const TABS = [
  { key: "pending", label: "승인 필요" },
  { key: "mine", label: "내가 요청한 항목" },
  { key: "scheduled", label: "적용 예정" },
  { key: "done", label: "완료" },
  { key: "all", label: "전체" },
];

export default function ApprovalsPage() {
  const qc = useQueryClient();
  const { state, set, reset, hasActiveFilters } = useListQuery({
    defaults: { tab: "pending", pageSize: 50 },
  });
  const tab = state.tab || "pending";
  const [drawerId, setDrawerId] = React.useState<string | null>(null);

  const { user: me } = useCurrentUser();
  // 실제 조직 구성원. 예전에는 mock 목록이라 **실존하지 않는 이름**이 표시됐다.
  const { data: members } = useQuery({
    queryKey: ["user-directory"],
    queryFn: () => usersApi.directory(),
    staleTime: 5 * 60 * 1000,
  });

  const canReview = me?.role === "ADMIN" || me?.role === "OPERATOR";
  const isAdmin = me?.role === "ADMIN";

  const params = React.useMemo(() => {
    const base = {
      q: state.q || undefined,
      from: state.from || undefined,
      to: state.to || undefined,
      target_type: state.extra.targetType || undefined,
      request_type: state.extra.requestType || undefined,
      requested_by: state.owner || undefined,
      page: state.page,
      page_size: state.pageSize,
    };
    if (tab === "pending") return { ...base, status: "PENDING" as ApprovalStatus, pending_for_me: true };
    if (tab === "mine") return { ...base, mine: true };
    if (tab === "scheduled") return { ...base, status: "APPROVED" as ApprovalStatus };
    if (tab === "done") return { ...base, status: "APPLIED" as ApprovalStatus };
    return { ...base, status: (state.status || undefined) as ApprovalStatus | undefined };
  }, [tab, state]);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["approvals", params],
    queryFn: () => approvalsApi.list(params),
  });

  const runDue = useMutation({
    mutationFn: () => approvalsApi.runDue(),
    onSuccess: (r) => {
      const parts = [
        r.applied ? `적용 ${r.applied}건` : null,
        r.held_by_lock ? `잠금 보류 ${r.held_by_lock}건` : null,
        r.validation_failed || r.other_failed
          ? `실패 ${r.validation_failed + r.other_failed}건` : null,
      ].filter(Boolean);
      toast.success(
        parts.length ? `자동 적용 완료 — ${parts.join(", ")}` : "적용할 요청이 없습니다",
        { description: `대상 ${r.scanned}건 검사` },
      );
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: (err) => {
      const g = describeApprovalError(err);
      toast.error(g.title, { description: g.description });
    },
  });

  const rows = data?.items ?? [];
  const memberName = React.useCallback(
    // 못 찾은 UUID 를 다른 이름으로 채우지 않는다.
    (id: string | null) => (id ? members?.find((m) => m.id === id)?.display_name ?? "-" : "-"),
    [members],
  );

  const columns = React.useMemo<ColumnDef<ApprovalRequest>[]>(
    () => [
      {
        accessorKey: "business_key", header: "요청번호", size: 100,
        cell: ({ row }) => (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setDrawerId(row.original.id); }}
            className="ops-focusable font-mono text-[11px] text-[--color-accent] hover:underline"
          >
            {row.original.business_key}
          </button>
        ),
      },
      {
        accessorKey: "target_type", header: "대상", size: 70,
        cell: ({ getValue }) => (
          <Badge variant="default" className="px-1.5 py-0 text-[10px]">
            {TARGET_TYPE_LABELS[getValue<string>()] ?? getValue<string>()}
          </Badge>
        ),
      },
      {
        accessorKey: "request_type", header: "변경 유형", size: 120,
        cell: ({ getValue }) => (
          <span className="text-[12px]">
            {REQUEST_TYPE_LABELS[getValue<string>()] ?? getValue<string>()}
          </span>
        ),
      },
      {
        accessorKey: "requested_by", header: "요청자", size: 84,
        cell: ({ getValue }) => <span className="text-[11px]">{memberName(getValue<string>())}</span>,
      },
      {
        accessorKey: "requested_at", header: "요청일", size: 100, meta: { align: "center" },
        cell: ({ getValue }) => (
          <span className="text-[11px] tabular-nums">{formatDate(getValue<string>())}</span>
        ),
      },
      {
        accessorKey: "effective_from", header: "적용 예정일", size: 100, meta: { align: "center" },
        cell: ({ getValue }) => {
          const v = getValue<string | null>();
          return (
            <span className="text-[11px] tabular-nums">
              {v ? formatDate(v) : <span className="text-[--color-muted-foreground]">승인 즉시</span>}
            </span>
          );
        },
      },
      {
        accessorKey: "status", header: "상태", size: 88, meta: { align: "center" },
        cell: ({ getValue }) => <ApprovalStatusBadge status={getValue<ApprovalStatus>()} />,
      },
      {
        accessorKey: "reviewed_by", header: "승인자", size: 84,
        cell: ({ getValue }) => <span className="text-[11px]">{memberName(getValue<string | null>())}</span>,
      },
      {
        id: "applyState", header: "적용", size: 96, meta: { align: "center" }, enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          if (r.status === "APPLIED") {
            return (
              <span className="text-[11px] text-[--color-profit]">
                완료{r.applied_by_type === "SYSTEM" ? " (자동)" : ""}
              </span>
            );
          }
          if (r.auto_apply_status === "HELD_BY_LOCK") {
            return <Badge variant="warning" className="px-1 py-0 text-[10px]">잠금 보류</Badge>;
          }
          if (r.auto_apply_status === "PERMANENT_FAILURE") {
            return <Badge variant="danger" className="px-1 py-0 text-[10px]">실패</Badge>;
          }
          if (r.auto_apply_status === "RETRYING") {
            return <Badge variant="info" className="px-1 py-0 text-[10px]">재시도 예정</Badge>;
          }
          if (r.status === "APPROVED" && r.effective_from) {
            return <span className="text-[11px] text-[--color-muted-foreground]">예정</span>;
          }
          return <span className="text-[11px] text-[--color-muted-foreground]">-</span>;
        },
      },
      {
        accessorKey: "updated_at", header: "최근 변경", size: 116, meta: { align: "center" },
        cell: ({ getValue }) => (
          <span className="text-[10px] tabular-nums text-[--color-muted-foreground]">
            {formatDateTime(getValue<string>())}
          </span>
        ),
      },
    ],
    [memberName],
  );

  return (
    <>
      <PageShell
        title="승인"
        description="중요한 변경은 승인 후 적용됩니다. 적용 예정일이 지정된 건은 그날 자동으로 반영됩니다."
        actions={
          isAdmin ? (
            <Button
              variant="outline" size="sm"
              onClick={() => runDue.mutate()} disabled={runDue.isPending}
              title="적용일이 도래한 승인 요청을 지금 일괄 적용합니다"
            >
              <PlayCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              적용 예정분 실행
            </Button>
          ) : undefined
        }
        tabs={<PageTabs tabs={TABS} value={tab} onChange={(k) => set({ tab: k })} />}
        filters={
          <FilterBar onReset={reset} hasActiveFilters={hasActiveFilters} resultCount={data?.total}>
            <SearchInput
              className="w-56" value={state.q} onChange={(v) => set({ q: v })}
              placeholder="요청번호·사유·변경 유형" ariaLabel="승인 요청 검색"
            />
            <DateRangeFilter
              label="요청일"
              value={{ from: state.from, to: state.to }}
              onChange={(r) => set({ from: r.from, to: r.to })}
              presets={["last7", "last30", "thisMonth", "lastMonth"]}
            />
            {tab === "all" && (
              <SelectFilter
                value={state.status} onChange={(v) => set({ status: v })}
                placeholder="상태 전체" label="상태"
                options={(Object.keys(STATUS_LABEL) as ApprovalStatus[]).map((s) => ({
                  value: s, label: STATUS_LABEL[s],
                }))}
              />
            )}
            {/* 대상 필터는 조회 파라미터로만 있고 UI 가 없었다 — 승인 대상이
                상품 하나였을 때는 티가 안 났지만, 비용·가격이 섞이면서 필요해졌다. */}
            <SelectFilter
              value={state.extra.targetType ?? ""}
              onChange={(v) => set({ extra: { targetType: v } })}
              placeholder="대상 전체" label="승인 대상"
              options={Object.entries(TARGET_TYPE_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <SelectFilter
              value={state.extra.requestType ?? ""}
              onChange={(v) => set({ extra: { requestType: v } })}
              placeholder="변경 유형 전체" label="변경 유형"
              options={Object.entries(REQUEST_TYPE_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <SelectFilter
              value={state.owner} onChange={(v) => set({ owner: v })}
              placeholder="요청자 전체" label="요청자"
              options={(members ?? []).map((m) => ({ value: m.id, label: m.display_name }))}
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
            onRetry={() => refetch()} getRowId={(r) => r.id}
            onRowClick={(r) => setDrawerId(r.id)}
            rowClassName={(r) =>
              r.auto_apply_status === "PERMANENT_FAILURE"
                ? "bg-[--color-danger-bg]/40"
                : r.auto_apply_status === "HELD_BY_LOCK"
                  ? "bg-[--color-warning-bg]/40"
                  : undefined
            }
            emptyText={
              tab === "pending" ? "승인할 요청이 없습니다"
                : tab === "mine" ? "내가 요청한 건이 없습니다"
                  : tab === "scheduled" ? "적용 예정인 승인이 없습니다"
                    : "승인 요청이 없습니다"
            }
            emptyDescription={
              tab === "pending"
                ? "새 요청이 들어오면 여기에 표시됩니다"
                : "상품 원가·판매처 가격·사업비용 금액을 바꿀 때 승인 요청이 만들어집니다"
            }
            className={cn("h-full")}
          />
        </div>
      </PageShell>

      <ApprovalDrawer
        requestId={drawerId}
        open={!!drawerId}
        onOpenChange={(v) => !v && setDrawerId(null)}
        currentUserId={me?.id}
        canReview={canReview}
      />
    </>
  );
}
