"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { parseAmountInput, parsePercentInput, type Rate, type Won } from "@/lib/money";

/**
 * 금액 입력.
 *
 * 편집 중에는 사용자가 친 문자열을 그대로 두고, **포커스를 잃을 때만** 천 단위 구분자를
 * 넣는다. 타이핑 중에 매 글자마다 포맷하면 커서가 튀고 지우기가 어려워진다 —
 * 금액 입력에서 제일 흔한 불만이다.
 *
 * 값은 항상 **원 단위 정수**(`Won`)로 올려보낸다.
 */
export const MoneyInput = React.forwardRef<
  HTMLInputElement,
  {
    value: Won | null;
    onChange: (next: Won | null) => void;
    placeholder?: string;
    disabled?: boolean;
    className?: string;
    id?: string;
    suffix?: string;
    /** 음수 허용 (조정 금액 등) */
    allowNegative?: boolean;
    "aria-describedby"?: string;
    "aria-invalid"?: boolean;
  }
>(function MoneyInput(
  { value, onChange, placeholder = "0", disabled, className, id, suffix = "원", allowNegative, ...aria },
  ref,
) {
  const [text, setText] = React.useState(() => (value === null ? "" : value.toLocaleString("ko-KR")));
  const [focused, setFocused] = React.useState(false);

  // 외부에서 값이 바뀌면(계산기 자동 채움 등) 편집 중이 아닐 때만 따라간다.
  React.useEffect(() => {
    if (!focused) setText(value === null ? "" : value.toLocaleString("ko-KR"));
  }, [value, focused]);

  return (
    <div className={cn("relative", className)}>
      <input
        ref={ref}
        id={id}
        type="text"
        inputMode={allowNegative ? "text" : "numeric"}
        disabled={disabled}
        value={text}
        placeholder={placeholder}
        onFocus={(e) => {
          setFocused(true);
          // 편집 시작 시 구분자를 걷어낸다 — 지우고 다시 치기 쉽게
          setText(value === null ? "" : String(value));
          requestAnimationFrame(() => e.target.select());
        }}
        onChange={(e) => {
          const raw = e.target.value;
          setText(raw);
          const parsed = parseAmountInput(raw);
          onChange(parsed === null ? null : allowNegative ? parsed : Math.max(0, parsed));
        }}
        onBlur={() => {
          setFocused(false);
          setText(value === null ? "" : value.toLocaleString("ko-KR"));
        }}
        className={cn(
          "ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 pr-7 text-right text-xs tabular-nums",
          disabled && "cursor-not-allowed bg-[--color-muted] text-[--color-muted-foreground]",
        )}
        {...aria}
      />
      {suffix && (
        <span
          className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-[--color-muted-foreground]"
          aria-hidden
        >
          {suffix}
        </span>
      )}
    </div>
  );
});

/**
 * 비율 입력.
 *
 * 화면에는 **퍼센트 숫자**(11)를 보여주고 값은 소수(0.11)로 올려보낸다.
 * 실무에서 "11"을 치면 11%를 의도하지 1100%를 의도하지 않는다.
 */
export const PercentInput = React.forwardRef<
  HTMLInputElement,
  {
    value: Rate | null;
    onChange: (next: Rate | null) => void;
    placeholder?: string;
    disabled?: boolean;
    className?: string;
    id?: string;
    /** 소수 자릿수 (표시용) */
    digits?: number;
  }
>(function PercentInput(
  { value, onChange, placeholder = "0", disabled, className, id, digits = 2 },
  ref,
) {
  const toText = React.useCallback(
    (v: Rate | null) => {
      if (v === null) return "";
      const pct = v * 100;
      return Number.isInteger(pct) ? String(pct) : pct.toFixed(digits).replace(/\.?0+$/, "");
    },
    [digits],
  );

  const [text, setText] = React.useState(() => toText(value));
  const [focused, setFocused] = React.useState(false);

  React.useEffect(() => {
    if (!focused) setText(toText(value));
  }, [value, focused, toText]);

  return (
    <div className={cn("relative", className)}>
      <input
        ref={ref}
        id={id}
        type="text"
        inputMode="decimal"
        disabled={disabled}
        value={text}
        placeholder={placeholder}
        onFocus={(e) => {
          setFocused(true);
          requestAnimationFrame(() => e.target.select());
        }}
        onChange={(e) => {
          const raw = e.target.value;
          setText(raw);
          if (raw.trim() === "") {
            onChange(null);
            return;
          }
          // 여기서는 화면 단위(%)를 그대로 해석한다. "0.5" 는 0.5% 다.
          const n = Number(raw.replace(/[%\s,]/g, ""));
          onChange(Number.isFinite(n) ? n / 100 : null);
        }}
        onBlur={() => {
          setFocused(false);
          setText(toText(value));
        }}
        className={cn(
          "ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 pr-6 text-right text-xs tabular-nums",
          disabled && "cursor-not-allowed bg-[--color-muted] text-[--color-muted-foreground]",
        )}
      />
      <span
        className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-[--color-muted-foreground]"
        aria-hidden
      >
        %
      </span>
    </div>
  );
});

/** 일반 텍스트 입력 — 폼 전체의 높이·테두리를 통일하기 위한 얇은 래퍼. */
export const TextInput = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }
>(function TextInput({ className, invalid, ...props }, ref) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        "ops-focusable h-8 w-full rounded-md border bg-[--color-card] px-2 text-xs",
        invalid ? "border-[--color-danger]" : "border-[--color-border]",
        className,
      )}
      {...props}
    />
  );
});

export { parseAmountInput, parsePercentInput };
