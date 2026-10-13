"use client";

/**
 * 오늘의 운영 — 하루 한 장.
 *
 * 왜 확인 필요를 맨 위에 두나
 * ------------------------
 * 숫자를 먼저 보여주면 사용자는 매일 같은 숫자를 훑다가 결국 안 보게 된다.
 * **오늘 사람이 해야 할 일**이 먼저고, 그게 없으면 "확인할 것 없음" 이라고
 * 분명히 말한다.
 *
 * AI 가 없어도 완성된 화면이다
 * -------------------------
 * 숫자는 서버가 계산해서 준다. AI 요약은 나중에 이 위에 얹는 것이지, 이 화면이
 * 동작하는 데 필요한 게 아니다.
 *
 * 모르는 것을 0 으로 그리지 않는다
 * ----------------------------
 * 집계가 없으면 "데이터 없음" 이라고 쓴다. 0 으로 그리면 "오늘 매출 0원" 과
 * "아직 집계 안 됨" 이 같아 보인다.
 */

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  automationApi,
  type DailyReport,
  type NeedsAttention,
} from "@/lib/api/automation";
import { categoryText } from "@/lib/api/exceptions";
import { formatDateTime, formatKRW } from "@/lib/format";

export default function DailyOperationsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["automation", "daily-report"],
    queryFn: () => automationApi.dailyReport(),
  });

  if (isLoading) {
    return (
      <PageShell title="오늘의 운영" variant="detail">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </PageShell>
    );
  }

  const attention = data?.needs_attention ?? [];
  const sections = data?.sections ?? {};

  return (
    <PageShell
      title="오늘의 운영"
      description={
        data
          ? `${data.report_date} 기준 · ${formatDateTime(data.generated_at)} 생성`
          : undefined
      }
      variant="detail"
    >
      <Section title="확인이 필요한 일">
        {attention.length === 0 ? (
          // "아무것도 없음" 을 분명히 말한다. 빈 화면은 고장으로 읽힌다.
          <div className="flex items-center gap-2 rounded-lg border border-[--color-border] px-3 py-4 text-sm">
            <CheckCircle2 className="h-4 w-4 text-[--color-success]" aria-hidden />
            <span>지금 확인이 필요한 일이 없습니다.</span>
          </div>
        ) : (
          <div className="space-y-1.5">
            {attention.map((a) => (
              <AttentionRow key={a.kind} item={a} />
            ))}
          </div>
        )}
      </Section>

      {/* 예외 센터로 들어가는 문. 숫자는 서버가 같은 읽기 모델에서 준 것이라
          여기와 예외 센터가 갈라지지 않는다. */}
      <ExceptionSummary summary={data?.exception_summary} />

      <Section title="어제 판매">
        <OrdersCard section={sections.orders} />
      </Section>

      <Section title="상태">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <StatCard
            title="마진 기준 미달"
            section={sections.margin} valueKey="below_threshold" suffix="건"
          />
          <StatCard
            title="원가 차이"
            section={sections.sourcing} valueKey="diverged" suffix="건"
          />
          <StatCard
            title="정산 미매칭"
            section={sections.settlement} valueKey="unmatched" suffix="건"
          />
          <StatCard
            title="승인 대기"
            section={sections.approvals} valueKey="pending" suffix="건"
          />
          <StatCard
            title="열린 운영 이슈"
            section={sections.issues} valueKey="open_total" suffix="건"
          />
          <CashCard section={sections.cashflow} />
        </div>
      </Section>

      <Section title="최근 원가 변화">
        <CostChanges section={sections.cost_changes} />
      </Section>

      <Section title="연동 상태">
        <Integrations section={sections.integrations} />
      </Section>

      <AutomationHealth section={sections.automation} />
    </PageShell>
  );
}

