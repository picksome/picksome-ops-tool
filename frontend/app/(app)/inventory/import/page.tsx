"use client";

/**
 * 재고 가져오기.
 *
 * 왜 이 화면이 필요한가
 * -------------------
 * 상품이 1,000개가 넘는데 초기 재고를 한 건씩 넣게 두면 아무도 재고 기능을
 * 쓰지 않는다. 재고가 없으면 재주문도 못 돌고, 결국 전부 장식이 된다.
 *
 * 검토 없이 적용하지 않는다
 * ---------------------
 * 파일을 고르면 **검토(DRY_RUN)까지만** 자동으로 돈다. 적용 버튼은 결과를
 * 본 뒤에야 눌린다 — 1,000행이 되돌릴 수 없이 들어가는 경로를 만들지 않는다.
 *
 * 문제 있는 행을 접어 두지 않는다
 * ---------------------------
 * "성공 975건" 만 보여주면 사용자는 나머지 25건을 영영 안 본다. 그 25건이
 * 나중에 "재고가 왜 없지" 가 된다. 문제부터 위에 놓는다.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, FileUp, Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageShell, Section } from "@/components/shell/page-shell";
import {
  OUTCOME_HELP,
  OUTCOME_LABEL,
  inventoryImportApi,
  type ImportPlan,
  type RowOutcome,
} from "@/lib/api/inventory-import";

/** 사람이 손봐야 하는 결과. 이 셋은 반영되지 않는다. */
const PROBLEM_OUTCOMES: RowOutcome[] = ["UNMATCHED", "AMBIGUOUS", "INVALID"];

