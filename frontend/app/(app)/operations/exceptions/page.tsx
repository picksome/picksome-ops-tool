"use client";

/**
 * 예외 센터 — 오늘 사람이 손대야 하는 것만.
 *
 * 정상 주문을 여기 올리지 않는다
 * --------------------------
 * 목록에 다 넣으면 아무도 안 본다. 여기 오르는 것은 **개입이 필요한 것**뿐이고,
 * 그래서 목록이 비어 있는 것이 좋은 상태다.
 *
 * 0 을 다섯 가지 뜻으로 쓰지 않는다
 * ----------------------------
 * ```
 * 이상 없음      확인했고 없다
 * 미연결         API 가 없어 셀 수 없다
 * 설정 필요      기준을 안 정해 계산 못 한다
 * ```
 *
 * 셋 다 건수는 0 이지만 사용자가 할 일이 완전히 다르다. 카테고리 칩은 건수가
 * 아니라 **상태 문구**를 쓴다.
 *
 * 일일 리포트와 숫자가 같다
 * ---------------------
 * 서버가 같은 읽기 모델(`exception_center`)을 쓴다. 리포트가 "3건" 이라 하고
 * 여기가 "5건" 이면 사용자는 어느 쪽도 믿지 않게 된다.
 */

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, Info, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  categoryText,
  exceptionsApi,
  type ExceptionCategory,
  type ExceptionItem,
  type Severity,
} from "@/lib/api/exceptions";

const SEVERITY_STYLE: Record<Severity, { variant: "danger" | "warning" | "info"; icon: typeof Info }> = {
  BLOCKING: { variant: "danger", icon: TriangleAlert },
  WARNING: { variant: "warning", icon: TriangleAlert },
  INFO: { variant: "info", icon: Info },
};

export default function ExceptionsPage() {
  // 일일 리포트에서 «재고/발주 3건» 을 눌러 들어오면 그 묶음만 보여야 한다.
  // 링크를 눌렀는데 전체 목록이 나오면 사용자가 방금 본 숫자를 다시 찾아야 한다.
  const params = useSearchParams();
  const initial = params.get("category") as ExceptionCategory | null;
  const [filter, setFilter] = React.useState<ExceptionCategory | null>(initial);

  const { data, isLoading } = useQuery({
    queryKey: ["operations", "exceptions", filter],
    queryFn: () => exceptionsApi.list(filter ?? undefined),
  });

  if (isLoading) {
    return (
      <PageShell title="확인 필요" variant="detail">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </PageShell>
    );
  }

  const categories = data?.categories ?? [];
  const items = data?.items ?? [];

  return (
    <PageShell title={`확인 필요 ${data?.total ?? 0}`} variant="detail">
      {/* ── 카테고리 요약 ────────────────────────────────────── */}
      <Section>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setFilter(null)}
            className={`rounded-md border px-3 py-2 text-sm ${
              filter === null
                ? "border-[--color-primary] bg-[--color-muted]"
                : "border-[--color-border]"
            }`}
          >
            전체 {data?.total ?? 0}
          </button>
          {categories.map((c) => (
            <button
              key={c.category}
              type="button"
              onClick={() => setFilter(c.category)}
              className={`rounded-md border px-3 py-2 text-left text-sm ${
                filter === c.category
                  ? "border-[--color-primary] bg-[--color-muted]"
                  : "border-[--color-border]"
              }`}
              title={c.note ?? undefined}
            >
              <span className="block">{c.label}</span>
              {/* 건수가 아니라 상태 문구. 0 이 여러 뜻을 갖기 때문이다. */}
              <span
                className={`block text-xs ${
                  c.status === "ATTENTION"
                    ? "font-medium"
                    : "text-[--color-muted-foreground]"
                }`}
              >
                {categoryText(c)}
              </span>
            </button>
          ))}
        </div>
      </Section>

      {/* ── 미연결·미설정 안내 ───────────────────────────────── */}
      {categories.some(
        (c) => c.status === "NOT_CONNECTED" || c.status === "NOT_CONFIGURED",
      ) && (
        <Section title="아직 확인할 수 없는 것">
          <ul className="space-y-1 text-sm text-[--color-muted-foreground]">
            {categories
              .filter(
                (c) =>
                  c.status === "NOT_CONNECTED" || c.status === "NOT_CONFIGURED",
              )
              .map((c) => (
                <li key={c.category}>
                  <span className="font-medium text-[--color-foreground]">
                    {c.label}
                  </span>{" "}
                  — {c.note}
                </li>
              ))}
          </ul>
        </Section>
      )}

      {/* ── 항목 ─────────────────────────────────────────────── */}
      <Section title={filter ? "해당 항목" : "전체 항목"}>
        {items.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-[--color-muted-foreground]">
            <CheckCircle2 className="size-4" />
            지금 손댈 것이 없습니다.
          </p>
        ) : (
          <ul className="space-y-2">
            {items.map((it, idx) => (
              <ExceptionRow key={`${it.kind}:${it.entity_id ?? idx}`} item={it} />
            ))}
          </ul>
        )}
      </Section>
    </PageShell>
  );
}

function ExceptionRow({ item }: { item: ExceptionItem }) {
  const style = SEVERITY_STYLE[item.severity];
  const Icon = style.icon;

  return (
    <li className="rounded-md border border-[--color-border] px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Badge variant={style.variant} className="gap-1">
              <Icon className="size-3" />
              {item.kind}
            </Badge>
            <span className="truncate text-sm font-medium">{item.title}</span>
          </div>
          {item.detail && (
            <p className="mt-1 text-xs text-[--color-muted-foreground]">
              {item.detail}
            </p>
          )}
        </div>
        {item.fix_link && (
          <Link
            href={item.fix_link as Route}
            className="flex shrink-0 items-center gap-1 text-sm text-[--color-primary] hover:underline"
          >
            {item.fix_label ?? "이동"}
            <ChevronRight className="size-4" />
          </Link>
        )}
      </div>
    </li>
  );
}