function AttentionRow({ item }: { item: NeedsAttention }) {
  return (
    <Link
      href={item.link as Route}
      className="ops-focusable flex items-center justify-between gap-2 rounded-lg border border-[--color-border] px-3 py-2 hover:bg-[--color-muted]"
    >
      <span className="flex items-center gap-2 text-[13px]">
        <TriangleAlert
          className={
            item.severity === "BLOCKING"
              ? "h-3.5 w-3.5 text-[--color-danger]"
              : item.severity === "WARNING"
                ? "h-3.5 w-3.5 text-[--color-warning]"
                : "h-3.5 w-3.5 text-[--color-muted-foreground]"
          }
          aria-hidden
        />
        {item.message}
      </span>
      <ChevronRight className="h-3.5 w-3.5 text-[--color-muted-foreground]" aria-hidden />
    </Link>
  );
}

type Section = Record<string, unknown> | undefined;

function OrdersCard({ section }: { section: Section }) {
  if (!section || section.available !== true) {
    return <Unavailable reason={String(section?.reason ?? "집계할 수 없습니다.")} />;
  }
  const orders = Number(section.orders ?? 0);
  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <div className="flex flex-wrap gap-6">
        <Figure label="주문" value={`${orders}건`} />
        <Figure label="매출" value={formatKRW(Number(section.revenue ?? 0))} />
        <Figure label="판매처" value={`${Number(section.channels ?? 0)}곳`} />
        {/* 클레임은 연동이 없으면 0 이 아니라 "모름" 이다. */}
        <Figure
          label="취소·반품"
          value={section.claims == null ? "확인 불가" : `${section.claims}건`}
          hint={section.claims == null
            ? String(section.claims_unavailable_reason ?? "")
            : undefined}
        />
      </div>
      {section.note ? (
        <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
          {String(section.note)}
        </p>
      ) : null}
    </div>
  );
}

