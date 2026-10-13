"use client";

import { Button } from "@/components/ui/button";
import { ChevronLeft, ChevronRight } from "lucide-react";

interface TablePaginationProps {
  page: number;
  pageSize: number;
  total?: number;
  hasNextPage?: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  pageSizeOptions?: number[];
}

export function TablePagination({
  page,
  pageSize,
  total,
  hasNextPage,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [50, 100, 200],
}: TablePaginationProps) {
  const start = (page - 1) * pageSize + 1;
  const end = total != null ? Math.min(page * pageSize, total) : page * pageSize;

  return (
    <div className="flex items-center justify-between px-1 py-3">
      <div className="flex items-center gap-2 text-xs text-[--color-muted-foreground]">
        <span>페이지당</span>
        <select
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
          className="rounded border border-[--color-border] bg-transparent px-1.5 py-0.5 text-xs focus:outline-none"
        >
          {pageSizeOptions.map((s) => (
            <option key={s} value={s}>{s}개</option>
          ))}
        </select>
        {total != null && (
          <span>
            {start}–{end} / {total.toLocaleString("ko-KR")}건
          </span>
        )}
      </div>
      <div className="flex items-center gap-1">
        <Button
          variant="outline"
          size="icon"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          aria-label="이전 페이지"
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <span className="min-w-[3rem] text-center text-xs text-[--color-muted-foreground]">
          {page}
        </span>
        <Button
          variant="outline"
          size="icon"
          onClick={() => onPageChange(page + 1)}
          disabled={!hasNextPage}
          aria-label="다음 페이지"
        >
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
