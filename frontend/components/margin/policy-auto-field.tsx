"use client";

import { Info, AlertTriangle } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatPercent, formatDate } from "@/lib/format";
import { formatKRW } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AppliedPolicy } from "@/lib/api/margin";

interface PolicyAutoFieldProps {
  label: string;
  value: number;
  /** "rate" | "money" */
  format?: "rate" | "money";
  policy?: AppliedPolicy;
  /** 수동 수정값 */
  override?: { value: number; reason?: string };
  onOverride?: (value: number, reason?: string) => void;
}

/** 정책 자동값 표시 셀. muted 배경 + ⓘ → 호버 시 출처 팝오버. 수동 수정 시 warn 배경 */
export function PolicyAutoField({
  label,
  value,
  format = "rate",
  policy,
  override,
}: PolicyAutoFieldProps) {
  const displayValue = override?.value ?? value;
  const isOverride = override != null;
  const isStale =
    policy &&
    (policy.reliability_status === "NEEDS_REVIEW" ||
      policy.reliability_status === "EXPIRED" ||
      policy.reliability_status === "NEEDS_VERIFICATION");

  const formatted =
    format === "rate" ? formatPercent(displayValue) : formatKRW(displayValue);

  return (
    <div
      className={cn(
        "flex items-center justify-between rounded px-2 py-1.5",
        isOverride
          ? "bg-[--color-surface-warn]"
          : "bg-[--color-surface-muted]",
      )}
    >
      <span className="text-xs text-[--color-muted-foreground]">{label}</span>
      <div className="flex items-center gap-1.5">
        {isStale && !isOverride && (
          <AlertTriangle className="h-3 w-3 text-[--color-warning]" />
        )}
        <span className={cn("text-sm tabular-nums font-medium", isOverride && "text-[--color-warning]")}>
          {formatted}
        </span>
        {isOverride && (
          <span className="rounded-full bg-amber-200 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
            수동 수정됨
          </span>
        )}
        {policy && (
          <Tooltip>
            <TooltipTrigger asChild>
              <button className="text-[--color-muted-foreground] hover:text-[--color-foreground]">
                <Info className="h-3.5 w-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent className="max-w-[220px] space-y-1 text-xs" side="left">
              <p className="font-medium">정책 출처</p>
              <p>테이블: {policy.table}</p>
              <p>버전: v{policy.version}</p>
              <p>효력 시작: {formatDate(policy.effective_from)}</p>
              <p>
                신뢰도:{" "}
                <span
                  className={cn(
                    isStale ? "text-[--color-warning]" : "text-[--color-success]",
                  )}
                >
                  {policy.reliability_status}
                </span>
              </p>
              <p>출처 유형: {policy.source_type}</p>
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </div>
  );
}