function StatCard({
  title, section, valueKey, suffix,
}: {
  title: string;
  section: Section;
  valueKey: string;
  suffix: string;
}) {
  const available = section?.available === true;
  const value = section?.[valueKey];

  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <p className="text-[11px] text-[--color-muted-foreground]">{title}</p>
      {available && value != null ? (
        <p className="mt-0.5 text-[18px] font-semibold tabular-nums">
          {String(value)}
          <span className="ml-0.5 text-[12px] font-normal">{suffix}</span>
        </p>
      ) : (
        <>
          <p className="mt-0.5 text-[13px] text-[--color-muted-foreground]">
            데이터 없음
          </p>
          {section?.reason ? (
            <p className="mt-0.5 text-[10px] text-[--color-muted-foreground]">
              {String(section.reason)}
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

function CashCard({ section }: { section: Section }) {
  const available = section?.available === true;
  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <p className="text-[11px] text-[--color-muted-foreground]">현금 기준 잔액</p>
      {available ? (
        <>
          <p className="mt-0.5 text-[18px] font-semibold tabular-nums">
            {formatKRW(Number(section.baseline_amount ?? 0))}
          </p>
          <p className="text-[10px] text-[--color-muted-foreground]">
            {String(section.baseline_date ?? "")} 기준
          </p>
        </>
      ) : (
        <>
          <p className="mt-0.5 text-[13px] text-[--color-muted-foreground]">
            확인 불가
          </p>
          <p className="mt-0.5 text-[10px] text-[--color-muted-foreground]">
            {String(section?.reason ?? "기준 잔액이 없습니다.")}
          </p>
        </>
      )}
    </div>
  );
}

/**
 * 자동화가 실제로 돌고 있나.
 *
 * "자동으로 하고 있다" 는 믿음이 틀렸을 때가 가장 위험하다 — 사용자는 확인을
 * 멈췄는데 아무것도 안 돌고 있는 상태가 된다.
 */
/**
 * 최근 원가 변화.
 *
 * "급등"·"위험" 같은 판정을 붙이지 않는다 — 몇 %가 위험한지는 업무 기준이고
 * 우리가 정할 수 없다. 변화량만 보여주고 판단은 사람이 한다.
 */
function CostChanges({ section }: { section: Section }) {
  if (!section || section.available !== true) {
    return <Unavailable reason="원가 이력을 읽을 수 없습니다." />;
  }
  const items = (section.items as Record<string, unknown>[]) ?? [];
  if (items.length === 0) {
    return (
      <Unavailable reason={`최근 7일간 원가가 바뀐 소싱처가 없습니다.${
        Number(section.first_time_count ?? 0) > 0
          ? ` (새로 등록된 원가 ${section.first_time_count}건은 비교 대상이 없어 제외했습니다.)`
          : ""
      }`} />
    );
  }

  return (
    <div className="ops-scroll-x">
      <table className="w-full text-[12px]">
        <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
          <tr>
            <th scope="col" className="px-2 py-1.5 text-left font-semibold">상품</th>
            <th scope="col" className="px-2 py-1.5 text-right font-semibold">이전</th>
            <th scope="col" className="px-2 py-1.5 text-right font-semibold">현재</th>
            <th scope="col" className="px-2 py-1.5 text-right font-semibold">변화</th>
            <th scope="col" className="px-2 py-1.5 text-left font-semibold">적용일</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[--color-border]">
          {items.map((it, i) => {
            const delta = Number(it.delta);
            return (
              <tr key={`${it.product_name}-${i}`}>
                <td className="px-2 py-1.5">{String(it.product_name)}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">
                  {Number(it.previous).toLocaleString("ko-KR")}
                </td>
                <td className="px-2 py-1.5 text-right tabular-nums">
                  {Number(it.current).toLocaleString("ko-KR")}
                </td>
                <td className="px-2 py-1.5 text-right tabular-nums">
                  <span className={delta > 0
                    ? "text-[--color-danger]" : "text-[--color-success]"}>
                    {delta > 0 ? "+" : ""}{delta.toLocaleString("ko-KR")}
                  </span>
                  <span className="ml-1 text-[10px] text-[--color-muted-foreground]">
                    {String(it.currency)}
                  </span>
                </td>
                <td className="px-2 py-1.5 text-[11px]">{String(it.valid_from)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * 연동 상태.
 *
 * **미구현을 "정상" 으로 보여주지 않는다.** 사용자가 연동이 돌고 있다고 믿은 채
 * 확인을 멈추는 것이 가장 위험하다.
 */
function Integrations({ section }: { section: Section }) {
  if (!section || section.available !== true) {
    return <Unavailable reason="연동 상태를 읽을 수 없습니다." />;
  }
  const accounts = (section.accounts as Record<string, unknown>[]) ?? [];

  const STATE: Record<string, { label: string; tone: string }> = {
    READY: { label: "사용 가능", tone: "text-[--color-success]" },
    CREDENTIAL_NOT_CONFIGURED: {
      label: "자격증명 미설정", tone: "text-[--color-warning]",
    },
    CONNECTOR_UNAVAILABLE: {
      label: "연동 미구현", tone: "text-[--color-muted-foreground]",
    },
  };

  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      {accounts.length === 0 ? (
        <p className="text-[12px] text-[--color-muted-foreground]">
          등록된 외부 계정이 없습니다. 주문·정산 자동 수집은 아직 하지 않습니다.
        </p>
      ) : (
        <ul className="space-y-1">
          {accounts.map((a, i) => {
            const st = STATE[String(a.state)] ?? STATE.CONNECTOR_UNAVAILABLE;
            return (
              <li key={i} className="flex flex-wrap items-center gap-x-2 text-[12px]">
                <span className="font-medium">{String(a.system_name)}</span>
                <span className="text-[11px] text-[--color-muted-foreground]">
                  {String(a.account)}
                </span>
                <span className={`text-[11px] ${st.tone}`}>{st.label}</span>
                <span className="text-[10px] text-[--color-muted-foreground]">
                  {a.last_successful_sync_at
                    ? `마지막 성공 ${formatDateTime(String(a.last_successful_sync_at))}`
                    : "성공한 동기화 없음"}
                </span>
              </li>
            );
          })}
        </ul>
      )}

      <p className="mt-2 border-t border-[--color-border] pt-1.5 text-[11px] text-[--color-muted-foreground]">
        AI 접근(MCP){" "}
        <span className={section.mcp_enabled
          ? "text-[--color-success]" : "text-[--color-muted-foreground]"}>
          {section.mcp_enabled ? "켜짐" : "꺼짐"}
        </span>
        {!section.mcp_enabled &&
          " — 꺼져 있어도 자동화와 리포트는 그대로 동작합니다."}
      </p>
      <Link
        href={"/integrations/external" as Route}
        className="ops-focusable mt-1 inline-flex items-center gap-1 text-[11px] underline underline-offset-2"
      >
        연동 설정으로 이동
        <ChevronRight className="h-3 w-3" aria-hidden />
      </Link>
    </div>
  );
}

function AutomationHealth({ section }: { section: Section }) {
  if (!section || section.available !== true) return null;

  const enabled = Number(section.enabled_count ?? 0);
  const failing = (section.failing as string[]) ?? [];
  const neverRan = (section.never_ran as string[]) ?? [];

  return (
    <Section title="자동화 상태">
      <div className="rounded-lg border border-[--color-border] p-3 text-[12px]">
        {enabled === 0 ? (
          <p className="text-[--color-muted-foreground]">
            켜 둔 자동화가 없습니다. 설정 → 자동화 에서 켤 수 있습니다.
          </p>
        ) : (
          <>
            <p>
              자동화 {enabled}개 사용 중
              {failing.length > 0 && (
                <Badge variant="danger" className="ml-1.5 px-1.5 py-0 text-[10px]">
                  실패 {failing.length}
                </Badge>
              )}
              {neverRan.length > 0 && (
                <Badge variant="warning" className="ml-1.5 px-1.5 py-0 text-[10px]">
                  실행 기록 없음 {neverRan.length}
                </Badge>
              )}
            </p>
            {(failing.length > 0 || neverRan.length > 0) && (
              <p className="mt-1 text-[11px] text-[--color-warning]">
                켜져 있지만 돌지 않는 자동화가 있습니다. 그 항목은 아무도 확인하지
                않고 있는 상태입니다.
              </p>
            )}
          </>
        )}
      </div>
    </Section>
  );
}

function Figure({
  label, value, hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div>
      <p className="text-[11px] text-[--color-muted-foreground]">{label}</p>
      <p className="text-[16px] font-semibold tabular-nums">{value}</p>
      {hint ? (
        <p className="text-[10px] text-[--color-muted-foreground]">{hint}</p>
      ) : null}
    </div>
  );
}

function Unavailable({ reason }: { reason: string }) {
  return (
    <div className="rounded-lg border border-dashed border-[--color-border] px-3 py-4 text-[12px] text-[--color-muted-foreground]">
      {reason}
    </div>
  );
}


/**
 * 예외 센터 요약 + 들어가는 문.
 *
 * 건수를 그대로 쓰지 않는다 — `count = 0` 이 "이상 없음"·"미연결"·"설정 필요"
 * 셋 다에서 나온다. `categoryText` 가 상태를 문구로 바꾼다.
 */
function ExceptionSummary({
  summary,
}: {
  summary: DailyReport["exception_summary"];
}) {
  if (!summary) return null;

  if (summary.unavailable) {
    return (
      <Section title="확인 필요">
        <p className="text-sm text-[--color-muted-foreground]">
          예외 목록을 불러오지 못했습니다. 0 건이라는 뜻이 아닙니다.
        </p>
      </Section>
    );
  }

  const categories = summary.categories ?? [];

  return (
    <Section
      title={`확인 필요 ${summary.total ?? 0}건`}
      actions={
        <Link
          href={"/operations/exceptions" as Route}
          className="flex items-center gap-1 text-sm text-[--color-primary] hover:underline"
        >
          전체 보기
          <ChevronRight className="size-4" />
        </Link>
      }
    >
      <div className="flex flex-wrap gap-2">
        {categories.map((c) => (
          <Link
            key={c.category}
            href={`/operations/exceptions?category=${c.category}` as Route}
            title={c.note ?? undefined}
            className="rounded-md border border-[--color-border] px-3 py-2 text-sm hover:bg-[--color-muted]"
          >
            <span className="block">{c.label}</span>
            <span
              className={
                c.status === "ATTENTION"
                  ? "block font-medium"
                  : "block text-xs text-[--color-muted-foreground]"
              }
            >
              {categoryText(c)}
            </span>
          </Link>
        ))}
      </div>
    </Section>
  );
}
