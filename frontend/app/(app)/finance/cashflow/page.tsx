"use client";

/**
 * 현금흐름 (Phase 6, docs/54_cashflow_semantics.md).
 *
 * 이 화면이 답하는 질문은 하나다: **지금 통장에 얼마가 들어오고 나가는가.**
 * 매출·순이익은 여기 없다 — 그건 대시보드와 월 마감이 답한다.
 * 팔렸다고 통장에 들어온 게 아니다.
 *
 * 화면이 지키는 것
 * -------------
 * · **"실제" 와 "확정" 을 섞지 않는다.** 판매처 정산서는 `settled_at` 이라는
 *   이름에도 불구하고 실제 입금 근거가 아니다. 확정으로만 표시하고 그 이유를 적는다
 * · **기준 잔액이 없으면 잔액 자리에 0원을 쓰지 않는다.** 0원은 "통장이 비었다" 로
 *   읽힌다. 계산할 수 없다는 사실을 그대로 말한다
 * · **모든 줄이 원본으로 이어진다.** "이 300만원이 뭐지" 에 답할 수 없으면
 *   화면 전체가 신뢰를 잃는다
 * · **은행 실잔액이라고 부르지 않는다.** 은행 연동이 없다
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { toast } from "sonner";
import type { Route } from "next";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Info,
  Landmark,
  Plus,
  TriangleAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MoneyText } from "@/components/ui/finance";
import { ApiError } from "@/lib/api";
import {
  CASH_SOURCE_LABEL,
  CASH_STATUS_HINT,
  CASH_STATUS_LABEL,
  cashflowApi,
  type CashDirection,
  type CashRow,
  type CashStatus,
  type CashflowSummary,
} from "@/lib/api/cashflow";

const PRESETS = [
  { key: "30d", label: "30일" },
  { key: "60d", label: "60일" },
  { key: "90d", label: "90일" },
] as const;

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다.`;
  // 409 는 같은 원본을 두 번 넣으려 한 경우다 — 서버 메시지가 이유를 설명한다.
  if (err.status === 409) return err.message || "이미 기록된 항목입니다.";
  return err.message || `${action}에 실패했습니다.`;
}

export default function CashflowPage() {
  const qc = useQueryClient();
  const [preset, setPreset] = useState<string>("30d");
  const [baselineOpen, setBaselineOpen] = useState(false);
  const [eventOpen, setEventOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["cashflow", preset],
    queryFn: () => cashflowApi.summary({ preset }),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["cashflow"] });

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">현금흐름</h1>
          <p className="text-sm text-[--color-muted-foreground]">
            통장에 들어오고 나가는 돈입니다. 매출·순이익과는 다릅니다 — 팔린 날과
            입금되는 날은 같지 않습니다.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-md border border-[--color-border]">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => setPreset(p.key)}
                aria-pressed={preset === p.key}
                className={`px-3 py-1.5 text-xs first:rounded-l-md last:rounded-r-md ${
                  preset === p.key
                    ? "bg-[--color-surface-muted] font-medium"
                    : "text-[--color-muted-foreground] hover:bg-[--color-surface-muted]"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <Button size="sm" variant="outline" onClick={() => setBaselineOpen(true)}>
            <Landmark className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            기준 잔액
          </Button>
          <Button size="sm" onClick={() => setEventOpen(true)}>
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            입출금 기록
          </Button>
        </div>
      </div>

      {isLoading ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중...</p>
      ) : !data ? (
        <p className="text-sm text-[--color-muted-foreground]">
          현금흐름을 불러오지 못했습니다.
        </p>
      ) : (
        <>
          <BalanceCards summary={data} onSetBaseline={() => setBaselineOpen(true)} />
          <SourceNotice summary={data} />
          <Timeline summary={data} />
          <RowTable rows={data.rows} onChanged={invalidate} />
        </>
      )}

      <BaselineDialog
        open={baselineOpen}
        onOpenChange={setBaselineOpen}
        current={data?.baseline ?? null}
        onSaved={invalidate}
      />
      <ManualEventDialog
        open={eventOpen}
        onOpenChange={setEventOpen}
        onSaved={invalidate}
      />
    </div>
  );
}

/* ──────────────────────────────────────────────────────────────
 * 잔액 · 합계
 * ────────────────────────────────────────────────────────────── */

