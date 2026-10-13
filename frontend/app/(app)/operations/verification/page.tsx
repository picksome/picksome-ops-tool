"use client";

/**
 * 검증센터 — "숫자가 맞나?" 가 아니라 "차이가 0인가?" 를 보는 화면.
 *
 * 이 화면이 지키는 것
 * ----------------
 * · **PASS / FAIL / 미검증을 절대 섞지 않는다.** 미검증을 정상에 더하면
 *   "환불 표본이 없다" 가 "환불이 맞다" 로 보인다
 * · **점수 하나로 뭉개지 않는다.** "정확도 98점" 은 무엇을 확인해야 하는지
 *   알려주지 않는다. 건수와 근거를 그대로 보여준다
 * · **모르는 값을 0 으로 그리지 않는다.** 미확인은 미확인으로 표시한다
 * · **임계값을 화면이 만들지 않는다.** 판정은 서버가 하고 여기서는 번역만 한다
 *
 * 정상 데이터를 나열하지 않는다
 * -------------------------
 * 378건을 표로 뿌리면 아무도 안 본다. 확인이 필요한 것만 남긴다.
 */

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  CheckCircle2, ChevronRight, CircleAlert, CircleHelp, TriangleAlert,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, Section } from "@/components/shell/page-shell";
import { GoldenReview } from "@/components/verification/golden-review";
import { ApiError } from "@/lib/api";
import {
  CASE_TYPE_LABEL, COMPONENT_LABEL, CONFIDENCE_LABEL, SOURCE_LABEL,
  VERDICT_LABEL, verificationApi,
  type Confidence, type OrderTrace, type Verdict,
} from "@/lib/api/verification";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const VERDICT_STYLE: Record<
  Verdict,
  { variant: "success" | "danger" | "warning" | "info"; icon: typeof CheckCircle2 }
> = {
  PASS: { variant: "success", icon: CheckCircle2 },
  FAIL: { variant: "danger", icon: CircleAlert },
  NOT_VERIFIED: { variant: "warning", icon: CircleHelp },
  NOT_AVAILABLE: { variant: "info", icon: CircleHelp },
};

const CONFIDENCE_STYLE: Record<Confidence, "success" | "info" | "warning"> = {
  ACTUAL: "success",
  ESTIMATED: "info",
  UNKNOWN: "warning",
};

/** 검사 항목의 사람 말. 코드를 그대로 보여주지 않는다. */
const VERDICT_ROW_LABEL: Record<string, string> = {
  sync_reconciliation: "수집 대사",
  settlement_reconciliation: "정산 대사",
  settlement_margin_coverage: "정산 → 마진 반영",
  integrity: "데이터 무결성",
  calculation_evidence: "계산 근거",
  unknown_as_zero: "미확인 값",
};

const GATE_LABEL: Record<string, string> = {
  refund_financial: "환불 손익",
  cash_receipt: "입금 확인",
  external_inventory: "외부 재고 대사",
  adjustment_posting: "조정 반영",
};

function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const style = VERDICT_STYLE[verdict] ?? VERDICT_STYLE.NOT_VERIFIED;
  return <Badge variant={style.variant}>{VERDICT_LABEL[verdict] ?? verdict}</Badge>;
}

function money(n: number): string {
  return `${n.toLocaleString("ko-KR")}원`;
}

