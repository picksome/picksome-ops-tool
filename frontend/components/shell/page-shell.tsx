"use client";

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import { cn } from "@/lib/utils";

/**
 * 화면 공통 골격.
 *
 * 목록 화면은 "제목 → 탭 → 필터(sticky) → 테이블 → 페이지네이션" 순서가 고정이다.
 * 화면마다 이 구조를 따로 짜면 여백과 sticky 기준선이 조금씩 달라진다.
 *
 * `variant`:
 *   - `list`   : 필터가 상단에 붙고 본문이 전체 높이를 채운다 (테이블 화면)
 *   - `detail` : 일반 문서 흐름 + 최대 폭 제한 (설정·프로필 화면)
 */
export function PageShell({
  title,
  description,
  actions,
  tabs,
  filters,
  children,
  footer,
  variant = "list",
  breadcrumb,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  tabs?: React.ReactNode;
  filters?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  variant?: "list" | "detail";
  breadcrumb?: Array<{ label: string; href?: Route }>;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-[--color-border] bg-[--color-card] px-4 pt-4">
        {breadcrumb && breadcrumb.length > 0 && (
          <nav aria-label="위치" className="mb-1.5">
            <ol className="flex items-center gap-1 text-[11px] text-[--color-muted-foreground]">
              {breadcrumb.map((b, i) => (
                <li key={`${b.label}-${i}`} className="flex items-center gap-1">
                  {i > 0 && <span aria-hidden>/</span>}
                  {b.href ? (
                    <Link href={b.href} className="ops-focusable hover:text-[--color-foreground]">
                      {b.label}
                    </Link>
                  ) : (
                    <span>{b.label}</span>
                  )}
                </li>
              ))}
            </ol>
          </nav>
        )}

        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
            {description && (
              <p className="mt-0.5 text-xs text-[--color-muted-foreground]">{description}</p>
            )}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>

        {tabs && <div className="mt-3">{tabs}</div>}
        {!tabs && <div className="h-4" />}
      </div>

      {filters}

      <div
        className={cn(
          "flex-1",
          variant === "list" ? "flex flex-col overflow-hidden px-4 py-4" : "overflow-y-auto px-4 py-5",
        )}
      >
        {variant === "detail" ? (
          <div className="mx-auto w-full max-w-4xl">{children}</div>
        ) : (
          children
        )}
      </div>

      {footer && (
        <div className="shrink-0 border-t border-[--color-border] bg-[--color-card] px-4 py-2">
          {footer}
        </div>
      )}
    </div>
  );
}

/**
 * 화면 내 탭 — URL `?tab=` 과 연동한다.
 * radix Tabs 를 쓰지 않는 이유: 탭 전환이 URL 에 남아야 하고(공유·새로고침),
 * 각 탭이 서로 다른 데이터를 부르므로 콘텐츠를 미리 렌더할 필요가 없다.
 */
export function PageTabs({
  tabs,
  value,
  onChange,
}: {
  tabs: Array<{ key: string; label: string; count?: number }>;
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div role="tablist" aria-label="화면 구획" className="ops-scroll-x flex gap-1">
      {tabs.map((t) => {
        const active = value === t.key;
        return (
          <button
            key={t.key}
            role="tab"
            type="button"
            aria-selected={active}
            onClick={() => onChange(t.key)}
            className={cn(
              "ops-focusable relative whitespace-nowrap px-3 py-2 text-[13px] transition-colors",
              active
                ? "font-semibold text-[--color-accent]"
                : "text-[--color-muted-foreground] hover:text-[--color-foreground]",
            )}
          >
            {t.label}
            {t.count !== undefined && (
              <span
                className={cn(
                  "ml-1.5 rounded-full px-1.5 py-0.5 text-[10px] tabular-nums",
                  active ? "bg-[--color-accent-bg] text-[--color-accent]" : "bg-[--color-muted]",
                )}
              >
                {t.count.toLocaleString("ko-KR")}
              </span>
            )}
            {/* 색만으로 선택을 표시하지 않는다 — 하단 인디케이터를 함께 둔다 */}
            {active && (
              <span
                className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-[--color-accent]"
                aria-hidden
              />
            )}
          </button>
        );
      })}
    </div>
  );
}

/** 흰 판 섹션 — 설정 화면처럼 문단이 나뉘는 곳에서 사용. */
export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  title?: string;
  description?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("ops-panel mb-4 overflow-hidden", className)}>
      {(title || actions) && (
        <div className="flex items-start justify-between gap-3 border-b border-[--color-border] px-4 py-3">
          <div>
            {title && <h2 className="text-sm font-semibold">{title}</h2>}
            {description && (
              <p className="mt-0.5 text-xs text-[--color-muted-foreground]">{description}</p>
            )}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

/** 라벨 + 값 한 줄. 상세 drawer/설정 화면에서 반복된다. */
export function Field({
  label,
  hint,
  required,
  htmlFor,
  children,
  className,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  htmlFor?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1", className)}>
      <label
        htmlFor={htmlFor}
        className="flex items-center gap-1 text-xs font-medium text-[--color-foreground]"
      >
        {label}
        {required && (
          <span className="text-[--color-danger]" aria-label="필수">
            *
          </span>
        )}
      </label>
      {children}
      {hint && <p className="text-[11px] text-[--color-muted-foreground]">{hint}</p>}
    </div>
  );
}