function BalanceCards({
  summary,
  onSetBaseline,
}: {
  summary: CashflowSummary;
  onSetBaseline: () => void;
}) {
  const inTotal =
    summary.inflow.expected + summary.inflow.confirmed + summary.inflow.actual;
  const outTotal =
    summary.outflow.expected + summary.outflow.confirmed + summary.outflow.actual;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {/* 잔액 — 계산할 수 없으면 0원 대신 그 사실을 말한다. */}
      <div className="rounded-lg border border-[--color-border] p-4">
        <p className="text-xs text-[--color-muted-foreground]">
          예상 잔액 <span className="text-[10px]">(기간 말)</span>
        </p>
        {summary.balance_available ? (
          <>
            <p className="mt-1 text-xl font-semibold">
              <MoneyText value={summary.projected_ending_balance} colored />
            </p>
            <p className="mt-1 text-xs text-[--color-muted-foreground]">
              기준 잔액 {summary.baseline?.as_of_date}{" "}
              <MoneyText value={summary.baseline?.amount ?? 0} /> 기준
            </p>
          </>
        ) : (
          <>
            {/* 0원으로 그리면 "통장이 비었다" 로 읽힌다. */}
            <p className="mt-1 text-xl font-semibold text-[--color-muted-foreground]">
              —
            </p>
            <button
              type="button"
              onClick={onSetBaseline}
              className="mt-1 text-xs text-[--color-primary] underline underline-offset-2"
            >
              기준 잔액 미설정 — 입력하면 잔액을 계산합니다
            </button>
          </>
        )}
      </div>

      <div className="rounded-lg border border-[--color-border] p-4">
        <p className="text-xs text-[--color-muted-foreground]">기간 중 최저 잔액</p>
        {summary.balance_available ? (
          <>
            <p className="mt-1 text-xl font-semibold">
              <MoneyText value={summary.lowest_projected_balance} colored />
            </p>
            <p className="mt-1 text-xs text-[--color-muted-foreground]">
              말일 잔액만 보면 중간의 자금 부족을 놓칩니다.
            </p>
          </>
        ) : (
          <p className="mt-1 text-xl font-semibold text-[--color-muted-foreground]">—</p>
        )}
      </div>

      <div className="rounded-lg border border-[--color-border] p-4">
        <p className="flex items-center gap-1 text-xs text-[--color-muted-foreground]">
          <ArrowDownLeft className="h-3 w-3 text-[--color-success]" aria-hidden />
          유입 합계
        </p>
        <p className="mt-1 text-xl font-semibold">
          <MoneyText value={inTotal} />
        </p>
        <StatusBreakdown parts={summary.inflow} />
      </div>

      <div className="rounded-lg border border-[--color-border] p-4">
        <p className="flex items-center gap-1 text-xs text-[--color-muted-foreground]">
          <ArrowUpRight className="h-3 w-3 text-[--color-danger]" aria-hidden />
          유출 합계
        </p>
        <p className="mt-1 text-xl font-semibold">
          <MoneyText value={outTotal} />
        </p>
        <StatusBreakdown parts={summary.outflow} />
      </div>
    </div>
  );
}

/** 상태별로 나눠 보여준다 — 합쳐 놓으면 예상 금액이 실제 돈처럼 보인다. */
function StatusBreakdown({
  parts,
}: {
  parts: { expected: number; confirmed: number; actual: number };
}) {
  return (
    <p className="mt-1 space-x-2 text-xs text-[--color-muted-foreground]">
      <span title={CASH_STATUS_HINT.ACTUAL}>
        실제 <MoneyText value={parts.actual} />
      </span>
      <span title={CASH_STATUS_HINT.CONFIRMED}>
        확정 <MoneyText value={parts.confirmed} />
      </span>
      <span title={CASH_STATUS_HINT.EXPECTED}>
        예상 <MoneyText value={parts.expected} />
      </span>
    </p>
  );
}

