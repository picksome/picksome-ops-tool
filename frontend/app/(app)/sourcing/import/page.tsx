"use client";

/**
 * 상품↔공급처 연결 가져오기.
 *
 * 왜 이 화면이 지금 가장 급한가
 * --------------------------
 * 재고·발주·재주문이 전부 만들어져 있는데 `product_source` 가 0건이라 재주문이
 * 통째로 `NO_SOURCE` 로 막혀 있다. 상품 1,065개를 한 건씩 연결하게 두면
 * 아무도 안 한다.
 *
 * 완성도를 먼저 보여준다
 * -------------------
 * "재주문이 왜 안 되지" 의 답이 이 숫자들이다. 가져오기 화면에 함께 두는 이유는
 * 무엇을 채워야 하는지 보고 나서 파일을 만들게 하려는 것이다.
 *
 * 이름으로 연결하지 않는다
 * ---------------------
 * 잘못 연결하면 **틀린 공급처의 원가로 발주가 나간다.** 못 찾은 채로 두는 편이
 * 훨씬 안전하고, 화면은 그 사실을 숨기지 않는다.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, FileUp, Info, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  SOURCE_OUTCOME_HELP,
  SOURCE_OUTCOME_LABEL,
  sourcingImportApi,
  type SourceImportPlan,
  type SourceOutcome,
} from "@/lib/api/sourcing-import";
import { formatKRW } from "@/lib/format";

const PROBLEM: SourceOutcome[] = [
  "CONFLICT_PRIMARY", "UNMATCHED_PRODUCT", "UNMATCHED_SUPPLIER", "INVALID",
];

export default function SourcingImportPage() {
  const [file, setFile] = React.useState<File | null>(null);
  const [plan, setPlan] = React.useState<SourceImportPlan | null>(null);
  const [applied, setApplied] = React.useState<SourceImportPlan | null>(null);

  const completeness = useQuery({
    queryKey: ["sourcing", "completeness"],
    queryFn: () => sourcingImportApi.completeness(),
  });

  const template = useQuery({
    queryKey: ["sourcing", "import", "template"],
    queryFn: () => sourcingImportApi.template(),
  });

  const dryRun = useMutation({
    mutationFn: () => sourcingImportApi.run(file!, "DRY_RUN"),
    onSuccess: (d) => {
      setPlan(d);
      setApplied(null);
    },
  });

  const apply = useMutation({
    mutationFn: () => sourcingImportApi.run(file!, "APPLY"),
    onSuccess: (d) => {
      setApplied(d);
      setPlan(d);
      void completeness.refetch();
    },
  });

  const c = completeness.data;
  const counts = plan?.counts;
  const willChange =
    (counts?.NEW_MAPPING ?? 0) + (counts?.UPDATE_METADATA ?? 0);
  const problemCount = PROBLEM.reduce((s, k) => s + (counts?.[k] ?? 0), 0);

  return (
    <PageShell title="소싱 연결 가져오기" variant="detail">
      {/* ── 완성도 ───────────────────────────────────────────── */}
      <Section title="소싱 정보 완성도">
        {!c ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              <Stat label="상품" value={c.products} />
              <Stat label="소싱처 연결됨" value={c.with_source} good />
              <Stat label="소싱처 없음" value={c.without_source} bad
                    hint="재주문이 NO_SOURCE 로 막힙니다" />
              <Stat label="주 소싱처 없음" value={c.without_primary} bad
                    hint="어느 곳으로 발주할지 정해지지 않았습니다" />
              <Stat label="리드타임 없음" value={c.without_lead_time} bad
                    hint="발주 시점을 계산할 수 없습니다" />
              <Stat label="MOQ 없음" value={c.without_moq} />
              <Stat label="원가 없는 소싱처" value={c.sources_without_cost} />
            </div>
            {c.without_source > 0 && (
              <p className="mt-3 text-sm text-[--color-muted-foreground]">
                상품 {c.without_source}개에 소싱처가 없어 재주문 계산이 되지
                않습니다. 아래에서 연결 파일을 올리세요.
              </p>
            )}
          </>
        )}
      </Section>

      {/* ── 파일 ─────────────────────────────────────────────── */}
      <Section title="파일">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="mb-1 block text-[--color-muted-foreground]">
              CSV 또는 XLSX
            </span>
            <Input
              type="file"
              accept=".csv,.xlsx,.xlsm"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setPlan(null);
                setApplied(null);
              }}
              className="w-80"
            />
          </label>
          <Button
            disabled={!file || dryRun.isPending}
            onClick={() => dryRun.mutate()}
            className="gap-1"
          >
            <FileUp className="size-4" />
            {dryRun.isPending ? "검토 중…" : "검토"}
          </Button>
        </div>
        {dryRun.isError && (
          <p className="mt-2 text-sm text-[--color-destructive]">
            {(dryRun.error as Error).message}
          </p>
        )}
      </Section>

      {/* ── 형식 안내 ────────────────────────────────────────── */}
      {!plan && template.data && (
        <Section title="파일 형식">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-[--color-muted-foreground]">
                <tr className="border-b border-[--color-border]">
                  <th className="py-1 pr-3 font-medium">열</th>
                  <th className="py-1 pr-3 font-medium">이렇게 써도 됩니다</th>
                  <th className="py-1 font-medium">설명</th>
                </tr>
              </thead>
              <tbody>
                {template.data.columns.map((col) => (
                  <tr key={col.field} className="border-b border-[--color-border] last:border-0">
                    <td className="py-1 pr-3 whitespace-nowrap">{col.label}</td>
                    <td className="py-1 pr-3 text-xs text-[--color-muted-foreground]">
                      {col.aliases.join(", ")}
                    </td>
                    <td className="py-1 text-xs text-[--color-muted-foreground]">
                      {col.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="mt-3 space-y-1 text-xs text-[--color-muted-foreground]">
            {template.data.notes.map((n) => (
              <li key={n} className="flex gap-2">
                <Info className="mt-0.5 size-3 shrink-0" />
                <span>{n}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {plan && plan.file_problems.length > 0 && (
        <Section title="파일을 읽을 수 없습니다">
          <ul className="space-y-1 text-sm text-[--color-destructive]">
            {plan.file_problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        </Section>
      )}

      {/* ── 결과 ─────────────────────────────────────────────── */}
      {plan && plan.file_problems.length === 0 && (
        <>
          <Section title={applied ? "적용 결과" : `검토 결과 (${plan.rows_total}행)`}>
            {plan.credential_detected && (
              <p className="mb-3 flex items-center gap-2 text-sm">
                <ShieldAlert className="size-4" />
                ID·비밀번호로 보이는 열 {plan.credential_headers_skipped}개를
                <strong>읽지 않고 버렸습니다.</strong>
              </p>
            )}
            <div className="mb-3 flex flex-wrap gap-2">
              {(Object.keys(SOURCE_OUTCOME_LABEL) as SourceOutcome[])
                .filter((k) => (counts?.[k] ?? 0) > 0)
                .map((k) => (
                  <span
                    key={k}
                    title={SOURCE_OUTCOME_HELP[k]}
                    className="rounded-md border border-[--color-border] px-3 py-2 text-sm"
                  >
                    <span className="block">{SOURCE_OUTCOME_LABEL[k]}</span>
                    <span className="block font-medium tabular-nums">
                      {counts![k]}
                    </span>
                  </span>
                ))}
            </div>
            <p className="mb-3 text-xs text-[--color-muted-foreground]">
              리드타임 {plan.lead_time_available}건 · MOQ {plan.moq_available}건 ·
              매입가 {plan.source_cost_candidates}건이 파일에 있습니다.
            </p>

            {applied ? (
              <p className="flex flex-wrap items-center gap-2 text-sm">
                <CheckCircle2 className="size-4" />
                새 연결 {applied.created ?? 0}건 · 정보 채움 {applied.updated ?? 0}건 ·
                원가 버전 {applied.cost_versions ?? 0}건.
                <span className="text-[--color-muted-foreground]">
                  상품에 적용된 원가는 바뀌지 않았습니다.
                </span>
              </p>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  disabled={willChange === 0 || apply.isPending}
                  onClick={() => apply.mutate()}
                >
                  {apply.isPending ? "적용 중…" : `${willChange}건 적용`}
                </Button>
                <span className="text-xs text-[--color-muted-foreground]">
                  {willChange === 0
                    ? "반영할 변화가 없습니다."
                    : "매입가는 새 원가 버전으로만 쌓입니다."}
                </span>
              </div>
            )}
            {apply.isError && (
              <p className="mt-2 text-sm text-[--color-destructive]">
                {(apply.error as Error).message}
              </p>
            )}
          </Section>

          {plan.problems.length > 0 && (
            <Section title={`손봐야 할 행 ${problemCount}건`}>
              <p className="mb-2 flex items-start gap-2 text-sm text-[--color-muted-foreground]">
                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                <span>
                  아래 행은 <strong>반영되지 않습니다.</strong> 이름으로는 찾지
                  않습니다 — 잘못 연결하면 틀린 공급처의 원가로 발주가 나갑니다.
                </span>
              </p>
              <RowTable rows={plan.problems} />
            </Section>
          )}

          {plan.changes.length > 0 && !applied && (
            <Section title={`반영될 행 ${willChange}건`}>
              <RowTable rows={plan.changes} />
            </Section>
          )}
        </>
      )}
    </PageShell>
  );
}

function Stat({
  label, value, good, bad, hint,
}: {
  label: string;
  value: number;
  good?: boolean;
  bad?: boolean;
  hint?: string;
}) {
  return (
    <span
      title={hint}
      className="rounded-md border border-[--color-border] px-3 py-2 text-sm"
    >
      <span className="block text-xs text-[--color-muted-foreground]">
        {label}
      </span>
      <span
        className={`block tabular-nums font-medium ${
          bad && value > 0
            ? "text-[--color-destructive]"
            : good && value > 0
              ? "text-[--color-success]"
              : ""
        }`}
      >
        {value}
      </span>
    </span>
  );
}

function RowTable({ rows }: { rows: SourceImportPlan["problems"] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-[--color-muted-foreground]">
          <tr className="border-b border-[--color-border]">
            <th className="py-1 pr-3 font-medium">행</th>
            <th className="py-1 pr-3 font-medium">결과</th>
            <th className="py-1 pr-3 font-medium">상품</th>
            <th className="py-1 pr-3 font-medium">공급처</th>
            <th className="py-1 pr-3 text-right font-medium">리드타임</th>
            <th className="py-1 pr-3 text-right font-medium">MOQ</th>
            <th className="py-1 pr-3 text-right font-medium">매입가</th>
            <th className="py-1 font-medium">설명</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.line_no} className="border-b border-[--color-border] last:border-0">
              <td className="py-1 pr-3 text-xs tabular-nums">{r.line_no}</td>
              <td className="py-1 pr-3">
                <Badge variant={PROBLEM.includes(r.outcome) ? "warning" : "info"}>
                  {SOURCE_OUTCOME_LABEL[r.outcome]}
                </Badge>
              </td>
              <td className="max-w-0 truncate py-1 pr-3">{r.product_name ?? "—"}</td>
              <td className="max-w-0 truncate py-1 pr-3">
                {r.supplier_name ?? "—"}
                {r.is_primary && (
                  <span className="ml-1 text-xs text-[--color-muted-foreground]">
                    (주)
                  </span>
                )}
              </td>
              {/* 없으면 «—». 0 이 아니다 — 리드타임 0일은 다른 뜻이다. */}
              <td className="py-1 pr-3 text-right tabular-nums">
                {r.lead_time_days ?? "—"}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">{r.moq ?? "—"}</td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {r.purchase_price === null ? "—" : formatKRW(r.purchase_price)}
              </td>
              <td className="py-1 text-xs text-[--color-muted-foreground]">
                {r.message ?? SOURCE_OUTCOME_HELP[r.outcome]}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
