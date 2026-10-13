"use client";

import * as React from "react";
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
  type RowSelectionState,
  type VisibilityState,
  type ColumnOrderState,
  type OnChangeFn,
} from "@tanstack/react-table";
import { ChevronUp, ChevronDown, ChevronsUpDown, AlertTriangle, RotateCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";

/**
 * 목록 공통 테이블.
 *
 * 기존 props(columns/data/isLoading/selectable/onRowClick/emptyText/stickyHeader)는
 * 그대로 유지한다 — 이미 쓰고 있는 화면들이 깨지면 안 된다.
 * 개편에 필요한 것만 **선택 props** 로 추가했다:
 *   - columnVisibility / columnOrder : 표시 열 선택·열 순서 변경
 *   - sorting(외부 제어)             : URL 과 동기화된 정렬
 *   - isError / onRetry              : 오류 상태 (지금까지는 빈 목록과 구분이 안 됐다)
 *   - getRowId                       : 행 선택을 페이지 이동 후에도 유지
 *   - density                        : 정보 밀도 조절
 */

export type TableDensity = "compact" | "normal" | "comfortable";

const DENSITY_CLASS: Record<TableDensity, string> = {
  compact: "px-2.5 py-1",
  normal: "px-3 py-2",
  comfortable: "px-3 py-3",
};

export interface DataTableProps<TData> {
  columns: ColumnDef<TData>[];
  data: TData[];
  isLoading?: boolean;
  /** 행 선택 활성 */
  selectable?: boolean;
  rowSelection?: RowSelectionState;
  onRowSelectionChange?: (selection: RowSelectionState) => void;
  /** 행 클릭 */
  onRowClick?: (row: TData) => void;
  /** 빈 상태 텍스트 */
  emptyText?: string;
  /** 빈 상태 설명 + 액션 */
  emptyDescription?: string;
  emptyAction?: React.ReactNode;
  /** sticky 헤더 */
  stickyHeader?: boolean;
  className?: string;

  /* ── 개편 추가 (전부 선택) ── */
  isError?: boolean;
  errorMessage?: string;
  onRetry?: () => void;
  columnVisibility?: VisibilityState;
  onColumnVisibilityChange?: OnChangeFn<VisibilityState>;
  columnOrder?: ColumnOrderState;
  onColumnOrderChange?: OnChangeFn<ColumnOrderState>;
  /** 외부(=URL)에서 정렬을 제어할 때. 주면 내부 정렬 상태를 쓰지 않는다. */
  sorting?: SortingState;
  onSortingChange?: OnChangeFn<SortingState>;
  /** 서버 정렬을 쓸 때 true — 클라 재정렬을 끈다 */
  manualSorting?: boolean;
  getRowId?: (row: TData, index: number) => string;
  density?: TableDensity;
  /** 각 행의 강조 스타일 (적자 행 등) */
  rowClassName?: (row: TData) => string | undefined;
}

export function DataTable<TData>({
  columns,
  data,
  isLoading,
  selectable,
  rowSelection = {},
  onRowSelectionChange,
  onRowClick,
  emptyText = "데이터가 없습니다",
  emptyDescription,
  emptyAction,
  stickyHeader = true,
  className,
  isError,
  errorMessage,
  onRetry,
  columnVisibility,
  onColumnVisibilityChange,
  columnOrder,
  onColumnOrderChange,
  sorting: sortingProp,
  onSortingChange,
  manualSorting,
  getRowId,
  density = "normal",
  rowClassName,
}: DataTableProps<TData>) {
  const [internalSorting, setInternalSorting] = React.useState<SortingState>([]);
  const sorting = sortingProp ?? internalSorting;
  const cellPad = DENSITY_CLASS[density];

  const allColumns: ColumnDef<TData>[] = React.useMemo(
    () =>
      selectable
        ? [
            {
              id: "__select__",
              size: 40,
              enableHiding: false,
              header: ({ table }) => (
                <Checkbox
                  checked={
                    table.getIsAllPageRowsSelected()
                      ? true
                      : table.getIsSomePageRowsSelected()
                        ? "indeterminate"
                        : false
                  }
                  onCheckedChange={(v) => table.toggleAllPageRowsSelected(!!v)}
                  aria-label="전체 선택"
                />
              ),
              cell: ({ row }) => (
                <Checkbox
                  checked={row.getIsSelected()}
                  onCheckedChange={(v) => row.toggleSelected(!!v)}
                  onClick={(e) => e.stopPropagation()}
                  aria-label="행 선택"
                />
              ),
              enableSorting: false,
            } as ColumnDef<TData>,
            ...columns,
          ]
        : columns,
    [selectable, columns],
  );

  const table = useReactTable({
    data,
    columns: allColumns,
    state: {
      sorting,
      rowSelection,
      ...(columnVisibility ? { columnVisibility } : {}),
      ...(columnOrder ? { columnOrder } : {}),
    },
    onSortingChange: onSortingChange ?? setInternalSorting,
    onColumnVisibilityChange,
    onColumnOrderChange,
    onRowSelectionChange: onRowSelectionChange
      ? (updater) => {
          const next = typeof updater === "function" ? updater(rowSelection) : updater;
          onRowSelectionChange(next);
        }
      : undefined,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: manualSorting ? undefined : getSortedRowModel(),
    manualSorting,
    enableRowSelection: selectable,
    getRowId,
  });

  const visibleLeafCount = table.getVisibleLeafColumns().length || 1;

  return (
    <div
      className={cn(
        "ops-scroll-x w-full rounded-lg border border-[--color-border] bg-[--color-card]",
        className,
      )}
    >
      <table className="w-full border-collapse text-[13px]">
        <thead className={cn("bg-[--color-muted]", stickyHeader && "sticky top-0 z-10")}>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((header) => {
                const canSort = header.column.getCanSort();
                const sorted = header.column.getIsSorted();
                const meta = header.column.columnDef.meta as { align?: "right" | "center" } | undefined;
                return (
                  <th
                    key={header.id}
                    scope="col"
                    className={cn(
                      "whitespace-nowrap border-b border-[--color-border] text-[12px] font-semibold tracking-wide text-[--color-muted-foreground]",
                      cellPad,
                      meta?.align === "right" ? "text-right" : meta?.align === "center" ? "text-center" : "text-left",
                      canSort && "cursor-pointer select-none hover:text-[--color-foreground]",
                    )}
                    style={{ width: header.getSize() !== 150 ? header.getSize() : undefined }}
                    onClick={canSort ? header.column.getToggleSortingHandler() : undefined}
                    aria-sort={
                      sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined
                    }
                  >
                    <span
                      className={cn(
                        "inline-flex items-center gap-1",
                        meta?.align === "right" && "flex-row-reverse",
                      )}
                    >
                      {header.isPlaceholder
                        ? null
                        : flexRender(header.column.columnDef.header, header.getContext())}
                      {canSort && (
                        <span className="opacity-50" aria-hidden>
                          {sorted === "asc" ? (
                            <ChevronUp className="h-3 w-3" />
                          ) : sorted === "desc" ? (
                            <ChevronDown className="h-3 w-3" />
                          ) : (
                            <ChevronsUpDown className="h-3 w-3" />
                          )}
                        </span>
                      )}
                    </span>
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {isError ? (
            <tr>
              <td colSpan={visibleLeafCount} className="py-12 text-center">
                <div className="flex flex-col items-center gap-3">
                  <AlertTriangle className="h-8 w-8 text-[--color-danger]" aria-hidden />
                  <p className="text-sm font-medium">데이터를 불러오지 못했습니다</p>
                  {errorMessage && (
                    <p className="max-w-md text-xs text-[--color-muted-foreground]">{errorMessage}</p>
                  )}
                  {onRetry && (
                    <Button variant="outline" size="sm" onClick={onRetry}>
                      <RotateCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                      다시 시도
                    </Button>
                  )}
                </div>
              </td>
            </tr>
          ) : isLoading ? (
            Array.from({ length: 8 }).map((_, i) => (
              <tr key={i} className="border-b border-[--color-border]">
                {Array.from({ length: visibleLeafCount }).map((_, j) => (
                  <td key={j} className={cellPad}>
                    <Skeleton className="h-4 w-full" />
                  </td>
                ))}
              </tr>
            ))
          ) : table.getRowModel().rows.length === 0 ? (
            <tr>
              <td colSpan={visibleLeafCount} className="py-12 text-center">
                <EmptyState title={emptyText} description={emptyDescription} action={emptyAction} />
              </td>
            </tr>
          ) : (
            table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                data-selected={row.getIsSelected()}
                className={cn(
                  "border-b border-[--color-border] transition-colors last:border-0",
                  "hover:bg-[--color-muted]/60",
                  row.getIsSelected() && "bg-[--color-info-bg]",
                  onRowClick && "cursor-pointer",
                  rowClassName?.(row.original),
                )}
                onClick={() => onRowClick?.(row.original)}
              >
                {row.getVisibleCells().map((cell) => {
                  const meta = cell.column.columnDef.meta as
                    | { align?: "right" | "center" }
                    | undefined;
                  return (
                    <td
                      key={cell.id}
                      className={cn(
                        "align-middle",
                        cellPad,
                        meta?.align === "right" && "ops-num",
                        meta?.align === "center" && "text-center",
                      )}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