/**
 * 데이터가 없는 이유를 화면이 직접 말한다.
 *
 * 아무 설명 없이 "실제 유입 0원" 만 보여주면 사용자는 장부가 틀렸다고 생각한다.
 * 실제로는 **입금을 확인할 수단이 아직 없는 것**이다.
 */
function SourceNotice({ summary }: { summary: CashflowSummary }) {
  const notices: string[] = [];

  if (!summary.actual_inflow_source_available) {
    notices.push(
      "실제 입금은 은행·판매처 연동이 없어 자동으로 확인되지 않습니다. " +
        "판매처 정산서는 금액이 확정된 것일 뿐 입금 근거가 아니므로 '확정' 으로만 표시합니다. " +
        "입금을 확인하셨다면 '입출금 기록' 으로 남겨 주세요.",
    );
  }
  if (summary.overdue_payout > 0) {
    notices.push(
      `지급 예정일이 지난 미지급액이 ${summary.overdue_payout.toLocaleString("ko-KR")}원 있습니다.`,
    );
  }
  if (summary.unpaid_payout > 0) {
    notices.push(
      `기간과 무관한 전체 미지급 잔액은 ${summary.unpaid_payout.toLocaleString("ko-KR")}원입니다. ` +
        "지급 예정일이 없는 항목은 아래 타임라인에 나타나지 않습니다.",
    );
  }

  if (notices.length === 0) return null;

  return (
    <div className="space-y-1.5 rounded-lg border border-[--color-border] bg-[--color-surface-muted] p-3">
      {notices.map((n, i) => (
        <p
          key={i}
          className="flex items-start gap-1.5 text-xs text-[--color-muted-foreground]"
        >
          {i === 0 && !summary.actual_inflow_source_available ? (
            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          ) : (
            <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          )}
          <span>{n}</span>
        </p>
      ))}
    </div>
  );
}

/* ──────────────────────────────────────────────────────────────
 * 타임라인
 * ────────────────────────────────────────────────────────────── */

