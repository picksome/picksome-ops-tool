"use client";

/**
 * 수익배분 (Phase 31 잔여 배선).
 *
 * 이전 화면은 `distributionAdapter`(메모리 배열)를 썼다. 규칙을 저장하면
 * "저장했습니다" 토스트가 떴지만 **DB 에는 아무 일도 일어나지 않았다.**
 * 새로고침하면 사라졌고, 화면은 cap·최소보장·성과급 구간처럼 **DB 에 존재하지도 않는**
 * 개념을 입력받았다.
 *
 * 이 화면이 지키는 것
 * ----------------
 * · 확정 배분의 진실은 `distribution_run` / `distribution_result` 다.
 *   월 확정 snapshot 이 있어야만 만들 수 있고, 이후 규칙이 바뀌어도 다시 계산되지 않는다
 * · **시뮬레이션과 확정을 섞지 않는다.** 미리보기는 저장하지 않고,
 *   월이 확정되지 않았으면 `is_provisional` 로 그 사실을 밝힌다
 * · 규칙은 `partner_share_policy` / `settlement_policy` 다. 버전과 유효기간이 있고,
 *   새 규칙을 승인해도 **과거 확정 배분은 그대로다**
 * · DB 에 없는 개념(cap·최소보장·기준금액 초과·단계별 성과급)은
 *   입력 칸을 만들지 않고 "미지원" 으로 명시한다
 */

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, ArrowRight, CheckCircle2, Info, Lock, Plus, ShieldCheck, Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs, Section, Field } from "@/components/shell/page-shell";
import { PercentInput, TextInput } from "@/components/ui/money-input";
import { useListQuery } from "@/hooks/useListQuery";
import { ApiError } from "@/lib/api";
import {
  distributionsApi, payoutsApi,
  type DistributionPreview, type DistributionRun, type PayoutBatchSummary,
} from "@/lib/api/payouts";
import { partnersApi, type Partner } from "@/lib/api/partners";
import { policiesApi, type Policy } from "@/lib/api/policies";
import { currentYearMonth } from "@/lib/date-range";
import { formatDateTime, formatKRW, formatPercent, formatYearMonth } from "@/lib/format";

const TABS = [
  { key: "result", label: "확정 배분" },
  { key: "rules", label: "배분 규칙" },
  { key: "simulation", label: "시뮬레이션" },
];

const RUN_STATUS_LABEL: Record<DistributionRun["status"], string> = {
  CALCULATED: "계산됨 (승인 대기)",
  APPROVED: "승인",
  SUPERSEDED: "대체됨",
  CANCELLED: "취소",
};

/**
 * DB 에 없는 배분 개념.
 *
 * 이전 화면은 이 값들을 입력받아 로컬에서 계산해 보여줬다. 저장되지 않았고,
 * 확정 배분에도 반영되지 않았다. 입력 칸을 없애고 사실을 적는다 —
 * 지원하는 척하는 것보다 못 한다고 말하는 편이 낫다.
 */
const UNSUPPORTED_CONCEPTS = [
  "최대 지급 한도(cap)",
  "최소 보장액",
  "기준금액 초과분 비율",
  "단계별 성과급(tier)",
  "투자 원금 우선 회수",
  "참여자 유형별 규칙(투자자·직원·외주)",
];

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  if (err.status === 409) return err.message || `${action}할 수 없는 상태입니다.`;
  return err.message || `${action}에 실패했습니다.`;
}

export default function DistributionPage() {
  const { state, set } = useListQuery({ defaults: { tab: "result" } });
  const tab = state.tab || "result";
  // 기준 월은 URL 에 남긴다 — 대시보드·월 마감에서 특정 달로 바로 들어올 수 있어야 한다.
  // `useListQuery` 에 월 전용 키가 없어 기간 시작(`from`)을 재사용한다.
  const ym = state.from || currentYearMonth();

  return (
    <PageShell
      title="수익배분"
      description="월 확정 순이익을 우선 분배 → 비율 분배 순으로 나눕니다"
      tabs={<PageTabs tabs={TABS} value={tab} onChange={(k) => set({ tab: k })} />}
      variant="detail"
    >
      {tab === "rules" ? (
        <RulesTab />
      ) : tab === "simulation" ? (
        <SimulationTab ym={ym} onYmChange={(v) => set({ from: v })} />
      ) : (
        <ConfirmedTab ym={ym} onYmChange={(v) => set({ from: v })} />
      )}
    </PageShell>
  );
}