export default function InventoryImportPage() {
  const [file, setFile] = React.useState<File | null>(null);
  const [asOf, setAsOf] = React.useState("");
  const [plan, setPlan] = React.useState<ImportPlan | null>(null);
  const [applied, setApplied] = React.useState<ImportPlan | null>(null);

  const template = useQuery({
    queryKey: ["inventory", "import", "template"],
    queryFn: () => inventoryImportApi.template(),
  });

  const runs = useQuery({
    queryKey: ["inventory", "import", "runs"],
    queryFn: () => inventoryImportApi.runs(),
  });

  const dryRun = useMutation({
    mutationFn: () => inventoryImportApi.run(file!, "DRY_RUN", asOf || undefined),
    onSuccess: (data) => {
      setPlan(data);
      setApplied(null);
    },
  });

  const apply = useMutation({
    mutationFn: () => inventoryImportApi.run(file!, "APPLY", asOf || undefined),
    onSuccess: (data) => {
      setApplied(data);
      setPlan(data);
      void runs.refetch();
    },
  });

  function pickFile(f: File | null) {
    setFile(f);
    setPlan(null);
    setApplied(null);
  }

  const counts = plan?.counts;
  const willChange = (counts?.OPENING ?? 0) + (counts?.ADJUSTMENT ?? 0);
  const problemCount = PROBLEM_OUTCOMES.reduce(
    (sum, k) => sum + (counts?.[k] ?? 0),
    0,
  );

  return (
    <PageShell title="재고 가져오기" variant="detail">
      {/* ── 파일 선택 ────────────────────────────────────────── */}
      <Section title="파일">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="mb-1 block text-[--color-muted-foreground]">
              CSV 또는 XLSX
            </span>
            <Input
              type="file"
              accept=".csv,.xlsx,.xlsm"
              onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
              className="w-80"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-[--color-muted-foreground]">
              기준일 (실사한 날)
            </span>
            <Input
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              className="w-44"
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
                  <th className="py-1 pr-3 font-medium">필수</th>
                  <th className="py-1 pr-3 font-medium">이렇게 써도 됩니다</th>
                  <th className="py-1 font-medium">설명</th>
                </tr>
              </thead>
              <tbody>
                {template.data.columns.map((c) => (
                  <tr key={c.field} className="border-b border-[--color-border] last:border-0">
                    <td className="py-1 pr-3">{c.label}</td>
                    <td className="py-1 pr-3">
                      {c.required ? (
                        <Badge variant="warning">필수</Badge>
                      ) : (
                        <span className="text-[--color-muted-foreground]">—</span>
                      )}
                    </td>
                    <td className="py-1 pr-3 text-xs text-[--color-muted-foreground]">
                      {c.aliases.join(", ")}
                    </td>
                    <td className="py-1 text-xs text-[--color-muted-foreground]">
                      {c.note}
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

      {/* ── 파일 자체의 문제 ─────────────────────────────────── */}
      {plan && plan.file_problems.length > 0 && (
        <Section title="파일을 읽을 수 없습니다">
          <ul className="space-y-1 text-sm text-[--color-destructive]">
            {plan.file_problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </Section>
      )}

      {/* ── 검토 결과 ────────────────────────────────────────── */}
      {plan && plan.file_problems.length === 0 && (
        <>
          <Section
            title={applied ? "적용 결과" : `검토 결과 (${plan.rows_total}행)`}
          >
            <div className="mb-3 flex flex-wrap gap-2">
              {(Object.keys(OUTCOME_LABEL) as RowOutcome[])
                .filter((k) => (counts?.[k] ?? 0) > 0)
                .map((k) => (
                  <span
                    key={k}
                    title={OUTCOME_HELP[k]}
                    className="rounded-md border border-[--color-border] px-3 py-2 text-sm"
                  >
                    <span className="block">{OUTCOME_LABEL[k]}</span>
                    <span className="block tabular-nums font-medium">
                      {counts![k]}
                    </span>
                  </span>
                ))}
            </div>

            {applied ? (
              <p className="flex items-center gap-2 text-sm">
                <CheckCircle2 className="size-4" />
                원장에 {applied.applied ?? 0}건을 기록했습니다.
                {problemCount > 0 && (
                  <span className="text-[--color-muted-foreground]">
                    {" "}
                    · 손봐야 할 {problemCount}건은 반영되지 않았습니다.
                  </span>
                )}
              </p>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  disabled={willChange === 0 || apply.isPending}
                  onClick={() => apply.mutate()}
                >
                  {apply.isPending
                    ? "적용 중…"
                    : `${willChange}건 적용`}
                </Button>
                <span className="text-xs text-[--color-muted-foreground]">
                  {willChange === 0
                    ? "원장에 기록할 변화가 없습니다."
                    : "적용하면 원장에 기록됩니다. 되돌리려면 재고 조정을 써야 합니다."}
                </span>
              </div>
            )}
            {apply.isError && (
              <p className="mt-2 text-sm text-[--color-destructive]">
                {(apply.error as Error).message}
              </p>
            )}
          </Section>

          {/* 문제부터 위에. 접어 두지 않는다. */}
          {plan.problems.length > 0 && (
            <Section title={`손봐야 할 행 ${problemCount}건`}>
              <p className="mb-2 flex items-start gap-2 text-sm text-[--color-muted-foreground]">
                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                <span>
                  아래 행은 <strong>반영되지 않습니다.</strong> 상품 이름으로는
                  찾지 않습니다 — 비슷한 이름을 잘못 맞추면 엉뚱한 상품의 재고가
                  바뀌기 때문입니다.
                </span>
              </p>
              <RowTable rows={plan.problems} />
            </Section>
          )}

          {plan.changes.length > 0 && !applied && (
            <Section title={`반영될 행 ${willChange}건`}>
              <RowTable rows={plan.changes} />
              {plan.changes.length < willChange && (
                <p className="mt-2 text-xs text-[--color-muted-foreground]">
                  앞 {plan.changes.length}건만 표시했습니다.
                </p>
              )}
            </Section>
          )}
        </>
      )}

      {/* ── 이력 ─────────────────────────────────────────────── */}
      {(runs.data?.length ?? 0) > 0 && (
        <Section title="지난 가져오기">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-[--color-muted-foreground]">
                <tr className="border-b border-[--color-border]">
                  <th className="py-1 pr-3 font-medium">일시</th>
                  <th className="py-1 pr-3 font-medium">파일</th>
                  <th className="py-1 pr-3 text-right font-medium">최초</th>
                  <th className="py-1 pr-3 text-right font-medium">조정</th>
                  <th className="py-1 pr-3 text-right font-medium">변화없음</th>
                  <th className="py-1 pr-3 text-right font-medium">미반영</th>
                  <th className="py-1 font-medium">실행자</th>
                </tr>
              </thead>
              <tbody>
                {runs.data!.map((r) => (
                  <tr key={r.id} className="border-b border-[--color-border] last:border-0">
                    <td className="py-1 pr-3 text-xs">
                      {r.started_at.slice(0, 16).replace("T", " ")}
                    </td>
                    <td className="max-w-0 truncate py-1 pr-3">{r.source_name}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{r.opening}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{r.adjustment}</td>
                    <td className="py-1 pr-3 text-right tabular-nums text-[--color-muted-foreground]">
                      {r.unchanged}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {r.unmatched + r.ambiguous + r.invalid || "—"}
                    </td>
                    <td className="py-1 text-xs text-[--color-muted-foreground]">
                      {r.actor_name}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </PageShell>
  );
}

function RowTable({ rows }: { rows: ImportPlan["problems"] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-[--color-muted-foreground]">
          <tr className="border-b border-[--color-border]">
            <th className="py-1 pr-3 font-medium">행</th>
            <th className="py-1 pr-3 font-medium">결과</th>
            <th className="py-1 pr-3 font-medium">열쇠</th>
            <th className="py-1 pr-3 font-medium">상품</th>
            <th className="py-1 pr-3 text-right font-medium">현재</th>
            <th className="py-1 pr-3 text-right font-medium">실사</th>
            <th className="py-1 pr-3 text-right font-medium">증감</th>
            <th className="py-1 font-medium">설명</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.line_no} className="border-b border-[--color-border] last:border-0">
              <td className="py-1 pr-3 tabular-nums text-xs">{r.line_no}</td>
              <td className="py-1 pr-3">
                <Badge
                  variant={
                    PROBLEM_OUTCOMES.includes(r.outcome) ? "warning" : "info"
                  }
                >
                  {OUTCOME_LABEL[r.outcome]}
                </Badge>
              </td>
              <td className="py-1 pr-3 font-mono text-xs">{r.identity ?? "—"}</td>
              <td className="max-w-0 truncate py-1 pr-3">
                {r.product_name ?? "—"}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums text-[--color-muted-foreground]">
                {/* 원장이 없던 상품은 «—». 0 이 아니다. */}
                {r.current ?? "—"}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {r.counted ?? "—"}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums font-medium">
                {r.delta === null || r.delta === undefined
                  ? "—"
                  : `${r.delta > 0 ? "+" : ""}${r.delta}`}
              </td>
              <td className="py-1 text-xs text-[--color-muted-foreground]">
                {r.message ?? OUTCOME_HELP[r.outcome]}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