function Timeline({ summary }: { summary: CashflowSummary }) {
  const max = useMemo(
    () =>
      Math.max(
        1,
        ...summary.timeline.map((t) => Math.max(t.inflow, t.outflow)),
      ),
    [summary.timeline],
  );

  if (summary.timeline.length === 0) {
    return (
      <div className="rounded-lg border border-[--color-border] p-6 text-center text-sm text-[--color-muted-foreground]">
        <p>이 기간에 예정되거나 기록된 입출금이 없습니다.</p>
        <p className="mt-1 text-xs">
          지급을 승인하거나 입출금을 기록하면 여기에 나타납니다.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-[--color-border]">
      <table className="w-full min-w-[640px] text-sm">
        <thead className="border-b border-[--color-border] text-xs text-[--color-muted-foreground]">
          <tr>
            <th className="px-3 py-2 text-left font-medium">날짜</th>
            <th className="px-3 py-2 text-right font-medium">유입</th>
            <th className="px-3 py-2 text-right font-medium">유출</th>
            <th className="px-3 py-2 text-right font-medium">순변동</th>
            <th className="px-3 py-2 text-right font-medium">잔액</th>
            <th className="w-[30%] px-3 py-2 text-left font-medium">비중</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[--color-border]">
          {summary.timeline.map((t) => (
            <tr key={t.date}>
              <td className="whitespace-nowrap px-3 py-2 tabular-nums">{t.date}</td>
              <td className="px-3 py-2 text-right">
                <MoneyText value={t.inflow} />
              </td>
              <td className="px-3 py-2 text-right">
                <MoneyText value={t.outflow} />
              </td>
              <td className="px-3 py-2 text-right">
                <MoneyText value={t.net} colored />
              </td>
              <td className="px-3 py-2 text-right">
                {/* 기준 잔액이 없으면 계산 불가 — 0 으로 채우지 않는다. */}
                {t.balance === null ? (
                  <span
                    className="text-[--color-muted-foreground]"
                    title="기준 잔액이 없어 잔액을 계산할 수 없습니다."
                  >
                    —
                  </span>
                ) : (
                  <MoneyText value={t.balance} colored />
                )}
              </td>
              <td className="px-3 py-2">
                <div className="flex h-2 gap-0.5">
                  <div
                    className="rounded-sm bg-[--color-success]"
                    style={{ width: `${(t.inflow / max) * 50}%` }}
                    aria-hidden
                  />
                  <div
                    className="rounded-sm bg-[--color-danger]"
                    style={{ width: `${(t.outflow / max) * 50}%` }}
                    aria-hidden
                  />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────────
 * 원본 줄
 * ────────────────────────────────────────────────────────────── */

function DirectionBadge({ direction }: { direction: CashDirection }) {
  const inflow = direction === "INFLOW";
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] ${
        inflow
          ? "bg-[--color-success-bg] text-[--color-success]"
          : "bg-[--color-danger-bg] text-[--color-danger]"
      }`}
    >
      {inflow ? (
        <ArrowDownLeft className="h-2.5 w-2.5" aria-hidden />
      ) : (
        <ArrowUpRight className="h-2.5 w-2.5" aria-hidden />
      )}
      {inflow ? "유입" : "유출"}
    </span>
  );
}

function StatusBadge({ status }: { status: CashStatus }) {
  // "확정" 을 "실제" 처럼 보이게 하면 안 들어온 돈을 들어온 것으로 읽는다.
  const tone =
    status === "ACTUAL"
      ? "border-[--color-border] font-medium"
      : "border-dashed border-[--color-border] text-[--color-muted-foreground]";
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[11px] ${tone}`}
      title={CASH_STATUS_HINT[status]}
    >
      {CASH_STATUS_LABEL[status]}
    </span>
  );
}

function RowTable({ rows, onChanged }: { rows: CashRow[]; onChanged: () => void }) {
  const [deleting, setDeleting] = useState<CashRow | null>(null);

  if (rows.length === 0) return null;

  return (
    <>
      <div className="overflow-x-auto rounded-lg border border-[--color-border]">
        <table className="w-full min-w-[720px] text-sm">
          <thead className="border-b border-[--color-border] text-xs text-[--color-muted-foreground]">
            <tr>
              <th className="px-3 py-2 text-left font-medium">날짜</th>
              <th className="px-3 py-2 text-left font-medium">구분</th>
              <th className="px-3 py-2 text-left font-medium">내용</th>
              <th className="px-3 py-2 text-left font-medium">출처</th>
              <th className="px-3 py-2 text-left font-medium">근거</th>
              <th className="px-3 py-2 text-right font-medium">금액</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-[--color-border]">
            {rows.map((r) => (
              <tr key={`${r.source_type}-${r.source_id}`}>
                <td className="whitespace-nowrap px-3 py-2 tabular-nums">
                  {r.occurred_on}
                </td>
                <td className="px-3 py-2">
                  <DirectionBadge direction={r.direction} />
                </td>
                <td className="px-3 py-2">{r.label}</td>
                <td className="px-3 py-2 text-xs text-[--color-muted-foreground]">
                  {/* 원본으로 되돌아갈 수 없는 숫자를 보여주지 않는다. */}
                  {r.drill_href ? (
                    <Link
                      href={r.drill_href as Route}
                      className="underline underline-offset-2 hover:text-[--color-foreground]"
                    >
                      {CASH_SOURCE_LABEL[r.source_type] ?? r.source_type}
                    </Link>
                  ) : (
                    (CASH_SOURCE_LABEL[r.source_type] ?? r.source_type)
                  )}
                </td>
                <td className="px-3 py-2">
                  <StatusBadge status={r.status} />
                </td>
                <td className="px-3 py-2 text-right">
                  <MoneyText value={r.amount} />
                </td>
                <td className="px-3 py-2 text-right">
                  {/* 지급 기록·정산서는 여기서 못 지운다 — 원본 화면에서 다뤄야 한다. */}
                  {r.source_type === "manual_cash_event" ? (
                    <button
                      type="button"
                      onClick={() => setDeleting(r)}
                      className="text-xs text-[--color-muted-foreground] underline underline-offset-2 hover:text-[--color-danger]"
                    >
                      삭제
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <DeleteEventDialog
        row={deleting}
        onClose={() => setDeleting(null)}
        onDeleted={() => {
          setDeleting(null);
          onChanged();
        }}
      />
    </>
  );
}

/* ──────────────────────────────────────────────────────────────
 * 입력 다이얼로그
 * ────────────────────────────────────────────────────────────── */

function BaselineDialog({
  open,
  onOpenChange,
  current,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  current: CashflowSummary["baseline"];
  onSaved: () => void;
}) {
  const [asOf, setAsOf] = useState(today());
  const [amount, setAmount] = useState("");
  const [memo, setMemo] = useState("");

  const save = useMutation({
    mutationFn: () =>
      cashflowApi.setBaseline({
        as_of_date: asOf,
        amount: Number(amount),
        memo: memo.trim() || null,
      }),
    onSuccess: () => {
      onSaved();
      onOpenChange(false);
      setAmount("");
      setMemo("");
      toast.success("기준 잔액을 저장했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "기준 잔액 저장")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>기준 잔액</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          {/* 은행 연동이 없다는 사실을 숨기지 않는다. */}
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-xs text-[--color-muted-foreground]">
            은행 연동이 없으므로 이 값은 직접 입력하는 기준점입니다. 실시간 계좌
            잔액이 아닙니다. 이 값을 시작점으로 예상 잔액을 계산합니다.
          </p>
          {current ? (
            <p className="text-xs text-[--color-muted-foreground]">
              현재 기준: {current.as_of_date} <MoneyText value={current.amount} />
              {current.memo ? ` · ${current.memo}` : ""}
            </p>
          ) : null}
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="cf-asof" className="text-xs">
                기준일 *
              </Label>
              <Input
                id="cf-asof"
                type="date"
                value={asOf}
                onChange={(e) => setAsOf(e.target.value)}
                className="h-8 text-sm"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="cf-amount" className="text-xs">
                잔액 (원) *
              </Label>
              <Input
                id="cf-amount"
                type="number"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="h-8 text-sm"
                placeholder="10000000"
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="cf-memo" className="text-xs">
              메모
            </Label>
            <Input
              id="cf-memo"
              value={memo}
              onChange={(e) => setMemo(e.target.value)}
              className="h-8 text-sm"
              placeholder="주거래 통장 (계좌번호는 적지 마세요)"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button
            size="sm"
            disabled={!amount || save.isPending}
            onClick={() => save.mutate()}
          >
            저장
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ManualEventDialog({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSaved: () => void;
}) {
  const [direction, setDirection] = useState<CashDirection>("INFLOW");
  const [status, setStatus] = useState<CashStatus>("ACTUAL");
  const [amount, setAmount] = useState("");
  const [when, setWhen] = useState(today());
  const [category, setCategory] = useState("");
  const [memo, setMemo] = useState("");

  const actual = status === "ACTUAL";

  const save = useMutation({
    mutationFn: () =>
      cashflowApi.createManualEvent({
        direction,
        status,
        amount: Number(amount),
        // 실제로 움직인 것이면 발생일, 아직이면 예정일이다. 서버도 같은 규칙을 강제한다.
        occurred_at: actual ? when : null,
        expected_at: actual ? null : when,
        category: category.trim() || null,
        memo: memo.trim() || null,
      }),
    onSuccess: () => {
      onSaved();
      onOpenChange(false);
      setAmount("");
      setCategory("");
      setMemo("");
      toast.success("입출금을 기록했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "입출금 기록")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>입출금 기록</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2 text-sm">
          <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2 text-xs text-[--color-muted-foreground]">
            파트너 지급은 지급 장부에서 기록하세요. 여기에 또 넣으면 같은 돈이 두 번
            나간 것으로 잡힙니다.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label className="text-xs">방향 *</Label>
              <div className="flex rounded-md border border-[--color-border]">
                {(["INFLOW", "OUTFLOW"] as const).map((d) => (
                  <button
                    key={d}
                    type="button"
                    onClick={() => setDirection(d)}
                    aria-pressed={direction === d}
                    className={`flex-1 px-2 py-1.5 text-xs first:rounded-l-md last:rounded-r-md ${
                      direction === d
                        ? "bg-[--color-surface-muted] font-medium"
                        : "text-[--color-muted-foreground]"
                    }`}
                  >
                    {d === "INFLOW" ? "입금" : "출금"}
                  </button>
                ))}
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">상태 *</Label>
              <div className="flex rounded-md border border-[--color-border]">
                {(["ACTUAL", "CONFIRMED", "EXPECTED"] as const).map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setStatus(s)}
                    aria-pressed={status === s}
                    title={CASH_STATUS_HINT[s]}
                    className={`flex-1 px-2 py-1.5 text-xs first:rounded-l-md last:rounded-r-md ${
                      status === s
                        ? "bg-[--color-surface-muted] font-medium"
                        : "text-[--color-muted-foreground]"
                    }`}
                  >
                    {CASH_STATUS_LABEL[s]}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="cf-ev-date" className="text-xs">
                {actual ? "발생일 *" : "예정일 *"}
              </Label>
              <Input
                id="cf-ev-date"
                type="date"
                value={when}
                onChange={(e) => setWhen(e.target.value)}
                className="h-8 text-sm"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="cf-ev-amount" className="text-xs">
                금액 (원) *
              </Label>
              <Input
                id="cf-ev-amount"
                type="number"
                min={1}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="h-8 text-sm"
                placeholder="3000000"
              />
              {/* 방향은 위에서 정한다 — 음수로 방향을 표현하지 않는다. */}
              <p className="text-[11px] text-[--color-muted-foreground]">
                금액은 항상 양수로 입력하고 방향은 위에서 고릅니다.
              </p>
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="cf-ev-cat" className="text-xs">
              분류
            </Label>
            <Input
              id="cf-ev-cat"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="h-8 text-sm"
              placeholder="쿠팡 정산 입금"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="cf-ev-memo" className="text-xs">
              메모
            </Label>
            <Input
              id="cf-ev-memo"
              value={memo}
              onChange={(e) => setMemo(e.target.value)}
              className="h-8 text-sm"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            취소
          </Button>
          <Button
            size="sm"
            disabled={!amount || Number(amount) <= 0 || save.isPending}
            onClick={() => save.mutate()}
          >
            기록
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeleteEventDialog({
  row,
  onClose,
  onDeleted,
}: {
  row: CashRow | null;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [reason, setReason] = useState("");

  const del = useMutation({
    mutationFn: () => cashflowApi.deleteManualEvent(row!.source_id!, reason.trim()),
    onSuccess: () => {
      setReason("");
      onDeleted();
      toast.success("기록을 삭제했습니다.");
    },
    onError: (e) => toast.error(errorMessage(e, "삭제")),
  });

  return (
    <Dialog open={!!row} onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>입출금 기록 삭제</DialogTitle>
        </DialogHeader>
        {row ? (
          <div className="space-y-3 py-2 text-sm">
            <p className="rounded-md border border-[--color-border] bg-[--color-surface-muted] px-3 py-2">
              {row.occurred_on} · {row.label} · <MoneyText value={row.amount} />
            </p>
            <div className="space-y-1">
              <Label htmlFor="cf-del-reason" className="text-xs">
                사유 *
              </Label>
              <Input
                id="cf-del-reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="h-8 text-sm"
                placeholder="금액을 잘못 입력함"
              />
              {/* 사유는 감사 기록에 남는다 — 나중에 금액 변화를 설명할 수 있어야 한다. */}
              <p className="text-[11px] text-[--color-muted-foreground]">
                삭제 사유는 감사 기록에 남습니다.
              </p>
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>
            취소
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={!reason.trim() || del.isPending}
            onClick={() => del.mutate()}
          >
            삭제
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