export default function VerificationPage() {
  const [openOrder, setOpenOrder] = React.useState<string | null>(null);
  const [review, setReview] = React.useState<
    { salesOrderId: string; caseId: string | null; caseType: string } | null
  >(null);
  const { user } = useCurrentUser();
  // 기준값 승인·보류는 재무 판단이다. 서버가 같은 규칙으로 막지만, 누를 수
  // 없는 버튼을 보여 주지 않는 편이 낫다.
  const canDecide = user?.role === "ADMIN" || user?.role === "ACCOUNTING";

  const { data, isLoading, error } = useQuery({
    queryKey: ["verification-summary"],
    queryFn: () => verificationApi.summary(),
    staleTime: 30_000,
  });

  const { data: exceptions } = useQuery({
    queryKey: ["verification-exceptions"],
    queryFn: () => verificationApi.exceptions(),
    staleTime: 30_000,
  });

  const { data: golden } = useQuery({
    queryKey: ["verification-golden"],
    queryFn: () => verificationApi.goldenSamples(),
    staleTime: 30_000,
  });

  if (error instanceof ApiError && error.status === 403) {
    return (
      <PageShell title="검증센터" description="원천자료와 계산 결과를 맞대어 봅니다" variant="detail">
        <Section title="권한">
          <p className="text-sm">
            검증센터는 원가·마진을 포함하므로 재무 데이터를 볼 수 있는 역할만 접근할 수 있습니다.
          </p>
        </Section>
      </PageShell>
    );
  }

  return (
    <PageShell
      title="검증센터"
      description="숫자를 믿으라고 하지 않습니다 — 원천과 맞대어 차이만 보여줍니다"
      variant="detail"
    >
      {isLoading || !data ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : (
        <div className="space-y-4">
          {/* ── 종합 ─────────────────────────────────────────── */}
          <section
            aria-label="검증 상태"
            className={cn(
              "rounded-lg border p-3",
              data.overall === "FAIL"
                ? "border-[--color-danger] bg-[--color-danger-bg]"
                : data.overall === "PASS"
                  ? "border-[--color-success] bg-[--color-success-bg]"
                  : "border-[--color-warning] bg-[--color-warning-bg]",
            )}
          >
            <p className="text-[13px] font-semibold">
              검증 상태 ·{" "}
              {data.overall === "PASS"
                ? "확인됨"
                : data.overall === "FAIL"
                  ? "불일치 있음"
                  : "확인 필요"}
            </p>
            <p className="mt-1 text-[11px]">
              확인됨 {data.counts.PASS ?? 0} · 불일치 {data.counts.FAIL ?? 0} ·
              미검증 {data.counts.NOT_VERIFIED ?? 0}
            </p>
            <p className="mt-1 text-[10px] opacity-80">
              미검증은 &ldquo;맞다&rdquo;가 아니라 &ldquo;확인할 근거가 없다&rdquo;는 뜻입니다.
            </p>
          </section>

          {/* ── 데이터 최신성 ────────────────────────────────── */}
          <Section
            title="데이터 최신성"
            description="숫자가 맞는지 이전에, 원천이 최신인지부터 봅니다"
          >
            <ul className="space-y-1.5 text-[12px]">
              {(["orders", "claims"] as const).map((key) => {
                const block = data.freshness[key];
                const label = key === "orders" ? "주문 수집" : "취소·반품 수집";
                return (
                  <li key={key} className="flex flex-wrap items-center gap-2">
                    <span className="w-28 shrink-0 text-[--color-muted-foreground]">{label}</span>
                    <Badge variant={block.state === "OK" ? "success" : "warning"}>
                      {block.state === "OK" ? "정상" : block.state}
                    </Badge>
                    {block.accounts.map((a) => (
                      <span key={a.account} className="text-[11px] text-[--color-muted-foreground]">
                        {a.account} · 마지막 성공{" "}
                        {a.last_success_at ? formatDateTime(a.last_success_at) : "없음"}
                      </span>
                    ))}
                  </li>
                );
              })}
              <li className="flex flex-wrap items-center gap-2">
                <span className="w-28 shrink-0 text-[--color-muted-foreground]">정산서</span>
                <Badge variant={data.freshness.settlement.rows_total ? "success" : "warning"}>
                  {data.freshness.settlement.state === "OK" ? "적재됨" : "미적재"}
                </Badge>
                <span className="text-[11px] text-[--color-muted-foreground]">
                  {data.freshness.settlement.rows_total}행 · 최근 정산기간{" "}
                  {data.freshness.settlement.latest_settlement_date ?? "—"}
                </span>
              </li>
            </ul>
          </Section>

          {/* ── 대사 ─────────────────────────────────────────── */}
          <Section title="전체 대사" description="원천과 파생이 맞는지">
            <ul className="space-y-1.5">
              {Object.entries(data.verdicts).map(([key, verdict]) => (
                <li
                  key={key}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[--color-border] p-2"
                >
                  <span className="text-[13px]">{VERDICT_ROW_LABEL[key] ?? key}</span>
                  <VerdictBadge verdict={verdict} />
                </li>
              ))}
            </ul>

            <dl className="mt-3 grid gap-2 text-[11px] sm:grid-cols-2">
              <div className="rounded-md bg-[--color-muted] p-2">
                <dt className="font-medium">정산 금액 대사</dt>
                <dd className="mt-0.5 text-[--color-muted-foreground]">
                  주문 {data.settlement.orders_checked}건 확인 · 차이 0원{" "}
                  {data.settlement.amount_verified}건 · 불일치{" "}
                  {data.settlement.amount_failure_count}건
                  {data.settlement.refund_excluded > 0 && (
                    <> · 환불 {data.settlement.refund_excluded}건은 표본이 없어 제외</>
                  )}
                </dd>
                <dd className="mt-1 font-mono text-[10px] opacity-70">
                  {data.settlement.formula}
                </dd>
              </div>
              <div className="rounded-md bg-[--color-muted] p-2">
                <dt className="font-medium">수집 대사</dt>
                <dd className="mt-0.5 text-[--color-muted-foreground]">
                  실행 {data.sync.runs_checked}건 확인 · 불일치{" "}
                  {data.sync.reconciliation_failures}건
                  {data.sync.legacy_unclassified_runs > 0 && (
                    <> · 과거 기록 {data.sync.legacy_unclassified_runs}건은 판정 제외</>
                  )}
                </dd>
                {data.sync.legacy_reason && (
                  <dd className="mt-1 text-[10px] opacity-70">{data.sync.legacy_reason}</dd>
                )}
              </div>
            </dl>
          </Section>

          {/* ── 매출 coverage ────────────────────────────────── */}
          <Section
            title="매출 확인 범위"
            description="정산서가 아직 안 온 주문이 있는 것은 오류가 아닙니다 — 얼마나 확인됐는지를 봅니다"
          >
            <div className="grid gap-2 sm:grid-cols-3">
              <div className="rounded-md border border-[--color-border] p-2.5">
                <p className="text-[11px] text-[--color-muted-foreground]">장부 매출</p>
                <p className="mt-0.5 text-lg font-semibold tabular-nums">
                  {money(data.revenue_split.ledger_revenue)}
                </p>
                <p className="text-[10px] text-[--color-muted-foreground]">
                  {data.revenue_split.orders}건
                </p>
              </div>
              <div className="rounded-md border border-[--color-border] p-2.5">
                <p className="text-[11px] text-[--color-muted-foreground]">실정산 확인분</p>
                <p className="mt-0.5 text-lg font-semibold tabular-nums">
                  {money(data.revenue_split.settled_ledger_revenue)}
                </p>
                <p className="text-[10px] text-[--color-muted-foreground]">
                  {data.revenue_split.settled_orders}건 · 정산서 기준{" "}
                  {money(data.revenue_split.settlement_net_revenue)}
                </p>
              </div>
              <div className="rounded-md border border-[--color-border] p-2.5">
                <p className="text-[11px] text-[--color-muted-foreground]">정책 추정분</p>
                <p className="mt-0.5 text-lg font-semibold tabular-nums">
                  {money(data.revenue_split.estimated_revenue)}
                </p>
                <p className="text-[10px] text-[--color-muted-foreground]">
                  {data.revenue_split.estimated_orders}건 · 정산서 대기
                </p>
              </div>
            </div>
            {data.revenue_split.settled_difference !== 0 && (
              <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
                정산 확인분에서 정산서와 장부가{" "}
                {money(data.revenue_split.settled_difference)} 다릅니다 — 배송비 매출과
                판매자 쿠폰 때문이며, 어느 쪽을 쓸지는 이미 정해져 있습니다(D1·D2).
                <strong> 판매 원본은 수정하지 않습니다.</strong>
              </p>
            )}
          </Section>

          {/* ── 신뢰도 ───────────────────────────────────────── */}
          <Section
            title="마진 신뢰도"
            description="한 주문은 한 칸에만 들어갑니다 — 합이 전체와 같아야 합니다"
          >
            <div className="grid gap-2 sm:grid-cols-4">
              {[
                { label: "실측 (정산 기준)", n: data.confidence.actual, tone: "success" as const },
                { label: "추정 (정책 기준)", n: data.confidence.estimated, tone: "info" as const },
                { label: "미확인", n: data.confidence.unknown, tone: "warning" as const },
                { label: "미계산", n: data.confidence.no_margin_row, tone: "warning" as const },
              ].map((c) => (
                <div key={c.label} className="rounded-md border border-[--color-border] p-2.5">
                  <p className="text-[11px] text-[--color-muted-foreground]">{c.label}</p>
                  <p className="mt-0.5 text-lg font-semibold tabular-nums">{c.n}</p>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
              전체 주문 {data.confidence.orders_total}건
              {!data.confidence.buckets_sum_matches_total && (
                <span className="text-[--color-danger]"> · 분류 합계가 전체와 다릅니다</span>
              )}
            </p>
            {data.confidence.without_calculation_evidence > 0 && (
              <p className="mt-2 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
                {data.confidence.without_calculation_evidence}건은 계산 근거가 박제돼 있지 않아
                &ldquo;왜 이 숫자인가&rdquo;를 되짚을 수 없습니다. 재계산하면 이후 계산부터
                근거가 남습니다 — <strong>자동으로 재계산하지 않습니다.</strong>
              </p>
            )}
          </Section>

          {/* ── 확인 필요 ────────────────────────────────────── */}
          <Section title="확인 필요" description="정상 건은 나열하지 않습니다">
            {!exceptions ? (
              <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
            ) : exceptions.verification.length === 0 ? (
              <p className="flex items-center gap-1.5 text-sm">
                <CheckCircle2 className="h-4 w-4 text-[--color-success]" aria-hidden />
                대사에서 확인할 항목이 없습니다.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {exceptions.verification.map((item) => (
                  <li
                    key={item.code}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[--color-border] p-2"
                  >
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium">{item.detail}</p>
                      <p className="font-mono text-[10px] text-[--color-muted-foreground]">
                        {item.code}
                      </p>
                    </div>
                    <Badge variant={item.severity === "BLOCKING" ? "danger" : "warning"}>
                      {item.count}건
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-[11px]">
              <Link href={"/operations/exceptions" as Route} className="underline">
                예외 센터에서 개별 건 보기
              </Link>
            </p>
          </Section>

          {/* ── 아직 검증할 수 없는 영역 ─────────────────────── */}
          <Section
            title="아직 검증할 수 없는 것"
            description="숨기지 않습니다 — 여기 있는 것은 '맞다'가 아니라 '모른다'입니다"
          >
            <ul className="space-y-1.5 text-[12px]">
              {Object.entries(data.evidence_gates).map(([key, gate]) => (
                <li
                  key={key}
                  className="flex flex-wrap items-start justify-between gap-2 rounded-md border border-[--color-border] p-2"
                >
                  <div className="min-w-0">
                    <p className="font-medium">{GATE_LABEL[key] ?? key}</p>
                    {gate.reason && (
                      <p className="text-[11px] text-[--color-muted-foreground]">{gate.reason}</p>
                    )}
                  </div>
                  <VerdictBadge verdict={gate.state} />
                </li>
              ))}
            </ul>
          </Section>

          {/* ── Golden ───────────────────────────────────────── */}
          <GoldenSection
            golden={golden}
            canDecide={canDecide}
            onReview={(salesOrderId, caseId, caseType) =>
              setReview({ salesOrderId, caseId, caseType })
            }
          />
        </div>
      )}

      {openOrder && (
        <OrderTraceDialog salesOrderId={openOrder} onClose={() => setOpenOrder(null)} />
      )}

      {review && (
        <GoldenReview
          salesOrderId={review.salesOrderId}
          caseId={review.caseId}
          caseType={review.caseType}
          canDecide={canDecide}
          onClose={() => setReview(null)}
        />
      )}
    </PageShell>
  );
}

/** 사람이 직접 확인할 소수의 사례. 자동 승인은 없다. */
function GoldenSection({
  golden,
  onReview,
  canDecide,
}: {
  golden:
    | {
        cases: import("@/lib/api/verification").GoldenCase[];
        regression: import("@/lib/api/verification").GoldenRegression;
        coverage: import("@/lib/api/verification").GoldenCoverage;
      }
    | undefined;
  onReview: (salesOrderId: string, caseId: string | null, caseType: string) => void;
  canDecide: boolean;
}) {
  const qc = useQueryClient();
  const { data: recommended } = useQuery({
    queryKey: ["verification-golden-recommended"],
    queryFn: () => verificationApi.goldenRecommended(),
    staleTime: 60_000,
  });

  const register = useMutation({
    mutationFn: (ids: string[]) => verificationApi.registerCandidates(ids),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["verification-golden"] });
      qc.invalidateQueries({ queryKey: ["verification-golden-recommended"] });
      toast.success(`${r.created}건을 검토 목록에 담았습니다.`);
    },
    onError: (e) =>
      toast.error(e instanceof ApiError ? e.message : "등록에 실패했습니다."),
  });

  const reg = golden?.regression;
  const cases = golden?.cases ?? [];
  const approved = cases.filter((c) => c.status === "APPROVED");
  const deferred = cases.filter((c) => c.status === "DEFERRED");
  /** 등록된 사례를 주문 기준으로 찾는다 — 후보 목록과 이어 준다. */
  const caseByOrder = new Map(cases.map((c) => [c.sales_order_id, c]));

  return (
    <Section
      title="기준 사례 (Golden Sample)"
      description="원천과 독립 검산을 한 화면에 모아 둡니다 — 엑셀을 열 필요가 없습니다"
    >
      {/* 회귀 상태 */}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[12px]">
        <span>승인 {approved.length}건</span>
        <Badge variant="success">통과 {reg?.counts.PASS ?? 0}</Badge>
        <Badge variant="danger">실패 {reg?.counts.FAIL ?? 0}</Badge>
        <Badge variant="warning">비교불가 {reg?.counts.NOT_COMPARABLE ?? 0}</Badge>
        {deferred.length > 0 && (
          <Badge variant="info">보류 {deferred.length}</Badge>
        )}
      </div>

      {approved.length === 0 && (
        <p className="mb-3 rounded-md bg-[--color-muted] p-2 text-[11px] text-[--color-muted-foreground]">
          아직 승인된 기준값이 없습니다. 기준값은 <strong>사람이 확인한 뒤에만</strong>{" "}
          만들어집니다 — 현재 계산 결과를 자동으로 복사하면 회귀 검사가 항상 통과해
          아무것도 지키지 못합니다.
        </p>
      )}

      {/* 경로별 커버리지 */}
      {golden?.coverage && (
        <div className="mb-3">
          <p className="mb-1 text-[12px] font-medium">
            계산 경로 커버리지 {golden.coverage.covered}/{golden.coverage.of}
          </p>
          <ul className="grid gap-1 sm:grid-cols-2">
            {golden.coverage.by_case_type.map((c) => (
              <li
                key={c.case_type}
                className="flex items-center justify-between gap-2 rounded-md border border-[--color-border] px-2 py-1 text-[11px]"
              >
                <span className="min-w-0">
                  <strong className="font-medium">
                    {CASE_TYPE_LABEL[c.case_type] ?? c.case_type}
                  </strong>
                  <span className="block text-[10px] text-[--color-muted-foreground]">
                    {c.covers}
                  </span>
                </span>
                {c.covered ? (
                  <Badge variant="success">승인 {c.approved}</Badge>
                ) : c.deferred > 0 ? (
                  <Badge variant="info">보류 {c.deferred}</Badge>
                ) : (
                  <Badge variant="warning">미확인</Badge>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 추천 최소 세트 */}
      {recommended && recommended.recommended.length > 0 && (
        <>
          <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
            <p className="text-[12px] font-medium">
              이 {recommended.recommended_count}건을 먼저 확인하면 주요 계산 경로를
              덮습니다{" "}
              <span className="font-normal text-[--color-muted-foreground]">
                (전체 후보 {recommended.total_candidates}건)
              </span>
            </p>
            {canDecide && (
              <Button
                size="sm"
                variant="outline"
                disabled={register.isPending}
                onClick={() =>
                  register.mutate(
                    recommended.recommended.map((r) => r.sales_order_id),
                  )
                }
              >
                추천 {recommended.recommended_count}건 검토 목록에 담기
              </Button>
            )}
          </div>
          <ul className="space-y-1.5">
            {recommended.recommended.map((c) => {
              const existing = caseByOrder.get(c.sales_order_id);
              return (
                <li
                  key={c.sales_order_id}
                  className="rounded-md border border-[--color-border] p-2"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium">
                        {c.business_key}{" "}
                        <span className="text-[--color-muted-foreground]">
                          · {c.marketplace} · {c.sale_date}
                        </span>
                      </p>
                      <p className="text-[11px] text-[--color-muted-foreground]">
                        덮는 경로: {c.covers}
                      </p>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Badge variant="info">
                        {CASE_TYPE_LABEL[c.case_type] ?? c.case_type}
                      </Badge>
                      {existing && (
                        <Badge
                          variant={
                            existing.status === "APPROVED"
                              ? "success"
                              : existing.status === "DEFERRED"
                                ? "info"
                                : "warning"
                          }
                        >
                          {existing.status === "APPROVED"
                            ? "승인됨"
                            : existing.status === "DEFERRED"
                              ? "보류"
                              : "검토 대기"}
                        </Badge>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          onReview(
                            c.sales_order_id,
                            existing?.case_id ?? null,
                            c.case_type,
                          )
                        }
                      >
                        원천 확인
                        <ChevronRight className="ml-0.5 h-3 w-3" aria-hidden />
                      </Button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
          {recommended.uncovered.length > 0 && (
            <p className="mt-2 text-[10px] text-[--color-muted-foreground]">
              실제 데이터에 없어 덮지 못한 경로:{" "}
              {recommended.uncovered
                .map((u) => CASE_TYPE_LABEL[u.case_type] ?? u.case_type)
                .join(", ")}
            </p>
          )}
        </>
      )}

      {/* 회귀 실패 상세 */}
      {reg && (reg.counts.FAIL ?? 0) > 0 && (
        <div className="mt-3 rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-2">
          <p className="text-[12px] font-semibold text-[--color-danger]">
            기준값과 다른 계산 {reg.counts.FAIL}건
          </p>
          <ul className="mt-1 space-y-0.5">
            {reg.cases
              .filter((c) => c.verdict === "FAIL")
              .map((c) => (
                <li key={c.case_id} className="text-[11px] text-[--color-danger]">
                  {c.business_key} —{" "}
                  {c.checks
                    .filter((k) => k.verdict === "FAIL")
                    .map(
                      (k) =>
                        `${k.field} 기준 ${k.expected.toLocaleString("ko-KR")} vs 현재 ${k.actual.toLocaleString("ko-KR")}`,
                    )
                    .join(" · ")}
                </li>
              ))}
          </ul>
          <p className="mt-1 text-[10px] text-[--color-danger]">
            자동으로 고치지 않습니다. 계산·정책·원천 중 무엇이 바뀌었는지 확인하세요.
          </p>
        </div>
      )}
    </Section>
  );
}

/** 주문 하나가 왜 이 숫자인지. */
function OrderTraceDialog({
  salesOrderId,
  onClose,
}: {
  salesOrderId: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["verification-order-trace", salesOrderId],
    queryFn: () => verificationApi.orderTrace(salesOrderId),
  });

  return (
    <div
      role="dialog"
      aria-label="계산 추적"
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-2 sm:items-center"
      onClick={onClose}
    >
      <div
        className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-lg bg-[--color-background] p-4 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">계산 추적</h2>
          <Button size="sm" variant="outline" onClick={onClose}>닫기</Button>
        </div>

        {isLoading || !data ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (
          <TraceBody trace={data} />
        )}
      </div>
    </div>
  );
}

function TraceBody({ trace }: { trace: OrderTrace }) {
  return (
    <div className="space-y-3 text-[12px]">
      <div className="rounded-md bg-[--color-muted] p-2">
        <p className="font-medium">
          {trace.order.business_key} · {trace.order.marketplace} · {trace.order.sale_date}
        </p>
        <p className="text-[11px] text-[--color-muted-foreground]">
          {trace.order.product_name ?? "상품명 없음"} · 수량 {trace.order.quantity}
        </p>
      </div>

      {trace.evidence_kind === "RECONSTRUCTED" && (
        <p className="rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
          이 주문에는 계산 근거가 박제돼 있지 않습니다. 아래 출처는 <strong>지금 원천에서
          되짚은 것</strong>이며 계산 당시와 다를 수 있습니다.
        </p>
      )}

      {!trace.margin_calculated ? (
        <p className="rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
          {trace.reason ?? "실 마진이 아직 계산되지 않았습니다."}
        </p>
      ) : (
        <>
          <table className="w-full border-collapse text-[12px]">
            <caption className="sr-only">성분별 금액과 출처</caption>
            <thead>
              <tr className="border-b border-[--color-border] text-left text-[11px] text-[--color-muted-foreground]">
                <th scope="col" className="py-1">항목</th>
                <th scope="col" className="py-1 text-right">금액</th>
                <th scope="col" className="py-1">출처</th>
                <th scope="col" className="py-1">신뢰도</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(trace.components).map(([key, c]) => (
                <tr key={key} className="border-b border-[--color-border] last:border-0">
                  <td className="py-1">{COMPONENT_LABEL[key] ?? key}</td>
                  <td className="py-1 text-right tabular-nums">
                    {/* 미확인은 금액을 강조하지 않는다 — 0 이 사실이 아니다 */}
                    {c.confidence === "UNKNOWN" && c.value === 0
                      ? <span className="text-[--color-muted-foreground]">확인 불가</span>
                      : money(c.value)}
                  </td>
                  <td className="py-1 text-[11px] text-[--color-muted-foreground]">
                    {SOURCE_LABEL[c.source] ?? c.source}
                  </td>
                  <td className="py-1">
                    <Badge variant={CONFIDENCE_STYLE[c.confidence]}>
                      {CONFIDENCE_LABEL[c.confidence]}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="rounded-md bg-[--color-muted] p-2">
            <p className="text-[11px] font-medium">적용된 산식</p>
            <p className="mt-0.5 font-mono text-[10px] break-all">{trace.formula}</p>
          </div>

          {typeof trace.margin_difference === "number" && (
            <p
              className={cn(
                "rounded-md p-2 text-[11px]",
                trace.margin_difference === 0
                  ? "bg-[--color-success-bg] text-[--color-success]"
                  : "bg-[--color-danger-bg] text-[--color-danger]",
              )}
            >
              저장된 마진 {money(Number(trace.stored_amounts?.margin_amount ?? 0))} · 성분
              재계산 {money(trace.recomputed_margin ?? 0)} · 차이{" "}
              {money(trace.margin_difference)}
            </p>
          )}

          {trace.warnings && trace.warnings.length > 0 && (
            <ul className="space-y-1">
              {trace.warnings.map((w) => (
                <li
                  key={w}
                  className="flex items-start gap-1.5 rounded-md bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]"
                >
                  <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                  <span>{w}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <div>
        <p className="text-[11px] font-medium">데이터 흐름</p>
        <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
          {trace.lineage.join(" → ")}
        </p>
      </div>
    </div>
  );
}