/* ────────────────────────────────────────────────────────────
 * 확정 배분
 * ──────────────────────────────────────────────────────────── */

function ConfirmedTab({ ym, onYmChange }: { ym: string; onYmChange: (v: string) => void }) {
  const qc = useQueryClient();

  const { data: run, isLoading, error } = useQuery({
    queryKey: ["distribution", ym],
    queryFn: () => distributionsApi.get(ym),
  });

  const { data: history } = useQuery({
    queryKey: ["distribution-history", ym],
    queryFn: () => distributionsApi.history(ym),
  });

  const { data: batches } = useQuery({
    queryKey: ["payouts", ym],
    queryFn: () => payoutsApi.list({ year_month: ym }),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["distribution", ym] });
    qc.invalidateQueries({ queryKey: ["distribution-history", ym] });
    qc.invalidateQueries({ queryKey: ["payouts", ym] });
  };

  const create = useMutation({
    mutationFn: () => distributionsApi.create(ym),
    onSuccess: () => { invalidate(); toast.success("확정 배분을 만들었습니다"); },
    onError: (e) => toast.error(errorMessage(e, "배분 확정")),
  });

  const approve = useMutation({
    mutationFn: (runId: string) => distributionsApi.approve(ym, runId),
    onSuccess: () => { invalidate(); toast.success("배분을 승인했습니다"); },
    onError: (e) => toast.error(errorMessage(e, "배분 승인")),
  });

  const makeBatch = useMutation({
    mutationFn: () => payoutsApi.createFromDistribution(ym),
    onSuccess: () => { invalidate(); toast.success("지급 배치를 만들었습니다"); },
    onError: (e) => toast.error(errorMessage(e, "지급 배치 생성")),
  });

  return (
    <div className="space-y-4">
      <MonthPicker ym={ym} onChange={onYmChange} />

      {error ? (
        <Section title="확정 배분">
          <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
            {errorMessage(error, "배분 조회")}
          </p>
        </Section>
      ) : isLoading ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : !run ? (
        <Section title="확정 배분">
          <div className="flex flex-col items-center gap-3 py-10 text-center">
            <Info className="h-7 w-7 text-[--color-muted-foreground]" aria-hidden />
            <p className="text-sm">{formatYearMonth(ym)}의 확정 배분이 없습니다</p>
            <p className="max-w-md text-xs text-[--color-muted-foreground]">
              확정 배분은 <strong>월 마감 확정 snapshot</strong> 이 있어야 만들 수 있습니다.
              확정된 순이익을 나누는 것이라, 근거가 없으면 만들 수 없습니다.
            </p>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending}>
                이 달 배분 확정하기
              </Button>
              <Link
                href={`/settlements/monthly/${ym}` as Route}
                className="ops-focusable flex items-center gap-1 rounded-md border border-[--color-border] px-3 py-1.5 text-xs"
              >
                월 마감으로 이동
                <ArrowRight className="h-3.5 w-3.5" aria-hidden />
              </Link>
            </div>
          </div>
        </Section>
      ) : (
        <RunPanel
          run={run}
          onApprove={() => approve.mutate(run.id)}
          approving={approve.isPending}
          onMakeBatch={() => makeBatch.mutate()}
          makingBatch={makeBatch.isPending}
          batches={batches?.items ?? []}
        />
      )}

      {(history?.items?.length ?? 0) > 1 && <HistoryPanel runs={history!.items} />}
    </div>
  );
}

