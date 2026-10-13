"use client";

import { CalendarDays, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DATE_RANGE_PRESETS,
  matchPreset,
  resolvePreset,
  type DateRange,
  type DateRangePresetKey,
} from "@/lib/date-range";

/**
 * 기간 필터 — 프리셋 칩 + 직접 지정.
 *
 * 모든 목록 화면이 이 컴포넌트 하나를 쓴다. 화면마다 날짜 UI 를 따로 만들면
 * "최근 7일"이 어디선 오늘 포함, 어디선 미포함이 되는 식으로 갈린다.
 *
 * 네이티브 `<input type="date">` 를 쓴다 — 커스텀 캘린더는 키보드 접근성과
 * 한글 로케일 처리를 직접 다 떠안아야 하고, 운영툴에서는 직접 타이핑이 더 빠르다.
 */
export function DateRangeFilter({
  value,
  onChange,
  presets = DATE_RANGE_PRESETS.map((p) => p.key),
  label = "기간",
  className,
}: {
  value: Partial<DateRange>;
  onChange: (next: DateRange | { from: ""; to: "" }) => void;
  presets?: DateRangePresetKey[];
  label?: string;
  className?: string;
}) {
  const current: DateRange | null =
    value.from && value.to ? { from: value.from, to: value.to } : null;
  const active = matchPreset(current);
  const invalid = Boolean(value.from && value.to && value.from > value.to);

  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      <span className="flex items-center gap-1 text-xs text-[--color-muted-foreground]">
        <CalendarDays className="h-3.5 w-3.5" aria-hidden />
        {label}
      </span>

      <div className="flex flex-wrap items-center gap-1" role="group" aria-label={`${label} 프리셋`}>
        {DATE_RANGE_PRESETS.filter((p) => presets.includes(p.key)).map((preset) => {
          const isActive = active === preset.key;
          return (
            <button
              key={preset.key}
              type="button"
              aria-pressed={isActive}
              onClick={() => {
                const r = resolvePreset(preset.key);
                if (r) onChange(r);
              }}
              className={cn(
                "ops-focusable rounded-md border px-2 py-1 text-xs transition-colors",
                isActive
                  ? "border-[--color-accent] bg-[--color-accent-bg] font-medium text-[--color-accent]"
                  : "border-[--color-border] text-[--color-muted-foreground] hover:bg-[--color-muted]",
              )}
            >
              {preset.label}
            </button>
          );
        })}
      </div>

      <div className="flex items-center gap-1">
        <input
          type="date"
          aria-label={`${label} 시작일`}
          value={value.from ?? ""}
          max={value.to || undefined}
          onChange={(e) => onChange({ from: e.target.value, to: value.to ?? "" } as DateRange)}
          className={cn(
            "ops-focusable h-8 rounded-md border bg-[--color-card] px-2 text-xs",
            invalid ? "border-[--color-danger]" : "border-[--color-border]",
          )}
        />
        <span className="text-xs text-[--color-muted-foreground]" aria-hidden>~</span>
        <input
          type="date"
          aria-label={`${label} 종료일`}
          value={value.to ?? ""}
          min={value.from || undefined}
          onChange={(e) => onChange({ from: value.from ?? "", to: e.target.value } as DateRange)}
          className={cn(
            "ops-focusable h-8 rounded-md border bg-[--color-card] px-2 text-xs",
            invalid ? "border-[--color-danger]" : "border-[--color-border]",
          )}
        />
        {(value.from || value.to) && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => onChange({ from: "", to: "" })}
            aria-label={`${label} 초기화`}
            title="기간 해제"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </Button>
        )}
      </div>

      {invalid && (
        <span role="alert" className="text-xs text-[--color-danger]">
          시작일이 종료일보다 늦습니다
        </span>
      )}
    </div>
  );
}
