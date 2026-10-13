"use client";

import * as React from "react";
import { Search, X, FilterX } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/**
 * 목록 화면 상단 필터 영역.
 *
 * 콘텐츠 위에 sticky 로 붙는다 — 1,000행을 스크롤하다 필터를 바꾸려고
 * 맨 위로 올라가야 하면 그 필터는 안 쓰이게 된다.
 */
export function FilterBar({
  children,
  onReset,
  hasActiveFilters,
  resultCount,
  actions,
  className,
}: {
  children: React.ReactNode;
  onReset?: () => void;
  hasActiveFilters?: boolean;
  /** 필터 적용 결과 건수 — 필터가 실제로 먹었는지 즉시 확인할 수 있어야 한다 */
  resultCount?: number;
  /** 우측 정렬 액션 (등록·내보내기·열 설정) */
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "sticky top-0 z-20 flex flex-col gap-2 border-b border-[--color-border] bg-[--color-card] px-4 py-3",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-1 flex-wrap items-center gap-2">{children}</div>
        {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
      </div>

      {(hasActiveFilters || resultCount !== undefined) && (
        <div className="flex items-center gap-3">
          {resultCount !== undefined && (
            <span className="text-xs text-[--color-muted-foreground]" aria-live="polite">
              <strong className="tabular-nums text-[--color-foreground]">
                {resultCount.toLocaleString("ko-KR")}
              </strong>
              건
            </span>
          )}
          {hasActiveFilters && onReset && (
            <Button
              variant="ghost"
              size="sm"
              onClick={onReset}
              className="h-6 px-2 text-xs text-[--color-muted-foreground]"
            >
              <FilterX className="mr-1 h-3 w-3" aria-hidden />
              필터 초기화
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 통합 검색 입력.
 *
 * 디바운스는 호출자가 아니라 여기서 처리한다 — 화면마다 각자 setTimeout 을 쓰면
 * 어떤 화면은 300ms, 어떤 화면은 즉시 요청이 되어 체감 속도가 들쭉날쭉해진다.
 */
export function SearchInput({
  value,
  onChange,
  placeholder = "검색",
  debounceMs = 300,
  className,
  ariaLabel,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  debounceMs?: number;
  className?: string;
  ariaLabel?: string;
}) {
  const [local, setLocal] = React.useState(value);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  // 외부(URL/필터 초기화)에서 값이 바뀌면 입력창도 따라간다.
  React.useEffect(() => {
    setLocal(value);
  }, [value]);

  React.useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  function push(next: string) {
    setLocal(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => onChange(next), debounceMs);
  }

  return (
    <div className={cn("relative", className)}>
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[--color-muted-foreground]"
        aria-hidden
      />
      <input
        type="search"
        value={local}
        aria-label={ariaLabel ?? placeholder}
        placeholder={placeholder}
        onChange={(e) => push(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            if (timer.current) clearTimeout(timer.current);
            onChange(local);
          }
          if (e.key === "Escape" && local) {
            if (timer.current) clearTimeout(timer.current);
            setLocal("");
            onChange("");
          }
        }}
        className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] pl-8 pr-7 text-xs placeholder:text-[--color-muted-foreground]"
      />
      {local && (
        <button
          type="button"
          onClick={() => {
            if (timer.current) clearTimeout(timer.current);
            setLocal("");
            onChange("");
          }}
          aria-label="검색어 지우기"
          className="ops-focusable absolute right-2 top-1/2 -translate-y-1/2 text-[--color-muted-foreground] hover:text-[--color-foreground]"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      )}
    </div>
  );
}

/**
 * 단일 선택 필터 (판매처/담당자/상태 등).
 * 네이티브 select 를 쓴다 — 옵션이 수십 개인 판매처 목록에서 네이티브가 훨씬 빠르고
 * 키보드 타이핑 점프가 공짜로 따라온다.
 */
export function SelectFilter({
  value,
  onChange,
  options,
  placeholder,
  label,
  className,
}: {
  value: string;
  onChange: (next: string) => void;
  options: Array<{ value: string; label: string }>;
  placeholder: string;
  label?: string;
  className?: string;
}) {
  return (
    <select
      value={value}
      aria-label={label ?? placeholder}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "ops-focusable h-8 rounded-md border bg-[--color-card] px-2 text-xs",
        value
          ? "border-[--color-accent] font-medium text-[--color-accent]"
          : "border-[--color-border] text-[--color-foreground]",
        className,
      )}
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

/** 숫자 범위 필터 (마진율 등). 빈 값이면 제한 없음. */
export function RangeFilter({
  min,
  max,
  onChange,
  label,
  suffix = "%",
  className,
}: {
  min: string;
  max: string;
  onChange: (next: { min: string; max: string }) => void;
  label: string;
  suffix?: string;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-1", className)}>
      <span className="text-xs text-[--color-muted-foreground]">{label}</span>
      <input
        type="number"
        inputMode="decimal"
        value={min}
        aria-label={`${label} 최소`}
        placeholder="최소"
        onChange={(e) => onChange({ min: e.target.value, max })}
        className="ops-focusable h-8 w-16 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-right text-xs"
      />
      <span className="text-xs text-[--color-muted-foreground]" aria-hidden>~</span>
      <input
        type="number"
        inputMode="decimal"
        value={max}
        aria-label={`${label} 최대`}
        placeholder="최대"
        onChange={(e) => onChange({ min, max: e.target.value })}
        className="ops-focusable h-8 w-16 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-right text-xs"
      />
      <span className="text-xs text-[--color-muted-foreground]">{suffix}</span>
    </div>
  );
}