function RunPanel({
  run, onApprove, approving, onMakeBatch, makingBatch, batches,
}: {
  run: DistributionRun;
  onApprove: () => void;
  approving: boolean;
  onMakeBatch: () => void;
  makingBatch: boolean;
  batches: PayoutBatchSummary[];
}) {
  const linked = batches.filter((b) => b.source_distribution_run_id === run.id);
  const remainder = run.distributable_base - run.total_distributed;

  return (
    <>
      <Section
        title={`${formatYearMonth(run.year_month)} 확정 배분`}
        description={`월 확정 snapshot ${run.snapshot_generation}세대 기준`}
        actions={
          <div className="flex gap-2">
            {run.status === "CALCULATED" && (
              <Button size="sm" onClick={onApprove} disabled={approving}>
                <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                배분 승인
              </Button>
            )}
            {run.status === "APPROVED" && linked.length === 0 && (
              <Button size="sm" onClick={onMakeBatch} disabled={makingBatch}>
                지급 배치 만들기
              </Button>
            )}
          </div>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Badge variant={run.status === "APPROVED" ? "success" : "info"}>
            {RUN_STATUS_LABEL[run.status]}
          </Badge>
          {run.status === "APPROVED" && <ShieldCheck className="h-3.5 w-3.5 text-[--color-success]" aria-hidden />}
          <span className="text-[11px] text-[--color-muted-foreground]">
            생성 {formatDateTime(run.created_at)}
            {run.approved_at && ` · 승인 ${formatDateTime(run.approved_at)}`}
          </span>
        </div>

        <dl className="grid gap-3 sm:grid-cols-3">
          <Stat label="배분 대상 순이익" value={formatKRW(run.distributable_base)} />
          <Stat label="배분 합계" value={formatKRW(run.total_distributed)} />
          <Stat
            label="이월 차감"
            value={formatKRW(run.carryover_applied)}
            hint="이전 적자월에서 이월된 손실"
          />
        </dl>

        {remainder !== 0 && (
          <p className="mt-3 flex items-start gap-1.5 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            배분 대상과 배분 합계가 {formatKRW(Math.abs(remainder))} 차이납니다.
          </p>
        )}

        {run.applied_rule_versions_json &&
          Object.keys(run.applied_rule_versions_json).length > 0 && (
            <p className="mt-3 flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] text-[--color-muted-foreground]">
              <Lock className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>
                적용된 규칙 버전이 박제되어 있습니다 —{" "}
                {Object.entries(run.applied_rule_versions_json)
                  .map(([k, v]) => `${k} v${JSON.stringify(v)}`)
                  .join(", ")}
                . 이후 규칙을 바꿔도 이 결과는 변하지 않습니다.
              </span>
            </p>
          )}
      </Section>

      <Section title="참여자별 배분">
        {run.results.length === 0 ? (
          <p className="py-8 text-center text-xs text-[--color-muted-foreground]">
            배분 항목이 없습니다. 배분 규칙(파트너 비율)이 설정되지 않았을 수 있습니다.
          </p>
        ) : (
          <div className="ops-scroll-x">
            <table className="w-full text-[12px]">
              <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                <tr>
                  <th scope="col" className="px-3 py-1.5 text-left font-semibold">참여자</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-semibold">방식</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-semibold">계산 기준</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-semibold">비율</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-semibold">배분액</th>
                </tr>
              </thead>
              <tbody>
                {run.results.map((r) => (
                  <tr key={r.id} className="border-t border-[--color-border]">
                    <th scope="row" className="px-3 py-1.5 text-left font-medium">
                      {r.partner_name_snapshot}
                    </th>
                    <td className="px-3 py-1.5">
                      <Badge
                        variant={r.distribution_kind === "PRIORITY" ? "warning" : "info"}
                        className="px-1.5 py-0 text-[10px]"
                      >
                        {r.distribution_kind === "PRIORITY" ? "우선 분배" : "비율 분배"}
                      </Badge>
                      {r.note && (
                        <span className="ml-1.5 text-[10px] text-[--color-muted-foreground]">{r.note}</span>
                      )}
                    </td>
                    <td className="ops-num px-3 py-1.5">{formatKRW(r.calculation_base)}</td>
                    <td className="ops-num px-3 py-1.5">
                      {r.ratio_used === null ? "—" : formatPercent(Number(r.ratio_used))}
                    </td>
                    <td className="ops-num px-3 py-1.5 font-semibold">{formatKRW(r.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section
        title="지급 상태"
        description="배분은 '얼마를 나눠야 하는가', 지급은 '실제로 보냈는가' 입니다"
      >
        {linked.length === 0 ? (
          <p className="py-6 text-center text-xs text-[--color-muted-foreground]">
            {run.status === "APPROVED"
              ? "아직 지급 배치가 없습니다. 위에서 지급 배치를 만드세요."
              : "배분을 승인해야 지급 배치를 만들 수 있습니다."}
          </p>
        ) : (
          <ul className="space-y-2">
            {linked.map((b) => (
              <li key={b.id}>
                <Link
                  href={"/payouts" as Route}
                  className="ops-focusable flex items-center justify-between gap-3 rounded-lg border border-[--color-border] px-3 py-2 hover:bg-[--color-muted]"
                >
                  <div className="min-w-0">
                    <p className="truncate text-[12px] font-medium">{b.title}</p>
                    <p className="text-[10px] text-[--color-muted-foreground]">
                      {b.business_key} · 항목 {b.item_count}건
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="text-[12px] font-semibold tabular-nums">
                      {formatKRW(b.paid_total)} / {formatKRW(b.due_total)}
                    </p>
                    <p className="text-[10px] text-[--color-muted-foreground]">
                      미지급 {formatKRW(b.due_total - b.paid_total)}
                    </p>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

function HistoryPanel({ runs }: { runs: DistributionRun[] }) {
  return (
    <Section
      title="배분 이력"
      description="월을 재오픈해 재확정하면 새 세대가 생기고 이전 결과는 대체됨으로 남습니다"
    >
      <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
        {runs.map((r) => (
          <li key={r.id} className="flex items-center justify-between gap-3 px-3 py-2 text-[11px]">
            <span>
              {r.snapshot_generation}세대 · {RUN_STATUS_LABEL[r.status]}
              <span className="ml-1.5 text-[--color-muted-foreground]">
                {formatDateTime(r.created_at)}
              </span>
            </span>
            <span className="tabular-nums">{formatKRW(r.total_distributed)}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 시뮬레이션 — 저장하지 않는다
 * ──────────────────────────────────────────────────────────── */

function SimulationTab({ ym, onYmChange }: { ym: string; onYmChange: (v: string) => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["distribution-preview", ym],
    queryFn: () => distributionsApi.preview(ym),
  });

  return (
    <div className="space-y-4">
      <MonthPicker ym={ym} onChange={onYmChange} />

      <p className="flex items-start gap-1.5 rounded-md bg-[--color-info-bg] p-2 text-[11px] text-[--color-info]">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>
          미리보기는 <strong>아무것도 저장하지 않습니다.</strong> 월 마감·확정 배분·지급 배치를
          만들지 않습니다. 실제로 적용하려면 월을 확정한 뒤 &quot;확정 배분&quot; 탭에서 배분을 만드세요.
        </span>
      </p>

      {error ? (
        <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
          {errorMessage(error, "미리보기")}
        </p>
      ) : isLoading || !data ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : (
        <PreviewPanel preview={data} />
      )}
    </div>
  );
}

function PreviewPanel({ preview }: { preview: DistributionPreview }) {
  return (
    <>
      <Section title={`${formatYearMonth(preview.year_month)} 배분 미리보기`}>
        {preview.is_provisional ? (
          <p className="mb-3 flex items-start gap-1.5 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            이 달은 <strong>아직 확정되지 않았습니다.</strong> 아래 금액은 현재 집계 기준 잠정치이며,
            확정 배분액이 아닙니다. 마감 전까지 판매·비용이 들어오면 달라집니다.
          </p>
        ) : (
          <p className="mb-3 flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[11px] text-[--color-muted-foreground]">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            확정 snapshot 의 순이익을 기준으로 계산했습니다.
          </p>
        )}

        <dl className="grid gap-3 sm:grid-cols-2">
          <Stat
            label={preview.is_provisional ? "잠정 배분 대상 이익" : "배분 대상 순이익"}
            value={formatKRW(preview.distributable_base)}
          />
          <Stat label="배분 합계" value={formatKRW(preview.total_distributed)} />
        </dl>
      </Section>

      <Section title="참여자별 예상 배분">
        {preview.results.length === 0 ? (
          <p className="py-8 text-center text-xs text-[--color-muted-foreground]">
            배분할 이익이 없거나 파트너 비율 규칙이 없습니다
          </p>
        ) : (
          <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
            {preview.results.map((r, i) => (
              <li key={r.id ?? i} className="flex items-center justify-between gap-3 px-3 py-2 text-[12px]">
                <div>
                  <span className="font-medium">{r.partner_name_snapshot}</span>
                  <span className="ml-1.5 text-[10px] text-[--color-muted-foreground]">
                    {r.distribution_kind === "PRIORITY" ? "우선 분배" : "비율 분배"}
                    {r.ratio_used !== null && ` · ${formatPercent(Number(r.ratio_used))}`}
                  </span>
                </div>
                <span className="tabular-nums font-semibold">{formatKRW(r.amount)}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

/* ────────────────────────────────────────────────────────────
 * 배분 규칙 — partner_share_policy / settlement_policy
 * ──────────────────────────────────────────────────────────── */

function RulesTab() {
  const qc = useQueryClient();

  const { data: partners } = useQuery({
    queryKey: ["partners", "all"],
    queryFn: () => partnersApi.list(true),
  });

  const { data: shares, isLoading } = useQuery({
    queryKey: ["policies", "partner_share"],
    queryFn: () => policiesApi.list("partner_share", { limit: 200 }),
  });

  const { data: settlement } = useQuery({
    queryKey: ["policies", "settlement"],
    queryFn: () => policiesApi.list("settlement", { limit: 50 }),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["policies"] });

  const create = useMutation({
    mutationFn: (body: { partnerId: string; ratio: number; from: string; ym: string | null }) =>
      policiesApi.create("partner_share", {
        version: nextVersion(shares?.items ?? []),
        effective_from: body.from,
        fields: {
          partner_id: body.partnerId,
          share_ratio: body.ratio,
          applies_year_month: body.ym,
        },
      }),
    onSuccess: () => {
      invalidate();
      // ⚠️ 정책은 저장하는 순간 활성이다(`services/policy_crud.create_policy`).
      // "승인해야 적용됩니다" 라고 안내하면 거짓말이 된다.
      toast.success("규칙을 저장했습니다 — 유효기간에 들어오면 바로 적용됩니다");
    },
    onError: (e) => toast.error(errorMessage(e, "규칙 저장")),
  });

  // 활성 여부를 바꾸지 않는다. "이 수치를 사람이 확인했다" 를 기록하는 절차다.
  const confirmReliability = useMutation({
    mutationFn: (id: string) => policiesApi.approve("partner_share", id),
    onSuccess: () => { invalidate(); toast.success("확인 처리했습니다"); },
    onError: (e) => toast.error(errorMessage(e, "확인 처리")),
  });

  const deprecate = useMutation({
    mutationFn: (id: string) =>
      policiesApi.deprecate("partner_share", id, "화면에서 규칙 종료"),
    onSuccess: () => { invalidate(); toast.success("규칙을 종료했습니다"); },
    onError: (e) => toast.error(errorMessage(e, "규칙 종료")),
  });

  const rows = shares?.items ?? [];
  const activeRows = rows.filter((r) => r.is_active);
  const ratioSum = activeRows.reduce(
    (a, r) => a + Number(r.fields.share_ratio ?? 0), 0,
  );

  return (
    <div className="space-y-4">
      <Section
        title="파트너 배분 비율"
        description="잔여 이익을 나누는 비율입니다. 활성 규칙의 합이 100% 여야 배분을 확정할 수 있습니다."
      >
        {isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (
          <>
            {activeRows.length > 0 && Math.abs(ratioSum - 1) > 0.0005 && (
              <p className="mb-3 flex items-start gap-1.5 rounded-md bg-[--color-danger-bg] p-2 text-[11px] text-[--color-danger]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                <span>
                  활성 비율의 합이 <strong>{formatPercent(ratioSum)}</strong> 입니다.
                  <strong> 서버가 배분 확정을 거부합니다</strong> — 화면 경고만으로는 넘어갈 수 없습니다.
                </span>
              </p>
            )}

            <RuleTable
              rows={rows}
              partners={partners ?? []}
              onConfirm={(id) => confirmReliability.mutate(id)}
              onDeprecate={(id) => deprecate.mutate(id)}
              busy={confirmReliability.isPending || deprecate.isPending}
            />

            <NewRuleForm
              partners={partners ?? []}
              onCreate={(v) => create.mutate(v)}
              busy={create.isPending}
            />
          </>
        )}
      </Section>

      <PriorityPanel policies={settlement?.items ?? []} partners={partners ?? []} />

      <Section title="지원하지 않는 배분 개념">
        <p className="mb-2 text-[11px] text-[--color-muted-foreground]">
          아래 항목은 <strong>데이터베이스에 저장할 곳이 없습니다.</strong> 입력받아 화면에서만
          계산하면 저장된 것처럼 보이지만 확정 배분에는 반영되지 않습니다 —
          그래서 입력 칸을 만들지 않았습니다.
        </p>
        <ul className="flex flex-wrap gap-1.5">
          {UNSUPPORTED_CONCEPTS.map((c) => (
            <li
              key={c}
              className="rounded-full border border-dashed border-[--color-border] px-2 py-0.5 text-[10px] text-[--color-muted-foreground]"
            >
              {c} — 미지원
            </li>
          ))}
        </ul>
      </Section>

      <p className="flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
        <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
        <span>
          <strong>직원 급여는 비용</strong>이라 순이익을 계산하기 전에 빠지고,
          <strong> 동업자 배분은 이익 처분</strong>이라 순이익을 계산한 뒤에 나뉩니다.
          급여는 사업비용으로 등록하고, 여기에는 이익을 나눌 파트너만 넣으세요.
        </span>
      </p>
    </div>
  );
}

function nextVersion(rows: Policy[]): number {
  return rows.reduce((max, r) => Math.max(max, r.version), 0) + 1;
}

function RuleTable({
  rows, partners, onConfirm, onDeprecate, busy,
}: {
  rows: Policy[];
  partners: Partner[];
  /** 활성 여부를 바꾸지 않는다 — 사람이 수치를 확인했다는 기록만 남긴다. */
  onConfirm: (id: string) => void;
  onDeprecate: (id: string) => void;
  busy: boolean;
}) {
  const nameOf = (id: unknown) =>
    partners.find((p) => p.id === String(id))?.name ?? String(id ?? "—");

  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[--color-border] p-6 text-center text-xs text-[--color-muted-foreground]">
        배분 비율 규칙이 없습니다. 아래에서 추가하세요.
      </p>
    );
  }

  return (
    <div className="ops-scroll-x">
      <table className="w-full text-[12px]">
        <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
          <tr>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">파트너</th>
            <th scope="col" className="px-3 py-1.5 text-right font-semibold">비율</th>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">적용 월</th>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">유효기간</th>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">계산 반영</th>
            <th scope="col" className="px-3 py-1.5 text-left font-semibold">검증</th>
            <th scope="col" className="px-3 py-1.5 text-right font-semibold">작업</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className={cn("border-t border-[--color-border]", !r.is_active && "opacity-60")}>
              <th scope="row" className="px-3 py-1.5 text-left font-medium">
                {nameOf(r.fields.partner_id)}
              </th>
              <td className="ops-num px-3 py-1.5">
                {formatPercent(Number(r.fields.share_ratio ?? 0))}
              </td>
              <td className="px-3 py-1.5">
                {r.fields.applies_year_month
                  ? formatYearMonth(String(r.fields.applies_year_month))
                  : "기본"}
              </td>
              <td className="px-3 py-1.5 text-[11px] text-[--color-muted-foreground]">
                {r.effective_from} ~ {r.effective_to ?? "계속"}
              </td>
              <td className="px-3 py-1.5">
                <Badge variant={r.is_active ? "success" : "warning"} className="px-1.5 py-0 text-[10px]">
                  {r.is_active ? `적용 중 (v${r.version})` : "종료됨"}
                </Badge>
              </td>
              <td className="px-3 py-1.5">
                {r.reliability_status === "CONFIRMED" ? (
                  <span className="text-[11px] text-[--color-muted-foreground]">확인됨</span>
                ) : (
                  <Button
                    size="sm" variant="outline" disabled={busy}
                    onClick={() => onConfirm(r.id)}
                  >
                    확인 처리
                  </Button>
                )}
              </td>
              <td className="px-3 py-1.5 text-right">
                {r.is_active && (
                  <Button
                    variant="ghost" size="icon-sm" disabled={busy}
                    onClick={() => onDeprecate(r.id)}
                    aria-label={`${nameOf(r.fields.partner_id)} 규칙 종료`}
                  >
                    <Trash2 className="h-3.5 w-3.5 text-[--color-danger]" aria-hidden />
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NewRuleForm({
  partners, onCreate, busy,
}: {
  partners: Partner[];
  onCreate: (v: { partnerId: string; ratio: number; from: string; ym: string | null }) => void;
  busy: boolean;
}) {
  const [partnerId, setPartnerId] = React.useState("");
  const [ratio, setRatio] = React.useState<number | null>(null);
  const [from, setFrom] = React.useState(`${currentYearMonth()}-01`);
  const [ym, setYm] = React.useState("");

  const canSubmit = !!partnerId && ratio !== null && ratio > 0 && !!from;

  return (
    <div className="mt-4 rounded-lg border border-[--color-border] p-3">
      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="파트너" htmlFor="new-partner">
          <select
            id="new-partner"
            value={partnerId}
            onChange={(e) => setPartnerId(e.target.value)}
            className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
          >
            <option value="">선택</option>
            {partners.filter((p) => p.is_active).map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </Field>
        <Field label="비율" htmlFor="new-ratio">
          <PercentInput id="new-ratio" value={ratio} onChange={setRatio} />
        </Field>
        <Field label="적용 시작일" htmlFor="new-from">
          <TextInput
            id="new-from" type="date" value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </Field>
        <Field
          label="특정 월만" htmlFor="new-ym"
          hint="비우면 기본 규칙 (모든 달)"
        >
          <TextInput
            id="new-ym" type="month" value={ym}
            onChange={(e) => setYm(e.target.value)}
          />
        </Field>
      </div>
      <div className="mt-2 flex items-center justify-between gap-3">
        <p className="text-[10px] text-[--color-muted-foreground]">
          저장하면 <strong>유효기간에 들어오는 즉시 계산에 반영됩니다</strong> — 별도 승인 단계가 없습니다.
          활성 비율의 합이 100% 가 아니면 <strong>월 마감이 차단됩니다.</strong>
          이미 확정된 과거 배분은 바뀌지 않습니다.
        </p>
        <Button
          size="sm"
          disabled={!canSubmit || busy}
          onClick={() => {
            onCreate({ partnerId, ratio: ratio!, from, ym: ym || null });
            setPartnerId("");
            setRatio(null);
            setYm("");
          }}
        >
          <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          규칙 추가
        </Button>
      </div>
    </div>
  );
}

function PriorityPanel({ policies, partners }: { policies: Policy[]; partners: Partner[] }) {
  const active = policies.find((p) => p.is_active);
  const entries =
    (active?.fields.priority_distribution_json as
      | Array<{ partner_id: string; fixed_amount: number }>
      | null) ?? [];
  const nameOf = (id: string) => partners.find((p) => p.id === id)?.name ?? id;

  return (
    <Section
      title="우선 분배"
      description="비율 분배 전에 특정 파트너에게 정액으로 먼저 배정합니다 (정산 정책)"
    >
      {!active ? (
        <p className="py-4 text-center text-xs text-[--color-muted-foreground]">
          적용 중인 정산 정책이 없습니다. 정책 화면에서 설정하세요.
        </p>
      ) : entries.length === 0 ? (
        <p className="py-4 text-center text-xs text-[--color-muted-foreground]">
          우선 분배 항목이 없습니다 — 순이익 전액이 비율로 나뉩니다.
        </p>
      ) : (
        <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
          {entries.map((e, i) => (
            <li key={`${e.partner_id}-${i}`} className="flex items-center justify-between px-3 py-2 text-[12px]">
              <span className="font-medium">{nameOf(e.partner_id)}</span>
              <span className="tabular-nums">{formatKRW(e.fixed_amount)}</span>
            </li>
          ))}
        </ul>
      )}
      {active && (
        <p className="mt-2 text-[10px] text-[--color-muted-foreground]">
          적자 정책: {String(active.fields.loss_policy ?? "미설정")} ·{" "}
          정책 v{active.version} ({active.effective_from} ~ {active.effective_to ?? "계속"})
        </p>
      )}
    </Section>
  );
}

/* ────────────────────────────────────────────────────────────
 * 공통
 * ──────────────────────────────────────────────────────────── */

function MonthPicker({ ym, onChange }: { ym: string; onChange: (v: string) => void }) {
  return (
    <Section title="기준 월">
      <Field label="대상 월" htmlFor="dist-ym">
        <TextInput
          id="dist-ym" type="month" value={ym} className="w-40"
          onChange={(e) => onChange(e.target.value)}
        />
      </Field>
    </Section>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-[--color-border] p-3">
      <dt className="text-[11px] text-[--color-muted-foreground]">{label}</dt>
      <dd className="mt-1 text-[15px] font-semibold tabular-nums">{value}</dd>
      {hint && <p className="mt-0.5 text-[10px] text-[--color-muted-foreground]">{hint}</p>}
    </div>
  );
}
